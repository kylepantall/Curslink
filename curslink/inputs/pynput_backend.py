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

    Windows and X11: relative motion while grabbed is measured by parking the
    real cursor at a fixed point and reading how far each event tried to move
    it (the same trick Synergy/Barrier use), then warping it back.

    macOS does that badly: every warp makes the OS ignore the real mouse for a
    quarter of a second (the "local events suppression interval"), which is
    what made the pointer on the other computer skip and stick. So on macOS
    the cursor is detached from the mouse while grabbed
    (``CGAssociateMouseAndMouseCursorPosition``) and the deltas are read
    straight from each event, and the listeners stay running the whole time,
    switching suppression on and off in their event tap instead of being torn
    down and rebuilt at every switch.
    """

    def __init__(self, park_at: tuple[int, int] = (400, 400)):
        self.park_at = park_at
        self._handler: CaptureHandler | None = None
        self._grabbed = False
        self._lock = threading.Lock()
        self._mouse_listener = None
        self._key_listener = None
        self._controller = None
        self._mac = sys.platform == "darwin"

    def start(self, handler: CaptureHandler) -> None:
        from pynput import mouse

        self._handler = handler
        self._controller = mouse.Controller()
        if self._mac:
            _mac_no_event_suppression()
        self._restart()

    def stop(self) -> None:
        with self._lock:
            for listener in (self._mouse_listener, self._key_listener):
                if listener is not None:
                    listener.stop()
            self._mouse_listener = self._key_listener = None
        if self._mac and self._grabbed:
            _mac_attach_cursor(True)

    def set_grab(self, grabbed: bool) -> None:
        if grabbed == self._grabbed:
            return
        self._grabbed = grabbed
        if self._mac:
            # The listeners keep running; the event taps read _grabbed.
            _mac_attach_cursor(not grabbed)
            return
        if grabbed and self._controller is not None:
            self._controller.position = self.park_at
        self._restart()

    def _restart(self) -> None:
        from pynput import keyboard, mouse

        _prime_macos()
        self.stop()
        with self._lock:
            if self._mac:
                self._mouse_listener = _mac_listener(mouse.Listener)(
                    on_move=self._on_move,
                    on_click=self._on_click,
                    on_scroll=self._on_scroll,
                    darwin_intercept=self._mac_mouse_tap,
                )
                self._key_listener = _mac_listener(keyboard.Listener)(
                    on_press=lambda k, injected=False: self._on_key(k, True, injected),
                    on_release=lambda k, injected=False: self._on_key(k, False, injected),
                    darwin_intercept=self._mac_key_tap,
                )
            else:
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

    # -- macOS event taps (run after pynput has called the callbacks) --------
    def _mac_mouse_tap(self, event_type, event):
        if not self._grabbed:
            return event
        if event_type in _MAC_MOVES and self._handler is not None and not _mac_injected(event):
            dx, dy = _mac_delta(event)
            if dx or dy:
                self._handler.on_motion(dx, dy)
        return None  # suppressed: this Mac doesn't see it

    def _mac_key_tap(self, event_type, event):
        return event if not self._grabbed else None

    # -- callbacks -------------------------------------------------------------
    def _on_move(self, x, y, injected=False):
        if injected or self._handler is None:
            return
        if not self._grabbed:
            self._handler.on_pointer(x, y)
            return
        if self._mac:
            return  # the event tap reads the deltas
        px, py = self.park_at
        dx, dy = x - px, y - py
        if dx or dy:
            self._handler.on_motion(dx, dy)
            # Not needed on Windows (suppression already freezes the cursor),
            # required on X11.
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


# ------------------------------------------------------------------ macOS bits
# CGEventType values: mouse moved and the three drags; the two tap-disabled notices.
_MAC_MOVES = frozenset({5, 6, 7, 27})
_MAC_TAP_DISABLED = frozenset({0xFFFFFFFE, 0xFFFFFFFF})
_MAC_DELTA_X, _MAC_DELTA_Y = 4, 5          # kCGMouseEventDeltaX / DeltaY
_MAC_SOURCE_PID = 41                       # kCGEventSourceUnixProcessID


def _mac_listener(base):
    """``base`` (a pynput listener class), surviving macOS turning its event
    tap off. macOS does that when a tap is ever slow to answer, and then
    hands the tap a "disabled" notice with no real event in it; pynput reads
    that as a key or mouse event, fails, and its listener stops for good:
    from then on nothing is captured or suppressed, and the pointer sticks.
    Here the notice turns the tap straight back on instead."""

    class Listener(base):
        _curslink_tap = None

        def _create_event_tap(self):
            self._curslink_tap = super()._create_event_tap()
            return self._curslink_tap

        def _handler(self, proxy, event_type, event, refcon):
            if event_type in _MAC_TAP_DISABLED:
                _mac_reenable(self._curslink_tap)
                return event
            return super()._handler(proxy, event_type, event, refcon)

    return Listener


def _mac_reenable(tap) -> None:
    if tap is None:
        return
    try:
        import Quartz

        Quartz.CGEventTapEnable(tap, True)
        log.info("macOS turned the input tap off; turned it back on")
    except Exception:  # pragma: no cover - macOS only
        log.warning("couldn't turn the input tap back on", exc_info=True)


def _mac_delta(event) -> tuple[int, int]:
    import Quartz

    return (Quartz.CGEventGetIntegerValueField(event, _MAC_DELTA_X),
            Quartz.CGEventGetIntegerValueField(event, _MAC_DELTA_Y))


def _mac_injected(event) -> bool:
    import Quartz

    return Quartz.CGEventGetIntegerValueField(event, _MAC_SOURCE_PID) != 0


def _mac_attach_cursor(attached: bool) -> None:
    """Detach the on-screen cursor from the mouse (it stays put while the
    mouse still reports movement) or join them again."""
    try:
        import Quartz

        Quartz.CGAssociateMouseAndMouseCursorPosition(attached)
    except Exception:  # pragma: no cover - macOS only
        log.warning("couldn't %s the cursor", "reattach" if attached else "detach", exc_info=True)


def _mac_no_event_suppression() -> None:
    """By default macOS ignores the real mouse for 0.25 s after a program
    moves the cursor. Moving it is how input comes home, so don't."""
    try:
        import Quartz

        fn = getattr(Quartz, "CGSetLocalEventsSuppressionInterval", None)
        if fn is not None:
            fn(0.0)
        source = Quartz.CGEventSourceCreate(Quartz.kCGEventSourceStateCombinedSessionState)
        if source is not None:
            Quartz.CGEventSourceSetLocalEventsSuppressionInterval(source, 0.0)
    except Exception:  # pragma: no cover - macOS only
        log.debug("couldn't turn off local event suppression", exc_info=True)


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
