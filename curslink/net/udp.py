"""Pointer moves over UDP, beside the TLS connection.

Over TCP every frame waits for the ones before it. On Wi-Fi a lost packet
then holds up every move behind it until it's resent, which takes 200 ms
or more: the pointer freezes and jumps. A move is an absolute position, so
a lost one doesn't matter as long as the next one gets through. So when
both ends can, moves go as UDP datagrams instead, and a lost one is simply
skipped. Everything else (clicks, keys, scrolls, enter and leave) stays on
the TLS connection, which is reliable and ordered.

Setting up (all over the TLS connection first, so it's as private as that):

* the agent lists ``"udp"`` in ``hello.features`` (only when it reached the
  host directly: through a relay there's no direct path);
* the host answers with ``welcome.udp = {port, id, key}``: its UDP port, an
  id for this connection and a fresh random key for it, plus ``address`` when
  its replies would come from another of its addresses (see below);
* the agent sends a PROBE datagram every ``PROBE_EVERY_S`` from its own
  socket to the host's address and that port; the host replies to each with
  an ACK. That lets the reply through the agent's firewall, and each probe
  says whether the agent has heard an ACK lately (``ok``);
* while the host keeps hearing ``ok`` probes, it sends moves as MOVE
  datagrams to wherever they came from. When they stop (Wi-Fi changed, a
  firewall in the way) it goes back to sending moves over TCP.

Datagram: header ``magic "CL", version, kind, connection id (8 bytes),
sequence number (u64)``, then the body XORed with a keystream of
HMAC-SHA256(enc key, kind + seq), then a 16-byte HMAC-SHA256 tag over all of
that. Each side only takes a sequence number higher than the last it took,
so a datagram can't be replayed, and one that arrives late is dropped.
The agent also drops a move older (by its ``ts``) than the newest it has
seen on either channel, so moves never step backwards.

A host on two networks at once (Ethernet and Wi-Fi on one LAN, say) can be
reached on one address while the OS routes its replies out of the other. The
agent's socket only accepts datagrams from the address it sends to, so those
replies would be dropped and UDP would look blocked. So the host works out
the address it would reply from and offers it as ``address``, and the agent
probes that one. An agent older than this ignores it and stays on TCP there.

On a lossy link the network optimiser (:mod:`curslink.net.quality`) has the
host send each move twice: the MOVE, then a few ms later a MOVE2 with the
same sequence number and body. Whichever arrives first is taken and the
other dropped as a repeat. Being its own kind, a MOVE2 lets the agent count
how many MOVEs were really lost (it reports that in ``netstats``), and an
agent older than MOVE2 simply ignores it.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import hmac
import ipaddress
import logging
import os
import socket
import struct
import time
from dataclasses import dataclass, field
from typing import Callable

from curslink.net.quality import COPY_DELAY_S, RttWindow, SeqLoss, mark_low_latency

log = logging.getLogger(__name__)

MAGIC = b"CL"
VERSION = 1
PROBE, ACK, MOVE, MOVE2 = 1, 2, 3, 4
HEADER = struct.Struct("!2sBB8sQ")
TAG_BYTES = 16
BODIES = {
    PROBE: struct.Struct("!B"),     # ok: the agent heard an ACK in the last ALIVE_S
    ACK: struct.Struct("!Q"),       # the probe's sequence number, echoed (times the round trip)
    MOVE: struct.Struct("!ddd"),    # x, y, ts (host monotonic ms, as in a TCP move)
    MOVE2: struct.Struct("!ddd"),   # the same move again (lossy links; see the module doc)
}
KEY_BYTES = 32
ID_BYTES = 8

PROBE_EVERY_S = 0.5   # agent -> host
# While input is here (and a little after), probe this often instead. Wi-Fi
# adapters doze between packets to save power and wake for the next beacon,
# so on a quiet link the first moves after a pause wait 30-100 ms (or far
# longer) at the access point. A steady trickle from this end keeps the radio
# awake (the link felt faster while Settings > Network's check ran, most
# likely because its mDNS traffic kept the radio up). 25 small datagrams a second each way.
PROBE_WARM_S = 0.04
WARM_AFTER_S = 5.0    # keep warm this long after input leaves (it often comes straight back)
ALIVE_S = 1.6         # a path that's been silent this long is gone (three probes missed)


class Keys:
    """The two keys derived from one connection's random UDP key."""

    def __init__(self, key: bytes):
        if len(key) != KEY_BYTES:
            raise ValueError("bad UDP key")
        self.enc = hmac.new(key, b"curslink-udp-enc", hashlib.sha256).digest()
        self.mac = hmac.new(key, b"curslink-udp-mac", hashlib.sha256).digest()


