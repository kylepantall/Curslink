"""The agent's side: send input to the helper when it's running."""

from __future__ import annotations

import logging
import threading
import time
from typing import Callable

from curslink.winhelper import WIRE_VERSION, pipe_name, wire

log = logging.getLogger(__name__)

RETRY_S = 3.0   # look for the helper again this often while it isn't there


def _open_pipe(path: str):
    f = open(path, "r+b", buffering=0)  # noqa: SIM115 - kept open
    try:
        hello = wire.decode(f.readline())
    except OSError:
        f.close()
        raise
    if not hello or hello[0] != "hello":
        f.close()
        raise OSError("not the Curslink helper")
    return f, hello[1]


class HelperSink:
    """An InputSink that forwards to the helper's pipe, or to ``local`` (the
    ordinary sink) whenever the helper isn't there."""

    def __init__(self, local, session: int, modifier_map: dict[str, str] | None = None,
                 opener: Callable[[str], tuple] = _open_pipe, clock: Callable[[], float] = time.monotonic):
        self.local = local
        self.path = pipe_name(session)
        self.modifier_map = modifier_map or {}
        self._open = opener
        self._clock = clock
        self._lock = threading.Lock()
        self._pipe = None
        self._tried_at: float | None = None
        self._said_missing = False

    @property
    def connected(self) -> bool:
        return self._pipe is not None

    def _send(self, *fields) -> bool:
        with self._lock:
            if self._pipe is None:
                now = self._clock()
                if self._tried_at is not None and now - self._tried_at < RETRY_S:
                    return False
                self._tried_at = now
                try:
                    self._pipe, version = self._open(self.path)
                except OSError as exc:
                    if not self._said_missing:
                        log.info("input helper not running (%s); injecting input here", exc)
                        self._said_missing = True
                    return False
                self._said_missing = False
                log.info("sending input through the input helper (wire v%s, this agent v%s): "
                         "admin apps and UAC prompts can be used", version, WIRE_VERSION)
            try:
                self._pipe.write(wire.encode(*fields))
                return True
            except OSError as exc:
                log.warning("lost the input helper (%s); injecting input here", exc)
                try:
                    self._pipe.close()
                except OSError:
                    pass
                self._pipe = None
                self._tried_at = self._clock()
                return False

    def move_to(self, x: int, y: int) -> None:
        if not self._send("m", int(x), int(y)):
            self.local.move_to(x, y)

    def button(self, button: str, pressed: bool) -> None:
        if not self._send("b", button, pressed):
            self.local.button(button, pressed)

    def scroll(self, dx: float, dy: float) -> None:
        if not self._send("s", dx, dy):
            self.local.scroll(dx, dy)

    def key(self, key: str, pressed: bool) -> None:
        # The local sink maps modifiers itself; the helper gets them mapped.
        if not self._send("k", self.modifier_map.get(key, key), pressed):
            self.local.key(key, pressed)

    def close(self) -> None:
        with self._lock:
            if self._pipe is not None:
                try:
                    self._pipe.close()
                except OSError:
                    pass
                self._pipe = None
