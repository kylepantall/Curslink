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
import os
import sys
import threading
import time
import types
from typing import Callable

from curslink.inputs.base import CaptureHandler
from curslink.inputs.keys import MODIFIER_FAMILIES, family, mac_pc_name, normalize_char

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

    def __init__(self, park_at: tuple[int, int] = (400, 400), hide_cursor: bool = True):
        self.park_at = park_at
        # Windows and X11: the parked cursor is hidden while grabbed (macOS
        # always hides it, below). See curslink.cursorhide.
        self._hider = None
        if hide_cursor and sys.platform != "darwin":
            from curslink.cursorhide import CursorHider

            self._hider = CursorHider()
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
        # macOS: while this says so, a real left button-up is held back (a file
        # drop waiting for its files, see curslink.files.native) until
        # release_held(). Called from the event tap: must be quick.
        self.hold_left_up: Callable[[], bool] = lambda: False
        self._held_up = False
        self._held_at = 0.0
        # Safety nets that need nothing but the event tap's own thread, so
        # they work when everything else is stuck (0.4.39: a file read
        # waiting on a macOS permission prompt held up the loop that
        # forwards input, so the Mac's mouse and keyboard went nowhere and
        # the prompt couldn't be clicked):
        # - loop_alive() false while input is grabbed: give it back here;
        # - Escape three times quickly: give it back here, always;
        # - a held button-up goes after HOLD_MAX_S, or at the next click.
        # took_back is set when one of them fires, for the router to catch up.
        self.loop_alive: Callable[[], bool] = lambda: True
        # Windows: Ctrl+Alt+Delete or Task Manager in use (curslink.escapehatch).
        from curslink.escapehatch import escape_wanted

        self.escape_wanted: Callable[[], str | None] = escape_wanted
        self._pc_mods: set[str] = set()  # ctrl/shift held, as this process's hooks heard them
        self.took_back = False
        self.hooks_lost = 0  # Windows: times the hooks were found gone (_watch_hooks)
        self._hook_heard_at = 0.0  # when the mouse hook (or the input helper) last reported a move
        self._own_heard_at = 0.0   # ... this process's own hook
        # Windows: the input helper's hooks (curslink.winhelper.client.HelperWatch),
        # which hear the mouse and keyboard over apps that run as administrator.
        self._helper = None
        self._restart_lock = threading.RLock()
        self._ungrabbed_at = 0.0
        self._deaf_said: dict[str, float] = {}
        self._closed = False
        self._escapes: list[float] = []
        # The take-back thread (_take_back_soon), started with the hooks.
        self._take_back_wake = threading.Event()
        self._take_back_why = ""
        # macOS: what keys typed here mean to a PC, where that differs (Option+D
        # types "∂" but is Alt+D to a PC; the Spotlight key is F4): the host's
        # key mapping reads it for keys going to Windows, Linux or Android.
        self.pc_names: dict[str, str] = {}
        self._held: set[str] = set()       # modifier families held down
        self._mac_down: dict[int, str] = {}  # keycode -> name it was pressed as
        # macOS: its own shortcuts (Cmd+Tab, Spaces, screenshots...) go to the
        # other computer too while input is there (see macshortcuts).
        self._shortcuts = None
        if self._mac:
            from curslink.inputs.macshortcuts import SystemShortcuts

            self._shortcuts = SystemShortcuts()

    def start(self, handler: CaptureHandler) -> None:
        from pynput import mouse

        self._handler = handler
        self._controller = mouse.Controller()
        threading.Thread(target=self._take_back_worker, name="curslink-take-back", daemon=True).start()
        if self._mac:
            _mac_no_event_suppression()
        if self._shortcuts is not None:
            self._shortcuts.restore_left_over()
        self._restart()
        if sys.platform == "win32":
            threading.Thread(target=self._watch_hooks, name="curslink-hooks", daemon=True).start()
            self._start_helper()

    def stop(self) -> None:
        with self._lock:
            for listener in (self._mouse_listener, self._key_listener):
                if listener is not None:
                    listener.stop()
            self._mouse_listener = self._key_listener = None
        if self._mac and self._grabbed:
            _mac_attach_cursor(True)
            _mac_cursor_visible(True)

    def close(self) -> None:
        """Stop for good: the cursor shows again, and macOS's shortcuts work."""
        self._closed = True
        self._take_back_wake.set()
        if self._helper is not None:
            self._helper.close()
        self.stop()
        if self._shortcuts is not None:
            self._shortcuts.set_off(False)
            self._shortcuts.wait(1.0)
        if self._hider is not None:
            self._hider.show()

    def set_grab(self, grabbed: bool, wait: float | None = None) -> None:
        """``wait``: as for _restart (the safety nets pass one)."""
        if grabbed == self._grabbed:
            return
        if grabbed:
            self._grabbed_at = time.monotonic()
        else:
            self._ungrabbed_at = time.monotonic()
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
            if self._shortcuts is not None:
                self._shortcuts.set_off(grabbed)
            return
        if self._helper is not None:
            self._helper.set_mode("grab" if grabbed else "moves")
        if grabbed and self._controller is not None:
            self._controller.position = self.park_at
        if self._hider is not None:
            (self._hider.hide if grabbed else self._hider.show)()
        if self._suppress_live(grabbed):
            return
        self._restart(wait)

    def set_cursor_hidden(self, hidden: bool) -> None:
        """While input is here (not grabbed): hide the cursor, or show it
        again. In a mirror of one of this computer's own apps, the pointer is
        drawn in the mirror on the other screen; this one's cursor, over the
        app's invisible window here, is only in the way."""
        if self._grabbed:
            return
        if self._mac:
            _mac_cursor_visible(not hidden)
        elif self._hider is not None:
            (self._hider.hide if hidden else self._hider.show)()

    def _suppress_live(self, on: bool) -> bool:
        """Windows: hold input back (or stop) with the hooks already in,
        rather than new ones. True if that did it.

        New hooks mean new threads, and a thread can't start while another
        holds Windows' loader lock: on Kyle's PC (0.4.99) the head tracker's
        ``import cv2`` sat in a DLL load for minutes, and the next switch
        to the Mac stuck in starting the new mouse hook. The old hooks
        were already out, the cursor already blank, and Escape had nothing
        left to hear it. pynput's Windows hooks read ``_suppress`` at every
        event (let_through uses it too), so flipping it is all a switch needs."""
        if sys.platform != "win32" or not self._restart_lock.acquire(timeout=RESTART_WAIT_S):
            return False
        try:
            with self._lock:
                listeners = [x for x in (self._mouse_listener, self._key_listener) if x is not None]
            if len(listeners) != 2 or not all(hasattr(x, "_suppress") and x.is_alive() and x.running
                                              for x in listeners):
                return False
            for x in listeners:
                x._suppress = on
            return True
        finally:
            self._restart_lock.release()

    def _restart(self, wait: float | None = None) -> bool:
        """New hooks. With ``wait``, give up (False) if another thread has
        been putting them back for that long: a safety net mustn't stick
        behind the very thing it's there for."""
        if not self._restart_lock.acquire(timeout=-1 if wait is None else wait):
            log.warning("input: the hooks are still being put back elsewhere; not waiting for that")
            return False
        try:  # the hooks' watchdog and the input helper restart them too
            self._restart_now()
        finally:
            self._restart_lock.release()
        return True

    def _restart_now(self) -> None:
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
                extra = {"win32_event_filter": self._win_mouse_filter} if sys.platform == "win32" else {}
                self._mouse_listener = mouse.Listener(
                    on_move=self._on_move,
                    on_click=self._on_click,
                    on_scroll=self._on_scroll,
                    suppress=suppress,
                    **extra,
                )
                keys_extra = {"win32_event_filter": _win_key_filter} if sys.platform == "win32" else {}
                self._key_listener = keyboard.Listener(
                    on_press=lambda k, injected=False: self._on_key(k, True, injected),
                    on_release=lambda k, injected=False: self._on_key(k, False, injected),
                    suppress=suppress,
                    **keys_extra,
                )
            self._mouse_listener.start()
            self._key_listener.start()
            if sys.platform == "win32" and not self._mac and suppress != self._grabbed:
                # Input came home (or left) while these were starting, which
                # can take a while (see _suppress_live): hold back what's
                # wanted now, not what was wanted then.
                for x in (self._mouse_listener, self._key_listener):
                    x._suppress = self._grabbed

    # -- macOS event taps (run after pynput has called the callbacks) --------
    def let_through(self, inject) -> None:
        """Run ``inject()``, which injects input this computer's OS must
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
        with self._lock:
            listeners = [x for x in (self._mouse_listener, self._key_listener) if x is not None]
        if sys.platform == "win32" and all(hasattr(x, "_suppress") for x in listeners):
            # The hooks suppress whatever they see while this is set; for a
            # moment, they let everything through.
            for x in listeners:
                x._suppress = False
            try:
                inject()
                time.sleep(0.05)
            finally:
                for x in listeners:
                    x._suppress = True
                if self._controller is not None:
                    self._controller.position = self.park_at
            return
        # X11: the grab belongs to the listeners, so drop it for a moment.
        for x in listeners:
            x.stop()
        try:
            inject()
            time.sleep(0.05)
        finally:
            self._restart()
            if self._controller is not None:
                self._controller.position = self.park_at

    def inject_drag_end(self, kind: str) -> None:
        """At the parked pointer, nudge a held left-button drag a pixel and
        back ("nudge"); or cancel it with Escape and let go of the button
        ("release"), so the app it came from drops nothing here; or just let
        go there ("let_go")."""
        x, y = self.park_at
        if kind == "let_go":  # just let go there: the drop lands at the parked pointer
            self.let_through(lambda: _drag_release(x, y))
            return
        self.let_through(lambda: drag_nudge(x, y, release=kind == "release"))

    def release_held(self) -> None:
        """Let go of a button-up held back by ``hold_left_up`` (now)."""
        if self._held_up:
            self._held_up = False
            if self._mac:
                x, y = mac_cursor()
            else:
                x, y = _win_cursor_pos() or (0, 0)  # (Windows lets go where the cursor is anyway)
            _drag_release(x, y)

    def take_back(self, why: str) -> None:
        """Give this computer its own mouse and keyboard back, from any
        thread, without the event loop (see loop_alive): with the cursor in
        the middle of this computer's main screen (the park point), showing,
        so it's easy to find (the router brings input home there too)."""
        log.warning("input: %s; giving the mouse and keyboard back to this computer", why)
        self.took_back = True
        # The cursor first: whatever below gets stuck, it's back.
        if self._hider is not None:
            self._hider.show()
        if sys.platform == "win32":
            from curslink.cursorhide import restore_windows

            restore_windows()
        if self._held_up:
            self.release_held()
        if self._grabbed:
            self.set_grab(False, wait=RESTART_WAIT_S)
        try:
            if self._mac:
                _mac_warp(*self.park_at)
                _mac_attach_cursor(True)
                _mac_cursor_visible(True)
            elif self._controller is not None:
                self._controller.position = self.park_at
            if self._hider is not None:
                self._hider.show()
            if sys.platform == "win32":
                from curslink.cursorhide import restore_windows

                restore_windows()
        except Exception:
            log.warning("input: couldn't put the cursor back in the middle", exc_info=True)

    def _held_at_now(self) -> tuple[float, float]:
        """Windows: where the cursor sits while grabbed, for the deltas.

        Suppressing the hooks' moves holds the cursor wherever it was, which
        isn't always quite the park point: Kyle's PC held it at (1442, 815)
        for a park point of (1440, 810), and taking deltas from the park
        point added (2, 5) to every mouse report. The pointer on the other
        computer crept right and down at every report, slid along the bottom
        and into a corner, and moving the mouse couldn't get it out. Asked
        from within the hook, before the move happens, the cursor's own
        position is exact (and stays right if the move does go through)."""
        if sys.platform == "win32":
            at = _win_cursor_pos()
            if at is not None:
                return at
        return self.park_at

    def _take_back_soon(self, why: str) -> None:
        """take_back() from a listener's callback: on the take-back thread
        (started up front), as it stops and restarts the very listener
        that's calling. Not a new thread each time: starting one can wait
        on Windows' loader lock, which is when this is needed most."""
        if self._grabbed or self._held_up:
            self._take_back_why = why
            self._take_back_wake.set()

    def _take_back_worker(self) -> None:
        while not self._closed:
            self._take_back_wake.wait()
            self._take_back_wake.clear()
            if self._closed:
                return
            if self._grabbed or self._held_up:
                try:
                    self.take_back(self._take_back_why)
                except Exception:
                    log.exception("input: couldn't give the mouse and keyboard back")

    def _watch_hooks(self) -> None:
        """Windows: while input is grabbed the hooks hold the cursor still
        at the park point. Windows quietly takes a low-level hook away when
        it answers too slowly (LowLevelHooksTimeout), and then the hidden
        cursor moves and the keys go to this PC's apps, while input still
        looks grabbed: the cursor was gone and Escape did nothing. If the
        cursor wanders off the park point, put the hooks back."""
        seen: tuple[float, tuple[float, float]] | None = None  # (grab it's for, where the cursor sat)
        lost: list[float] = []
        escape: tuple[float, str | None] | None = None  # (grab it's for, way out seen last)
        deaf = _DeafHook(self)
        while not self._closed:
            time.sleep(HOOK_CHECK_S)
            if not self._grabbed:
                deaf.check()
                continue
            deaf.reset()
            way_out = self.escape_wanted() if sys.platform == "win32" else None
            if escape is None or escape[0] != self._grabbed_at:
                escape = (self._grabbed_at, way_out)  # Task Manager already in front isn't a way out
            elif way_out and way_out != escape[1]:
                # Ctrl+Alt+Delete (no hook ever sees it) or Task Manager
                # coming up: Windows' own ways out free the cursor too.
                self.take_back(f"{way_out} on this PC")
                continue
            else:
                escape = (self._grabbed_at, way_out)
            if not self.loop_alive():
                # No hook callback needed: with the loop stuck mid-switch the
                # hooks may not be in at all (0.4.99), so nothing would hear
                # the mouse or Escape to notice.
                self.take_back("Curslink stopped forwarding input")
                continue
            if self._controller is None or time.monotonic() - self._grabbed_at < 0.5:
                continue
            try:
                x, y = self._controller.position
            except Exception:
                continue
            # Where the OS put the cursor for this grab (the park point, or
            # as near as the screen allows), read once it has settled.
            if seen is None or seen[0] != self._grabbed_at:
                px, py = self.park_at
                if abs(x - px) + abs(y - py) <= HOOK_DRIFT_PX:
                    seen = (self._grabbed_at, (x, y))
                    continue
                seen = (self._grabbed_at, (px, py))
            px, py = seen[1]
            if abs(x - px) + abs(y - py) <= HOOK_DRIFT_PX:
                continue
            self.hooks_lost += 1
            now = time.monotonic()
            lost = [t for t in lost if now - t < 10.0] + [now]
            log.warning("input: the cursor moved to (%d, %d) while held at (%d, %d): Windows dropped the "
                        "input hooks; putting them back (%d time(s))", x, y, px, py, self.hooks_lost)
            if not self._grabbed:
                continue
            if len(lost) >= HOOK_LOST_MAX:
                self.take_back("Windows kept dropping the input hooks")
                continue
            try:
                if self._restart(wait=RESTART_WAIT_S):
                    self._controller.position = self.park_at
                else:
                    self.take_back("the input hooks couldn't be put back")
            except Exception:
                log.exception("input: couldn't put the input hooks back")
                self.take_back("the input hooks couldn't be put back")

    def _win_mouse_filter(self, msg, data):
        """Windows: the mouse hook's first look at every event (on the hook's
        thread, so quick). A real left button-up is held back while a file
        drop waits for its files (``hold_left_up``, as the Mac's event tap
        does), and let go by ``release_held()``: reported as let go, kept
        from Windows, so the drag it ends carries on until then."""
        if msg not in (_WM_LBUTTONDOWN, _WM_LBUTTONUP) or (getattr(data, "flags", 0) & _LLMHF_INJECTED_ANY):
            return True
        if self._held_up and (msg == _WM_LBUTTONDOWN or time.monotonic() - self._held_at > HOLD_MAX_S):
            log.warning("drop: letting go of the held button (%s)",
                        "clicked again" if msg == _WM_LBUTTONDOWN else "waited too long")
            self.release_held()
        if msg == _WM_LBUTTONUP and not self._grabbed and self.hold_left_up():
            self._held_up = True
            self._held_at = time.monotonic()
            if self._handler is not None:
                self._handler.on_button("left", False)  # the drop: suppressed events skip the callbacks
            listener = self._mouse_listener
            if listener is not None:
                listener.suppress_event()  # raises: pynput's hook swallows the event
        return True

    def _escape_pressed(self) -> bool:
        """Escape three times within ESCAPE_WINDOW_S?"""
        now = time.monotonic()
        self._escapes = [t for t in self._escapes if now - t < ESCAPE_WINDOW_S] + [now]
        if len(self._escapes) >= 3:
            self._escapes = []
            return True
        return False

    def _mac_mouse_tap(self, event_type, event):
        if self._held_up and not _mac_injected(event) and (
                event_type == _MAC_LEFT_DOWN or time.monotonic() - self._held_at > HOLD_MAX_S):
            log.warning("drop: letting go of the held button (%s)",
                        "clicked again" if event_type == _MAC_LEFT_DOWN else "waited too long")
            self.release_held()
        if self._grabbed and not self.loop_alive():
            self.take_back("Curslink stopped forwarding input")
            return event
        if not self._grabbed:
            if event_type == _MAC_LEFT_UP and not _mac_injected(event) and self.hold_left_up():
                self._held_up = True
                self._held_at = time.monotonic()
                return None  # held back until the dropped files have arrived
            return event
        if self._pass_injected and _mac_injected(event):
            return event
        if event_type in _MAC_OTHER_BUTTONS and self._handler is not None and not _mac_ours(event):
            # pynput calls every button past left and right "middle"; the
            # button number says which (back and forward are 3 and 4).
            name = _MAC_BUTTON_NUMBERS.get(_mac_field(event, _MAC_BUTTON_NUMBER))
            if name:
                self._handler.on_button(name, event_type == _MAC_OTHER_DOWN)
            return None
        if event_type == _MAC_SCROLL and self._handler is not None and not _mac_ours(event):
            # pynput reports whole lines only, so a smooth wheel, a Magic
            # Mouse or a trackpad mostly scrolled 0: send the fractional lines.
            dx, dy = _mac_scroll(event)
            if dx or dy:
                self._handler.on_scroll(dx, dy)
            return None
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
        if (event_type == _MAC_KEY_DOWN and not _mac_injected(event)
                and _mac_field(event, _MAC_KEYCODE) == _MAC_ESCAPE and self._escape_pressed()):
            if self._grabbed or self._held_up:
                self.take_back("Escape pressed three times")
            return event
        if self._grabbed and not self.loop_alive():
            self.take_back("Curslink stopped forwarding input")
            return event
        if not self._grabbed or (self._pass_injected and _mac_injected(event)):
            return event
        return None

    # -- callbacks -------------------------------------------------------------
    def _on_move(self, x, y, injected=False):
        now = time.monotonic()
        self._hook_heard_at = now
        if not injected:
            self._own_heard_at = now
        self._moved(x, y, injected)

    # -- Windows: the input helper's hooks (wire v2) ---------------------------
    def _start_helper(self) -> None:
        try:
            from curslink.winhelper import win32 as helper_win32
            from curslink.winhelper.client import HelperWatch

            self._helper = HelperWatch(helper_win32.this_session(), self._helper_event, self._helper_listening)
            self._helper.mode = "grab" if self._grabbed else "moves"
            self._helper.start()
        except Exception:
            log.warning("input: couldn't look for the input helper", exc_info=True)
            self._helper = None

    def _helper_listening(self) -> None:
        """The helper's hooks are in: put ours back in front of them, so the
        helper only gets what ours don't take (and holds back nothing ours do)."""
        if not self._closed:
            self._restart(wait=RESTART_WAIT_S)

    def _helper_event(self, command: tuple) -> None:
        """Something the input helper's hooks heard (curslink.winhelper.watch).
        While input is on another computer, it's only what this process's
        hooks didn't take: they hold input back themselves, so it never gets
        that far. While input is here, the helper reports every move, and
        only the ones this process's own hook didn't hear count."""
        op, a, b, grabbed = command
        if grabbed != self._grabbed or self._handler is None:
            return  # seen before a switch: not what it means now
        now = time.monotonic()
        if op == "pm":
            if not grabbed and now - self._own_heard_at < HELPER_FILL_S:
                return  # our own hook hears the mouse: that's the one that counts
            self._hook_heard_at = now
            self._moved(a, b)
        elif op == "pb":
            self._on_click(0, 0, types.SimpleNamespace(name=a), b)
        elif op == "ps":
            self._on_scroll(0, 0, a, b)
        elif op == "pk":
            key = self._key_for_vk(a, b)
            if key is not None:
                self._on_key(key, b, False)

    def _key_for_vk(self, vk: int, pressed: bool):
        """The pynput key our own keyboard hook would have made of ``vk``."""
        listener = self._key_listener
        try:
            if listener is not None and hasattr(listener, "_event_to_key"):
                return listener._event_to_key(0x100 if pressed else 0x101, vk)
            from pynput import keyboard

            return keyboard.KeyCode.from_vk(vk)
        except Exception:
            log.debug("no key for vk %s", vk, exc_info=True)
            return None

    def _moved(self, x, y, injected=False):
        if injected or self._handler is None:
            return
        if not self._grabbed:
            self._handler.on_pointer(x, y)
            return
        if self._mac:
            return  # the event tap reads the deltas
        if not self.loop_alive():
            self._take_back_soon("Curslink stopped forwarding input")
            return
        px, py = self._held_at_now()
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
        if self._mac and name == "middle":
            return  # every "other" button on macOS; the event tap tells them apart
        if name == "button8":
            name = "x1"
        elif name == "button9":
            name = "x2"
        if name in _BUTTON_NAMES:
            self._handler.on_button(name, pressed)

    def _on_scroll(self, x, y, dx, dy, injected=False):
        if self._mac:
            return  # the event tap sends the fractional lines (see _mac_mouse_tap)
        if not injected and self._handler is not None:
            self._handler.on_scroll(dx, dy)

    def _on_key(self, key, pressed, injected):
        if injected or self._handler is None:
            return
        name = key_to_name(key)
        if not self._mac and name and family(name) in ("ctrl", "shift"):
            (self._pc_mods.add if pressed else self._pc_mods.discard)(family(name))
        if (not self._mac and self._grabbed and pressed and name == "esc"
                and self._pc_mods >= {"ctrl", "shift"}):
            # Ctrl+Shift+Esc (Task Manager) brings input home at once.
            self._take_back_soon("Ctrl+Shift+Esc pressed")
            return
        if not self._mac and (self._grabbed or self._held_up):
            # The same safety nets as the Mac's event tap (_mac_key_tap), on
            # the listener's own thread: Escape three times quickly always
            # brings input home, and so does a stuck loop. (Before 0.4.72
            # they were only on macOS, so a Windows host had no way back.)
            if pressed and name == "esc" and self._escape_pressed():
                if self._held_up and not self._grabbed:
                    log.warning("drop: letting go of the held button (Escape pressed three times)")
                    self.release_held()
                self._take_back_soon("Escape pressed three times")
                return
            if self._grabbed and not self.loop_alive():
                self._take_back_soon("Curslink stopped forwarding input")
                return
        if name and self._mac:
            name = self._mac_key(key, name, pressed)
        if name:
            self._handler.on_key(name, pressed)

    def _mac_key(self, key, name: str, pressed: bool) -> str:
        """The name for a key typed on this Mac, noting what it means to a PC
        (see pc_names). A key lets go under the name it went down as, even
        if the modifiers changed in between (Option+D down, Option up, D up)."""
        fam = family(name)
        if fam in MODIFIER_FAMILIES:
            (self._held.add if pressed else self._held.discard)(fam)
            return name
        vk = getattr(key, "vk", None)
        if vk is None or getattr(key, "_is_media", None):
            return name
        if not pressed:
            return self._mac_down.pop(vk, name)
        pc = mac_pc_name(name, vk, self._mac_base(vk), self._held)
        if pc is not None:
            self.pc_names[name] = pc
        self._mac_down[vk] = name
        return name

    def _mac_base(self, vk: int) -> str:
        """What the key types with no modifiers but Shift, on this Mac's
        keyboard layout."""
        shift = "shift" in self._held
        ctx = getattr(self._key_listener, "_context", None)
        if ctx and ctx[1] is not None:
            try:
                from pynput._util.darwin import keycode_to_string

                text = keycode_to_string(ctx, vk, 0x02 if shift else 0)  # shiftKey >> 8
                if text and text.isprintable():
                    return text
            except Exception:  # pragma: no cover - macOS only
                log.debug("couldn't read the keyboard layout", exc_info=True)
        try:
            from pynput._util.darwin_vks import SYMBOLS
        except Exception:
            return ""
        text = SYMBOLS.get(vk, "")
        return text.upper() if shift else text


