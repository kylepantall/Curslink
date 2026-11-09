"""Hide this computer's cursor while the pointer is on another device.

Only one device has the pointer at a time, but every other one still shows
its own cursor wherever input last left it, which reads as a second (or
third) pointer. Agents hide theirs when input leaves and show it again when
it comes back; a Windows or Linux host hides its parked cursor while input
is on another device (a Mac host already does, see
:mod:`curslink.inputs.pynput_backend`).

How, per platform:

* macOS: ``CGDisplayHideCursor`` after setting "SetsCursorInBackground" (what
  Synergy and Barrier do), shared with the Mac host's own hiding.
* Windows: every standard system cursor is swapped for a blank one
  (``SetSystemCursor``) and the user's own scheme is loaded back
  (``SystemParametersInfo(SPI_SETCURSORS)``) to show it. That outlives this
  process, so the cursors are also put back when it starts and exits, and by
  the app when the service it runs exits (see :func:`restore_windows`).
* Linux (X11): ``XFixesHideCursor`` on the root window. Wayland: not hidden.

An agent's own mouse or touchpad still works while its cursor is hidden: a
hidden cursor that moves without the host moving it shows again at once
(see :class:`CursorHider`).
"""

from __future__ import annotations

import atexit
import ctypes
import logging
import os
import sys
from typing import Callable

log = logging.getLogger(__name__)

# While hidden, how often to look whether this computer's own mouse moved it.
WATCH_S = 0.1
# Further than this (OS px) from where it was hidden is someone using it.
WATCH_SLOP_PX = 2


class Backend:
    """Hides and shows the cursor on this OS; ``ok`` False where it can't."""

    ok = False

    def hide(self) -> bool:
        return False

    def show(self) -> None:
        pass


class _MacBackend(Backend):
    ok = True

    def hide(self) -> bool:
        from curslink.inputs.pynput_backend import _mac_cursor_visible

        _mac_cursor_visible(False)
        return True

    def show(self) -> None:
        from curslink.inputs.pynput_backend import _mac_cursor_visible

        _mac_cursor_visible(True)


# The standard cursors (OCR_*): arrow, I-beam, wait, cross, up arrow, the four
# sizing arrows, move, no, hand, busy arrow, help, pin, person.
_OCR_IDS = (32512, 32513, 32514, 32515, 32516, 32642, 32643, 32644, 32645, 32646,
            32648, 32649, 32650, 32651, 32671, 32672)
_SPI_SETCURSORS = 0x0057


def _user32():
    user32 = ctypes.WinDLL("user32", use_last_error=True)  # type: ignore[attr-defined]
    user32.CreateCursor.restype = ctypes.c_void_p
    user32.CreateCursor.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                    ctypes.c_void_p, ctypes.c_void_p]
    user32.CopyIcon.restype = ctypes.c_void_p
    user32.CopyIcon.argtypes = [ctypes.c_void_p]
    user32.SetSystemCursor.restype = ctypes.c_int
    user32.SetSystemCursor.argtypes = [ctypes.c_void_p, ctypes.c_uint]
    user32.DestroyCursor.argtypes = [ctypes.c_void_p]
    user32.SystemParametersInfoW.argtypes = [ctypes.c_uint, ctypes.c_uint, ctypes.c_void_p, ctypes.c_uint]
    return user32


def restore_windows() -> None:
    """Load the user's own cursors back (undoes any blank ones left behind)."""
    if sys.platform != "win32":
        return
    try:
        _user32().SystemParametersInfoW(_SPI_SETCURSORS, 0, None, 0)
    except Exception:  # pragma: no cover - Windows only
        log.debug("couldn't put the cursors back", exc_info=True)


