"""Self-hosted relay so a host and its agents can link up over the internet.

The relay is a small rendezvous server you run anywhere both sides can
reach, usually by a DNS name (``relay.example.com``). The host and every
agent make *outgoing* connections to it, so nobody needs to open ports or
know anyone else's IP address. The relay pairs one agent connection with
one waiting host connection and then copies bytes between them.

Two keys are involved, and they do different jobs:

* The **relay key** (in the relay's own config, and given to every client)
  decides who may use the relay. Clients prove they know it with an HMAC
  challenge-response, and the relay proves it back, so the key itself never
  crosses the network and a fake relay is spotted.
* The **pairing secret** (the usual ``secret``) never reaches the relay.
  Once paired, host and agent run the same TLS-PSK handshake they use on a
  LAN, *through* the relay. Every keystroke is end-to-end encrypted and
  authenticated between the two devices; the relay only sees ciphertext and
  a room id, which is a one-way hash of the pairing secret used to find
  matching peers.

Handshake (newline-delimited JSON, before any TLS)::

    client -> relay   {"t":"relay_hello","v":1,"role":"host"|"agent","room":hex,"nonce":hex}
    relay  -> client  {"t":"relay_challenge","nonce":hex,"proof":hex}
    client -> relay   {"t":"relay_auth","proof":hex}
    relay  -> client  {"t":"relay_ok"}          (or {"t":"relay_error","reason":...})
    relay  -> host    {"t":"relay_ping"}        while the host waits, keeps NATs open
    relay  -> host    {"t":"relay_paired"}      an agent arrived
    host   -> relay   {"t":"relay_ready"}       host is switching to TLS
    relay  -> agent   {"t":"relay_paired"}      start TLS now (agent is the TLS client)

After ``relay_paired`` the connection is a plain byte pipe between host and
agent. The host keeps one spare connection waiting at the relay and opens a
new one each time an agent takes it, so several agents can join.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import contextlib
import hashlib
import hmac
import json
import logging
import secrets
import ssl
from collections import deque
from dataclasses import dataclass, field
from typing import Awaitable, Callable

from curslink.net.framing import STREAM_LIMIT

log = logging.getLogger(__name__)

RELAY_PROTOCOL_VERSION = 1
DEFAULT_RELAY_PORT = 24852
MIN_RELAY_KEY_LENGTH = 16
MAX_HANDSHAKE_LINE = 1024
HANDSHAKE_TIMEOUT_S = 10.0
WAITING_PING_S = 20.0
PAIR_TIMEOUT_S = 20.0
PIPE_CHUNK = 64 * 1024

_ROOM_LABEL = b"curslink/v1/relay-room"
_RELAY_PROOF = b"curslink/v1/relay-proof"
_CLIENT_PROOF = b"curslink/v1/client-proof"


@dataclass(frozen=True)
class RelayTarget:
    """Where a host or agent meets its peers: relay address, relay key, room."""

    address: str
    key: str
    room: str


class RelayError(Exception):
    """The relay refused us, or spoke something unexpected."""


# ----------------------------------------------------------------- helpers
def generate_key() -> str:
    """A fresh random relay key or pairing secret (43 URL-safe characters).

    Never starting with "-", which would read as an option on a command line
    (``curslink invite --relay-key -abc...``)."""
    while True:
        key = secrets.token_urlsafe(32)
        if not key.startswith("-"):
            return key


PAIRING_CODE_PREFIX = "curslink1-"


def encode_pairing_code(relay: str, relay_key: str, secret: str) -> str:
    """One string holding everything a device needs to join over a relay.

    It contains the pairing secret, so treat it like a password: send it
    over something private, and paste it into each device's config.
    """
    payload = json.dumps({"r": relay, "k": relay_key, "s": secret}, separators=(",", ":")).encode()
    return PAIRING_CODE_PREFIX + base64.urlsafe_b64encode(payload).decode().rstrip("=")


def decode_pairing_code(code: str) -> dict[str, str]:
    code = "".join(code.split())
    if not code.startswith(PAIRING_CODE_PREFIX):
        raise ValueError(f"pairing codes start with {PAIRING_CODE_PREFIX!r}")
    body = code[len(PAIRING_CODE_PREFIX) :]
    try:
        obj = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
    except (binascii.Error, ValueError) as exc:
        raise ValueError("the code is damaged; copy it again") from exc
    if not isinstance(obj, dict) or not all(isinstance(obj.get(k), str) and obj[k] for k in "rks"):
        raise ValueError("the code is damaged; copy it again")
    return {"relay": obj["r"], "relay_key": obj["k"], "secret": obj["s"]}


def room_id(pairing_key: bytes) -> str:
    """One-way room name from the (already stretched) pairing key."""
    return hmac.new(pairing_key, _ROOM_LABEL, hashlib.sha256).hexdigest()


_PAIRED_ROOM_LABEL = b"curslink/v2/relay-room\0"


def paired_room_id(link_key: bytes, host_id: str) -> str:
    """Room for one paired host/agent pair: both sides hold the link key, the
    relay learns nothing from it, and re-pairing moves the pair to a new room."""
    return hmac.new(link_key, _PAIRED_ROOM_LABEL + host_id.encode(), hashlib.sha256).hexdigest()


def parse_address(address: str, default_port: int = DEFAULT_RELAY_PORT) -> tuple[str, int]:
    """``host``, ``host:port``, ``[v6]:port`` or a bare IPv6 address."""
    address = address.strip()
    if not address:
        raise ValueError("relay address is empty")
    if address.startswith("["):
        host, _, rest = address[1:].partition("]")
        port = rest.removeprefix(":") or str(default_port)
    elif address.count(":") == 1:
        host, port = address.split(":")
    else:
        host, port = address, str(default_port)
    if not host or not port.isdigit() or not 0 < int(port) < 65536:
        raise ValueError(f"invalid relay address {address!r}")
    return host, int(port)


def _proof(key: str, label: bytes, *parts: str) -> str:
    return hmac.new(key.encode(), label + b"|" + "|".join(parts).encode(), hashlib.sha256).hexdigest()


def _line(**obj) -> bytes:
    return json.dumps(obj, separators=(",", ":")).encode() + b"\n"


async def _read(reader: asyncio.StreamReader) -> dict:
    try:
        line = await reader.readuntil(b"\n")
    except asyncio.IncompleteReadError as exc:
        raise RelayError("connection closed during relay handshake") from exc
    except asyncio.LimitOverrunError as exc:
        raise RelayError("relay handshake line too long") from exc
    if len(line) > MAX_HANDSHAKE_LINE:
        raise RelayError("relay handshake line too long")
    try:
        obj = json.loads(line)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise RelayError("invalid relay handshake line") from exc
    if not isinstance(obj, dict) or not isinstance(obj.get("t"), str):
        raise RelayError("invalid relay handshake line")
    return obj


def _hex(obj: dict, key: str, length: int) -> str:
    value = obj.get(key)
    if not isinstance(value, str) or len(value) != length:
        raise RelayError(f"bad {key}")
    try:
        bytes.fromhex(value)
    except ValueError as exc:
        raise RelayError(f"bad {key}") from exc
    return value


async def start_tls(
    writer: asyncio.StreamWriter, ctx: ssl.SSLContext, server_side: bool
) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    """Upgrade an open connection to TLS, as either side, and return new streams.

    ``StreamWriter.start_tls`` always picks the side from who dialled, but
    over the relay both peers dialled out, so the host needs to be the TLS
    server on an outgoing socket.
    """
    loop = asyncio.get_running_loop()
    reader = asyncio.StreamReader(limit=STREAM_LIMIT, loop=loop)
    protocol = asyncio.StreamReaderProtocol(reader, loop=loop)
    transport = await loop.start_tls(
        writer.transport, protocol, ctx, server_side=server_side, ssl_handshake_timeout=HANDSHAKE_TIMEOUT_S
    )
    protocol.connection_made(transport)
    tls_writer = asyncio.StreamWriter(transport, protocol, reader, loop)
    # The plaintext writer closes its socket when garbage collected, so keep
    # it alive for as long as the TLS writer that now owns the socket.
    tls_writer._curslink_raw_writer = writer  # type: ignore[attr-defined]
    return reader, tls_writer


# ------------------------------------------------------------------ client
async def relay_connect(
    address: str, relay_key: str, room: str, role: str
) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    """Dial the relay and authenticate. Returns plain streams, not yet paired."""
    host, port = parse_address(address)
    reader, writer = await asyncio.wait_for(
        asyncio.open_connection(host, port, limit=STREAM_LIMIT), HANDSHAKE_TIMEOUT_S
    )
    try:
        nonce = secrets.token_hex(16)
        writer.write(_line(t="relay_hello", v=RELAY_PROTOCOL_VERSION, role=role, room=room, nonce=nonce))
        challenge = await asyncio.wait_for(_read(reader), HANDSHAKE_TIMEOUT_S)
        if challenge["t"] == "relay_error":
            raise RelayError(str(challenge.get("reason", "refused")))
        if challenge["t"] != "relay_challenge":
            raise RelayError("unexpected reply from relay")
        server_nonce = _hex(challenge, "nonce", 32)
        expected = _proof(relay_key, _RELAY_PROOF, nonce, server_nonce)
        if not hmac.compare_digest(str(challenge.get("proof", "")), expected):
            raise RelayError("relay could not prove it knows the relay key (wrong key, or not your relay)")
        writer.write(
            _line(t="relay_auth", proof=_proof(relay_key, _CLIENT_PROOF, server_nonce, nonce, role, room))
        )
        ok = await asyncio.wait_for(_read(reader), HANDSHAKE_TIMEOUT_S)
        if ok["t"] != "relay_ok":
            raise RelayError(str(ok.get("reason", "relay refused the connection")))
        return reader, writer
    except BaseException:
        writer.close()
        raise


async def host_accept(
    address: str, relay_key: str, room: str, ctx: ssl.SSLContext
) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    """Wait at the relay for one agent; return the end-to-end TLS streams."""
    reader, writer = await relay_connect(address, relay_key, room, "host")
    try:
        while True:
            msg = await _read(reader)  # no timeout: we may wait hours for an agent
            if msg["t"] == "relay_ping":
                continue
            if msg["t"] != "relay_paired":
                raise RelayError(f"unexpected {msg['t']!r} from relay")
            break
        # Write "ready" and start TLS without yielding to the loop, so no TLS
        # bytes from the agent can land in the old plaintext reader.
        writer.write(_line(t="relay_ready"))
        return await start_tls(writer, ctx, server_side=True)
    except BaseException:
        writer.close()
        raise


async def agent_connect(
    address: str, relay_key: str, room: str, ctx: ssl.SSLContext
) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    """Meet the host at the relay; return the end-to-end TLS streams."""
    reader, writer = await relay_connect(address, relay_key, room, "agent")
    try:
        msg = await asyncio.wait_for(_read(reader), PAIR_TIMEOUT_S + HANDSHAKE_TIMEOUT_S)
        if msg["t"] == "relay_error":
            raise RelayError(str(msg.get("reason", "refused")))
        if msg["t"] != "relay_paired":
            raise RelayError(f"unexpected {msg['t']!r} from relay")
        return await start_tls(writer, ctx, server_side=False)
    except BaseException:
        writer.close()
        raise


class HostRelayLink:
    """Keeps one connection waiting at the relay and hands each paired
    agent connection to ``handle`` (``HostServer.handle_streams``)."""

    def __init__(
        self,
        target: RelayTarget,
        ctx: ssl.SSLContext,
        handle: Callable[[asyncio.StreamReader, asyncio.StreamWriter], Awaitable[None]],
        max_backoff_s: float = 30.0,
    ):
        self.target = target
        self.ctx = ctx
        self.handle = handle
        self.max_backoff_s = max_backoff_s
        self._task: asyncio.Task | None = None
        self._sessions: set[asyncio.Task] = set()

    def start(self) -> None:
        self._task = asyncio.create_task(self._run())

    async def close(self) -> None:
        tasks = [t for t in (self._task, *self._sessions) if t]
        for t in tasks:
            t.cancel()
        for t in tasks:
            with contextlib.suppress(BaseException):
                await t

    async def _run(self) -> None:
        backoff = 0.5
        while True:
            try:
                reader, writer = await host_accept(self.target.address, self.target.key, self.target.room, self.ctx)
                backoff = 0.5
            except asyncio.CancelledError:
                raise
            except ssl.SSLError as exc:
                # The relay paired us, but the agent's pairing secret differs.
                log.warning("TLS with an agent via the relay failed (pairing secret mismatch?): %s", exc)
                continue
            except (OSError, RelayError, asyncio.TimeoutError, ValueError) as exc:
                log.info("relay %s unavailable: %s", self.target.address, exc)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, self.max_backoff_s)
                continue
            task = asyncio.create_task(self.handle(reader, writer))
            self._sessions.add(task)
            task.add_done_callback(self._sessions.discard)


# ------------------------------------------------------------------ server
@dataclass(eq=False)
class _Peer:
    reader: asyncio.StreamReader
    writer: asyncio.StreamWriter
    partner: asyncio.Future = field(default_factory=lambda: asyncio.get_running_loop().create_future())
    done: asyncio.Event = field(default_factory=asyncio.Event)


class RelayServer:
    """``curslink relay``: authenticates clients and splices host and agent."""

    def __init__(
        self,
        key: str,
        host: str = "0.0.0.0",
        port: int = DEFAULT_RELAY_PORT,
        max_connections: int = 256,
        max_rooms: int = 64,
        pair_timeout_s: float = PAIR_TIMEOUT_S,
        ping_interval_s: float = WAITING_PING_S,
    ):
        if len(key) < MIN_RELAY_KEY_LENGTH:
            raise ValueError(f"relay key must be at least {MIN_RELAY_KEY_LENGTH} characters")
        self.key = key
        self.host = host
        self.port = port
        self.max_connections = max_connections
        self.max_rooms = max_rooms
        self.pair_timeout_s = pair_timeout_s
        self.ping_interval_s = ping_interval_s
        self.hosts: dict[str, deque[_Peer]] = {}
        self.agents: dict[str, deque[_Peer]] = {}
        self.active_pipes = 0
        self._connections = 0
        self._server: asyncio.base_events.Server | None = None

    async def start(self) -> None:
        self._server = await asyncio.start_server(
            self._handle, self.host, self.port, limit=MAX_HANDSHAKE_LINE * 2
        )
        self.port = self._server.sockets[0].getsockname()[1]
        log.info("relay listening on %s:%d", self.host, self.port)

    async def close(self) -> None:
        if self._server:
            self._server.close()
            for queue in (*self.hosts.values(), *self.agents.values()):
                for peer in queue:
                    peer.writer.close()
            await self._server.wait_closed()

    async def serve_forever(self) -> None:
        await self.start()
        assert self._server
        await self._server.serve_forever()

    # ---------------------------------------------------------- handshake
    async def _authenticate(self, reader, writer) -> tuple[str, str]:
        hello = await _read(reader)
        if hello["t"] != "relay_hello" or hello.get("v") != RELAY_PROTOCOL_VERSION:
            raise RelayError("expected relay_hello v1")
        role = hello.get("role")
        if role not in ("host", "agent"):
            raise RelayError("role must be host or agent")
        room = _hex(hello, "room", 64)
        client_nonce = _hex(hello, "nonce", 32)
        nonce = secrets.token_hex(16)
        writer.write(
            _line(t="relay_challenge", nonce=nonce, proof=_proof(self.key, _RELAY_PROOF, client_nonce, nonce))
        )
        auth = await _read(reader)
        expected = _proof(self.key, _CLIENT_PROOF, nonce, client_nonce, role, room)
        if auth["t"] != "relay_auth" or not hmac.compare_digest(str(auth.get("proof", "")), expected):
            raise RelayError("wrong relay key")
        if room not in self.hosts and room not in self.agents and self._room_count() >= self.max_rooms:
            raise RelayError("relay is full")
        writer.write(_line(t="relay_ok"))
        return role, room

    def _room_count(self) -> int:
        return len(self.hosts.keys() | self.agents.keys())

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        peer_addr = writer.get_extra_info("peername")
        self._connections += 1
        try:
            if self._connections > self.max_connections:
                writer.write(_line(t="relay_error", reason="relay is busy"))
                return
            try:
                role, room = await asyncio.wait_for(self._authenticate(reader, writer), HANDSHAKE_TIMEOUT_S)
            except (RelayError, asyncio.TimeoutError) as exc:
                log.warning("rejecting %s: %s", peer_addr, exc)
                with contextlib.suppress(Exception):
                    writer.write(_line(t="relay_error", reason=str(exc) or "timeout"))
                return
            log.info("%s %s joined room %s", role, peer_addr, room[:8])
            peer = _Peer(reader, writer)
            if role == "host":
                await self._run_host(room, peer)
            else:
                await self._run_agent(room, peer)
        except (ConnectionError, OSError) as exc:
            log.debug("connection from %s ended: %s", peer_addr, exc)
        finally:
            self._connections -= 1
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()

    # ------------------------------------------------------------ pairing
    def _take(self, table: dict[str, deque[_Peer]], room: str) -> _Peer | None:
        queue = table.get(room)
        while queue:
            peer = queue.popleft()
            if not peer.writer.transport.is_closing() and not peer.partner.done():
                if not queue:
                    del table[room]
                return peer
        table.pop(room, None)
        return None

    def _enqueue(self, table: dict[str, deque[_Peer]], room: str, peer: _Peer) -> None:
        table.setdefault(room, deque()).append(peer)

    def _dequeue(self, table: dict[str, deque[_Peer]], room: str, peer: _Peer) -> None:
        queue = table.get(room)
        if queue and peer in queue:
            queue.remove(peer)
        if queue is not None and not queue:
            del table[room]

    async def _run_agent(self, room: str, agent: _Peer) -> None:
        host = self._take(self.hosts, room)
        if host is not None:
            host.partner.set_result(agent)
        else:
            self._enqueue(self.agents, room, agent)
            try:
                await asyncio.wait_for(asyncio.shield(agent.partner), self.pair_timeout_s)
            except asyncio.TimeoutError:
                agent.writer.write(_line(t="relay_error", reason="host is not connected to the relay"))
                return
            finally:
                self._dequeue(self.agents, room, agent)
        # The host's handler drives the pipe from here.
        await agent.done.wait()

    async def _run_host(self, room: str, host: _Peer) -> None:
        agent = self._take(self.agents, room)
        if agent is not None:
            agent.partner.set_result(host)
            host.partner.set_result(agent)
        else:
            self._enqueue(self.hosts, room, host)
        read = asyncio.ensure_future(_read(host.reader))
        try:
            while not host.partner.done():
                done, _ = await asyncio.wait(
                    {read, host.partner}, timeout=self.ping_interval_s, return_when=asyncio.FIRST_COMPLETED
                )
                if read in done and not host.partner.done():
                    return  # host hung up (or spoke out of turn) while waiting
                if not done:
                    host.writer.write(_line(t="relay_ping"))
            agent: _Peer = host.partner.result()
            try:
                host.writer.write(_line(t="relay_paired"))
                ready = await asyncio.wait_for(read, HANDSHAKE_TIMEOUT_S)
                if ready["t"] != "relay_ready":
                    raise RelayError("expected relay_ready")
                agent.writer.write(_line(t="relay_paired"))
                log.info("paired host and agent in room %s", room[:8])
                self.active_pipes += 1
                try:
                    await _splice(host, agent)
                finally:
                    self.active_pipes -= 1
            finally:
                agent.done.set()
        except (RelayError, asyncio.TimeoutError) as exc:
            log.info("pairing in room %s failed: %s", room[:8], exc)
        finally:
            self._dequeue(self.hosts, room, host)
            if not read.done():
                read.cancel()
            with contextlib.suppress(BaseException):
                await read
            if host.partner.done():
                host.partner.result().done.set()


async def _pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    while True:
        data = await reader.read(PIPE_CHUNK)
        if not data:
            return
        writer.write(data)
        await writer.drain()


async def _splice(a: _Peer, b: _Peer) -> None:
    tasks = {
        asyncio.create_task(_pipe(a.reader, b.writer)),
        asyncio.create_task(_pipe(b.reader, a.writer)),
    }
    try:
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for t in tasks:
            t.cancel()
        for t in tasks:
            with contextlib.suppress(BaseException):
                await t
        a.writer.close()
        b.writer.close()