# ---------------------------------------------------------------- Windows bits
_WIN_LCONTROL = 0xA2
_WIN_ALTGR_CTRL_SCAN = 0x21D  # the left Ctrl Windows makes up for AltGr


def is_altgr_ctrl(vk: int, scan: int) -> bool:
    """On a UK or European layout, Windows sends AltGr as a made-up left
    Ctrl followed by right Alt. That Ctrl isn't a key anyone pressed: sent
    on, a Mac would see Control+Option, and AltGr+4 wouldn't type "€"."""
    return vk == _WIN_LCONTROL and scan == _WIN_ALTGR_CTRL_SCAN


def _win_key_filter(msg, data) -> bool:
    """pynput's keyboard filter: ``False`` keeps a key from our callbacks
    (it is still held back from Windows while input is grabbed)."""
    return not is_altgr_ctrl(data.vkCode, data.scanCode)


def mac_keycode_for(char: str, shift: bool, base: dict[str, int], shifted: dict[str, int]) -> int | None:
    """The Mac keycode to press for ``char`` with the modifiers already held.

    ``base`` is what each key types alone on this Mac's layout and
    ``shifted`` what it types with Shift. pynput only knows ``base``: any
    other character (a capital, "!") went out as keycode 0, which is the A
    key, with the character attached. Text came out right, but Cmd+Shift+Z
    was Cmd+Shift+A to the app. Shortcuts go by keycode, so look the key up
    on the Shift layer when Shift is held, and use the letter's key for a
    capital. ``None``: let pynput type the character as text."""
    if not char or len(char) != 1:
        return None
    if char in base:
        return base[char]
    if shift and char in shifted:
        return shifted[char]
    if char.lower() != char and char.lower() in base:
        return base[char.lower()]
    return None


