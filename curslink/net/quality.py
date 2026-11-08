"""How good the link to each device is, and what to do about it.

Two halves, both pure logic so they test without a network:

* measuring: :class:`RttWindow` keeps the last minute of round trips (from
  pings over TLS, or UDP probes) for latency and jitter; :class:`SeqLoss`
  counts the gaps in a stream of sequence numbers for packet loss;
* tuning: :class:`Optimiser` turns those numbers into a :class:`Tuning` for
  one connection, with hysteresis so it doesn't flap.

What the optimiser can change, all on the host's side of the pointer path
(clicks and keys always go over TLS, as they are):

* ``copies``: on a lossy link, each pointer move by UDP is sent twice, a few
  ms apart (the second as a ``MOVE2`` datagram, see :mod:`curslink.net.udp`),
  so one lost datagram no longer leaves the pointer behind;
* ``move_interval_s``: once the link is less than good, moves are coalesced
  to at most one per this long. A 1000 Hz mouse makes 1000 datagrams a
  second; the agent plays the pointer back at 250 a second anyway
  (:mod:`curslink.smoothing`), so above that they only take Wi-Fi airtime
  from the moves that matter. On a poor link it goes down to 100 a second.
  A good link (a cable, quiet Wi-Fi) sends every move as it comes, since
  holding one back for its turn would add a few ms;
* packets are marked low-latency (DSCP EF), which puts them in Wi-Fi's
  voice queue on most access points (:func:`mark_low_latency`).

The agent's half (taking a direct path again once the host shows up on the
LAN after a relay connection) is in :mod:`curslink.app`.
"""

from __future__ import annotations

import socket
import time
from collections import deque
from dataclasses import asdict, dataclass
from typing import Callable

WINDOW_S = 60.0           # latency and jitter from this much recent history
LOSS_WINDOW_S = 30.0      # loss from this much

# Grades, by the 95th-percentile round trip and loss. A move can only be as
# late as its round trip's one-way half, plus the smoother's delay.
GOOD_RTT_MS, FAIR_RTT_MS = 40.0, 120.0
GOOD_LOSS, FAIR_LOSS = 0.5, 3.0          # percent
GOOD_JITTER_MS, FAIR_JITTER_MS = 8.0, 30.0

# The optimiser's thresholds (percent, ms), each with a lower "off" so it
# doesn't switch back and forth on the boundary.
COPIES_ON_LOSS, COPIES_OFF_LOSS = 1.0, 0.3
FAIR_ON_RTT, FAIR_OFF_RTT = 60.0, 40.0
FAIR_ON_LOSS, FAIR_OFF_LOSS = 1.0, 0.5
FAIR_ON_JITTER, FAIR_OFF_JITTER = 15.0, 8.0
POOR_ON_RTT, POOR_OFF_RTT = 150.0, 80.0
POOR_ON_LOSS, POOR_OFF_LOSS = 5.0, 2.0
HOLD_S = 20.0             # a change stays at least this long
MIN_SAMPLES = 10          # fewer than this and nothing is decided

FAST_INTERVAL_S = 0.004   # 250 moves a second: the smoother's own rate
POOR_INTERVAL_S = 0.010   # 100 a second on a poor link
COPY_DELAY_S = 0.004      # the second copy of a move, this much after the first

DSCP_EF = 0xB8            # Expedited Forwarding (46 << 2): Wi-Fi WMM voice


def _pct(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(len(ordered) * q))]


class RttWindow:
    """Round trips (seconds) over the last ``window_s``."""

    def __init__(self, window_s: float = WINDOW_S, clock: Callable[[], float] = time.monotonic):
        self.window_s = window_s
        self.clock = clock
        self._samples: deque[tuple[float, float]] = deque()

    def add(self, rtt_s: float) -> None:
        now = self.clock()
        self._samples.append((now, max(0.0, float(rtt_s))))
        self._trim(now)

    def _trim(self, now: float) -> None:
        while self._samples and now - self._samples[0][0] > self.window_s:
            self._samples.popleft()
        while len(self._samples) > 2000:
            self._samples.popleft()

    def stats(self) -> dict:
        """``{samples, p50_ms, p95_ms, max_ms, jitter_ms}``. Jitter is the mean
        change between one round trip and the next (as RTP measures it)."""
        self._trim(self.clock())
        rtts = [r for _, r in self._samples]
        if not rtts:
            return {"samples": 0, "p50_ms": None, "p95_ms": None, "max_ms": None, "jitter_ms": None}
        steps = [abs(b - a) for a, b in zip(rtts, rtts[1:])]
        return {
            "samples": len(rtts),
            "p50_ms": round(_pct(rtts, 0.5) * 1000, 1),
            "p95_ms": round(_pct(rtts, 0.95) * 1000, 1),
            "max_ms": round(max(rtts) * 1000, 1),
            "jitter_ms": round(sum(steps) / len(steps) * 1000, 1) if steps else 0.0,
        }


