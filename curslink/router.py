"""The routing state machine: which device owns the keyboard and mouse.

Everything here is synchronous, platform free and single threaded (it runs
on the host's asyncio loop), which keeps the logic easy to test. Inputs:

* local pointer positions (while the host itself is active) for edge pushes,
* relative motion (while a remote device is active),
* buttons, scroll, keys (forwarded to the active remote device),
* gaze targets from the camera classifier,
* agent connect / disconnect.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Callable, Protocol

from curslink.gaze.zones import Target
from curslink.inputs.base import InputCapture, InputSink
from curslink.inputs.keys import Hotkey
from curslink.layout import Layout, Screen
from curslink.tally import Tally

log = logging.getLogger(__name__)

# Right after input moves to a device, an edge push straight back to the
# device it came from is ignored for this long. A real "oops, back" takes
# longer than this; a bogus first report (see PynputCapture._mac_motion)
# doesn't, and used to bounce input home within 15 ms of arriving.
RETURN_GUARD_S = 0.15

# While the mouse is moving, the hand is driving: gaze waits until it has
# been still this long. Otherwise a glance mid-move (or gaze beating the
# pointer to the edge) took the pointer to the remembered spot or the middle
# of the screen instead of where it was crossing.
GAZE_HOLD_S = 0.4

# Arriving on another computer by gaze or a hotkey, the pointer comes back
# where it was left, usually just inside the edge it left by. Edge switching
# waits until it has been at least this far from that edge.
ARM_PX = 24


class Transport(Protocol):
    def is_connected(self, device_id: str) -> bool: ...

    def send(self, device_id: str, t: str, **data) -> None: ...


class Overlay(Protocol):
    def show(self, effect: str, x: int, y: int) -> None: ...


class NullOverlay:
    def show(self, effect: str, x: int, y: int) -> None:
        pass


@dataclass
class RouterSettings:
    effect: str = "pop"                      # pop | blink | none
    edge_switching: bool = True
    gaze_switching: bool = True
    gap_tolerance_px: int = 64
    # Legacy switch: every monitor is a jump monitor (see gaze_jump_screens).
    gaze_within_device: bool = False
    # Monitors, as (device_id, screen_id), where looking puts the pointer on
    # that very monitor, even one of the device it's already on. Looking at
    # any other monitor only picks its device: the pointer comes back where
    # you left it there, and nothing happens if that device is already active.
    gaze_jump_screens: frozenset[tuple[str, str]] = field(default_factory=frozenset)
    return_hotkey: str = "ctrl+alt+home"     # always brings input back home
    gaze_toggle_hotkey: str = "ctrl+alt+g"   # pause / resume gaze switching


@dataclass(frozen=True)
class SwitchEvent:
    from_device: str
    to_device: str
    screen: Screen
    reason: str  # edge | gaze | hotkey | disconnect


class Router:
    def __init__(
        self,
        layout: Layout,
        local_device_id: str,
        capture: InputCapture,
        local_sink: InputSink,
        transport: Transport,
        overlay: Overlay | None = None,
        settings: RouterSettings | None = None,
    ):
        self.layout = layout
        self.local_id = local_device_id
        self.capture = capture
        self.local_sink = local_sink
        self.transport = transport
        self.overlay = overlay or NullOverlay()
        self.settings = settings or RouterSettings()
        self.listeners: list[Callable[[SwitchEvent], None]] = []
        # Told whenever gaze switching is paused or resumed.
        self.on_gaze_paused: Callable[[bool], None] = lambda paused: None
        # Why input can't leave this device right now ("" = it can). On
        # macOS without Accessibility/Input Monitoring nothing is captured,
        # so "switching" would only move the label, not the mouse.
        self.blocked: Callable[[], str] = lambda: ""
        self._blocked_said = ""
        # After arriving by gaze or a hotkey (home or on another device) the pointer may land right on
        # the edge it left by; it has to leave that edge before an edge push
        # counts again, or the next pointer event bounces input straight back.
        self._edge_armed = True
        self.clock: Callable[[], float] = time.monotonic
        self._arrived: tuple[float, str] | None = None  # (when, from which device)
        self._moved_at = -1e9  # when the mouse last moved (self.clock())
        self._scrolls = Tally("scroll sent to the other computer")

        first = next(iter(layout.screens(local_device_id)), None)
        if first is None:
            raise ValueError(f"local device {local_device_id!r} has no screens in the layout")
        self.active_device = local_device_id
        self.screen = first
        self.x, self.y = first.center
        self.gaze_paused = False
        self._last_pos: dict[str, tuple[float, float]] = {}
        # Where the pointer last was on each monitor, for jump monitors.
        self._screen_pos: dict[tuple[str, str], tuple[float, float]] = {}
        self._remote_keys: set[str] = set()
        self._remote_buttons: set[str] = set()
        self._local_keys: set[str] = set()
        self._return_hotkey = Hotkey(self.settings.return_hotkey)
        self._gaze_hotkey = Hotkey(self.settings.gaze_toggle_hotkey)

    # ------------------------------------------------------------------ state
    @property
    def is_local(self) -> bool:
        return self.active_device == self.local_id

    def available(self, device_id: str) -> bool:
        return device_id == self.local_id or self.transport.is_connected(device_id)

    # Where the pointer was, kept across restarts (the app restarts the host
    # whenever a setting changes), so gaze brings it back there rather than
    # to the middle of the screen.
    def remembered(self) -> dict:
        self._screen_pos[(self.screen.device_id, self.screen.id)] = (self.x, self.y)
        self._last_pos[self.active_device] = (self.x, self.y)
        return {
            "devices": {d: [round(x, 1), round(y, 1)] for d, (x, y) in self._last_pos.items()},
            "screens": {f"{d}\t{s}": [round(x, 1), round(y, 1)] for (d, s), (x, y) in self._screen_pos.items()},
        }

    def remember(self, data: dict) -> None:
        """Take back what :meth:`remembered` gave, for screens still in the layout."""
        def point(v):
            ok = isinstance(v, list) and len(v) == 2 and all(isinstance(n, (int, float)) for n in v)
            return (float(v[0]), float(v[1])) if ok else None

        devices = data.get("devices") if isinstance(data, dict) else None
        for device, v in (devices or {}).items():
            p = point(v)
            if p is not None and self.layout.screen_at(*p, device) is not None:
                self._last_pos[device] = p
        screens = data.get("screens") if isinstance(data, dict) else None
        for key, v in (screens or {}).items():
            device, _, screen_id = str(key).partition("\t")
            p = point(v)
            try:
                screen = self.layout.screen(device, screen_id)
            except KeyError:
                continue
            if p is not None and screen.contains(*p):
                self._screen_pos[(device, screen_id)] = p

    def jumps_to(self, screen: Screen) -> bool:
        """Does looking at ``screen`` put the pointer on it (not just its device)?"""
        return self.settings.gaze_within_device or (screen.device_id, screen.id) in self.settings.gaze_jump_screens

    def _set_screen(self, screen: Screen) -> None:
        if screen != self.screen:
            self._screen_pos[(self.screen.device_id, self.screen.id)] = (self.x, self.y)
        self.screen = screen

    # ----------------------------------------------------------- local input
    def on_pointer(self, ox: float, oy: float) -> None:
        if not self.is_local:
            return
        screen = self.layout.screen_at_os(self.local_id, ox, oy)
        if screen is None:
            return
        x, y = screen.from_os(ox, oy)
        self._moved_at = self.clock()
        if self.settings.edge_switching:
            front = self.layout.covered_by(self.local_id, x, y)
            if front is not None:
                # The real cursor went into a part of this screen that another
                # device's screen stands in front of: that's where it is now.
                if self.available(front.device_id) and self._edge_armed:
                    self.switch_to(front, *front.clamp(x, y), "edge")
                return
        self._set_screen(screen)
        self.x, self.y = x, y
        if not self.settings.edge_switching:
            return
        pushes = self.layout.edge_pushes(self.local_id, self.x, self.y)
        if not self._edge_armed:
            self._edge_armed = not pushes
            return
        for ex, ey in pushes:
            crossing = self.layout.find_crossing(
                self.local_id, self.x, self.y, ex, ey, self.settings.gap_tolerance_px
            )
            if crossing and self.available(crossing.screen.device_id):
                self.switch_to(crossing.screen, crossing.x, crossing.y, "edge")
                return

    def on_motion(self, dx: float, dy: float, ts: float | None = None) -> None:
        """``ts``: when the motion was captured (time.monotonic()). Sent with
        the move so the agent can play moves back as evenly as they happened
        (see curslink.smoothing)."""
        if self.is_local:
            return
        if dx or dy:
            self._moved_at = self.clock()
        nx, ny = self.x + dx, self.y + dy
        here = self.layout.screen_at(nx, ny, self.active_device)
        if here is None and self.settings.edge_switching:
            crossing = self.layout.find_crossing(
                self.active_device, self.x, self.y, dx, dy, self.settings.gap_tolerance_px
            )
            if crossing and (self._just_came_from(crossing.screen.device_id) or not self._edge_armed):
                crossing = None
            if crossing and self.available(crossing.screen.device_id):
                self.switch_to(crossing.screen, crossing.x, crossing.y, "edge")
                return
        if here is None:
            here, nx, ny = self.layout.clamp_to_device(self.active_device, nx, ny)
            if self.layout.screen_at(nx, ny, self.active_device) is None:
                # Blocked out behind another screen: stay put.
                if not self._edge_armed:
                    self._edge_armed = not self.layout.edge_pushes(self.active_device, self.x, self.y, ARM_PX)
                return
        self._set_screen(here)
        self.x, self.y = nx, ny
        if not self._edge_armed:
            self._edge_armed = not self.layout.edge_pushes(self.active_device, nx, ny, ARM_PX)
        ox, oy = here.to_os(nx, ny)
        if ts is None:
            self.transport.send(self.active_device, "move", x=ox, y=oy)
        else:
            self.transport.send(self.active_device, "move", x=ox, y=oy, ts=round(ts * 1000.0, 2))

    def _just_came_from(self, device_id: str) -> bool:
        if self._arrived is None:
            return False
        when, source = self._arrived
        return source == device_id and self.clock() - when < RETURN_GUARD_S

    def on_button(self, button: str, pressed: bool) -> None:
        if self.is_local:
            return
        self._moved_at = self.clock()  # clicking counts as using the mouse
        if pressed:
            self._remote_buttons.add(button)
        else:
            self._remote_buttons.discard(button)
        self.transport.send(self.active_device, "button", button=button, pressed=pressed)

    def on_scroll(self, dx: float, dy: float) -> None:
        if not self.is_local:
            self._moved_at = self.clock()  # so does scrolling: gaze waits
            self._scrolls.add(dx, dy)
            self.transport.send(self.active_device, "scroll", dx=dx, dy=dy)

    def on_key(self, key: str, pressed: bool) -> None:
        if self._return_hotkey.feed(key, pressed) and not self.is_local:
            self.go_home("hotkey")
            return
        if self._gaze_hotkey.feed(key, pressed):
            self.set_gaze_paused(not self.gaze_paused)
        if self.is_local:
            (self._local_keys.add if pressed else self._local_keys.discard)(key)
            return
        if pressed:
            self._remote_keys.add(key)
        else:
            self._remote_keys.discard(key)
        self.transport.send(self.active_device, "key", key=key, pressed=pressed)

    # ------------------------------------------------------------------ gaze
    def set_gaze_paused(self, paused: bool) -> None:
        if paused == self.gaze_paused:
            return
        self.gaze_paused = paused
        log.info("gaze switching %s", "paused" if paused else "resumed")
        self.on_gaze_paused(paused)

    def on_gaze(self, target: Target) -> None:
        if not self.settings.gaze_switching or self.gaze_paused:
            return
        if self._remote_buttons:
            return  # never yank the pointer away mid-drag
        if self.clock() - self._moved_at < GAZE_HOLD_S:
            return  # the mouse is moving: it decides (gaze is asked again shortly)
        try:
            screen = self.layout.screen(target.device_id, target.screen_id)
        except KeyError:
            return
        if not self.available(screen.device_id):
            return
        if self.jumps_to(screen) and not target.guess:
            # A jump monitor: the pointer goes to that very monitor, where
            # it last was on it (or its middle the first time).
            if screen == self.screen:
                return
            spot = self._screen_pos.get((screen.device_id, screen.id))
            if spot is None or not screen.contains(*spot):
                spot = screen.center
            self.switch_to(screen, *spot, "gaze")
            return
        if screen.device_id == self.active_device:
            return  # another monitor of this device: gaze picks devices
        # Gaze picks the device, not the monitor: the pointer comes back
        # wherever you left it on that device, even if your eyes land on
        # another of its screens. Only a device never visited starts on the
        # screen you looked at.
        last = self._last_pos.get(screen.device_id)
        if last is not None:
            here = self.layout.screen_at(*last, screen.device_id)
            if here is not None:
                self.switch_to(here, *last, "gaze")
                return
        self.switch_to(screen, *screen.center, "gaze")

    # ------------------------------------------------------- connectivity
    def on_disconnect(self, device_id: str) -> None:
        if self.active_device == device_id:
            self._remote_keys.clear()
            self._remote_buttons.clear()
            self.go_home("disconnect")

    def go_home(self, reason: str) -> None:
        x, y = self._last_pos.get(self.local_id, self.screen_for_home().center)
        screen = self.layout.screen_at(x, y, self.local_id) or self.screen_for_home()
        self.switch_to(screen, x, y, reason)

    def screen_for_home(self) -> Screen:
        return next(iter(self.layout.screens(self.local_id)))

    # ---------------------------------------------------------- switching
    def switch_to(self, screen: Screen, x: float, y: float, reason: str) -> None:
        old = self.active_device
        new = screen.device_id
        if old == new and screen == self.screen and reason != "edge":
            return  # already there: no re-entry, no animation
        if old == self.local_id and new != self.local_id:
            why = self.blocked()
            if why:
                if why != self._blocked_said:
                    log.warning("not switching to %s: %s", new, why)
                    self._blocked_said = why
                return
        self._last_pos[old] = (self.x, self.y)
        self._screen_pos[(self.screen.device_id, self.screen.id)] = (self.x, self.y)
        effect = self.settings.effect
        # Where the pointer leaves from: the animation plays there too, so you
        # see it cut across the edge rather than only where it lands.
        exit_x, exit_y = self.screen.to_os(self.x, self.y)

        if old != new:
            if old == self.local_id:
                self._release_local_keys()
                self.overlay.show(effect, exit_x, exit_y)
            elif self.transport.is_connected(old):
                for key in sorted(self._remote_keys):
                    self.transport.send(old, "key", key=key, pressed=False)
                for button in sorted(self._remote_buttons):
                    self.transport.send(old, "button", button=button, pressed=False)
                self.transport.send(old, "leave", effect=effect, x=exit_x, y=exit_y)
            self._remote_keys.clear()
            self._remote_buttons.clear()

        self.active_device, self.screen, self.x, self.y = new, screen, x, y
        if old != new:
            self._arrived = (self.clock(), old)
        ox, oy = screen.to_os(x, y)
        if new == self.local_id:
            if old != new:
                self.capture.set_grab(False)
                self._edge_armed = reason == "edge"
            self.local_sink.move_to(ox, oy)
            if old != new:
                self.overlay.show(effect, ox, oy)
        else:
            if old == self.local_id:
                self.capture.set_grab(True)
            if old != new:
                # Same as coming home: by gaze the pointer comes back where it
                # was left, often on the very edge it left by, and the next
                # nudge used to send input straight back (and blink twice).
                self._edge_armed = reason == "edge"
                self.transport.send(new, "enter", x=ox, y=oy, effect=effect)
            else:
                self.transport.send(new, "move", x=ox, y=oy)

        log.info("input -> %s/%s (%s)", new, screen.id, reason)
        event = SwitchEvent(old, new, screen, reason)
        for listener in self.listeners:
            listener(event)

    def _release_local_keys(self) -> None:
        # The key-up for anything held now would be swallowed by the grab and
        # leave e.g. Ctrl stuck down on the host. Release them ourselves.
        for key in sorted(self._local_keys):
            try:
                self.local_sink.key(key, False)
            except Exception:  # pragma: no cover - best effort
                log.debug("could not release %s", key, exc_info=True)
        self._local_keys.clear()
