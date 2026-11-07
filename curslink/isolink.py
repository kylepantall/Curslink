"""Isolink Connect: talking to Isolink, the messenger, on this computer.

Isolink listens on ``127.0.0.1:47231`` for apps on the same computer (one
JSON frame per line; its docs/connect.md). The first time Curslink says
hello, Isolink asks the person whether to allow it, and gives it a secret
(kept in ``isolink-token`` beside the pairing store) so it isn't asked
again. Curslink asks for the ``windows`` capability: it hears Isolink's
floating chats being dragged (``windows.drag``) and can open one under the
pointer, still held (``windows.open``). The person can turn Curslink, or
just that, off in Isolink's Settings > Privacy > Connected apps.

Isolink gives every app its own handles for people and chats, the same on
all of the person's devices: two computers are linked to the same Isolink
account when their ``me`` handles match, and a chat's handle from one names
the same chat on the other. See :mod:`curslink.popups` for what's done
with that.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

log = logging.getLogger(__name__)

CONNECT_HOST = "127.0.0.1"
CONNECT_PORT = 47231
PROTOCOL = 1
APP = {"id": "io.github.orimovo.curslink", "name": "Curslink", "publisher": "orimovo"}
WINDOWS = "windows"
RETRY_S = 5.0            # Isolink isn't running: look again this often
CALL_TIMEOUT_S = 5.0
HELLO_TIMEOUT_S = 300.0  # the person may take a while to answer the consent sheet


@dataclass
class ChatDrag:
    """A floating chat being dragged on this computer, as Isolink tells it."""

    chat: str        # its handle
    open: bool       # open rather than folded to its bubble
    width: float = 0
    height: float = 0
    grab_x: float = 0
    grab_y: float = 0

    def to_json(self) -> dict:
        return {"chat": self.chat, "open": self.open, "width": self.width, "height": self.height,
                "grabX": self.grab_x, "grabY": self.grab_y}

    @classmethod
    def from_json(cls, p) -> ChatDrag | None:
        if not isinstance(p, dict) or not isinstance(p.get("chat"), str) or not p["chat"]:
            return None

        def n(key: str) -> float:
            v = p.get(key)
            return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else 0.0

        return cls(p["chat"], p.get("open") is True, n("width"), n("height"), n("grabX"), n("grabY"))


class IsolinkError(Exception):
    def __init__(self, code: str, message: str = ""):
        super().__init__(f"{code}: {message}" if message else code)
        self.code = code


class IsolinkLink:
    """A connection to this computer's Isolink, kept up while Curslink runs.

    ``on_change()`` is called (on the loop) whenever :attr:`me`,
    :attr:`windows` or :attr:`dragging` change.
    """

    def __init__(self, token_file: Path | None, host: str = CONNECT_HOST, port: int = CONNECT_PORT):
        self.token_file = token_file
        self.host = host
        self.port = port
        self.me: str | None = None          # this account's handle for Curslink; None: not connected
        self.windows = False                 # Isolink gave us the windows capability
        self.dragging: ChatDrag | None = None
        self.on_change: Callable[[], None] = lambda: None
        self._writer: asyncio.StreamWriter | None = None
        self._next = 0
        self._waiting: dict[int, asyncio.Future] = {}
        self._task: asyncio.Task | None = None
        self._said_waiting = False

    # ------------------------------------------------------------ running
    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.get_running_loop().create_task(self._run())

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._task
            self._task = None

    @property
    def ready(self) -> bool:
        """Connected, and allowed to carry floating chats."""
        return self.me is not None and self.windows

    async def _run(self) -> None:
        while True:
            try:
                stop = await self._session()
            except asyncio.CancelledError:
                raise
            except OSError as exc:
                if not self._said_waiting:
                    log.info("isolink: not running here (%s); floating chats stay put until it is", exc)
                    self._said_waiting = True
                stop = False
            except Exception:
                log.warning("isolink: connection failed", exc_info=True)
                stop = False
            self._lost()
            if stop:
                return
            await asyncio.sleep(RETRY_S)

    async def _session(self) -> bool:
        """One connection, until it closes. True: don't try again this run."""
        reader, writer = await asyncio.open_connection(self.host, self.port, limit=512 * 1024)
        self._writer = writer
        self._said_waiting = False
        reading = asyncio.get_running_loop().create_task(self._read(reader))
        try:
            token = self._token()
            hello = {"protocol": PROTOCOL, "app": APP, "capabilities": [WINDOWS]}
            if token:
                hello["token"] = token
            try:
                answer = await self.call("hello", hello, timeout=HELLO_TIMEOUT_S)
            except IsolinkError as exc:
                if exc.code in ("denied", "turned_off"):
                    log.info("isolink: Curslink isn't allowed (%s); turn it on in Isolink's Connected apps "
                             "and restart Curslink", exc.code)
                    return True
                if exc.code == "consent_required" or exc.code == "not_connected":
                    return False
                raise
            if not isinstance(answer, dict):
                raise IsolinkError("bad_request", "hello answered nothing")
            if isinstance(answer.get("token"), str):
                self._save_token(answer["token"])
            me = answer.get("me")
            self.me = me.get("handle") if isinstance(me, dict) and isinstance(me.get("handle"), str) else None
            self.windows = WINDOWS in (answer.get("capabilities") or [])
            log.info("isolink: connected%s", "" if self.windows else
                     " (floating chats aren't allowed: turn them on for Curslink in Isolink's Connected apps)")
            self.on_change()
            await reading
            return False
        finally:
            reading.cancel()
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()

    async def _read(self, reader: asyncio.StreamReader) -> None:
        try:
            await self._frames(reader)
        finally:
            for fut in self._waiting.values():
                if not fut.done():
                    fut.set_exception(ConnectionError("isolink closed the connection"))

    async def _frames(self, reader: asyncio.StreamReader) -> None:
        while True:
            line = await reader.readline()
            if not line:
                return
            try:
                frame = json.loads(line)
            except ValueError:
                continue
            if not isinstance(frame, dict):
                continue
            if "ev" in frame:
                self._event(frame.get("ev"), frame.get("p"))
                continue
            fut = self._waiting.pop(frame.get("id"), None) if isinstance(frame.get("id"), int) else None
            if fut is None or fut.done():
                continue
            if isinstance(frame.get("e"), dict):
                e = frame["e"]
                fut.set_exception(IsolinkError(str(e.get("code", "failed")), str(e.get("message", ""))))
            else:
                fut.set_result(frame.get("r"))

    def _event(self, ev, p) -> None:
        if ev == "windows.drag":
            drag = ChatDrag.from_json(p)
            if drag is None:
                return
            held = isinstance(p, dict) and p.get("held") is True
            if held:
                self.dragging = drag
            elif self.dragging is not None and self.dragging.chat == drag.chat:
                self.dragging = None
            else:
                return
            self.on_change()
        elif ev == "capabilities.changed":
            caps = p.get("capabilities") if isinstance(p, dict) else None
            self.windows = isinstance(caps, list) and WINDOWS in caps
            if not self.windows:
                self.dragging = None
            log.info("isolink: floating chats %s for Curslink", "allowed" if self.windows else "turned off")
            self.on_change()
        elif ev == "closed":
            log.info("isolink: it ended the connection (%s)", p.get("reason") if isinstance(p, dict) else "")

    def _lost(self) -> None:
        self._writer = None
        for fut in self._waiting.values():
            if not fut.done():
                fut.set_exception(ConnectionError("isolink went away"))
        self._waiting.clear()
        changed = self.me is not None or self.dragging is not None
        self.me, self.windows, self.dragging = None, False, None
        if changed:
            self.on_change()

    # -------------------------------------------------------------- calls
    async def call(self, method: str, params: dict | None = None, timeout: float = CALL_TIMEOUT_S):
        writer = self._writer
        if writer is None or writer.transport.is_closing():
            raise ConnectionError("not connected to isolink")
        self._next += 1
        rid = self._next
        fut = asyncio.get_running_loop().create_future()
        self._waiting[rid] = fut
        writer.write((json.dumps({"id": rid, "m": method, "p": params or {}}, separators=(",", ":")) + "\n")
                     .encode())
        try:
            return await asyncio.wait_for(fut, timeout)
        finally:
            self._waiting.pop(rid, None)

    async def open_chat(self, chat: str, held: bool, open_: bool) -> bool:
        """Float ``chat`` here; ``held``: under the pointer, carried on with
        the press that's down now."""
        try:
            return await self.call("windows.open", {"chat": chat, "held": held, "open": open_}) is True
        except (IsolinkError, ConnectionError, asyncio.TimeoutError) as exc:
            log.info("isolink: couldn't open the chat here: %s", exc)
            return False

    async def close_chat(self, chat: str) -> None:
        try:
            await self.call("windows.close", {"chat": chat})
        except (IsolinkError, ConnectionError, asyncio.TimeoutError) as exc:
            log.info("isolink: couldn't close the chat here: %s", exc)

    # -------------------------------------------------------------- token
    def _token(self) -> str | None:
        if self.token_file is None:
            return None
        try:
            return self.token_file.read_text().strip() or None
        except OSError:
            return None

    def _save_token(self, token: str) -> None:
        if self.token_file is None:
            return
        try:
            self.token_file.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(self.token_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as f:
                f.write(token)
        except OSError:
            log.warning("isolink: couldn't keep its secret; it will ask again next time", exc_info=True)


def default_token_file() -> Path:
    from curslink.pairing.store import default_dir

    return default_dir() / "isolink-token"
