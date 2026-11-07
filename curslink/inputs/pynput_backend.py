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

import ctypes
import logging
import sys
import threading
import time

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
    (``CGAssociateMouseAndMouseCursorPosition``), the hidden cursor is warped back to the park
    point after every report, and the listeners stay running the whole time,
    switching suppression on and off in their event tap instead of being torn
    down and rebuilt at every switch.

    The first reports after grabbing can carry the parking jump in their
    deltas; see ``_mac_motion``.
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
        # How long each grabbed mouse report took in our tap (s). On macOS
        # the tap holds up every mouse event on the system, so it must be quick.
        self.tap_times: list[float] = []
        self._grabbed_at = 0.0
        self._park_lock = threading.Lock()
        # While set, input this process injects reaches the OS although
        # input is grabbed (see let_through).
        self._pass_injected = False

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
            _mac_cursor_visible(True)

    def set_grab(self, grabbed: bool) -> None:
        if grabbed == self._grabbed:
            return
        if grabbed:
            self._grabbed_at = time.monotonic()
        with self._park_lock:  # no tap warps to the park point once this says "not grabbed"
            self._grabbed = grabbed
        if self._mac:
            # The listeners keep running; the event taps read _grabbed.
            # Detaching the cursor only works for the frontmost app, which
            # this background service never is, so the cursor is also held
            # at the park point (a warp: no event, so nothing here hovers)
            # and hidden while input is on another computer.
            _mac_attach_cursor(not grabbed)
            if grabbed:
                _mac_warp(*self.park_at)
            _mac_cursor_visible(not grabbed)
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
    def let_through(self, inject) -> None:
        """Run ``inject()``, which injects mouse input this computer's OS must
        act on even though input is grabbed for another device (ending a
        file drag that went to another computer: see curslink.files.handoff).
        Blocking: call it off the event loop."""
        if not self._grabbed:
            inject()
            return
        if self._mac:
            self._pass_injected = True  # the event tap passes injected events now
            try:
                inject()
                time.sleep(0.05)
            finally:
                self._pass_injected = False
            return
        listener = self._mouse_listener
        if sys.platform == "win32" and listener is not None and hasattr(listener, "_suppress"):
            # The hook suppresses whatever it sees while this is set; for a
            # moment, it lets everything through.
            listener._suppress = False
            try:
                inject()
                time.sleep(0.05)
            finally:
                listener._suppress = True
                if self._controller is not None:
                    self._controller.position = self.park_at
            return
        # X11: the grab belongs to the listeners, so drop it for a moment.
        with self._lock:
            mouse = self._mouse_listener
        if mouse is not None:
            mouse.stop()
        try:
            inject()
            time.sleep(0.05)
        finally:
            self._restart()
            if self._controller is not None:
                self._controller.position = self.park_at

    def inject_drag_end(self, kind: str) -> None:
        """At the parked pointer, nudge a held left-button drag a pixel and
        back ("nudge"), and then let go of it ("release")."""
        x, y = self.park_at
        self.let_through(lambda: drag_nudge(x, y, release=kind == "release"))

    def _mac_mouse_tap(self, event_type, event):
        if not self._grabbed:
            return event
        if self._pass_injected and _mac_injected(event):
            return event
        if event_type in _MAC_MOVES and self._handler is not None and not _mac_injected(event):
            started = time.perf_counter()
            dx, dy = self._mac_motion(event)
            if dx or dy:
                self._handler.on_motion(dx, dy)
            # This very report may be the one that sends input home: by now
            # the router may have let go and put the cursor on the Mac's edge,
            # so don't drag it back to the park point.
            with self._park_lock:
                if self._grabbed:
                    _mac_warp(*self.park_at)
            if len(self.tap_times) < 20000:
                self.tap_times.append(time.perf_counter() - started)
        return None  # suppressed: this Mac doesn't see it

    def _mac_motion(self, event) -> tuple[float, float]:
        """How far this report moved the mouse: the event's own delta fields,
        which keep counting when the hidden cursor is up against the edge of
        a Mac display (0.4.11 read where the cursor went instead, so the
        pointer on the other computer stopped wherever the Mac's displays
        ended).

        Just after input leaves the Mac, though, macOS hands over a report
        or two whose delta is really the cursor being parked: hundreds of
        pixels straight back towards the edge just crossed, which used to
        bounce input home. Reports that big, that soon, are dropped.
        """
        dx, dy = _mac_delta(event)
        if time.monotonic() - self._grabbed_at < _MAC_SETTLE_S and max(abs(dx), abs(dy)) > _MAC_JUMP_PX:
            return 0, 0
        return dx, dy

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
# Further from the park point than one report could move it: the report was
# made before the cursor was parked there.
# For this long after grabbing, a single report moving further than
# _MAC_JUMP_PX is the cursor being parked, not the mouse.
_MAC_SETTLE_S = 0.15
_MAC_JUMP_PX = 150


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


