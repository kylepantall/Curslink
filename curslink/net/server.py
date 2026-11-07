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
from curslink.protocol import HOST_FEATURES, Message, ProtocolError, encode

log = logging.getLogger(__name__)

HELLO_TIMEOUT_S = 5.0
PING_INTERVAL_S = 0.5   # often enough to measure the round trip as it changes
DEAD_AFTER_S = 7.0
# If an agent stops reading, don't buffer more than this before dropping it.
MAX_BUFFER_BYTES = 256 * 1024
# Bulk frames (dropped files) wait for the connection to drain past this, so
# input frames queued behind them stay milliseconds away.
BULK_BUFFER_BYTES = 64 * 1024


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
    ):
        self.known_devices = known_devices
        self.ssl_context = ssl_context
        self.on_connect = on_connect
        self.on_disconnect = on_disconnect
        # (device_id, facing): an agent's own camera sees the user facing it or not.
        self.on_facing: Callable[[str, bool], None] = lambda device_id, facing: None
        # (device_id, file_result frame data): an agent saved dropped files, or couldn't.
        self.on_file_result: Callable[[str, dict], None] = lambda device_id, data: None
        self.host = host
        self.port = port
        # (device_id, hello.auth, TLS channel binding) -> allowed? See security.HelloVerifier.
        self.verify_hello = verify_hello
        self.connections: dict[str, AgentConnection] = {}
        self._server: asyncio.base_events.Server | None = None

    async def start(self) -> None:
        self._server = await asyncio.start_server(
            self.handle_streams, self.host, self.port, ssl=self.ssl_context, limit=STREAM_LIMIT
        )
        sock = self._server.sockets[0]
        self.port = sock.getsockname()[1]
        log.info("listening on %s:%d", self.host, self.port)

    async def close(self) -> None:
        for conn in list(self.connections.values()):
            conn.writer.close()
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
            if hello is None or hello.t != "hello":
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
            )
            self.connections[device_id] = conn
            writer.write(encode("welcome", device_id=device_id, features=list(HOST_FEATURES)))
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

    async def _ping(self, conn: AgentConnection) -> None:
        loop = asyncio.get_running_loop()
        n = 0
        while True:
            await asyncio.sleep(PING_INTERVAL_S)
            if loop.time() - conn.last_seen > DEAD_AFTER_S:
                log.warning("agent %s timed out", conn.device_id)
                conn.writer.transport.abort()
                return
            n += 1
            self.send(conn.device_id, "ping", n=n, ts=round(time.monotonic() * 1000.0, 2))