class SeqLoss:
    """Packet loss from the gaps in a stream of increasing sequence numbers.

    Only the newest number counts: one that arrives after a later one was
    already counted lost (reordering is as bad as loss for the pointer)."""

    def __init__(self, window_s: float = LOSS_WINDOW_S, clock: Callable[[], float] = time.monotonic):
        self.window_s = window_s
        self.clock = clock
        self.last: int | None = None
        self._events: deque[tuple[float, int, int]] = deque()  # (when, received, lost)

    def reset(self) -> None:
        self.last = None
        self._events.clear()

    def saw(self, seq: int) -> None:
        if self.last is not None and seq <= self.last:
            return
        lost = 0 if self.last is None else min(seq - self.last - 1, 1000)
        self.last = seq
        now = self.clock()
        self._events.append((now, 1, lost))
        self._trim(now)

    def _trim(self, now: float) -> None:
        while self._events and now - self._events[0][0] > self.window_s:
            self._events.popleft()

    def stats(self) -> dict:
        """``{received, lost, loss_pct}`` over the window (loss_pct None if nothing came)."""
        self._trim(self.clock())
        got = sum(e[1] for e in self._events)
        lost = sum(e[2] for e in self._events)
        return {"received": got, "lost": lost,
                "loss_pct": round(100.0 * lost / (got + lost), 2) if got + lost else None}


# ---------------------------------------------------------------- grading
def grade(rtt: dict, loss_pct: float | None) -> str:
    """``good``, ``fair``, ``poor`` or ``unknown`` (not enough measured yet)."""
    if not rtt.get("samples"):
        return "unknown"
    p95, jitter, loss = rtt["p95_ms"] or 0.0, rtt["jitter_ms"] or 0.0, loss_pct or 0.0
    if p95 > FAIR_RTT_MS or loss > FAIR_LOSS or jitter > FAIR_JITTER_MS:
        return "poor"
    if p95 > GOOD_RTT_MS or loss > GOOD_LOSS or jitter > GOOD_JITTER_MS:
        return "fair"
    return "good"


def advice(link: dict) -> list[str]:
    """Plain-language tips for one link (a ``network`` event's entry)."""
    tips: list[str] = []
    path = link.get("path")
    rtt = link.get("rtt") or {}
    loss = link.get("loss_pct") or 0.0
    iface = link.get("interface") or {}
    if path == "relay":
        tips.append("This link goes through the relay over the internet. If both computers are on the same "
                    "network, the LAN is much faster: check that a firewall or the router isn't blocking "
                    "mDNS (UDP 5353) and Curslink's port (24850)." + (
                        " Curslink switches to the LAN by itself once it can see the other computer there."
                        if link.get("optimise") and link.get("upgrades", True) else ""))
    elif path == "tcp" and link.get("udp_offered"):
        tips.append("Pointer moves can't get through by UDP, so they wait behind any lost packet over TCP. "
                    "Allow Curslink (UDP 24850) through the firewall on both computers.")
    if iface.get("kind") == "wifi" and (grade(rtt, loss) in ("fair", "poor")):
        tips.append("This computer is on Wi-Fi. An Ethernet cable (or a Thunderbolt or USB-C cable between "
                    "two computers) is the biggest improvement for pointer lag.")
    elif grade(rtt, loss) == "poor":
        tips.append("The network between these computers is slow or busy. Big downloads, video calls or a "
                    "far-away access point all show up as pointer lag.")
    if loss >= FAIR_LOSS:
        tips.append(f"{loss:.1f}% of packets are being lost. Moving closer to the access point, or using its "
                    "5 GHz network, usually fixes that.")
    if link.get("mdns") is False:
        tips.append("This computer can't see the other one by mDNS, so it reconnects by its last known "
                    "address. If that address changes, linking stops until mDNS works again.")
    return tips