class _CGPoint(ctypes.Structure):
    _fields_ = [("x", ctypes.c_double), ("y", ctypes.c_double)]


_CG_PATH = "/System/Library/Frameworks/ApplicationServices.framework/ApplicationServices"
_CF_PATH = "/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation"
_mac_libs: dict = {}
_mac_hidden = False


def _mac_lib(path: str):
    if path not in _mac_libs:
        _mac_libs[path] = ctypes.CDLL(path)
    return _mac_libs[path]


def _mac_warp(x: float, y: float) -> None:
    """Put the cursor at (x, y) without posting an event (no hover, nothing
    for the tap to see)."""
    try:
        cg = _mac_lib(_CG_PATH)
        cg.CGWarpMouseCursorPosition.argtypes = [_CGPoint]
        cg.CGWarpMouseCursorPosition(_CGPoint(float(x), float(y)))
    except Exception:  # pragma: no cover - macOS only
        log.debug("couldn't warp the cursor", exc_info=True)


def _mac_cursor_visible(visible: bool) -> None:
    """Hide or show the Mac's cursor. A background process may only do that
    after setting "SetsCursorInBackground" on its window server connection
    (what Synergy and Barrier do)."""
    global _mac_hidden
    if visible != _mac_hidden:
        return  # already that way (hiding counts, so never hide twice)
    try:
        cg, cf = _mac_lib(_CG_PATH), _mac_lib(_CF_PATH)
        if not visible:
            cg._CGSDefaultConnection.restype = ctypes.c_int
            cid = cg._CGSDefaultConnection()
            cf.CFStringCreateWithCString.restype = ctypes.c_void_p
            cf.CFStringCreateWithCString.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_uint32]
            key = cf.CFStringCreateWithCString(None, b"SetsCursorInBackground", 0x08000100)
            true = ctypes.c_void_p.in_dll(cf, "kCFBooleanTrue")
            cg.CGSSetConnectionProperty.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p]
            cg.CGSSetConnectionProperty(cid, cid, key, true)
            cg.CGDisplayHideCursor(0)
        else:
            cg.CGDisplayShowCursor(0)
        _mac_hidden = not visible
    except Exception:  # pragma: no cover - macOS only
        log.debug("couldn't %s the cursor", "show" if visible else "hide", exc_info=True)


def _mac_no_event_suppression() -> None:
    """By default macOS ignores the real mouse for 0.25 s after a program
    moves the cursor. Warping it is how it stays parked and how input comes
    home, so don't."""
    try:
        cg = _mac_lib(_CG_PATH)
        cg.CGSetLocalEventsSuppressionInterval.argtypes = [ctypes.c_double]
        cg.CGSetLocalEventsSuppressionInterval(0.0)
    except Exception:  # pragma: no cover - macOS only
        log.debug("couldn't turn off local event suppression (C)", exc_info=True)
    try:
        import Quartz

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
        if sys.platform == "win32" and _win_move(x, y):
            return
        if sys.platform == "darwin":
            # Input coming home: put the cursor where it crossed the edge by
            # warping it (what Synergy does), then tie it back to the mouse,
            # which also cancels macOS's short freeze after a warp. A posted
            # mouse-moved event could lose to reports still in flight from
            # the parked position and leave the cursor mid-screen.
            _mac_warp(x, y)
            _mac_attach_cursor(True)
            return
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