class _WinBackend(Backend):
    ok = True

    def __init__(self):
        self._user32 = _user32()
        self._swapped = False
        restore_windows()  # blank cursors left by a copy that didn't get to put them back
        atexit.register(self.show)

    def hide(self) -> bool:
        u = self._user32
        size = 32
        and_mask = (ctypes.c_ubyte * (size * size // 8))(*([0xFF] * (size * size // 8)))
        xor_mask = (ctypes.c_ubyte * (size * size // 8))()
        blank = u.CreateCursor(None, 0, 0, size, size, and_mask, xor_mask)
        if not blank:
            log.warning("couldn't make a blank cursor (error %s)", ctypes.get_last_error())
            return False
        done = 0
        try:
            for ocr in _OCR_IDS:
                copy = u.CopyIcon(blank)  # SetSystemCursor takes it (and destroys it later)
                if copy and u.SetSystemCursor(copy, ocr):
                    done += 1
                elif copy:
                    u.DestroyCursor(copy)
        finally:
            u.DestroyCursor(blank)
        self._swapped = done > 0
        if not done:
            log.warning("couldn't hide the cursor (error %s)", ctypes.get_last_error())
        return self._swapped

    def show(self) -> None:
        if self._swapped:
            self._swapped = False
            restore_windows()


class _X11Backend(Backend):
    def __init__(self):
        self._display = None
        try:
            x11 = ctypes.CDLL("libX11.so.6")
            xfixes = ctypes.CDLL("libXfixes.so.3")
            x11.XOpenDisplay.restype = ctypes.c_void_p
            x11.XOpenDisplay.argtypes = [ctypes.c_char_p]
            x11.XDefaultRootWindow.restype = ctypes.c_ulong
            x11.XDefaultRootWindow.argtypes = [ctypes.c_void_p]
            x11.XFlush.argtypes = [ctypes.c_void_p]
            xfixes.XFixesHideCursor.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
            xfixes.XFixesShowCursor.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
            display = x11.XOpenDisplay(None)
            if not display:
                return
            self._x11, self._xfixes, self._display = x11, xfixes, display
            self._root = x11.XDefaultRootWindow(display)
            self.ok = True
        except OSError:
            log.debug("no XFixes: the cursor can't be hidden here", exc_info=True)
        self._hidden = False

    def hide(self) -> bool:
        if not self.ok or self._hidden:
            return self._hidden
        self._xfixes.XFixesHideCursor(self._display, self._root)
        self._x11.XFlush(self._display)
        self._hidden = True
        return True

    def show(self) -> None:
        if self.ok and self._hidden:
            self._xfixes.XFixesShowCursor(self._display, self._root)
            self._x11.XFlush(self._display)
            self._hidden = False


def platform_backend() -> Backend:
    try:
        if sys.platform == "darwin":
            return _MacBackend()
        if sys.platform == "win32":
            return _WinBackend()
        if sys.platform.startswith("linux") and os.environ.get("DISPLAY") and not os.environ.get("WAYLAND_DISPLAY"):
            return _X11Backend()
    except Exception:
        log.warning("can't hide the cursor on this computer", exc_info=True)
    return Backend()


def _pynput_position() -> Callable[[], tuple[float, float] | None]:
    try:
        from pynput import mouse

        controller = mouse.Controller()
    except Exception:
        return lambda: None

    def position():
        try:
            x, y = controller.position
            return float(x), float(y)
        except Exception:
            return None

    return position


class CursorHider:
    """Hides this computer's cursor (:meth:`hide`) until :meth:`show`.

    With ``call_later`` (the event loop's), it also looks every
    :data:`WATCH_S` while hidden whether something here moved the cursor
    (this computer's own mouse) and shows it again if so, so a device used
    on its own never ends up without a cursor."""

    def __init__(self, backend: Backend | None = None,
                 position: Callable[[], tuple[float, float] | None] | None = None,
                 call_later: Callable[[float, Callable[[], None]], object] | None = None):
        self.backend = backend if backend is not None else platform_backend()
        self.position = position if position is not None else (_pynput_position() if call_later else (lambda: None))
        self.call_later = call_later
        self.hidden = False
        self._at: tuple[float, float] | None = None
        self._gen = 0

    @property
    def can_hide(self) -> bool:
        return self.backend.ok

    def hide(self) -> None:
        if self.hidden or not self.backend.ok:
            return
        try:
            self.hidden = bool(self.backend.hide())
        except Exception:
            log.warning("couldn't hide the cursor", exc_info=True)
            self.hidden = False
        if self.hidden and self.call_later is not None:
            self._gen += 1
            # Where it is a moment from now, not now: the last move from the
            # host (smoothing, the Windows input helper) may still be landing.
            self._at = None
            self.call_later(WATCH_S, lambda gen=self._gen: self._watch(gen))

    def show(self) -> None:
        self._gen += 1
        if not self.hidden:
            return
        self.hidden = False
        try:
            self.backend.show()
        except Exception:
            log.warning("couldn't show the cursor", exc_info=True)

    def moved(self) -> None:
        """This program moved the cursor while hidden: where it is now isn't
        someone using the mouse here."""
        if self.hidden:
            self._at = self.position()

    def _watch(self, gen: int) -> None:
        if gen != self._gen or not self.hidden:
            return
        now = self.position()
        if now is not None and self._at is not None and (
                abs(now[0] - self._at[0]) > WATCH_SLOP_PX or abs(now[1] - self._at[1]) > WATCH_SLOP_PX):
            log.info("this computer's own mouse moved: showing the cursor")
            self.show()
            return
        if self._at is None:
            self._at = now
        if self.call_later is not None:
            self.call_later(WATCH_S, lambda: self._watch(gen))
