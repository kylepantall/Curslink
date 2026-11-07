"""Host side: accepts agent connections and sends them input."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import ssl
import time
from dataclasses import dataclass, field
from typing import Callable

from curslink.net.framing import STREAM_LIMIT, read_message
from curslink.net.udp import HostPath, HostUdp
from curslink.protocol import HOST_FEATURES, UDP_FEATURE, Message, ProtocolError, encode

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


@dataclass
class AgentConnection:
    device_id: str
    name: str
    platform: str
    writer: asyncio.StreamWriter
    screens: list = field(default_factory=list)
    last_seen: float = 0.0
    camera: bool = False          # runs its own camera to confirm gaze switches
    facing: bool | None = None    # last thing that camera reported
    features: frozenset = frozenset()  # hello.features: what else it understands
    rtts: list = field(default_factory=list)  # ping round trips (s) since last taken
    udp: HostPath | None = None   # moves by UDP (see curslink.net.udp), when the agent takes them
    ping_s: float = PING_INTERVAL_S

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
        self.host = host
        self.port = port
        # (device_id, hello.auth, TLS channel binding) -> allowed? See security.HelloVerifier.
        self.verify_hello = verify_hello
        self.connections: dict[str, AgentConnection] = {}
        self._server: asyncio.base_events.Server | None = None
        self.udp_enabled = udp
        self.udp: HostUdp | None = None

    async def start(self) -> None:
        self._server = await asyncio.start_server(
            self.handle_streams, self.host, self.port, ssl=self.ssl_context, limit=STREAM_LIMIT
        )
        sock = self._server.sockets[0]
        self.port = sock.getsockname()[1]
        log.info("listening on %s:%d", self.host, self.port)
        if self.udp_enabled:
            await self._start_udp()

    async def _start_udp(self) -> None:
        loop = asyncio.get_running_loop()
        try:
            _, self.udp = await loop.create_datagram_endpoint(HostUdp, local_addr=(self.host, self.port))
        except OSError as exc:
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
    async def handle_streams(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
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
                last_seen=loop.time(),
                camera=hello.get("camera") is True,
                features=frozenset(f for f in (hello.get("features") or []) if isinstance(f, str)),
                ping_s=_ping_interval(hello.get("ping_s")),
            )
            self.connections[device_id] = conn
            welcome = {"device_id": device_id, "features": list(HOST_FEATURES)}
            # Only for an agent that asked: older ones would drop the connection
            # on a datagram-era frame they don't know (they never get one).
            if self.udp is not None and UDP_FEATURE in conn.features:
                conn.udp = self.udp.add(device_id)
                welcome["udp"] = conn.udp.offer(self.udp.port)
            writer.write(encode("welcome", **welcome))
            log.info("agent %s (%s, %s) connected from %s", device_id, conn.name, conn.platform, peer)
            self.on_connect(conn)
            pinger = asyncio.create_task(self._ping(conn))
            try:
                while True:
                    msg = await read_message(reader)
                    if msg is None:
                        break
                    conn.last_seen = loop.time()
                    self._on_message(conn, msg)
            finally:
                pinger.cancel()
        except (asyncio.TimeoutError, ProtocolError) as exc:
            log.warning("rejecting %s: %s", peer, exc)
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
            conn.writer.close()
        elif msg.t == "pong":
            ts = msg.get("ts")
            if isinstance(ts, (int, float)) and not isinstance(ts, bool) and len(conn.rtts) < 1000:
                conn.rtts.append(max(0.0, time.monotonic() - ts / 1000.0))
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
        elif msg.t.startswith("file_"):
            reply = self.on_files(conn.device_id, msg)
            if reply and not conn.writer.transport.is_closing():
                conn.writer.write(reply)

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
            self.send(conn.device_id, "ping", n=n, ts=round(time.monotonic() * 1000.0, 2))


def _ping_interval(value) -> float:
    if not _is_num(value):
        return PING_INTERVAL_S
    return min(max(float(value), PING_INTERVAL_S), MAX_PING_INTERVAL_S)


def _is_num(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)