# ------------------------------------------------------------------ macOS bits
# CGEventType values: mouse moved and the three drags; the two tap-disabled notices.
_MAC_MOVES = frozenset({5, 6, 7, 27})
_MAC_LEFT_DOWN, _MAC_LEFT_UP = 1, 2
_MAC_KEY_DOWN = 10                         # kCGEventKeyDown
_MAC_KEYCODE = 9                           # kCGKeyboardEventKeycode
_MAC_ESCAPE = 53
ESCAPE_WINDOW_S = 1.5   # Escape three times within this: input comes home
HOOK_CHECK_S = 0.25     # Windows: how often the parked cursor is checked (_watch_hooks)
HOOK_DRIFT_PX = 24      # ... and how far off the park point means the hooks are gone
HOOK_LOST_MAX = 4       # gone this often in 10 s: input comes home instead
RESTART_WAIT_S = 1.0    # a safety net waits this long at most for hooks being put back elsewhere
HELPER_FILL_S = 0.1     # Windows: helper moves count after our own hook heard none for this long
DEAF_TICKS = 3          # Windows: checks in a row the cursor moved and the hook heard nothing (_DeafHook)
DEAF_SAY_S = 60.0       # ... said at most this often per app in front
HOLD_MAX_S = 60.0       # a button-up held for a drop goes after this at most
_MAC_TAP_DISABLED = frozenset({0xFFFFFFFE, 0xFFFFFFFF})
_MAC_DELTA_X, _MAC_DELTA_Y = 4, 5          # kCGMouseEventDeltaX / DeltaY
_MAC_SOURCE_PID = 41                       # kCGEventSourceUnixProcessID
_MAC_OTHER_DOWN, _MAC_OTHER_UP = 25, 26    # kCGEventOtherMouseDown / Up
_MAC_OTHER_BUTTONS = frozenset({_MAC_OTHER_DOWN, _MAC_OTHER_UP})
_MAC_BUTTON_NUMBER = 3                     # kCGMouseEventButtonNumber
_MAC_BUTTON_NUMBERS = {2: "middle", 3: "x1", 4: "x2"}
_MAC_SCROLL = 22                           # kCGEventScrollWheel
_MAC_SCROLL_Y, _MAC_SCROLL_X = 93, 94      # kCGScrollWheelEventFixedPtDeltaAxis1 / 2 (lines)
_MAC_SCROLL_LINES_Y, _MAC_SCROLL_LINES_X = 11, 12  # kCGScrollWheelEventDeltaAxis1 / 2 (whole lines)
_MAC_SCROLL_CONTINUOUS = 88                # kCGScrollWheelEventIsContinuous (trackpad, Magic Mouse)
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


