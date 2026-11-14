"""The agent's side: send input to the helper when it's running."""

from __future__ import annotations

import logging
import threading
import time
from typing import Callable

from curslink.winhelper import WIRE_VERSION, events_pipe_name, pipe_name, wire

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


def _open_events(path: str):
    return open(path, "rb", buffering=0)  # noqa: SIM115 - kept open


class HelperWatch:
    """The host's side (wire v2): ask the helper to listen to the mouse and
    keyboard as well, and hand what it hears to the capture
    (``on_event(command)``). This PC's own hooks hear nothing over an app
    that runs as administrator; the helper's do (see curslink.winhelper.watch).

    ``on_connected()`` is called once the helper is listening, so the
    capture can put its own hooks back in front of the helper's."""

    def __init__(self, session: int, on_event: Callable[[tuple], None], on_connected: Callable[[], None],
                 opener: Callable[[str], tuple] = _open_pipe, open_events: Callable[[str], object] = _open_events,
                 sleep: Callable[[float], None] = time.sleep):
        self.path = pipe_name(session)
        self.events_path = events_pipe_name(session)
        self.on_event = on_event
        self.on_connected = on_connected
        self._open, self._open_events, self._sleep = opener, open_events, sleep
        self._lock = threading.Lock()
        self._pipe = None
        self.mode = "moves"
        self.version: int | None = None
        self.listening = False
        self._closed = False
        self._said: set[str] = set()

    def start(self) -> None:
        threading.Thread(target=self.run, name="curslink-helper-watch", daemon=True).start()

    def set_mode(self, mode: str) -> None:
        """"moves" while input is here, "grab" while it's on another computer."""
        with self._lock:
            self.mode = mode
            if self._pipe is not None and self.listening:
                try:
                    self._pipe.write(wire.encode("watch", mode))
                except OSError:
                    pass  # run() finds out and connects again

    def close(self) -> None:
        self._closed = True
        with self._lock:
            pipe, self._pipe = self._pipe, None
        if pipe is not None:
            try:
                pipe.close()
            except OSError:
                pass

    def _say(self, key: str, level: int, message: str, *args) -> None:
        if key not in self._said:
            self._said.add(key)
            log.log(level, message, *args)

    def run(self) -> None:
        while not self._closed:
            try:
                self.watch_once()
            except Exception:
                log.exception("input helper: watching failed")
            if not self._closed:
                self._sleep(RETRY_S if self.version is None or self.version >= 2 else 60.0)

    def watch_once(self) -> None:
        """Connect, ask the helper to listen, and read its events until either end goes."""
        try:
            pipe, version = self._open(self.path)
        except OSError as exc:
            self._say("missing", logging.INFO, "input helper not running (%s): the mouse isn't heard over apps "
                      "that run as administrator (Settings > Control admin apps turns it on)", exc)
            return
        self._said.discard("missing")
        self.version = version
        if version < 2:
            pipe.close()
            self._say("old", logging.WARNING, "input helper is from an older Curslink (wire v%s): turn "
                      "\"Control admin apps\" off and on again in Settings so it can hear the mouse over apps "
                      "that run as administrator", version)
            return
        events = None
        try:
            with self._lock:
                self._pipe = pipe
                pipe.write(wire.encode("watch", self.mode))
            for attempt in range(20):
                try:
                    events = self._open_events(self.events_path)
                    break
                except OSError:
                    if attempt == 19:
                        raise
                    self._sleep(0.1)
            with self._lock:
                self.listening = True
                pipe.write(wire.encode("watch", self.mode))  # anything changed while it was opening
            log.info("input helper is listening too (wire v%s): the mouse and keyboard are heard over apps "
                     "that run as administrator", version)
            self.on_connected()
            pending = b""
            while not self._closed:
                chunk = events.read(4096)
                if not chunk:
                    break
                pending += chunk
                *lines, pending = pending.split(b"\n")
                for line in lines:
                    command = wire.decode_event(line)
                    if command is not None:
                        self.on_event(command)
            log.info("input helper stopped sending events")
        except OSError as exc:
            log.info("lost the input helper (%s)", exc)
        finally:
            with self._lock:
                self.listening = False
                self._pipe = None
            for f in (events, pipe):
                if f is not None:
                    try:
                        f.close()
                    except OSError:
                        pass
