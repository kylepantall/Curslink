"""macOS: hand the system's own shortcuts to the computer input is on.

macOS acts on its system-wide shortcuts (Cmd+Tab, Cmd+Space, Ctrl+arrows
for Spaces and Mission Control, Cmd+Shift+3/4/5 for screenshots, the input
source switch, and the rest of System Settings > Keyboard > Keyboard
Shortcuts) in the window server, before the event tap that hands the
keyboard to another computer sees them. So while input was on another
computer, those still worked on the Mac and never reached it.

While input is on another computer they are turned off here (the way
app switchers such as AltTab take over Cmd+Tab), and the ones turned off
are turned back on as soon as input comes home. Which ones were turned off
is written to a file first, so that if Curslink stops without turning them
back on, the next start does.

Turning them off and on is a call to the window server each, so it runs on
a thread of its own, never in the event tap.
"""

from __future__ import annotations

import ctypes
import json
import logging
import sys
import threading
from pathlib import Path

log = logging.getLogger(__name__)

# Symbolic hot key numbers in use run from 1 to a little over 200.
HOTKEY_IDS = range(1, 300)
_LIBS = (
    "/System/Library/PrivateFrameworks/SkyLight.framework/SkyLight",
    "/System/Library/Frameworks/CoreGraphics.framework/CoreGraphics",
)


class _CGS:
    """The window server's calls for turning symbolic hot keys off and on."""

    def __init__(self):
        self.lib = None
        for path in _LIBS:
            try:
                lib = ctypes.CDLL(path)
                lib.CGSIsSymbolicHotKeyEnabled.argtypes = [ctypes.c_int]
                lib.CGSIsSymbolicHotKeyEnabled.restype = ctypes.c_bool
                lib.CGSSetSymbolicHotKeyEnabled.argtypes = [ctypes.c_int, ctypes.c_bool]
                lib.CGSSetSymbolicHotKeyEnabled.restype = ctypes.c_int
            except (OSError, AttributeError):
                continue
            self.lib = lib
            break

    def enabled(self, hotkey: int) -> bool:
        return bool(self.lib.CGSIsSymbolicHotKeyEnabled(hotkey))

    def set_enabled(self, hotkey: int, on: bool) -> bool:
        return self.lib.CGSSetSymbolicHotKeyEnabled(hotkey, on) == 0


def default_state_path() -> Path:
    from curslink.pairing.store import default_dir

    return default_dir() / "system-shortcuts-off.json"


class SystemShortcuts:
    """Turns this Mac's system shortcuts off while input is elsewhere."""

    def __init__(self, cgs=None, state_path: Path | None = None):
        self._cgs = cgs
        self._state_path = state_path
        self._cond = threading.Condition()
        self._want_off = False
        self._is_off = False
        self._off: list[int] = []
        self._thread: threading.Thread | None = None
        self._failed = False

    # -- the window server ---------------------------------------------------
    def _api(self):
        if self._cgs is None:
            self._cgs = _CGS()
        return self._cgs if getattr(self._cgs, "lib", True) is not None else None

    def _path(self) -> Path:
        if self._state_path is None:
            self._state_path = default_state_path()
        return self._state_path

    def restore_left_over(self) -> None:
        """Turn back on what an earlier run turned off and never turned on."""
        try:
            ids = json.loads(self._path().read_text())
        except (OSError, ValueError):
            return
        api = self._api()
        if api is None or not isinstance(ids, list):
            return
        ids = [i for i in ids if isinstance(i, int)]
        for hotkey in ids:
            api.set_enabled(hotkey, True)
        log.info("turned %d macOS shortcuts back on (left off when Curslink last stopped)", len(ids))
        self._forget()

    def _turn_off(self) -> None:
        api = self._api()
        if api is None:
            if not self._failed:
                log.info("can't hand macOS's own shortcuts to other computers here")
                self._failed = True
            return
        off = [i for i in HOTKEY_IDS if api.enabled(i)]
        try:
            path = self._path()
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(off))
        except OSError:
            log.warning("couldn't note which macOS shortcuts are off; leaving them on", exc_info=True)
            return
        self._off = [i for i in off if api.set_enabled(i, False)]
        log.debug("turned off %d macOS shortcuts while input is on another computer", len(self._off))

    def _turn_on(self) -> None:
        api = self._api()
        if api is not None:
            for hotkey in self._off:
                api.set_enabled(hotkey, True)
        self._off = []
        self._forget()

    def _forget(self) -> None:
        try:
            self._path().unlink()
        except OSError:
            pass

    # -- the worker ------------------------------------------------------------
    def set_off(self, off: bool) -> None:
        """Ask for the shortcuts off (input left this Mac) or on (it came
        home). Returns at once; quick changes settle on the last one."""
        with self._cond:
            self._want_off = off
            if self._thread is None:
                self._thread = threading.Thread(target=self._run, name="curslink-shortcuts", daemon=True)
                self._thread.start()
            self._cond.notify_all()

    def wait(self, timeout: float = 1.0) -> bool:
        """Wait until what was asked for is done (stopping)."""
        with self._cond:
            return self._cond.wait_for(lambda: self._is_off == self._want_off, timeout)

    def _run(self) -> None:
        while True:
            with self._cond:
                self._cond.wait_for(lambda: self._is_off != self._want_off)
                off = self._want_off
            try:
                (self._turn_off if off else self._turn_on)()
            except Exception:  # pragma: no cover - macOS only
                log.warning("couldn't turn macOS's shortcuts %s", "off" if off else "on", exc_info=True)
            with self._cond:
                self._is_off = off
                self._cond.notify_all()


def for_this_computer() -> SystemShortcuts | None:
    return SystemShortcuts() if sys.platform == "darwin" else None
