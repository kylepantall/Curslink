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
  id for this connection and a fresh random key for it;
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
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import logging
import os
import struct
import time
from dataclasses import dataclass, field
from typing import Callable

log = logging.getLogger(__name__)

MAGIC = b"CL"
VERSION = 1
PROBE, ACK, MOVE = 1, 2, 3
HEADER = struct.Struct("!2sBB8sQ")
TAG_BYTES = 16
BODIES = {
    PROBE: struct.Struct("!B"),     # ok: the agent heard an ACK in the last ALIVE_S
    ACK: struct.Struct("!Q"),       # the probe's sequence number, echoed (times the round trip)
    MOVE: struct.Struct("!ddd"),    # x, y, ts (host monotonic ms, as in a TCP move)
}
KEY_BYTES = 32
ID_BYTES = 8

PROBE_EVERY_S = 0.5   # agent -> host
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
    # The newest move sent by UDP that TCP hasn't carried: sent over TCP
    # before the next click or key, so that lands where the pointer is even
    # if that datagram was lost.
    unsynced: dict | None = None

    def __post_init__(self):
        self.keys = Keys(self.key)

    def offer(self, port: int) -> dict:
        return {"port": port, "id": b64(self.conn_id), "key": b64(self.key)}

    def usable(self, now: float) -> bool:
        return self.addr is not None and now - self.heard_at < ALIVE_S


class HostUdp(asyncio.DatagramProtocol):
    """The host's one UDP socket, shared by all agent connections."""

    def __init__(self, clock: Callable[[], float] = time.monotonic):
        self.clock = clock
        self.paths: dict[bytes, HostPath] = {}
        self.transport: asyncio.DatagramTransport | None = None

    def connection_made(self, transport) -> None:
        self.transport = transport

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
        if not self._send(path, seal(path.keys, path.conn_id, MOVE, path.seq_out, float(x), float(y), float(ts))):
            return False
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
                 clock: Callable[[], float] = time.monotonic):
        conn_id, key = unb64(offer.get("id")), unb64(offer.get("key"))
        port = offer.get("port")
        if (conn_id is None or len(conn_id) != ID_BYTES or key is None or len(key) != KEY_BYTES
                or not isinstance(port, int) or isinstance(port, bool) or not 0 < port < 65536):
            raise ValueError("the host's UDP offer is malformed")
        self.conn_id, self.keys, self.port = conn_id, Keys(key), port
        self.on_move = on_move
        self.clock = clock
        self.transport: asyncio.DatagramTransport | None = None
        self.seq_out = 0
        self.seq_in = 0
        self.acked_at = 0.0
        self.sent_at: dict[int, float] = {}
        self.rtts: list[float] = []
        self.moves = 0
        self.up = False
        self._task: asyncio.Task | None = None

    async def start(self, host_ip: str) -> None:
        loop = asyncio.get_running_loop()
        await loop.create_datagram_endpoint(lambda: self, remote_addr=(host_ip, self.port))
        self._task = asyncio.create_task(self._probe())

    def connection_made(self, transport) -> None:
        self.transport = transport

    def heard_lately(self) -> bool:
        return self.clock() - self.acked_at < ALIVE_S

    async def _probe(self) -> None:
        while True:
            self.probe()
            await asyncio.sleep(PROBE_EVERY_S)

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
        for old in [s for s in self.sent_at if s < self.seq_out - 8]:
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
        if seq <= self.seq_in:
            return  # late or replayed
        self.seq_in = seq
        if kind == ACK:
            now = self.clock()
            sent = self.sent_at.pop(values[0], None)
            if sent is not None and len(self.rtts) < 1000:
                self.rtts.append(now - sent)
            first = not self.heard_lately()
            self.acked_at = now
            if first:
                log.info("UDP path to the host is up; pointer moves can come over it")
                self.probe()  # tell the host straight away
        elif kind == MOVE:
            self.moves += 1
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