def _mac_field(event, field: int) -> int:
    import Quartz

    return Quartz.CGEventGetIntegerValueField(event, field)


def _mac_scroll(event) -> tuple[float, float]:
    import Quartz

    return mac_scroll_lines(
        bool(Quartz.CGEventGetIntegerValueField(event, _MAC_SCROLL_CONTINUOUS)),
        (Quartz.CGEventGetIntegerValueField(event, _MAC_SCROLL_LINES_X),
         Quartz.CGEventGetIntegerValueField(event, _MAC_SCROLL_LINES_Y)),
        (Quartz.CGEventGetDoubleValueField(event, _MAC_SCROLL_X),
         Quartz.CGEventGetDoubleValueField(event, _MAC_SCROLL_Y)),
    )


def mac_scroll_lines(continuous: bool, lines: tuple[int, int], fixed: tuple[float, float]) -> tuple[float, float]:
    """How many lines a Mac scroll event scrolled, as (dx, dy).

    A trackpad or Magic Mouse (continuous) scrolls fractions of a line, so
    its fixed-point delta is the true amount. A wheel mouse's notch, with
    macOS's scroll acceleration, comes as a fixed-point 0.1 or so when
    turned slowly, which a Windows app would need ten notches to move for;
    its whole-line delta is at least one line per notch, like a wheel on
    Windows."""
    if continuous:
        return float(fixed[0]), float(fixed[1])
    return float(lines[0]), float(lines[1])


def _mac_injected(event) -> bool:
    import Quartz

    return Quartz.CGEventGetIntegerValueField(event, _MAC_SOURCE_PID) != 0


def _mac_ours(event) -> bool:
    """Did Curslink itself post this event? Scroll and extra-button events
    from a mouse often come through another app that re-posts them (Logi
    Options+, Mos, Mac Mouse Fix, LinearMouse...), so they carry that app's
    process id rather than none: they're still the user's, and go to the
    other computer like the trackpad's."""
    import Quartz

    return Quartz.CGEventGetIntegerValueField(event, _MAC_SOURCE_PID) == os.getpid()


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