def _xor(keys: Keys, kind: int, seq: int, data: bytes) -> bytes:
    stream = hmac.new(keys.enc, bytes([kind]) + seq.to_bytes(8, "big"), hashlib.sha256).digest()
    return bytes(a ^ b for a, b in zip(data, stream))


def seal(keys: Keys, conn_id: bytes, kind: int, seq: int, *values) -> bytes:
    head = HEADER.pack(MAGIC, VERSION, kind, conn_id, seq)
    body = _xor(keys, kind, seq, BODIES[kind].pack(*values))
    tag = hmac.new(keys.mac, head + body, hashlib.sha256).digest()[:TAG_BYTES]
    return head + body + tag


def peek_id(packet: bytes) -> bytes | None:
    """The connection id a datagram claims, to find its keys (not yet trusted)."""
    if len(packet) < HEADER.size + TAG_BYTES or packet[:2] != MAGIC or packet[2] != VERSION:
        return None
    return packet[4:4 + ID_BYTES]


def open_packet(keys: Keys, packet: bytes) -> tuple[int, int, tuple] | None:
    """(kind, seq, values) if the datagram is genuine and well formed, else None."""
    if len(packet) < HEADER.size + TAG_BYTES:
        return None
    try:
        magic, version, kind, _conn_id, seq = HEADER.unpack_from(packet)
    except struct.error:
        return None
    if magic != MAGIC or version != VERSION or kind not in BODIES:
        return None
    body, tag = packet[HEADER.size:-TAG_BYTES], packet[-TAG_BYTES:]
    want = hmac.new(keys.mac, packet[:-TAG_BYTES], hashlib.sha256).digest()[:TAG_BYTES]
    if not hmac.compare_digest(tag, want) or len(body) != BODIES[kind].size:
        return None
    return kind, seq, BODIES[kind].unpack(_xor(keys, kind, seq, body))


def b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode()


def unb64(text) -> bytes | None:
    if not isinstance(text, str):
        return None
    try:
        return base64.b64decode(text, validate=True)
    except ValueError:
        return None


def routed_source(peer_ip: str) -> str | None:
    """The local IPv4 address the OS would send from to reach ``peer_ip``.

    Connecting a UDP socket only picks the route: nothing is sent.
    """
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect((peer_ip, 9))
            return sock.getsockname()[0]
    except OSError:
        return None


def probe_address(value) -> str | None:
    """``value`` if it's an IPv4 address an agent can sensibly probe, else None."""
    if not isinstance(value, str):
        return None
    try:
        ip = ipaddress.IPv4Address(value)
    except ValueError:
        return None
    return None if ip.is_loopback or ip.is_unspecified or ip.is_multicast else value


def reply_address(local_ip: str, peer_ip: str) -> str | None:
    """The address to offer an agent, when it isn't the one the agent connected to."""
    source = routed_source(peer_ip)
    return probe_address(source) if source != local_ip else None


# ------------------------------------------------------------------ host side
@dataclass
class HostPath:
    """One agent connection's UDP path, as the host sees it."""

    device_id: str
    conn_id: bytes = field(default_factory=lambda: os.urandom(ID_BYTES))
    key: bytes = field(default_factory=lambda: os.urandom(KEY_BYTES))
    addr: tuple | None = None       # where its probes come from
    heard_at: float = 0.0           # last ok probe
    seq_in: int = 0
    seq_out: int = 0
    sent: int = 0                   # moves sent by UDP since last taken (diagnostics)
    up: bool = False                # last logged state
    copies: int = 1                 # each move sent this many times (the optimiser's choice)
    probes: SeqLoss = field(default_factory=SeqLoss)  # agent -> host loss, from probe gaps
    # The newest move sent by UDP that TCP hasn't carried: sent over TCP
    # before the next click or key, so that lands where the pointer is even
    # if that datagram was lost.
    unsynced: dict | None = None

    def __post_init__(self):
        self.keys = Keys(self.key)

    def offer(self, port: int, address: str | None = None) -> dict:
        offer = {"port": port, "id": b64(self.conn_id), "key": b64(self.key)}
        return {**offer, "address": address} if address else offer

    def usable(self, now: float) -> bool:
        return self.addr is not None and now - self.heard_at < ALIVE_S


