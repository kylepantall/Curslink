"""Windows / macOS / Linux (X11) input capture and injection via pynput.

Notes per platform:

* macOS: grant Accessibility *and* Input Monitoring to your terminal/Python.
* Linux: X11 sessions work out of the box. Wayland does not allow global
  capture or injection; use an X11 session for now (a uinput/evdev backend
  is a planned follow-up).
* Windows: run the host from a non-elevated prompt unless the apps you want
  to control are elevated too (UIPI blocks injection into elevated windows).
"""

from __future__ import annotations

import logging
import sys
import threading

from curslink.inputs.base import CaptureHandler
from curslink.inputs.keys import normalize_char

log = logging.getLogger(__name__)

_BUTTON_NAMES = {"left", "right", "middle", "x1", "x2"}
_primed = threading.Lock()


def _prime_macos() -> None:
    """Load the pyobjc names pynput's listener threads look up, once, here.
    Two listeners starting together race pyobjc's lazy loader, and the loser
    dies with KeyError: 'AXIsProcessTrusted'."""
    if sys.platform != "darwin":
        return
    with _primed:
        try:
            import HIServices
            import Quartz

            HIServices.AXIsProcessTrusted  # noqa: B018
            Quartz.CGEventTapCreate  # noqa: B018
        except Exception:
            log.debug("couldn't preload pyobjc names", exc_info=True)


def key_to_name(key) -> str | None:
    from pynput import keyboard

    if isinstance(key, keyboard.Key):
        return key.name
    if isinstance(key, keyboard.KeyCode):
        return normalize_char(key.char, key.vk)
    return None


def name_to_key(name: str):
    from pynput import keyboard

    if len(name) == 1:
        # On Windows, a char without a virtual-key code is typed as unicode,
        # which shortcuts (Ctrl+C) ignore. Use the vk for letters and digits.
        if sys.platform == "win32" and name.isascii() and name.isalnum():
            return keyboard.KeyCode.from_vk(ord(name.upper()))
        return keyboard.KeyCode.from_char(name)
    if name.startswith("vk:"):
        return keyboard.KeyCode.from_vk(int(name[3:]))
    try:
        return keyboard.Key[name]
    except KeyError:
        return None


class PynputCapture:
    """Watches local input; when grabbed, suppresses it and reports deltas.

    Relative motion while grabbed is measured by parking the real cursor at
    a fixed point and reading how far each event tried to move it (the same
    trick Synergy/Barrier use), then warping it back.
    """

    def __init__(self, park_at: tuple[int, int] = (400, 400)):
        self.park_at = park_at
        self._handler: CaptureHandler | None = None
        self._grabbed = False
        self._lock = threading.Lock()
        self._mouse_listener = None
        self._key_listener = None
        self._controller = None

    def start(self, handler: CaptureHandler) -> None:
        from pynput import mouse

        self._handler = handler
        self._controller = mouse.Controller()
        self._restart()

    def stop(self) -> None:
        with self._lock:
            for listener in (self._mouse_listener, self._key_listener):
                if listener is not None:
                    listener.stop()
            self._mouse_listener = self._key_listener = None

    def set_grab(self, grabbed: bool) -> None:
        if grabbed == self._grabbed:
            return
        self._grabbed = grabbed
        if grabbed and self._controller is not None:
            self._controller.position = self.park_at
        self._restart()

    def _restart(self) -> None:
        from pynput import keyboard, mouse

        _prime_macos()
        self.stop()
        with self._lock:
            suppress = self._grabbed
            self._mouse_listener = mouse.Listener(
                on_move=self._on_move,
                on_click=self._on_click,
                on_scroll=self._on_scroll,
                suppress=suppress,
            )
            self._key_listener = keyboard.Listener(
                on_press=lambda k, injected=False: self._on_key(k, True, injected),
                on_release=lambda k, injected=False: self._on_key(k, False, injected),
                suppress=suppress,
            )
            self._mouse_listener.start()
            self._key_listener.start()

    def _on_move(self, x, y, injected=False):
        if injected or self._handler is None:
            return
        if not self._grabbed:
            self._handler.on_pointer(x, y)
            return
        px, py = self.park_at
        dx, dy = x - px, y - py
        if dx or dy:
            self._handler.on_motion(dx, dy)
            # Not needed on Windows (suppression already freezes the cursor),
            # harmless there, required on macOS/X11.
            if sys.platform != "win32" and self._controller is not None:
                self._controller.position = self.park_at

    def _on_click(self, x, y, button, pressed, injected=False):
        if injected or self._handler is None:
            return
        name = getattr(button, "name", None)
        if name == "button8":
            name = "x1"
        elif name == "button9":
            name = "x2"
        if name in _BUTTON_NAMES:
            self._handler.on_button(name, pressed)

    def _on_scroll(self, x, y, dx, dy, injected=False):
        if not injected and self._handler is not None:
            self._handler.on_scroll(dx, dy)

    def _on_key(self, key, pressed, injected):
        if injected or self._handler is None:
            return
        name = key_to_name(key)
        if name:
            self._handler.on_key(name, pressed)


class PynputSink:
    """Injects input into this machine's OS."""

    def __init__(self, modifier_map: dict[str, str] | None = None):
        from pynput import keyboard, mouse

        self._mouse = mouse.Controller()
        self._keyboard = keyboard.Controller()
        self.modifier_map = modifier_map or {}

    def move_to(self, x: int, y: int) -> None:
        self._mouse.position = (x, y)

    def button(self, button: str, pressed: bool) -> None:
        from pynput import mouse

        names = {"x1": "button8", "x2": "button9"} if sys.platform.startswith("linux") else {}
        target = getattr(mouse.Button, names.get(button, button), None)
        if target is None:
            log.debug("button %s not supported on this platform", button)
            return
        (self._mouse.press if pressed else self._mouse.release)(target)

    def scroll(self, dx: float, dy: float) -> None:
        self._mouse.scroll(dx, dy)

    def key(self, key: str, pressed: bool) -> None:
        target = name_to_key(self.modifier_map.get(key, key))
        if target is None:
            log.debug("key %s not supported on this platform", key)
            return
        (self._keyboard.press if pressed else self._keyboard.release)(target)
