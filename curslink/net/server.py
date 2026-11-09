"""Host side: accepts agent connections and sends them input."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import ssl
import time
from dataclasses import dataclass, field
from typing import Callable

from curslink.net import quality
from curslink.net.framing import STREAM_LIMIT, read_message
from curslink.net.udp import HostPath, HostUdp, reply_address
from curslink.protocol import HOST_FEATURES, NETSTATS_FEATURE, UDP_FEATURE, Message, ProtocolError, encode

log = logging.getLogger(__name__)

HELLO_TIMEOUT_S = 5.0
PING_INTERVAL_S = 0.5   # often enough to measure the round trip as it changes
DEAD_AFTER_S = 7.0
# An agent on battery (a phone) can ask for slower pings in hello.ping_s, so
# a link kept open all day doesn't keep waking its radio twice a second.
MAX_PING_INTERVAL_S = 15.0
# If an agent stops reading, don't buffer more than this before dropping it.
MAX_BUFFER_BYTES = 256 * 1024
# Bulk frames (dropped files) wait for the connection to drain past this, so
# input frames queued behind them stay milliseconds away.
BULK_BUFFER_BYTES = 64 * 1024
# Before these, a move that only went by UDP is repeated over TCP, so a
# click or key lands where the pointer is even if that datagram was lost.
SYNC_BEFORE = frozenset({"button", "scroll", "key", "leave", "drag"})
# Re-tune each connection (see curslink.net.quality) this often.
TUNE_EVERY_S = 2.0
# An agent's netstats older than this is stale (it sends them every few seconds).
NETSTATS_STALE_S = 15.0


@dataclass
class AgentConnection:
    device_id: str
    name: str
    platform: str
    writer: asyncio.StreamWriter
    screens: list = field(default_factory=list)
    # Its monitors' real sizes (curslink.screensize), for mirror mode.
    screen_mm: list = field(default_factory=list)
    last_seen: float = 0.0
    camera: bool = False          # runs its own camera to confirm gaze switches
    facing: bool | None = None    # last thing that camera reported
    features: frozenset = frozenset()  # hello.features: what else it understands
    rtts: list = field(default_factory=list)  # ping round trips (s) since last taken
    udp: HostPath | None = None   # moves by UDP (see curslink.net.udp), when the agent takes them
    ping_s: float = PING_INTERVAL_S
    relay: bool = False           # reached us through the relay, not directly
    rtt: quality.RttWindow = field(default_factory=quality.RttWindow)  # ping round trips, last minute
    netstats: dict | None = None  # the agent's last netstats frame
    netstats_at: float = 0.0
    optimiser: quality.Optimiser = field(default_factory=lambda: quality.Optimiser(False))
    # The newest move held back to keep to tuning.move_interval_s, and when one last went.
    pending_move: dict | None = None
    last_move_at: float = 0.0
    flush_handle: asyncio.TimerHandle | None = None

    @property
    def tuning(self) -> quality.Tuning:
        return self.optimiser.tuning

    @property
    def dead_after_s(self) -> float:
        return max(DEAD_AFTER_S, 3 * self.ping_s + 1)


class HostServer:
    """Implements the router's ``Transport`` over TLS-PSK TCP connections."""

    def __init__(
        self,
        known_devices: set[str],
        ssl_context: ssl.SSLContext | None,
        on_connect: Callable[[AgentConnection], None] = lambda c: None,
        on_disconnect: Callable[[str], None] = lambda d: None,
        host: str = "0.0.0.0",
        port: int = 0,
        verify_hello: Callable[[str, object, bytes | None], bool] | None = None,
        udp: bool = True,
        optimise: bool = False,
    ):
        self.known_devices = known_devices
        self.ssl_context = ssl_context
        self.on_connect = on_connect
        self.on_disconnect = on_disconnect
        # (device_id, facing): an agent's own camera sees the user facing it or not.
        self.on_facing: Callable[[str, bool], None] = lambda device_id, facing: None
        # (device_id, file_result frame data): an agent saved dropped files, or couldn't.
        self.on_file_result: Callable[[str, dict], None] = lambda device_id, data: None
        # (device_id, drag_out frame data): what a drag leaving that agent carries.
        self.on_drag_out: Callable[[str, dict], None] = lambda device_id, data: None
        # (device_id, file_* frame) -> reply frame or None: files an agent sends us.
        self.on_files: Callable[[str, Message], bytes | None] = lambda device_id, msg: None
        # (device_id, frame data): what that agent's Isolink says (isolink), and
        # whether it opened a floating chat carried to it (popup_result).
        self.on_isolink: Callable[[str, dict], None] = lambda device_id, data: None
        self.on_popup_result: Callable[[str, dict], None] = lambda device_id, data: None
        # (device_id, frame type, frame data): mirror_frame and mirror_ack
        # from an agent taking part in mirror mode (see curslink.mirror).
        self.on_mirror: Callable[[str, str, dict], None] = lambda device_id, t, data: None
        # (device_id, frame type, frame data): clip and clip_data, something
        # copied on that agent (see curslink.clipboard).
        self.on_clip: Callable[[str, str, dict], None] = lambda device_id, t, data: None
        # welcome.features: what this host understands (more is added when
        # turned on, e.g. "clipboard").
        self.features: list[str] = list(HOST_FEATURES)
        self.host = host
        self.port = port
        # (device_id, hello.auth, TLS channel binding) -> allowed? See security.HelloVerifier.
        self.verify_hello = verify_hello
        self.connections: dict[str, AgentConnection] = {}
        # Agents that said goodbye because they were paused there (a phone
        # taking a call, say), rather than dropping off: device_id -> True.
        # Cleared when they connect again.
        self.paused: set[str] = set()
        self._server: asyncio.base_events.Server | None = None
        self.udp_enabled = udp
        self.udp: HostUdp | None = None
        # Tune each link to how it's doing (see curslink.net.quality).
        self.optimise = optimise
        # (device_id, Tuning): the optimiser changed what it does for that agent.
        self.on_tuning: Callable[[str, quality.Tuning], None] = lambda device_id, tuning: None

    async def start(self) -> None:
        any_port = self.port == 0
        tries = 20  # Windows can hold back big ranges of UDP ports (Hyper-V, WinNAT)
        for attempt in range(tries):
            self._server = await asyncio.start_server(
                self.handle_streams, self.host, 0 if any_port else self.port, ssl=self.ssl_context,
                limit=STREAM_LIMIT,
            )
            sock = self._server.sockets[0]
            self.port = sock.getsockname()[1]
            if not self.udp_enabled:
                break
            # A port the OS picked can be free for TCP but taken for UDP
            # (Windows, often); then pick another rather than lose UDP.
            last = attempt == tries - 1
            await self._start_udp(warn=not any_port or last)
            if self.udp is not None or not any_port or last:
                break
            self._server.close()
            await self._server.wait_closed()
        log.info("listening on %s:%d", self.host, self.port)

    async def _start_udp(self, warn: bool = True) -> None:
        loop = asyncio.get_running_loop()
        try:
            _, self.udp = await loop.create_datagram_endpoint(lambda: HostUdp(mark=self.optimise),
                                                              local_addr=(self.host, self.port))
        except OSError as exc:
            if not warn:
                self.udp = None
                return
            log.warning("can't listen for UDP on port %d (%s); pointer moves will use TCP", self.port, exc)
            self.udp = None
            return
        log.info("pointer moves can go by UDP on port %d", self.udp.port)

    async def close(self) -> None:
        for conn in list(self.connections.values()):
            conn.writer.close()
        if self.udp is not None:
            self.udp.close()
            self.udp = None
        if self._server:
            self._server.close()
            await self._server.wait_closed()

    # -------------------------------------------------------- Transport API
    def is_connected(self, device_id: str) -> bool:
        return device_id in self.connections

    def supports(self, device_id: str, feature: str) -> bool:
        conn = self.connections.get(device_id)
        return conn is not None and feature in conn.features

    def send(self, device_id: str, t: str, **data) -> None:
        conn = self.connections.get(device_id)
        if conn is None:
            return
        interval = conn.tuning.move_interval_s
        if t == "move" and interval > 0:
            # Moves are absolute positions: one held back is simply replaced by
            # the next, and the newest goes when its turn comes.
            now = time.monotonic()
            wait = conn.last_move_at + interval - now
            if wait > 0:
                conn.pending_move = data
                if conn.flush_handle is None:
                    conn.flush_handle = asyncio.get_running_loop().call_later(wait, self._flush_move, conn)
                return
            conn.last_move_at = now
            conn.pending_move = None
        elif conn.pending_move is not None:
            self._flush_move(conn)  # a click, key or leave happens after the move before it
        self._send_now(conn, t, data)

    def _flush_move(self, conn: AgentConnection) -> None:
        if conn.flush_handle is not None:
            conn.flush_handle.cancel()
            conn.flush_handle = None
        data, conn.pending_move = conn.pending_move, None
        if data is not None and self.connections.get(conn.device_id) is conn:
            conn.last_move_at = time.monotonic()
            self._send_now(conn, "move", data)

    def _send_now(self, conn: AgentConnection, t: str, data: dict) -> None:
        transport = conn.writer.transport
        if transport.is_closing():
            return
        if transport.get_write_buffer_size() > MAX_BUFFER_BYTES:
            log.warning("%s is not keeping up; dropping connection", device_id)
            transport.abort()
            return
        path = conn.udp
        if path is not None and self.udp is not None:
            if t == "move" and _is_num(data.get("ts")) and _is_num(data.get("x")) and _is_num(data.get("y")):
                if self.udp.send_move(path, data["x"], data["y"], data["ts"]):
                    return
            if t == "move":
                path.unsynced = None  # TCP carries this one
            elif path.unsynced is not None and t in SYNC_BEFORE:
                conn.writer.write(encode("move", **path.unsynced))
                path.unsynced = None
        conn.writer.write(encode(t, **data))

    async def send_bulk(self, device_id: str, t: str, **data) -> None:
        """Send a frame of a big transfer, waiting while the connection is
        busy. Raises ConnectionError if the agent is gone (or reconnected:
        a transfer never continues on a new connection)."""
        conn = self.connections.get(device_id)
        if conn is None or conn.writer.transport.is_closing():
            raise ConnectionError(f"{device_id} is not connected")
        conn.writer.write(encode(t, **data))
        while conn.writer.transport.get_write_buffer_size() > BULK_BUFFER_BYTES:
            if self.connections.get(device_id) is not conn or conn.writer.transport.is_closing():
                raise ConnectionError(f"{device_id} disconnected")
            await asyncio.sleep(0.002)

    def bulk_sender(self, device_id: str):
        """``send(t, **data)`` for :func:`curslink.files.transfer.send_files`,
        bound to the agent's current connection."""
        conn = self.connections.get(device_id)

        async def send(t: str, **data) -> None:
            if self.connections.get(device_id) is not conn:
                raise ConnectionError(f"{device_id} disconnected")
            await self.send_bulk(device_id, t, **data)

        return send

    # ------------------------------------------------------------ internals
    async def handle_streams(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter,
                             relay: bool = False) -> None:
        """Serve one agent connection that is already end-to-end encrypted
        (accepted on the LAN listener, or paired through a relay)."""
        peer = writer.get_extra_info("peername")
        conn: AgentConnection | None = None
        loop = asyncio.get_running_loop()
        try:
            hello = await asyncio.wait_for(read_message(reader), HELLO_TIMEOUT_S)
            if hello is None:
                # An agent checking this address answers (a TLS handshake,
                # then gone: see pairing.discovery.reachable). Not an error.
                log.debug("%s checked we're here", peer)
                return
            if hello.t != "hello":
                raise ProtocolError("expected hello")
            device_id = hello.data["device_id"]
            if device_id not in self.known_devices:
                raise ProtocolError(f"unknown device {device_id!r}; add it to [[devices]]")
            if self.verify_hello is not None:
                ssl_object = writer.get_extra_info("ssl_object")
                binding = ssl_object.get_channel_binding("tls-unique") if ssl_object else None
                if not self.verify_hello(device_id, hello.get("auth"), binding):
                    raise ProtocolError(f"{device_id!r} did not prove it holds that device's key")
            old = self.connections.get(device_id)
            if old is not None:
                log.info("%s reconnected; replacing old connection", device_id)
                old.writer.close()
            conn = AgentConnection(
                device_id=device_id,
                name=str(hello.get("name", device_id)),
                platform=str(hello.get("platform", "?")),
                writer=writer,
                screens=list(hello.get("screens", []) or []),
                screen_mm=[m for m in (hello.get("screen_mm") or []) if isinstance(m, dict)][:16],
                last_seen=loop.time(),
                camera=hello.get("camera") is True,
                features=frozenset(f for f in (hello.get("features") or []) if isinstance(f, str)),
                ping_s=_ping_interval(hello.get("ping_s")),
                relay=relay,
                optimiser=quality.Optimiser(self.optimise),
            )
            if self.optimise:
                quality.mark_low_latency(quality.transport_socket(writer))
            self.connections[device_id] = conn
            self.paused.discard(device_id)
            welcome = {"device_id": device_id, "features": list(self.features)}
            # Only for an agent that asked: older ones would drop the connection
            # on a datagram-era frame they don't know (they never get one).
            if self.udp is not None and UDP_FEATURE in conn.features:
                conn.udp = self.udp.add(device_id)
                local = writer.get_extra_info("sockname")
                welcome["udp"] = conn.udp.offer(self.udp.port, reply_address(local[0], peer[0]) if local and peer else None)
            writer.write(encode("welcome", **welcome))
            log.info("agent %s (%s, %s) connected from %s", device_id, conn.name, conn.platform, peer)
            self.on_connect(conn)
            pinger = asyncio.create_task(self._ping(conn))
            tuner = asyncio.create_task(self._tune(conn)) if self.optimise else None
            try:
                while True:
                    msg = await read_message(reader)
                    if msg is None:
                        break
                    conn.last_seen = loop.time()
                    self._on_message(conn, msg)
            finally:
                pinger.cancel()
                if tuner is not None:
                    tuner.cancel()
                if conn.flush_handle is not None:
                    conn.flush_handle.cancel()
                    conn.flush_handle = None
        except (asyncio.TimeoutError, ProtocolError) as exc:
            log.warning("rejecting %s: %s", peer, exc)
            if conn is None and isinstance(exc, ProtocolError):
                # Say why, so the agent (a phone, say) can show it rather
                # than just seeing the connection close.
                with contextlib.suppress(Exception):
                    writer.write(encode("refused", reason=str(exc)))
                    await asyncio.wait_for(writer.drain(), 1)
        except (ConnectionError, ssl.SSLError, OSError) as exc:
            log.info("connection from %s ended: %s", peer, exc)
        finally:
            if conn is not None and self.udp is not None:
                self.udp.remove(conn.udp)
            if conn is not None and self.connections.get(conn.device_id) is conn:
                del self.connections[conn.device_id]
                log.info("agent %s disconnected", conn.device_id)
                self.on_disconnect(conn.device_id)
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()

    def _on_message(self, conn: AgentConnection, msg: Message) -> None:
        if msg.t == "bye":
            if msg.get("reason") == "paused":
                self.paused.add(conn.device_id)
                log.info("agent %s was paused there", conn.device_id)
            conn.writer.close()
        elif msg.t == "pong":
            ts = msg.get("ts")
            if isinstance(ts, (int, float)) and not isinstance(ts, bool):
                rtt = max(0.0, time.monotonic() - ts / 1000.0)
                conn.rtt.add(rtt)
                if len(conn.rtts) < 1000:
                    conn.rtts.append(rtt)
        elif msg.t == "netstats":
            conn.netstats = dict(msg.data)
            conn.netstats_at = time.monotonic()
        elif msg.t == "facing":
            conn.facing = msg.data["facing"]
            self.on_facing(conn.device_id, conn.facing)
        elif msg.t == "file_result":
            self.on_file_result(conn.device_id, dict(msg.data))
        elif msg.t == "drag_out":
            self.on_drag_out(conn.device_id, dict(msg.data))
        elif msg.t == "isolink":
            self.on_isolink(conn.device_id, dict(msg.data))
        elif msg.t == "popup_result":
            self.on_popup_result(conn.device_id, dict(msg.data))
        elif msg.t in ("mirror_frame", "mirror_ack", "mirror_tune"):
            self.on_mirror(conn.device_id, msg.t, dict(msg.data))
        elif msg.t in ("clip", "clip_data"):
            self.on_clip(conn.device_id, msg.t, dict(msg.data))
        elif msg.t.startswith("file_"):
            reply = self.on_files(conn.device_id, msg)
            if reply and not conn.writer.transport.is_closing():
                conn.writer.write(reply)

    # ------------------------------------------------------- link quality
    def link_report(self, device_id: str) -> dict | None:
        """How the link to that agent is doing, for the app's Network
        settings and the optimiser: the path, the round trip and jitter of
        pings over TLS, packet loss by UDP (the worse of the agent's view of
        our datagrams and ours of its probes) and what the optimiser does."""
        conn = self.connections.get(device_id)
        if conn is None:
            return None
        path = conn.udp
        udp_up = path is not None and self.udp is not None and path.usable(self.udp.clock())
        stats = conn.netstats if time.monotonic() - conn.netstats_at < NETSTATS_STALE_S else None
        losses = [v for v in ((stats or {}).get("loss_pct"), path.probes.stats()["loss_pct"] if path else None)
                  if v is not None]
        return {
            "device_id": device_id,
            "name": conn.name,
            "platform": conn.platform,
            "path": "relay" if conn.relay else ("udp" if udp_up else "tcp"),
            "udp_offered": path is not None,
            "rtt": conn.rtt.stats(),
            "loss_pct": max(losses) if losses else None,
            "agent": stats,
            "optimise": self.optimise,
            # The agent goes direct by itself once it sees us on the LAN (not the Android app).
            "upgrades": NETSTATS_FEATURE in conn.features,
            "tuning": conn.tuning.as_dict(),
            "local": _sockname(conn.writer),
        }

    def retune(self, conn: AgentConnection) -> None:
        report = self.link_report(conn.device_id)
        if report is None:
            return
        before = conn.tuning
        after = conn.optimiser.update(report["rtt"], report["loss_pct"], report["path"] == "udp",
                                      copies_ok=NETSTATS_FEATURE in conn.features)
        if conn.udp is not None:
            conn.udp.copies = after.copies
        if after != before:
            if after.reason:
                log.info("network: tuning the link to %s: %s", conn.device_id, after.reason)
            else:
                log.info("network: the link to %s recovered; back to normal", conn.device_id)
            self.on_tuning(conn.device_id, after)

    async def _tune(self, conn: AgentConnection) -> None:
        while True:
            await asyncio.sleep(TUNE_EVERY_S)
            self.retune(conn)

    async def _ping(self, conn: AgentConnection) -> None:
        loop = asyncio.get_running_loop()
        n = 0
        while True:
            await asyncio.sleep(conn.ping_s)
            if loop.time() - conn.last_seen > conn.dead_after_s:
                log.warning("agent %s timed out", conn.device_id)
                conn.writer.transport.abort()
                return
            n += 1
            extra = {}
            if NETSTATS_FEATURE in conn.features:
                # So the agent's Network settings can show the round trip too
                # (only the host can time it: the ts is on its clock).
                stats = conn.rtt.stats()
                extra = {"rtt_ms": stats["p50_ms"], "p95_ms": stats["p95_ms"], "jitter_ms": stats["jitter_ms"],
                         "tuning": conn.tuning.reason}
            self.send(conn.device_id, "ping", n=n, ts=round(time.monotonic() * 1000.0, 2), **extra)


def _ping_interval(value) -> float:
    if not _is_num(value):
        return PING_INTERVAL_S
    return min(max(float(value), PING_INTERVAL_S), MAX_PING_INTERVAL_S)


def _sockname(writer) -> str:
    """Our end of a connection's address (which interface it went out on)."""
    try:
        name = writer.get_extra_info("sockname")
    except Exception:  # pragma: no cover
        return ""
    return str(name[0]) if isinstance(name, tuple) and name else ""


def _is_num(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)
