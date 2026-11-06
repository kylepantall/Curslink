"""Agent side: connects to the host and replays the input it receives."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import platform
import ssl
from typing import Callable

from curslink.inputs.base import InputSink
from curslink.net.framing import STREAM_LIMIT, read_message
from curslink.net.relay import RelayError, RelayTarget, agent_connect
from curslink.protocol import PROTOCOL_VERSION, Message, ProtocolError, encode

log = logging.getLogger(__name__)

# With a relay configured too, give up on the LAN address this quickly.
LAN_ATTEMPT_TIMEOUT_S = 2.0


class AgentSession:
    """Applies host messages to the local OS. Pure logic, no networking."""

    def __init__(self, sink: InputSink, show_effect: Callable[[str, int, int], None]):
        self.sink = sink
        self.show_effect = show_effect
        self.active = False
        self.keys: set[str] = set()
        self.buttons: set[str] = set()

    def handle(self, msg: Message) -> bytes | None:
        t = msg.t
        if t == "ping":
            return encode("pong", n=msg.get("n"))
        if t == "enter":
            self.active = True
            x, y = int(msg.data["x"]), int(msg.data["y"])
            self.sink.move_to(x, y)
            self.show_effect(msg.get("effect", "none"), x, y)
        elif t == "leave":
            self.active = False
            self.release_all()
        elif t == "move":
            self.sink.move_to(int(msg.data["x"]), int(msg.data["y"]))
        elif t == "button":
            name, pressed = msg.data["button"], msg.data["pressed"]
            if not pressed and name not in self.buttons:
                return None
            (self.buttons.add if pressed else self.buttons.discard)(name)
            self.sink.button(name, pressed)
        elif t == "scroll":
            self.sink.scroll(msg.data["dx"], msg.data["dy"])
        elif t == "key":
            key, pressed = msg.data["key"], msg.data["pressed"]
            if not pressed and key not in self.keys:
                return None  # a release whose press happened elsewhere
            (self.keys.add if pressed else self.keys.discard)(key)
            self.sink.key(key, pressed)
        return None

    def release_all(self) -> None:
        for key in sorted(self.keys):
            with contextlib.suppress(Exception):
                self.sink.key(key, False)
        for button in sorted(self.buttons):
            with contextlib.suppress(Exception):
                self.sink.button(button, False)
        self.keys.clear()
        self.buttons.clear()


def detect_screens() -> list[dict]:
    try:
        from screeninfo import get_monitors  # optional
    except ImportError:
        return []
    return [
        {"x": m.x, "y": m.y, "width": m.width, "height": m.height, "name": m.name}
        for m in get_monitors()
    ]


class AgentClient:
    def __init__(
        self,
        device_id: str,
        name: str,
        host: str,
        port: int,
        ssl_context: ssl.SSLContext | None,
        session: AgentSession,
        max_backoff_s: float = 10.0,
        relay: RelayTarget | None = None,
    ):
        if not host and relay is None:
            raise ValueError("an agent needs a host address, a relay, or both")
        self.device_id = device_id
        self.name = name
        self.host = host
        self.port = port
        self.ssl_context = ssl_context
        self.session = session
        self.max_backoff_s = max_backoff_s
        self.relay = relay
        self.via = ""
        self.connected = asyncio.Event()

    async def _open(self) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        """LAN first (fast, direct); fall back to the relay when there is one."""
        if self.host:
            try:
                streams = await asyncio.wait_for(
                    asyncio.open_connection(self.host, self.port, ssl=self.ssl_context, limit=STREAM_LIMIT),
                    LAN_ATTEMPT_TIMEOUT_S if self.relay else None,
                )
                self.via = f"{self.host}:{self.port}"
                return streams
            except (OSError, asyncio.TimeoutError) as exc:
                if self.relay is None:
                    raise
                log.debug("host not reachable on the LAN (%s); trying the relay", exc)
        assert self.relay is not None
        if self.ssl_context is None:
            raise ValueError("relay connections need the pairing secret")
        streams = await agent_connect(self.relay.address, self.relay.key, self.relay.room, self.ssl_context)
        self.via = f"relay {self.relay.address}"
        return streams

    async def run_once(self) -> None:
        reader, writer = await self._open()
        try:
            writer.write(
                encode(
                    "hello",
                    v=PROTOCOL_VERSION,
                    device_id=self.device_id,
                    name=self.name,
                    platform=f"{platform.system()} {platform.release()}",
                    screens=detect_screens(),
                )
            )
            welcome = await asyncio.wait_for(read_message(reader), 5)
            if welcome is None or welcome.t != "welcome":
                raise ProtocolError("host did not welcome us")
            log.info("connected to host via %s", self.via)
            self.connected.set()
            while True:
                msg = await read_message(reader)
                if msg is None:
                    break
                reply = self.session.handle(msg)
                if reply:
                    writer.write(reply)
        finally:
            self.connected.clear()
            self.session.release_all()
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()

    async def run_forever(self) -> None:
        backoff = 0.5
        while True:
            try:
                await self.run_once()
                backoff = 0.5
                log.info("disconnected from host; reconnecting")
            except ssl.SSLError as exc:
                log.error("TLS handshake failed (is the pairing secret the same on both sides?): %s", exc)
            except (OSError, ProtocolError, RelayError, asyncio.TimeoutError) as exc:
                log.info("host unavailable: %s", exc)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, self.max_backoff_s)
