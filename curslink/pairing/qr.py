"""Pairing a phone by scanning a QR code on the host.

The host shows a QR code holding a ``curslink://pair`` link::

    curslink://pair?v=1&id=<host id>&n=<name>&a=<ip>,<ip>&p=<pairing port>
                   &c=<control port>&t=<token>[&r=<relay address>&k=<relay key>]

The phone scans it and pairs with the host at one of those addresses, as the
initiator of the usual handshake (see :mod:`curslink.pairing.handshake`),
proving it saw the code with the one-time ``t`` token instead of having
someone type a 6-digit code. ``c`` is where it then connects for input, and
``r``/``k`` (when the host uses a relay) let it reach the host away from home
too. The token works for one pairing, for a few minutes, so a photo of an
old QR code is no use.

:class:`QrPairing` is the listener behind one QR code. It runs on its own
port, apart from the host's usual pairing listener, so only the window that
shows the code takes requests for it.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import logging
import secrets
import time
from typing import Callable
from urllib.parse import parse_qs, quote, urlparse

from curslink.net.framing import STREAM_LIMIT
from curslink.pairing.handshake import DeviceInfo, PairingError, PairResult, respond

log = logging.getLogger(__name__)

SCHEME = "curslink"
TOKEN_BYTES = 16
LIFETIME_S = 300.0
MAX_FAILURES = 3


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def invite_url(me: DeviceInfo, addresses: list[str], pairing_port: int, control_port: int, token: bytes,
               relay: str = "", relay_key: str = "") -> str:
    parts = [("v", "1"), ("id", me.device_id), ("n", me.name), ("a", ",".join(addresses)),
             ("p", str(pairing_port)), ("c", str(control_port)), ("t", _b64(token))]
    if relay and relay_key:
        parts += [("r", relay), ("k", relay_key)]
    return f"{SCHEME}://pair?" + "&".join(f"{k}={quote(v, safe=',:.-_')}" for k, v in parts)


def parse_invite(url: str) -> dict:
    """The fields of a ``curslink://pair`` link (what the phone does, for tests)."""
    u = urlparse(url.strip())
    if u.scheme != SCHEME or u.netloc != "pair":
        raise ValueError("not a Curslink pairing code")
    q = {k: v[0] for k, v in parse_qs(u.query).items()}
    if q.get("v") != "1" or not q.get("id") or not q.get("a") or not q.get("t"):
        raise ValueError("this pairing code is from a newer Curslink, or damaged")
    return {
        "id": q["id"], "name": q.get("n") or q["id"], "addresses": q["a"].split(","),
        "pairing_port": int(q["p"]), "control_port": int(q.get("c") or 0), "token": _unb64(q["t"]),
        "relay": q.get("r", ""), "relay_key": q.get("k", ""),
    }


class QrPairing:
    """Takes one pairing request from a device that scanned our QR code."""

    def __init__(self, me: DeviceInfo, on_paired: Callable[[PairResult, str], None],
                 host: str = "0.0.0.0", port: int = 0, lifetime_s: float = LIFETIME_S,
                 on_failed: Callable[[str], None] = lambda reason: None):
        self.me = me
        self.on_paired = on_paired
        self.on_failed = on_failed
        self.host, self.port = host, port
        self.lifetime_s = lifetime_s
        self.token = secrets.token_bytes(TOKEN_BYTES)
        self.expires_at = time.monotonic() + lifetime_s
        self.failures = 0
        self.done = False
        self._busy = False
        self._server: asyncio.base_events.Server | None = None

    def renew(self) -> None:
        """A fresh token (the QR code must be redrawn)."""
        self.token = secrets.token_bytes(TOKEN_BYTES)
        self.expires_at = time.monotonic() + self.lifetime_s
        self.failures = 0

    def url(self, addresses: list[str], control_port: int, relay: str = "", relay_key: str = "") -> str:
        return invite_url(self.me, addresses, self.port, control_port, self.token, relay, relay_key)

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._handle, self.host, self.port, limit=STREAM_LIMIT)
        self.port = self._server.sockets[0].getsockname()[1]

    async def close(self) -> None:
        if self._server:
            self._server.close()
            with contextlib.suppress(Exception):
                await self._server.wait_closed()
            self._server = None

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        from curslink.protocol import encode

        peer_addr = writer.get_extra_info("peername")
        address = peer_addr[0] if isinstance(peer_addr, tuple) and peer_addr else ""

        async def no_code(_peer):  # a request without the QR proof: nobody here to type a code
            return None

        try:
            if self.done or self._busy or time.monotonic() > self.expires_at:
                reason = "this QR code has expired; show a new one" if not self._busy else "busy pairing"
                writer.write(encode("pair_done", ok=False, reason=reason))
                await writer.drain()
                return
            self._busy = True
            try:
                result = await respond(reader, writer, self.me, no_code, token=self.token)
            except PairingError as exc:
                log.info("QR pairing from %s failed: %s", address, exc)
                self.failures += 1
                if self.failures >= MAX_FAILURES:
                    self.renew()
                self.on_failed(str(exc))
                return
            finally:
                self._busy = False
            self.done = True
            log.info("paired with %s (%s) by QR code", result.peer.name, result.peer.device_id)
            self.on_paired(result, address)
        except (ConnectionError, OSError) as exc:
            log.debug("QR pairing connection from %s ended: %s", address, exc)
        finally:
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()