def _mac_post_move(x: int, y: int, held: set[str], last: tuple[int, int] | None,
                   clicks: "ClickCounter | None" = None) -> bool:
    """Move the cursor to (x, y) with a real mouse event, as a mouse would:
    mouse-dragged while a button is held (so a window being dragged moves,
    and a drag of files begins and moves), mouse-moved otherwise (so what's
    under it hovers). False if it couldn't be posted."""
    try:
        import Quartz

        # What the window server thinks is held: the host's button-ups don't
        # all come through button() (a drag sent to the host ends here with
        # Escape and a posted button-up), and a stale "held" would turn
        # every move after it into a drag.
        state = Quartz.kCGEventSourceStateCombinedSessionState
        held = {name for name, number in (("left", 0), ("right", 1), ("middle", 2))
                if Quartz.CGEventSourceButtonState(state, number)} or (held & {"x1", "x2"})
        if "left" in held:
            kind, button = Quartz.kCGEventLeftMouseDragged, Quartz.kCGMouseButtonLeft
        elif "right" in held:
            kind, button = Quartz.kCGEventRightMouseDragged, Quartz.kCGMouseButtonRight
        elif held:
            kind, button = Quartz.kCGEventOtherMouseDragged, Quartz.kCGMouseButtonCenter
        else:
            kind, button = Quartz.kCGEventMouseMoved, Quartz.kCGMouseButtonLeft
        ev = Quartz.CGEventCreateMouseEvent(None, kind, (float(x), float(y)), button)
        if kind != Quartz.kCGEventMouseMoved:
            # A real mouse's drag events carry the click count of the press
            # that began them (1, or 2 for a double-click drag selecting by
            # words); a created one may say 0, and an app can take that for
            # a drag with no press behind it.
            name = "left" if "left" in held else "right" if "right" in held else "middle"
            count = clicks.held(name) if clicks is not None else 1
            Quartz.CGEventSetIntegerValueField(ev, _MAC_CLICK_STATE, count)
        if last is not None:  # some apps (and games) read how far it moved
            Quartz.CGEventSetIntegerValueField(ev, _MAC_DELTA_X, int(x - last[0]))
            Quartz.CGEventSetIntegerValueField(ev, _MAC_DELTA_Y, int(y - last[1]))
        Quartz.CGEventPost(Quartz.kCGHIDEventTap, ev)
        return True
    except Exception:  # pragma: no cover - macOS only
        log.debug("couldn't post the mouse move", exc_info=True)
        return False


_MAC_BUTTONS = {"left": (1, 2, 0), "right": (3, 4, 1), "middle": (25, 26, 2)}  # down, up, CGMouseButton
_MAC_CLICK_STATE = 1                       # kCGMouseEventClickState


class ClickCounter:
    """Counts clicks the way the OS would for a real mouse: a press of the
    same button soon enough after the last one, close enough to it, is the
    next click of a double (or triple) click; its release carries the same
    count. A Mac app sees a double click only from the click count on the
    event (kCGMouseEventClickState), not from the timing, and a bare posted
    press always says 1: 0.4.121 and before, double clicks driven from
    another computer never registered on a Mac."""

    SLOP_PX = 5

    def __init__(self, interval: float = 0.5):
        self.interval = interval
        self._last: tuple[str, float, float, float, int] | None = None  # button, t, x, y, count
        self._held: dict[str, int] = {}

    def press(self, button: str, x: float, y: float, now: float | None = None) -> int:
        now = time.monotonic() if now is None else now
        count = 1
        if self._last is not None:
            b, t, lx, ly, n = self._last
            if (b == button and now - t <= self.interval
                    and abs(x - lx) <= self.SLOP_PX and abs(y - ly) <= self.SLOP_PX):
                count = n + 1
        self._last = (button, now, x, y, count)
        self._held[button] = count
        return count

    def release(self, button: str) -> int:
        return self._held.pop(button, 1)

    def held(self, button: str) -> int:
        """The click count of ``button``'s press, while it's held (1 if this
        counter didn't see it go down)."""
        return self._held.get(button, 1)


def _mac_double_click_interval() -> float:
    try:
        from AppKit import NSEvent

        return float(NSEvent.doubleClickInterval()) or 0.5
    except Exception:  # pragma: no cover - macOS only
        return 0.5


def _mac_post_button(button: str, pressed: bool, clicks: ClickCounter) -> bool:
    """Press or let go of a mouse button where the cursor is, as a real
    mouse would, with the click count set so double and triple clicks
    work. False if it couldn't be posted."""
    try:
        import Quartz

        down, up, number = _MAC_BUTTONS[button]
        loc = Quartz.CGEventGetLocation(Quartz.CGEventCreate(None))
        at = (float(loc.x), float(loc.y))
        count = clicks.press(button, *at) if pressed else clicks.release(button)
        ev = Quartz.CGEventCreateMouseEvent(None, down if pressed else up, at, number)
        Quartz.CGEventSetIntegerValueField(ev, _MAC_CLICK_STATE, count)
        Quartz.CGEventPost(Quartz.kCGHIDEventTap, ev)
        if count > 1:
            log.debug("click %d of %s", count, button)
        return True
    except Exception:  # pragma: no cover - macOS only
        log.debug("couldn't post the mouse button", exc_info=True)
        return False


def _mac_sets_cursor_in_background(cg, cf) -> None:
    cg._CGSDefaultConnection.restype = ctypes.c_int
    cid = cg._CGSDefaultConnection()
    cf.CFStringCreateWithCString.restype = ctypes.c_void_p
    cf.CFStringCreateWithCString.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_uint32]
    key = cf.CFStringCreateWithCString(None, b"SetsCursorInBackground", 0x08000100)
    true = ctypes.c_void_p.in_dll(cf, "kCFBooleanTrue")
    cg.CGSSetConnectionProperty.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p]
    cg.CGSSetConnectionProperty(cid, cid, key, true)


def _mac_cursor_visible(visible: bool) -> None:
    """Hide or show the Mac's cursor. A background process may only do that
    after setting "SetsCursorInBackground" on its window server connection
    (what Synergy and Barrier do).

    macOS doesn't always keep it hidden: something else can bring it back
    while input is on another computer (Kyle saw it sitting on the laptop's
    screen, at the park point). So while hidden, :func:`_mac_keep_hidden`
    looks every so often and hides it again if it's showing; every hide is
    counted, and showing it matches them all."""
    global _mac_hidden, _mac_show_gen, _mac_hides, _mac_rehides
    if visible != _mac_hidden:
        return  # already that way (hiding counts, so never hide twice)
    try:
        cg, cf = _mac_lib(_CG_PATH), _mac_lib(_CF_PATH)
        _mac_sets_cursor_in_background(cg, cf)
        _mac_show_gen += 1
        if not visible:
            cg.CGDisplayHideCursor(0)
            _mac_hides, _mac_rehides = 1, 0
            _mac_hidden = True
            _mac_start_keeping_hidden(_mac_show_gen)
        else:
            if _mac_rehides:
                log.info("macOS showed the cursor %d time(s) while input was on another computer; "
                         "it was hidden again each time", _mac_rehides)
            for _ in range(max(1, _mac_hides)):
                cg.CGDisplayShowCursor(0)
            _mac_hides = _mac_rehides = 0
            _mac_hidden = False
            _mac_check_shown(_mac_show_gen, 0)
    except Exception:  # pragma: no cover - macOS only
        log.debug("couldn't %s the cursor", "show" if visible else "hide", exc_info=True)