class HostUdp(asyncio.DatagramProtocol):
    """The host's one UDP socket, shared by all agent connections."""

    def __init__(self, clock: Callable[[], float] = time.monotonic, mark: bool = False):
        self.clock = clock
        self.mark = mark  # DSCP EF on our datagrams (see quality.mark_low_latency)
        self.paths: dict[bytes, HostPath] = {}
        self.transport: asyncio.DatagramTransport | None = None

    def connection_made(self, transport) -> None:
        self.transport = transport
        if self.mark:
            mark_low_latency(transport.get_extra_info("socket"))

    @property
    def port(self) -> int:
        return self.transport.get_extra_info("sockname")[1] if self.transport else 0

    def add(self, device_id: str) -> HostPath:
        path = HostPath(device_id)
        self.paths[path.conn_id] = path
        return path

    def remove(self, path: HostPath | None) -> None:
        if path is not None:
            self.paths.pop(path.conn_id, None)

    def datagram_received(self, data: bytes, addr) -> None:
        path = self.paths.get(peek_id(data) or b"")
        if path is None:
            return
        opened = open_packet(path.keys, data)
        if opened is None:
            return
        kind, seq, values = opened
        if kind != PROBE or seq <= path.seq_in:
            return
        path.probes.saw(seq)
        path.seq_in = seq
        path.addr = addr
        if values[0]:
            path.heard_at = self.clock()
        path.seq_out += 1
        self._send(path, seal(path.keys, path.conn_id, ACK, path.seq_out, seq))
        self.check(path)

    def check(self, path: HostPath) -> None:
        up = path.usable(self.clock())
        if up != path.up:
            path.up = up
            if up:
                log.info("pointer moves to %s now go over UDP (%s:%s)", path.device_id, *path.addr[:2])
            else:
                log.info("UDP path to %s went quiet; pointer moves are back on TCP", path.device_id)

    def send_move(self, path: HostPath, x: float, y: float, ts: float) -> bool:
        """Send a move by UDP if the path is up. False: use TCP instead."""
        if self.transport is None or not path.usable(self.clock()):
            if path.up:
                self.check(path)
            return False
        path.seq_out += 1
        seq, values = path.seq_out, (float(x), float(y), float(ts))
        if not self._send(path, seal(path.keys, path.conn_id, MOVE, seq, *values)):
            return False
        if path.copies > 1:
            copy = seal(path.keys, path.conn_id, MOVE2, seq, *values)
            try:
                asyncio.get_running_loop().call_later(COPY_DELAY_S, self._send, path, copy)
            except RuntimeError:  # no loop (tests calling directly): send it straight after
                self._send(path, copy)
        path.sent += 1
        path.unsynced = {"x": x, "y": y, "ts": ts}
        return True

    def _send(self, path: HostPath, packet: bytes) -> bool:
        if self.transport is None or self.transport.is_closing() or path.addr is None:
            return False
        try:
            self.transport.sendto(packet, path.addr)
        except OSError:
            return False
        return True

    def error_received(self, exc) -> None:
        log.debug("UDP: %s", exc)

    def close(self) -> None:
        if self.transport is not None:
            self.transport.close()
            self.transport = None