def drag_nudge(x: int, y: int, release: bool = False) -> None:
    """Move a held left-button drag at (x, y) a pixel and back, so the OS's
    drag-and-drop notices what's under it now; then let go if ``release``."""
    if sys.platform == "darwin":
        try:
            import Quartz

            for px in (x + 1, x):
                ev = Quartz.CGEventCreateMouseEvent(None, Quartz.kCGEventLeftMouseDragged, (px, y),
                                                    Quartz.kCGMouseButtonLeft)
                Quartz.CGEventPost(Quartz.kCGHIDEventTap, ev)
                time.sleep(0.03)
            if release:
                ev = Quartz.CGEventCreateMouseEvent(None, Quartz.kCGEventLeftMouseUp, (x, y),
                                                    Quartz.kCGMouseButtonLeft)
                Quartz.CGEventPost(Quartz.kCGHIDEventTap, ev)
        except Exception:  # pragma: no cover - macOS only
            log.warning("couldn't end the drag on this Mac", exc_info=True)
        return
    if sys.platform == "win32":
        try:
            user32 = ctypes.windll.user32
            for dx in (1, -1):
                inp = _INPUT(type=0)
                inp.mi = _MOUSEINPUT(dx, 0, 0, _MOUSEEVENTF_MOVE, 0, 0)
                user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(_INPUT))
                time.sleep(0.03)
            if release:
                inp = _INPUT(type=0)
                inp.mi = _MOUSEINPUT(0, 0, 0, _MOUSEEVENTF_LEFTUP, 0, 0)
                user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(_INPUT))
        except Exception:  # pragma: no cover - Windows only
            log.warning("couldn't end the drag on this PC", exc_info=True)
        return
    from pynput import mouse

    m = mouse.Controller()
    m.position = (x + 1, y)
    time.sleep(0.03)
    m.position = (x, y)
    if release:
        time.sleep(0.03)
        m.release(mouse.Button.left)


# ---------------------------------------------------------------- Windows bits
# pynput moves the pointer with SetCursorPos, which jumps it: apps under it
# get no mouse input, so nothing hovers, and some only catch up later. A real
# mouse arrives through SendInput, so move it that way: absolute, over the
# whole virtual desktop.

class _MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", ctypes.c_int32), ("dy", ctypes.c_int32), ("mouseData", ctypes.c_uint32),
                ("dwFlags", ctypes.c_uint32), ("time", ctypes.c_uint32), ("dwExtraInfo", ctypes.c_size_t)]


class _INPUT(ctypes.Structure):
    class _U(ctypes.Union):
        # The union is as big as its largest member (KEYBDINPUT/HARDWAREINPUT are smaller).
        _fields_ = [("mi", _MOUSEINPUT)]

    _anonymous_ = ("u",)
    _fields_ = [("type", ctypes.c_uint32), ("u", _U)]


_MOUSEEVENTF_MOVE, _MOUSEEVENTF_ABSOLUTE, _MOUSEEVENTF_VIRTUALDESK = 0x0001, 0x8000, 0x4000
_MOUSEEVENTF_LEFTUP = 0x0004
def win_absolute(x: float, y: float, vx: int, vy: int, vw: int, vh: int) -> tuple[int, int]:
    """A virtual-desktop pixel -> SendInput's 0..65535 absolute coordinates."""
    nx = round((x - vx) * 65535 / max(1, vw - 1))
    ny = round((y - vy) * 65535 / max(1, vh - 1))
    return min(max(nx, 0), 65535), min(max(ny, 0), 65535)


def _win_move(x: int, y: int) -> bool:
    # Deliberately not DPI aware: the layout gives a Windows screen in the
    # coordinates Windows shows apps that aren't (logical pixels, e.g. a 4K
    # screen at 200% is 1920x1080), which is also what SetCursorPos used
    # before 0.4.9. Absolute SendInput coordinates are a fraction of the
    # virtual desktop, so a logical position over the logical desktop lands
    # in the right physical place. 0.4.9-0.4.11 made the process DPI aware,
    # which read the same numbers as physical pixels: the pointer was boxed
    # into the top-left part of a scaled screen and moved too slowly.
    try:
        user32 = ctypes.windll.user32
        vx, vy = user32.GetSystemMetrics(76), user32.GetSystemMetrics(77)
        vw, vh = user32.GetSystemMetrics(78), user32.GetSystemMetrics(79)
        nx, ny = win_absolute(x, y, vx, vy, vw, vh)
        inp = _INPUT(type=0)
        inp.mi = _MOUSEINPUT(nx, ny, 0, _MOUSEEVENTF_MOVE | _MOUSEEVENTF_ABSOLUTE | _MOUSEEVENTF_VIRTUALDESK, 0, 0)
        if user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(_INPUT)) == 1:
            return True
    except Exception:
        log.debug("SendInput move failed; falling back to SetCursorPos", exc_info=True)
    return False