_mac_hides = 0     # CGDisplayHideCursor calls not yet matched by a show
_mac_rehides = 0   # times it was found showing and hidden again, this time away
# While hidden: how soon to look whether it's showing again (the first one
# soon, after the system shortcuts are turned off), then how often.
# (Every 0.05 s, not 0.2: going into a mirrored app, macOS shows it as the
# mirror comes forward, and for 0.2 s it was a second pointer, Kyle 0.4.125.)
_MAC_HIDE_CHECKS_S = (0.05, 0.05)
# Hidden again this many times while away and still showing: macOS won't
# have it, so stop (and say so) rather than pile up hides.
_MAC_REHIDE_MAX = 400  # (each time into a mirrored app can be one)


def _mac_keep_hidden(gen: int) -> bool:
    """One look while hidden: hide the cursor again if it's showing. False
    once there's nothing more to do (shown on purpose since, or given up)."""
    global _mac_hides, _mac_rehides
    if gen != _mac_show_gen or not _mac_hidden:
        return False
    try:
        cg, cf = _mac_lib(_CG_PATH), _mac_lib(_CF_PATH)
        cg.CGCursorIsVisible.restype = ctypes.c_bool
        if not cg.CGCursorIsVisible():
            return True
        if _mac_rehides >= _MAC_REHIDE_MAX:
            log.warning("macOS keeps showing the cursor while input is on another computer "
                        "(hidden again %d times); leaving it", _mac_rehides)
            return False
        if not _mac_rehides:
            log.info("the cursor showed again while input is on another computer; hiding it again")
        _mac_sets_cursor_in_background(cg, cf)
        cg.CGDisplayHideCursor(0)
        _mac_hides += 1
        _mac_rehides += 1
    except Exception:  # pragma: no cover - macOS only
        log.debug("couldn't check the cursor is hidden", exc_info=True)
        return False
    return True


def _mac_start_keeping_hidden(gen: int) -> None:
    def run():
        time.sleep(_MAC_HIDE_CHECKS_S[0])
        while _mac_keep_hidden(gen):
            time.sleep(_MAC_HIDE_CHECKS_S[1])

    threading.Thread(target=run, name="curslink-cursor-hidden", daemon=True).start()


_mac_show_gen = 0
# When the frontmost app isn't responding, the window server can leave the
# cursor hidden after we show it again (it waits on that app). So look again
# a few times after showing it, and show it again if it's still hidden.
_MAC_SHOW_CHECKS_S = (0.05, 0.25, 1.0, 3.0)


def _mac_check_shown(gen: int, attempt: int) -> None:
    if gen != _mac_show_gen or _mac_hidden:
        return  # hidden again on purpose since, or shown for good
    try:
        cg, cf = _mac_lib(_CG_PATH), _mac_lib(_CF_PATH)
        if attempt > 0:
            cg.CGCursorIsVisible.restype = ctypes.c_bool
            if not cg.CGCursorIsVisible():
                log.warning("the cursor was still hidden after input came home (is the app in front "
                            "not responding?); showing it again")
                _mac_sets_cursor_in_background(cg, cf)
                cg.CGDisplayShowCursor(0)
                _mac_nudge_cursor()
    except Exception:  # pragma: no cover - macOS only
        log.debug("couldn't check the cursor is showing", exc_info=True)
    if attempt < len(_MAC_SHOW_CHECKS_S):
        timer = threading.Timer(_MAC_SHOW_CHECKS_S[attempt], _mac_check_shown, (gen, attempt + 1))
        timer.daemon = True
        timer.start()


def _mac_nudge_cursor() -> None:
    """Post a mouse-moved event where the cursor already is: the window
    server redraws the cursor for it even when the app in front is hung."""
    try:
        import Quartz

        at = Quartz.CGEventGetLocation(Quartz.CGEventCreate(None))
        event = Quartz.CGEventCreateMouseEvent(None, Quartz.kCGEventMouseMoved, at, Quartz.kCGMouseButtonLeft)
        Quartz.CGEventPost(Quartz.kCGHIDEventTap, event)
    except Exception:  # pragma: no cover - macOS only
        log.debug("couldn't nudge the cursor", exc_info=True)


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

    post_moves = False
    _posted_at: tuple[int, int] | None = None

    def __init__(self, modifier_map: dict[str, str] | None = None, post_moves: bool = False):
        """``post_moves``: this computer is being driven from another one (an
        agent). On a Mac every move is then posted as a real mouse event,
        mouse-dragged while a button is held, so windows follow a held drag,
        Finder starts dragging files and the apps under the pointer see it
        (0.4.75 and before warped it there with no event: a dragged window
        stayed put until let go, and a file drag never began)."""
        from pynput import keyboard, mouse

        self.post_moves = post_moves
        self._down: set[str] = set()  # buttons held, as this sink pressed them
        self._posted_at: tuple[int, int] | None = None
        self._mouse = mouse.Controller()
        self._keyboard = keyboard.Controller()
        self.modifier_map = modifier_map or {}
        self._scroll_carry = (0.0, 0.0)
        from curslink.tally import Tally

        self.notches = Tally("scroll given to Windows as whole notches")

    def move_to(self, x: int, y: int) -> None:
        if sys.platform == "win32" and _win_move(x, y):
            return
        if sys.platform == "darwin" and self.post_moves:
            if _mac_post_move(x, y, self.__dict__.get("_down", set()), self._posted_at,
                              self.__dict__.get("_clicks")):
                self._posted_at = (x, y)
                return
        if sys.platform == "darwin":
            # Input coming home: put the cursor where it crossed the edge by
            # warping it (what Synergy does), then tie it back to the mouse,
            # which also cancels macOS's short freeze after a warp. A posted
            # mouse-moved event could lose to reports still in flight from
            # the parked position and leave the cursor mid-screen.
            _mac_warp(x, y)
            _mac_attach_cursor(True)
            self._posted_at = None
            return
        self._mouse.position = (x, y)

    def park(self, x: int, y: int) -> None:
        """Put the cursor out of the way (input went to another device):
        on a Mac a warp, so nothing under the park point hovers."""
        if sys.platform == "darwin":
            _mac_warp(x, y)
            self._posted_at = None
            return
        self.move_to(x, y)

    def button(self, button: str, pressed: bool) -> None:
        from pynput import mouse

        names = {"x1": "button8", "x2": "button9"} if sys.platform.startswith("linux") else {}
        target = getattr(mouse.Button, names.get(button, button), None)
        if target is None:
            log.debug("button %s not supported on this platform", button)
            return
        down = self.__dict__.setdefault("_down", set())
        (down.add if pressed else down.discard)(button)
        if sys.platform == "darwin" and button in _MAC_BUTTONS:
            clicks = self.__dict__.get("_clicks")
            if clicks is None:
                clicks = self._clicks = ClickCounter(_mac_double_click_interval())
            if _mac_post_button(button, pressed, clicks):
                return
        (self._mouse.press if pressed else self._mouse.release)(target)

    def scroll(self, dx: float, dy: float) -> None:
        if sys.platform == "win32":
            # A Mac's smooth wheel or trackpad sends fractions of a line, and
            # plenty of Windows apps ignore a wheel turn under one notch
            # (120): add them up and send whole notches.
            sx, sy = self._scroll_carry
            sx, sy = sx + dx, sy + dy
            wx, wy = int(sx), int(sy)  # towards zero, so the rest carries over
            self._scroll_carry = (sx - wx, sy - wy)
            if wx or wy:
                self.notches.add(wx, wy)
            if (wx or wy) and not _win_wheel(wx, wy):
                self._mouse.scroll(wx, wy)
            return
        self._mouse.scroll(dx, dy)

    def key(self, key: str, pressed: bool) -> None:
        name = self.modifier_map.get(key, key)
        target = self._mac_key(name) if sys.platform == "darwin" and len(name) == 1 else None
        if target is None:
            target = name_to_key(name)
        if target is None:
            log.debug("key %s not supported on this platform", key)
            return
        (self._keyboard.press if pressed else self._keyboard.release)(target)


    def _mac_key(self, char: str):
        """A character as the Mac key that types it (see mac_keycode_for)."""
        from pynput import keyboard

        layers = self.__dict__.get("_mac_layers")
        if layers is None:
            layers = self._mac_layers = _mac_layers(getattr(self._keyboard, "_mapping", {}) or {})
        with self._keyboard.modifiers as held:
            shift = keyboard.Key.shift in held
        vk = mac_keycode_for(char, shift, *layers)
        return None if vk is None else keyboard.KeyCode.from_vk(vk)