# ----------------------------------------------------------------- agent side
class AgentUdp(asyncio.DatagramProtocol):
    """The agent's end: probes the host and takes the moves it sends."""

    def __init__(self, offer: dict, on_move: Callable[[float, float, float], None],
                 clock: Callable[[], float] = time.monotonic, mark: bool = False,
                 warm: bool = False, active: Callable[[], bool] | None = None):
        conn_id, key = unb64(offer.get("id")), unb64(offer.get("key"))
        port = offer.get("port")
        if (conn_id is None or len(conn_id) != ID_BYTES or key is None or len(key) != KEY_BYTES
                or not isinstance(port, int) or isinstance(port, bool) or not 0 < port < 65536):
            raise ValueError("the host's UDP offer is malformed")
        self.conn_id, self.keys, self.port = conn_id, Keys(key), port
        self.address = probe_address(offer.get("address"))  # the host's reply address, if not the one we dialled
        self.on_move = on_move
        self.clock = clock
        self.transport: asyncio.DatagramTransport | None = None
        self.seq_out = 0
        self.seq_in = 0
        self.acked_at = 0.0
        self.sent_at: dict[int, float] = {}
        self.rtt = RttWindow(clock=clock)      # probe round trips
        self.loss = SeqLoss(clock=clock)       # host -> agent loss, from gaps in ACK and MOVE numbers
        self.copies = 0                        # MOVE2s that arrived before (or instead of) their MOVE
        self.mark = mark
        # Keep the radio awake (see PROBE_WARM_S) while active() says input
        # is here, and until warm_until; only when warm is on ([network] optimise).
        self.warm_enabled = warm
        self.active = active
        self.warm_until = 0.0
        self._kick = asyncio.Event()
        self.moves = 0
        self.up = False
        self._task: asyncio.Task | None = None

    async def start(self, host_ip: str) -> None:
        loop = asyncio.get_running_loop()
        await loop.create_datagram_endpoint(lambda: self, remote_addr=(self.address or host_ip, self.port))
        self._task = asyncio.create_task(self._probe())

    def connection_made(self, transport) -> None:
        self.transport = transport
        if self.mark:
            mark_low_latency(transport.get_extra_info("socket"))

    def heard_lately(self) -> bool:
        return self.clock() - self.acked_at < ALIVE_S

    def warming(self) -> bool:
        if not self.warm_enabled:
            return False
        return self.clock() < self.warm_until or (self.active is not None and bool(self.active()))

    def warm(self, for_s: float = WARM_AFTER_S) -> None:
        """Input is here: probe fast for at least ``for_s``."""
        if not self.warm_enabled:
            return
        was = self.warming()
        self.warm_until = max(self.warm_until, self.clock() + for_s)
        if not was:
            self._kick.set()  # don't wait out a slow probe interval

    async def _probe(self) -> None:
        while True:
            self.probe()
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self._kick.wait(), PROBE_WARM_S if self.warming() else PROBE_EVERY_S)
            self._kick.clear()

    def probe(self) -> None:
        if self.transport is None or self.transport.is_closing():
            return
        up = self.heard_lately()
        if up != self.up:
            self.up = up
            if not up:
                log.info("lost the UDP path to the host; pointer moves come over TCP")
        self.seq_out += 1
        self.sent_at[self.seq_out] = self.clock()
        for old in [s for s in self.sent_at if s < self.seq_out - 64]:  # 2.5 s of warm probes
            del self.sent_at[old]
        try:
            self.transport.sendto(seal(self.keys, self.conn_id, PROBE, self.seq_out, 1 if up else 0))
        except OSError as exc:
            log.debug("UDP probe: %s", exc)

    def datagram_received(self, data: bytes, addr) -> None:
        if peek_id(data) != self.conn_id:
            return
        opened = open_packet(self.keys, data)
        if opened is None:
            return
        kind, seq, values = opened
        if kind in (ACK, MOVE):
            self.loss.saw(seq)  # MOVE2s are repeats: only the originals count for loss
        if seq <= self.seq_in:
            return  # late, replayed, or the second copy of a move already taken
        self.seq_in = seq
        if kind == ACK:
            now = self.clock()
            sent = self.sent_at.pop(values[0], None)
            if sent is not None:
                self.rtt.add(now - sent)
            first = not self.heard_lately()
            self.acked_at = now
            if first:
                log.info("UDP path to the host is up; pointer moves can come over it")
                self.probe()  # tell the host straight away
        elif kind in (MOVE, MOVE2):
            self.warm()
            self.moves += 1
            self.copies += kind == MOVE2
            self.on_move(*values)

    def error_received(self, exc) -> None:
        log.debug("UDP: %s", exc)

    def close(self) -> None:
        if self._task is not None:
            self._task.cancel()
            self._task = None
        if self.transport is not None:
            self.transport.close()
            self.transport = None
