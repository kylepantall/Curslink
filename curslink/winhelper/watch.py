"""The injector's hooks for the host (wire v2, ``["watch", mode]``).

A program that isn't running as administrator doesn't hear the mouse or
the keyboard over one that is: Windows skips its low-level hooks for input
going to that app. So a Windows host stopped seeing the pointer at the edge
of the screen over an admin app (the pointer "couldn't leave the monitor"),
and while input was on another computer the keys went to that app instead.
The injector runs as SYSTEM and hears everything.

The host's own hooks stay first in line: it puts them back after the helper
starts its own, so the helper only gets what the host's hooks didn't take.
In "moves" mode the helper reports where the pointer goes and holds nothing
back (the host keeps the reports it didn't hear itself). In "grab" mode
input is on another computer: everything that reaches the helper's hooks is
reported and kept from this PC, as the host's own hooks would have.

Reports go out on a second pipe through a queue, never from the hook itself:
a hook that waits on a pipe gets taken away by Windows (LowLevelHooksTimeout).
"""

from __future__ import annotations

import ctypes
import logging
import os
import queue
import threading
import time
from typing import Callable

from curslink.winhelper import events_pipe_name, wire

log = logging.getLogger(__name__)

QUEUE_MAX = 4096

_WM_MOUSEMOVE = 0x0200
_BUTTON_MSGS = {
    0x0201: ("left", True), 0x0202: ("left", False),
    0x0204: ("right", True), 0x0205: ("right", False),
    0x0207: ("middle", True), 0x0208: ("middle", False),
}
_WM_XBUTTONDOWN, _WM_XBUTTONUP = 0x020B, 0x020C
_WM_MOUSEWHEEL, _WM_MOUSEHWHEEL = 0x020A, 0x020E
_KEY_MSGS = {0x0100: True, 0x0104: True, 0x0101: False, 0x0105: False}
_LLMHF_INJECTED_ANY = 0x01 | 0x02
_LLKHF_INJECTED_ANY = 0x10 | 0x02


def _hiword(value: int) -> int:
    return ctypes.c_short((value >> 16) & 0xFFFF).value


def mouse_report(msg: int, data, mode: str) -> tuple | None:
    """What to tell the host about one mouse hook event (None: nothing)."""
    if mode == "off" or getattr(data, "flags", 0) & _LLMHF_INJECTED_ANY:
        return None
    g = 1 if mode == "grab" else 0
    if msg == _WM_MOUSEMOVE:
        return ("pm", int(data.pt.x), int(data.pt.y), g)
    if mode != "grab":
        return None
    if msg in _BUTTON_MSGS:
        button, pressed = _BUTTON_MSGS[msg]
        return ("pb", button, pressed, g)
    if msg in (_WM_XBUTTONDOWN, _WM_XBUTTONUP):
        which = _hiword(data.mouseData)
        if which in (1, 2):
            return ("pb", f"x{which}", msg == _WM_XBUTTONDOWN, g)
        return None
    if msg == _WM_MOUSEWHEEL:
        return ("ps", 0, _hiword(data.mouseData) / 120, g)
    if msg == _WM_MOUSEHWHEEL:
        return ("ps", _hiword(data.mouseData) / 120, 0, g)
    return None


def key_report(msg: int, data, mode: str) -> tuple | None:
    """What to tell the host about one keyboard hook event (only while grabbing)."""
    if mode != "grab" or msg not in _KEY_MSGS or getattr(data, "flags", 0) & _LLKHF_INJECTED_ANY:
        return None
    vk = int(data.vkCode)
    if not 0 < vk < 256:
        return None
    return ("pk", vk, _KEY_MSGS[msg], 1)