def _mac_layers(base: dict[str, int]) -> tuple[dict[str, int], dict[str, int]]:
    """What each key types on this Mac's layout, alone and with Shift.
    The first key wins, so digits are the top row's, not the keypad's."""
    fallback = {c: vk for c, vk in base.items() if c and len(c) == 1}
    base, shifted = {}, {}
    try:
        from pynput._util.darwin import keycode_context, keycode_to_string

        with keycode_context() as ctx:
            for vk in range(128):
                for layer, state in ((base, 0), (shifted, 0x02)):  # shiftKey >> 8
                    text = keycode_to_string(ctx, vk, state)
                    if text and len(text) == 1 and text.isprintable():
                        layer.setdefault(text, vk)
    except Exception:  # pragma: no cover - macOS only
        log.debug("couldn't read the keyboard layout", exc_info=True)
    return base or fallback, shifted


def drag_nudge(x: int, y: int, release: bool = False) -> None:
    """Move a held left-button drag at (x, y) a pixel and back, so the OS's
    drag-and-drop notices what's under it now. With ``release``, then cancel
    the drag with Escape (macOS and Windows both end a drag on Escape, with
    nothing dropped wherever the parked pointer happens to be) and let go."""
    _drag_move(x, y)
    if release:
        _cancel_drag()
        time.sleep(0.05)
        _drag_release(x, y)


def mac_cursor() -> tuple[float, float]:
    import Quartz

    loc = Quartz.CGEventGetLocation(Quartz.CGEventCreate(None))
    return loc.x, loc.y


def press_left_here() -> None:
    """Post a left button-down where the cursor is (to start a real drag
    from the overlay helper's window there).

    Windows: the button is physically held too, but its down was kept from
    Windows while input was on the other computer, so Windows thinks it's
    up; this down (injected, so the hooks let it by) makes it held, and
    the helper's drag starts on it. The real button-up then ends the drag."""
    if sys.platform == "win32":
        try:
            inp = _INPUT(type=0)
            inp.mi = _MOUSEINPUT(0, 0, 0, _MOUSEEVENTF_LEFTDOWN, 0, 0)
            ctypes.windll.user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(_INPUT))
        except Exception:  # pragma: no cover - Windows only
            log.warning("couldn't press the button to start the drag", exc_info=True)
        return
    try:
        import Quartz

        # The button is physically held already (it went down while input was
        # on the other computer, and the event tap kept it from apps, not from
        # the window server). A second down would be ignored, so let go first:
        # then this down makes the drag source's app the one the window server
        # sends the mouse-dragged events to, which moves the drag and tells
        # the apps under it.
        at = mac_cursor()
        for kind in (Quartz.kCGEventLeftMouseUp, Quartz.kCGEventLeftMouseDown):
            ev = Quartz.CGEventCreateMouseEvent(None, kind, at, Quartz.kCGMouseButtonLeft)
            Quartz.CGEventSetIntegerValueField(ev, _MAC_CLICK_STATE, 1)
            Quartz.CGEventPost(Quartz.kCGHIDEventTap, ev)
            time.sleep(0.02)
    except Exception:  # pragma: no cover - macOS only
        log.warning("couldn't press the button to start the drag", exc_info=True)


def _cancel_drag() -> None:
    try:
        from pynput import keyboard

        keyboard.Controller().tap(keyboard.Key.esc)
    except Exception:  # pragma: no cover - platform specific
        log.warning("couldn't cancel the drag with Escape", exc_info=True)


def _drag_move(x: int, y: int) -> None:
    if sys.platform == "darwin":
        try:
            import Quartz

            for px in (x + 1, x):
                ev = Quartz.CGEventCreateMouseEvent(None, Quartz.kCGEventLeftMouseDragged, (px, y),
                                                    Quartz.kCGMouseButtonLeft)
                Quartz.CGEventSetIntegerValueField(ev, _MAC_CLICK_STATE, 1)
                Quartz.CGEventPost(Quartz.kCGHIDEventTap, ev)
                time.sleep(0.03)
        except Exception:  # pragma: no cover - macOS only
            log.warning("couldn't nudge the drag on this Mac", exc_info=True)
        return
    if sys.platform == "win32":
        try:
            user32 = ctypes.windll.user32
            for dx in (1, -1):
                inp = _INPUT(type=0)
                inp.mi = _MOUSEINPUT(dx, 0, 0, _MOUSEEVENTF_MOVE, 0, 0)
                user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(_INPUT))
                time.sleep(0.03)
        except Exception:  # pragma: no cover - Windows only
            log.warning("couldn't nudge the drag on this PC", exc_info=True)
        return
    from pynput import mouse

    m = mouse.Controller()
    m.position = (x + 1, y)
    time.sleep(0.03)
    m.position = (x, y)


def _drag_release(x: int, y: int) -> None:
    if sys.platform == "darwin":
        try:
            import Quartz

            ev = Quartz.CGEventCreateMouseEvent(None, Quartz.kCGEventLeftMouseUp, (x, y),
                                                Quartz.kCGMouseButtonLeft)
            Quartz.CGEventSetIntegerValueField(ev, _MAC_CLICK_STATE, 1)
            Quartz.CGEventPost(Quartz.kCGHIDEventTap, ev)
        except Exception:  # pragma: no cover - macOS only
            log.warning("couldn't let go of the drag on this Mac", exc_info=True)
        return
    if sys.platform == "win32":
        try:
            inp = _INPUT(type=0)
            inp.mi = _MOUSEINPUT(0, 0, 0, _MOUSEEVENTF_LEFTUP, 0, 0)
            ctypes.windll.user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(_INPUT))
        except Exception:  # pragma: no cover - Windows only
            log.warning("couldn't let go of the drag on this PC", exc_info=True)
        return
    from pynput import mouse

    mouse.Controller().release(mouse.Button.left)


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
_MOUSEEVENTF_LEFTDOWN, _MOUSEEVENTF_LEFTUP = 0x0002, 0x0004
_WM_LBUTTONDOWN, _WM_LBUTTONUP = 0x0201, 0x0202
_LLMHF_INJECTED_ANY = 0x00000001 | 0x00000002  # LLMHF_INJECTED | LLMHF_LOWER_IL_INJECTED
_MOUSEEVENTF_WHEEL, _MOUSEEVENTF_HWHEEL, _WHEEL_DELTA = 0x0800, 0x1000, 120
def win_absolute(x: float, y: float, vx: int, vy: int, vw: int, vh: int) -> tuple[int, int]:
    """A virtual-desktop pixel -> SendInput's 0..65535 absolute coordinates."""
    nx = round((x - vx) * 65535 / max(1, vw - 1))
    ny = round((y - vy) * 65535 / max(1, vh - 1))
    return min(max(nx, 0), 65535), min(max(ny, 0), 65535)


class _POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


_win_user32 = None


def _win_cursor_pos() -> tuple[int, int] | None:
    """Windows: where the cursor is in physical pixels, as the input hooks
    report it whether or not this process is DPI aware, quickly (called for
    every grabbed mouse report)."""
    global _win_user32
    try:
        if _win_user32 is None:
            _win_user32 = ctypes.WinDLL("user32", use_last_error=True)
        pt = _POINT()
        if _win_user32.GetPhysicalCursorPos(ctypes.byref(pt)):
            return pt.x, pt.y
    except Exception:
        log.debug("GetCursorPos failed", exc_info=True)
    return None