# --------------------------------------------------------------- optimiser
@dataclass
class Tuning:
    """What the optimiser has chosen for one connection."""

    copies: int = 1                        # each UDP move sent this many times
    move_interval_s: float = 0.0           # 0 = send every move as it comes
    reason: str = ""                       # why, in words, for the app and the log

    def as_dict(self) -> dict:
        return asdict(self)


class Optimiser:
    """Picks a :class:`Tuning` for one connection from its measurements.

    ``update`` is called every few seconds; it returns the tuning to use
    now. Changes are held for ``HOLD_S`` at least, and each switches on at
    a worse number than it switches off at."""

    LEVELS = ("good", "fair", "poor")

    def __init__(self, enabled: bool = True, clock: Callable[[], float] = time.monotonic):
        self.enabled = enabled
        self.clock = clock
        self.tuning = Tuning()
        self.copies = False
        self.level = "good"
        self._changed_at = -HOLD_S

    def _level(self, p95: float, loss: float, jitter: float) -> str:
        level = self.level
        if p95 >= POOR_ON_RTT or loss >= POOR_ON_LOSS:
            return "poor"
        if level == "poor" and not (p95 <= POOR_OFF_RTT and loss <= POOR_OFF_LOSS):
            return "poor"
        if p95 >= FAIR_ON_RTT or loss >= FAIR_ON_LOSS or jitter >= FAIR_ON_JITTER:
            return "fair"
        if level != "good" and not (p95 <= FAIR_OFF_RTT and loss <= FAIR_OFF_LOSS and jitter <= FAIR_OFF_JITTER):
            return "fair"
        return "good"

    def update(self, rtt: dict, loss_pct: float | None, udp: bool, copies_ok: bool = True) -> Tuning:
        if not self.enabled:
            return self.tuning
        now = self.clock()
        if (rtt.get("samples") or 0) < MIN_SAMPLES or now - self._changed_at < HOLD_S:
            return self.tuning
        loss = loss_pct or 0.0
        p95, jitter = rtt.get("p95_ms") or 0.0, rtt.get("jitter_ms") or 0.0
        copies = self.copies
        if not (udp and copies_ok):
            copies = False
        elif not copies and loss >= COPIES_ON_LOSS:
            copies = True
        elif copies and loss <= COPIES_OFF_LOSS:
            copies = False
        level = self._level(p95, loss, jitter)
        if (copies, level) == (self.copies, self.level):
            return self.tuning
        self.copies, self.level, self._changed_at = copies, level, now
        reasons = []
        if copies:
            reasons.append(f"{loss:.1f}% of packets lost, so each pointer move goes twice")
        if level == "poor":
            reasons.append(f"slow link (p95 {p95:.0f} ms, {loss:.1f}% lost), so 100 pointer updates a "
                           "second, steadier on a busy network")
        elif level == "fair":
            reasons.append(f"busy link (p95 {p95:.0f} ms, jitter {jitter:.0f} ms), so at most 250 pointer "
                           "updates a second")
        self.tuning = Tuning(
            copies=2 if copies else 1,
            move_interval_s={"good": 0.0, "fair": FAST_INTERVAL_S, "poor": POOR_INTERVAL_S}[level],
            reason="; ".join(reasons),
        )
        return self.tuning


# ---------------------------------------------------------------- sockets
def mark_low_latency(sock) -> bool:
    """Mark a socket's packets DSCP EF (low latency). Most Wi-Fi access
    points then send them in the voice queue, ahead of bulk traffic. Best
    effort: True if the OS took it (Windows takes it but ignores it without
    a QoS policy; routers may clear it; neither does any harm)."""
    if sock is None:
        return False
    family = getattr(sock, "family", None)
    try:
        if family == socket.AF_INET6:
            sock.setsockopt(socket.IPPROTO_IPV6, getattr(socket, "IPV6_TCLASS", 67), DSCP_EF)
        else:
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_TOS, DSCP_EF)
    except (OSError, AttributeError, ValueError):
        return False
    return True


def transport_socket(stream_or_transport):
    """The socket under an asyncio writer or transport (TLS included), or None."""
    try:
        return stream_or_transport.get_extra_info("socket")
    except Exception:  # pragma: no cover - a transport without one
        return None
