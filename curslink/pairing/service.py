"""Receiving pairing requests while the host or agent runs.

When another device asks to pair, the recipient asks its user for the code
shown on the other device: in a small window if there is a desktop to show
one on (see :mod:`curslink.pairing.dialog`), otherwise in the terminal.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import sys
import time
from typing import Awaitable, Callable

from curslink.net.framing import STREAM_LIMIT
from curslink.pairing.handshake import DeviceInfo, PairingError, PairResult, respond
from curslink.pairing.store import PairingStore
from curslink.protocol import encode

log = logging.getLogger(__name__)

DEFAULT_PAIRING_PORT = 24851
# After this many failed attempts in FAIL_WINDOW_S, refuse requests for LOCKOUT_S.
MAX_FAILURES = 3
FAIL_WINDOW_S = 300.0
LOCKOUT_S = 60.0

AskCode = Callable[[DeviceInfo], Awaitable[str | None]]


class PairingListener:
    """Plain TCP listener for pairing requests (the handshake brings its own security)."""

    def __init__(
        self,
        me: DeviceInfo,
        store: PairingStore,
        ask_code: AskCode,
        host: str = "0.0.0.0",
        port: int = DEFAULT_PAIRING_PORT,
        on_paired: Callable[[PairResult], None] = lambda r: None,
    ):
        self.me = me
        self.store = store
        self.ask_code = ask_code
        self.host = host
        self.port = port
        self.on_paired = on_paired
        self._busy = False
        self._failures: list[float] = []
        self._locked_until = 0.0
        self._server: asyncio.base_events.Server | None = None

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._handle, self.host, self.port, limit=STREAM_LIMIT)
        self.port = self._server.sockets[0].getsockname()[1]

    async def close(self) -> None:
        if self._server:
            self._server.close()
            await self._server.wait_closed()

    def _refusal(self) -> str | None:
        now = time.monotonic()
        if now < self._locked_until:
            return "too many failed attempts; try again in a minute"
        if self._busy:
            return "already pairing with another device"
        return None

    def _failed(self) -> None:
        now = time.monotonic()
        self._failures = [t for t in self._failures if now - t < FAIL_WINDOW_S] + [now]
        if len(self._failures) >= MAX_FAILURES:
            self._locked_until = now + LOCKOUT_S
            self._failures.clear()
            log.warning("pairing locked for %ds after repeated failures", LOCKOUT_S)

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        peer_addr = writer.get_extra_info("peername")
        try:
            refusal = self._refusal()
            if refusal:
                writer.write(encode("pair_done", ok=False, reason=refusal))
                await writer.drain()
                return
            self._busy = True
            try:
                result = await respond(reader, writer, self.me, self.ask_code)
            except PairingError as exc:
                log.info("pairing request from %s failed: %s", peer_addr, exc)
                if str(exc) not in ("declined",):
                    self._failed()
                return
            finally:
                self._busy = False
            self.store.add(result.peer.device_id, result.peer.name, result.peer.role, result.key)
            log.info("paired with %s (%s)", result.peer.name, result.peer.device_id)
            self.on_paired(result)
        except (ConnectionError, OSError) as exc:
            log.debug("pairing connection from %s ended: %s", peer_addr, exc)
        finally:
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()


# ------------------------------------------------------------- asking the user
def _prompt_text(peer: DeviceInfo) -> str:
    return f"{peer.name} ({peer.role}, id {peer.device_id}) wants to pair with this device."


async def ask_in_terminal(peer: DeviceInfo) -> str | None:
    print(f"\n*** {_prompt_text(peer)}")
    print(f"*** Type the 6-digit code shown on {peer.name} and press Enter (blank to decline):")
    loop = asyncio.get_running_loop()
    line = await loop.run_in_executor(None, sys.stdin.readline)
    return line.strip() or None


def dialog_command(peer: DeviceInfo) -> list[str]:
    args = ["_pair-dialog", peer.name, peer.role, peer.device_id]
    if getattr(sys, "frozen", False):
        return [sys.executable, *args]
    return [sys.executable, "-m", "curslink", *args]


async def ask_in_dialog(peer: DeviceInfo) -> str | None:
    """Shows the pairing window; falls back to the terminal without a desktop."""
    from curslink.pairing.dialog import EXIT_DECLINED, EXIT_NO_DISPLAY

    try:
        proc = await asyncio.create_subprocess_exec(
            *dialog_command(peer), stdout=asyncio.subprocess.PIPE, stdin=asyncio.subprocess.DEVNULL
        )
    except OSError:
        log.debug("pairing window unavailable", exc_info=True)
        return await ask_in_terminal(peer)
    try:
        out, _ = await proc.communicate()
    except asyncio.CancelledError:  # timed out: take the window away
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
        raise
    if proc.returncode == 0:
        return out.decode().strip() or None
    if proc.returncode == EXIT_DECLINED:
        return None
    if proc.returncode != EXIT_NO_DISPLAY:
        log.debug("pairing window exited with %s", proc.returncode)
    return await ask_in_terminal(peer)