def _win_move(x: int, y: int) -> bool:
    # In this process's pixels: physical ones, as the layout has them (the
    # host and agent are DPI aware since 0.4.75, see cli.windows_dpi_aware;
    # 0.4.9-0.4.11 tried it while layouts were still in logical pixels).
    # Absolute SendInput coordinates are a fraction of the virtual desktop
    # as this process sees it, so they land in the right place either way.
    try:
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        vx, vy = user32.GetSystemMetrics(76), user32.GetSystemMetrics(77)
        vw, vh = user32.GetSystemMetrics(78), user32.GetSystemMetrics(79)
        nx, ny = win_absolute(x, y, vx, vy, vw, vh)
        inp = _INPUT(type=0)
        inp.mi = _MOUSEINPUT(nx, ny, 0, _MOUSEEVENTF_MOVE | _MOUSEEVENTF_ABSOLUTE | _MOUSEEVENTF_VIRTUALDESK, 0, 0)
        if user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(_INPUT)) == 1:
            _win_failures.ok()
            return True
        error = ctypes.get_last_error()
        # Windows refuses injected input while the app in front runs as
        # administrator and we don't (UIPI), and on the lock and UAC screens.
        # SetCursorPos still works in some of those cases.
        moved = bool(user32.SetCursorPos(int(x), int(y)))
        _win_failures.failed(error, moved)
        return True
    except Exception:
        log.debug("SendInput move failed; falling back to SetCursorPos", exc_info=True)
    return False


def _win_wheel(notches_x: int, notches_y: int) -> bool:
    """Whole wheel notches through SendInput, so a refusal is logged (with
    what's in front) like a refused move. False if this can't be tried."""
    try:
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        for flag, notches in ((_MOUSEEVENTF_WHEEL, notches_y), (_MOUSEEVENTF_HWHEEL, notches_x)):
            if not notches:
                continue
            inp = _INPUT(type=0)
            inp.mi = _MOUSEINPUT(0, 0, (notches * _WHEEL_DELTA) & 0xFFFFFFFF, flag, 0, 0)
            if user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(_INPUT)) == 1:
                _win_failures.ok()
            else:
                _win_failures.failed(ctypes.get_last_error(), False, what="scroll")
        return True
    except Exception:
        log.debug("SendInput scroll failed; falling back to pynput", exc_info=True)
    return False


class _WinInjectFailures:
    """Logs (at most every few seconds) when Windows won't take the moves
    we inject, with what's in front, since that's nearly always why."""

    EVERY_S = 5.0

    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self.count = 0
        self.logged_at: float | None = None

    def ok(self) -> None:
        if self.count:
            log.info("Windows takes injected input again (%d moves were refused)", self.count)
            self.count = 0
            self.logged_at = None

    def failed(self, error: int, moved: bool, what: str = "move") -> None:
        self.count += 1
        now = self.clock()
        if self.logged_at is not None and now - self.logged_at < self.EVERY_S:
            return
        self.logged_at = now
        log.warning(
            "Windows refused an injected pointer %s (error %d, %d so far%s). In front: %s. "
            "If that app runs as administrator, Windows blocks input from Curslink until it's closed, "
            "or until Curslink runs as administrator too.",
            what, error, self.count,
            ("; SetCursorPos worked" if moved else "; SetCursorPos failed too") if what == "move" else "",
            _win_foreground(),
        )


_win_failures = _WinInjectFailures()


class _DeafHook:
    """Windows, input here: the cursor moving while the mouse hook hears
    nothing. Windows doesn't show this (unelevated) process's hooks the
    mouse over an app that runs as administrator, so at the edge of the
    screen the cursor just stops: nothing here saw it get there. Kyle
    (0.4.76): "Windows feels like it's forcefully stopping the cursor
    leaving the monitor". Says so in the log, with the app in front, once
    per app in DEAF_SAY_S."""

    def __init__(self, capture: "PynputCapture", clock=time.monotonic,
                 cursor=lambda: _win_cursor_pos(), foreground=lambda: _win_foreground()):
        self.capture = capture
        self.clock = clock
        self.cursor = cursor
        self.foreground = foreground
        self.last: tuple[int, int] | None = None
        self.ticks = 0

    def reset(self) -> None:
        self.last, self.ticks = None, 0

    def check(self) -> None:
        at = self.cursor()
        last, self.last = self.last, at
        now = self.clock()
        if at is None or last is None or at == last or now - self.capture._ungrabbed_at < 1.0 \
                or now - self.capture._hook_heard_at < HOOK_CHECK_S * 1.5:
            self.ticks = 0
            return
        self.ticks += 1  # it moved since the last look, and the hook heard nothing
        if self.ticks != DEAF_TICKS:
            return
        app = self.foreground()
        said = self.capture._deaf_said
        if now - said.get(app, -1e9) < DEAF_SAY_S:
            return
        said[app] = now
        admin = "runs as administrator" in app
        log.warning("input: the cursor is moving at (%d, %d) but Curslink's mouse hook hears nothing (in front: %s)%s",
                    at[0], at[1], app,
                    ": Windows keeps the mouse from Curslink over apps that run as administrator, so the "
                    "pointer can't cross to another computer until another app is in front (Settings > "
                    "Control admin apps lets Curslink hear it there)"
                    if admin else ": the pointer can't cross to another computer until it hears the mouse again")


def win_cursor_clip() -> tuple[int, int, int, int] | None:
    """Windows: the box an app holds the cursor in with ClipCursor, as
    ``(x, y, width, height)`` in physical pixels; None when it can go
    anywhere on the desktop (or this isn't Windows)."""
    if sys.platform != "win32":
        return None
    try:
        from ctypes import wintypes

        user32 = ctypes.WinDLL("user32", use_last_error=True)
        rect = wintypes.RECT()
        if not user32.GetClipCursor(ctypes.byref(rect)):
            return None
        vx, vy = user32.GetSystemMetrics(76), user32.GetSystemMetrics(77)  # SM_X/YVIRTUALSCREEN
        vw, vh = user32.GetSystemMetrics(78), user32.GetSystemMetrics(79)  # SM_CX/CYVIRTUALSCREEN
        if rect.left <= vx and rect.top <= vy and rect.right >= vx + vw and rect.bottom >= vy + vh:
            return None
        return rect.left, rect.top, rect.right - rect.left, rect.bottom - rect.top
    except Exception:
        log.debug("GetClipCursor failed", exc_info=True)
        return None


def _win_foreground() -> str:
    """The app in front: its program name, whether it's not responding and
    whether it runs as administrator (best effort)."""
    try:
        from ctypes import wintypes

        user32 = ctypes.WinDLL("user32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
        user32.GetForegroundWindow.restype = wintypes.HWND
        hwnd = user32.GetForegroundWindow()
        if not hwnd:
            return "nothing (a lock, UAC or secure screen?)"
        hung = bool(user32.IsHungAppWindow(hwnd))
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        kernel32.OpenProcess.restype = wintypes.HANDLE
        process = kernel32.OpenProcess(0x1000, False, pid.value)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not process:
            return f"pid {pid.value}{', not responding' if hung else ''} (can't look inside it)"
        try:
            buf = ctypes.create_unicode_buffer(1024)
            size = wintypes.DWORD(len(buf))
            name = buf.value.rsplit("\\", 1)[-1] if kernel32.QueryFullProcessImageNameW(
                process, 0, buf, ctypes.byref(size)) else f"pid {pid.value}"
            elevated = None
            token = wintypes.HANDLE()
            if advapi32.OpenProcessToken(process, 0x0008, ctypes.byref(token)):  # TOKEN_QUERY
                value, got = wintypes.DWORD(), wintypes.DWORD()
                if advapi32.GetTokenInformation(token, 20, ctypes.byref(value), 4, ctypes.byref(got)):
                    elevated = bool(value.value)  # TokenElevation
                kernel32.CloseHandle(token)
        finally:
            kernel32.CloseHandle(process)
        notes = [n for n, on in (("not responding", hung), ("runs as administrator", elevated)) if on]
        if elevated is None:
            notes.append("couldn't tell if it runs as administrator")
        return name + (f" ({', '.join(notes)})" if notes else "")
    except Exception:
        return "unknown"