class HookWatcher:
    """The hooks, the queue and the events pipe, for one injector."""

    def __init__(self, session: int, open_events: Callable[[], object] | None = None,
                 listeners: Callable[["HookWatcher"], tuple] | None = None):
        self.session = session
        self.mode = "off"
        self._open_events = open_events or self._serve_events
        self._make_listeners = listeners or _pynput_listeners
        self._queue: queue.Queue = queue.Queue(QUEUE_MAX)
        self._lock = threading.Lock()
        self._listeners: tuple = ()
        self._writer = None          # the host's end of the events pipe, once it's open
        self._thread: threading.Thread | None = None
        self._closed = False

    # -- the mode ---------------------------------------------------------------
    def set_mode(self, mode: str) -> None:
        with self._lock:
            if mode == self.mode:
                return
            log.info("hooks for the host: %s", mode)
            self.mode = mode
            if mode == "off":
                for listener in self._listeners:
                    listener.stop()
                self._listeners = ()
                return
            if self._thread is None:
                self._thread = threading.Thread(target=self._pump, name="helper-events", daemon=True)
                self._thread.start()
            if not self._listeners:
                self._listeners = self._make_listeners(self)
                for listener in self._listeners:
                    listener.start()

    def close(self) -> None:
        self._closed = True
        self.set_mode("off")
        self._queue.put(None)

    @property
    def holding(self) -> bool:
        """Keep input from this PC? Only while grabbing with the host there
        to take it: with nobody reading, holding it back would leave this PC
        with no mouse and keyboard at all."""
        return self.mode == "grab" and self._writer is not None

    # -- from the hooks (quick: on the hook's own thread) ----------------------
    def on_mouse(self, msg: int, data) -> bool:
        """True: keep this event from this PC."""
        report = mouse_report(msg, data, self.mode)
        return self._report(report)

    def on_key(self, msg: int, data) -> bool:
        return self._report(key_report(msg, data, self.mode))

    def _report(self, report) -> bool:
        if report is None:
            return False
        holding = self.holding and report[3] == 1
        if self._writer is None:
            return False
        try:
            self._queue.put_nowait(report)
        except queue.Full:
            return False  # the host isn't keeping up: let this one through here
        return holding

    # -- to the host -------------------------------------------------------------
    def _pump(self) -> None:
        while not self._closed:
            if self._writer is None:
                try:
                    self._writer = self._open_events()
                except OSError:
                    log.exception("events pipe failed")
                    time.sleep(1.0)
                    continue
                if self._writer is None:
                    time.sleep(1.0)
                    continue
                log.info("the host is reading the helper's events")
            report = self._queue.get()
            if report is None:
                break
            try:
                self._writer.write(wire.encode(*report))
            except OSError as exc:
                log.info("the host stopped reading the helper's events (%s)", exc)
                try:
                    self._writer.close()
                except OSError:
                    pass
                self._writer = None
                _drain(self._queue)

    def _serve_events(self):
        """Wait for the host to open the events pipe; its end, or None if
        someone from another session tried."""
        import msvcrt

        from curslink.winhelper import win32

        sid = win32.session_user_sid(self.session)
        if sid is None:
            return None
        handle = win32.create_pipe(events_pipe_name(self.session), win32.pipe_sddl(sid))
        try:
            pid = win32.wait_for_client(handle)
        except OSError:
            win32.k32.CloseHandle(handle)
            raise
        raw = os.fdopen(msvcrt.open_osfhandle(handle, 0), "wb", buffering=0)
        if win32.session_of(pid) != self.session:
            log.warning("refused process %d on the events pipe: it isn't in session %d", pid, self.session)
            raw.close()
            return None
        _drain(self._queue)  # nothing from before the host was there
        return raw


def _drain(q: queue.Queue) -> None:
    try:
        while True:
            if q.get_nowait() is None:
                q.put_nowait(None)
                return
    except queue.Empty:
        pass


def _pynput_listeners(watcher: HookWatcher) -> tuple:
    from pynput import keyboard, mouse

    listeners: dict[str, object] = {}

    def mouse_filter(msg, data):
        if watcher.on_mouse(msg, data):
            listeners["mouse"].suppress_event()  # raises: pynput's hook swallows the event
        return False  # no callbacks to call

    def key_filter(msg, data):
        if watcher.on_key(msg, data):
            listeners["keyboard"].suppress_event()
        return False

    listeners["mouse"] = mouse.Listener(win32_event_filter=mouse_filter)
    listeners["keyboard"] = keyboard.Listener(win32_event_filter=key_filter)
    return listeners["mouse"], listeners["keyboard"]
