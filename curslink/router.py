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
from curslink.pointerfeel import PointerFeel
from curslink.tally import Tally

log = logging.getLogger(__name__)

# Right after input moves to a device, an edge push straight back to the
# device it came from is ignored for this long. A real "oops, back" takes
# longer than this; a bogus first report (see PynputCapture._mac_motion)
# doesn't, and used to bounce input home within 15 ms of arriving.
RETURN_GUARD_S = 0.15
# ...and for this long while the pointer hasn't gone further than this from
# where it landed: a wobble on the join isn't a change of mind.
RETURN_NEAR_S = 0.6
RETURN_NEAR_PX = 12
RETURN_WOBBLE_PX = 6

# While the mouse is moving, the hand is driving: gaze waits until it has
# been still this long. Otherwise a glance mid-move (or gaze beating the
# pointer to the edge) took the pointer to the remembered spot or the middle
# of the screen instead of where it was crossing.
GAZE_HOLD_S = 0.4

# Arriving on another computer by gaze or a hotkey, the pointer comes back
# where it was left, usually just inside the edge it left by. Edge switching
# waits until it has been at least this far from that edge.
ARM_PX = 24

# The pointer pressed against an edge of this computer for this many mouse
# reports without crossing: the log says why, once per edge and reason in
# STOP_SAY_S (Kyle, 0.4.76: "Windows feels like it's forcefully stopping the
# cursor leaving the monitor", and nothing in the log said why).
STOP_REPORTS = 6
STOP_SAY_S = 30.0
# Points tried along an edge to say where it leads (1% steps).
NOWHERE_SAMPLES = 100

# Input coming back to this computer, its cursor is put where it lands; for
# this long, a report nearer the point the cursor was held at while input
# was away (and further than WARP_NEAR_PX from where it landed) is from
# before that move went through, and is dropped. Taken as a real move it
# put the pointer in the middle of the screen: going into a mirror of one
# of this computer's own apps, that's out of the app, straight out again,
# and in, out, in every 10-30 ms (Kyle, 0.4.124: "mouse kept jumping").
WARP_SETTLE_S = 0.25
WARP_NEAR_PX = 48


_SIDE_NAMES = {(-1, 0): "left", (1, 0): "right", (0, -1): "top", (0, 1): "bottom"}


class Transport(Protocol):
    def is_connected(self, device_id: str) -> bool: ...

    def send(self, device_id: str, t: str, **data) -> None: ...


class Overlay(Protocol):
    def show(self, effect: str, x: int, y: int, box: tuple[int, int, int, int] | None = None) -> None: ...


class NullOverlay:
    def show(self, effect: str, x: int, y: int, box: tuple[int, int, int, int] | None = None) -> None:
        pass


@dataclass
class RouterSettings:
    effect: str = "pop"                      # pop | blink | none
    edge_switching: bool = True
    # Near an edge that leads to another device, show where it goes
    # (curslink.edgehint): a bar of light and a label with its name.
    edge_indicator: bool = True
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
    # Mac <-> PC: Cmd on one is Ctrl on the other, so shortcuts stay put.
    swap_cmd_ctrl: bool = True


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
        # The app is setting up (Layout page): no crossing animations.
        self.setup_quiet = False
        self.listeners: list[Callable[[SwitchEvent], None]] = []
        # Told whenever gaze switching is paused or resumed.
        self.on_gaze_paused: Callable[[bool], None] = lambda paused: None
        # Why input can't leave this device right now ("" = it can). On
        # macOS without Accessibility/Input Monitoring nothing is captured,
        # so "switching" would only move the label, not the mouse.
        self.blocked: Callable[[], str] = lambda: ""
        # The key name to send to a device: lets the app swap Cmd and Ctrl
        # between a Mac and a PC (see inputs.keys.swap_for).
        self.key_for: Callable[[str, str], str] = lambda device_id, key: key
        # File drags leaving another device for this one (curslink.files.handoff):
        # can_drag_out(device) says whether it can hand its drag over, and
        # drag_out(device) asks it to, instead of letting go of its left button.
        self.can_drag_out: Callable[[str], bool] = lambda device_id: False
        self.drag_out: Callable[[str], None] = lambda device_id: None
        # carries_drag(from, to): a drag held on device ``from`` can go on on
        # ``to`` (an Isolink floating chat, see curslink.popups), so gaze may
        # move input mid-drag.
        self.carries_drag: Callable[[str, str], bool] = lambda from_device, to_device: False
        # takes_drag(device): another agent can take a file drag handed over
        # by the one input leaves (it shows the card, and the files go there
        # if it's let go of there), so drag_out is asked for agent to agent too.
        self.takes_drag: Callable[[str], bool] = lambda device_id: False
        self._blocked_said = ""
        # App mirror (curslink.appmirror.AppMirrorHost): where the pointer
        # goes into and out of mirrored apps (hop), how fast it moves in one
        # (scale), whether a device holds the pointer from gaze (holds), and
        # whether a held crossing from a device is a window being dragged
        # off it (window_drag) rather than a file. None: no App mirror.
        self.portal = None
        # Every button of the host's mouse, wherever input is (App mirror
        # watches the left one).
        self.on_any_button: Callable[[str, bool], None] = lambda button, pressed: None
        # After every change of where the pointer is (any device, any
        # screen): mirror mode follows the screen in use and the pointer on it.
        self.on_moved: Callable[[], None] = lambda: None
        # Where the pointer is now (device, global x, y), after each move: the
        # edge indicator (curslink.edgehint) follows it.
        self.on_position: Callable[[str, float, float], None] = lambda device_id, x, y: None
        # While the Layout page calibrates an edge or an overlap: left clicks
        # (and Esc, Backspace, Tab) on another computer come here instead of going to it.
        self.picker: Callable[[dict], None] | None = None
        # After arriving by gaze or a hotkey (home or on another device) the pointer may land right on
        # the edge it left by; it has to leave that edge before an edge push
        # counts again, or the next pointer event bounces input straight back.
        self._edge_armed = True
        # Why the last switch home wasn't by an edge (see _edge_armed).
        self._armed_why = ""
        # The pointer pressed against an edge here: (where, reports so far).
        self._pressing: tuple[tuple[float, float, tuple], int] | None = None
        self._stop_said: dict[tuple, float] = {}
        # Windows: the rectangle an app holds the cursor in (ClipCursor), in
        # OS pixels, or None when the cursor is free. Set by the app.
        self.cursor_clip: Callable[[], tuple[int, int, int, int] | None] = lambda: None
        # Carrying a held chat onto the screen in front (mirror mode) waits
        # until the pointer has been ARM_PX clear of the part it covers, or
        # is ARM_PX inside it: see _carried_to_front.
        self._carry_armed = True
        self.clock: Callable[[], float] = time.monotonic
        self._arrived: tuple[float, str] | None = None  # (when, from which device)
        # Input came back here: (x, y) the cursor was put at, when, and where
        # it was held while away (see WARP_SETTLE_S).
        self._warped: tuple | None = None
        self._arrived_at: tuple[float, float] | None = None  # where it landed then
        self._moved_at = -1e9  # when the mouse last moved (self.clock())
        self._scrolls = Tally("scroll sent to the other computer")
        # How far the pointer goes on another device for the host's mouse
        # motion ([pointer]): its speed, or the feel of that device's own mouse.
        self.feel = PointerFeel()

        first = next(iter(layout.screens(local_device_id)), None)
        if first is None:
            raise ValueError(f"local device {local_device_id!r} has no screens in the layout")
        self.active_device = local_device_id
        self.screen = first
        self.x, self.y = first.center
        self.gaze_paused = False
        # Devices turned off on the Layout page ([[devices]] enabled = false).
        self.turned_off: set[str] = {d.id for d in layout.devices.values() if not d.enabled and d.id != local_device_id}
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
        if device_id == self.local_id:
            return True
        return device_id not in self.turned_off and self.transport.is_connected(device_id)

    def set_device_enabled(self, device_id: str, on: bool) -> None:
        """Turn a device off (the Layout page): it stays connected and laid
        out, but no edge, gaze or app mirror takes input to it. Input on it
        right now comes home."""
        if device_id == self.local_id or on == (device_id not in self.turned_off):
            return
        if on:
            self.turned_off.discard(device_id)
        else:
            self.turned_off.add(device_id)
        log.info("%s turned %s", device_id, "on" if on else "off")
        if not on and self.active_device == device_id:
            self._remote_keys.clear()
            self._remote_buttons.clear()
            self.go_home("turned off")
        self.on_moved()

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
    def _stale(self, ox: float, oy: float) -> bool:
        """A local report from before the cursor was put where input came
        back to (see WARP_SETTLE_S)."""
        if self._warped is None:
            return False
        wx, wy, when, park = self._warped
        if self.clock() - when > WARP_SETTLE_S:
            self._warped = None
            return False
        to_here = max(abs(ox - wx), abs(oy - wy))
        if to_here <= WARP_NEAR_PX:
            self._warped = None  # the move went through: everything after is real
            return False
        if park is None:
            return False
        return max(abs(ox - park[0]), abs(oy - park[1])) < to_here

    def on_pointer(self, ox: float, oy: float) -> None:
        if not self.is_local:
            return
        if self._stale(ox, oy):
            return
        screen = self.layout.screen_at_os(self.local_id, ox, oy)
        if screen is None:
            # Past the edge of every screen here (Windows' mouse hook reports
            # where a fast move was headed, before the cursor stops on the
            # edge): that's a push on the edge it stops on. Dropping it left
            # a quick flick at the edge doing nothing, as if Windows held the
            # cursor back.
            near = self.layout.nearest_os(self.local_id, ox, oy)
            if near is None:
                return
            screen, ox, oy = near
        x, y = screen.from_os(ox, oy)
        self._moved_at = self.clock()
        if self.portal is not None:
            hop = self.portal.hop(self.local_id, screen, x, y)
            if hop is None and getattr(self.portal, "inside", None) is not None \
                    and self.portal.inside(self.local_id, x, y) is not None:
                # In a mirror of one of this computer's own apps: the mirror's
                # edges are the app's, not this screen's. Pushing on this
                # screen's edge (the window against it) is moving out of the
                # app that way; nothing else here (edge switching, the screens
                # in front) applies (it took the pointer off to wherever this
                # screen's edge leads, and straight back into the mirror).
                for ex, ey in self.layout.edge_pushes(self.local_id, x, y):
                    hop = self.portal.hop(self.local_id, screen, x + 2 * ex, y + 2 * ey)
                    if hop is not None:
                        break
                if hop is None:
                    self._set_screen(screen)
                    self.x, self.y = x, y
                    self.on_moved()
                    self.on_position(self.local_id, x, y)
                    return
            if hop is not None and hop[0].device_id == self.local_id:
                # Held in the app, at its edge: the cursor goes back there.
                screen, x, y = hop
                self.local_sink.move_to(*screen.to_os(x, y))
            elif hop is not None:
                self.switch_to(*hop, "app")
                return
        if self.settings.edge_switching:
            carried = self._carried_to_front(screen, x, y)
            if carried is not None:
                self.switch_to(*carried, "edge")
                return
            front = self.layout.covered_by(self.local_id, x, y)
            if front is not None:
                # The real cursor went into a part of this screen that another
                # device's screen stands in front of: that's where it is now.
                if self.available(front.device_id) and self._edge_armed:
                    self.switch_to(front, *front.clamp(x, y), "edge")
                return
        self._set_screen(screen)
        self.x, self.y = x, y
        self.on_moved()
        self.on_position(self.local_id, x, y)
        if not self.settings.edge_switching:
            return
        pushes = self.layout.edge_pushes(self.local_id, self.x, self.y)
        if not self._edge_armed:
            self._edge_armed = not pushes
            if pushes:
                self._pressed(screen, pushes, "edge switching waits for the pointer to come off this edge first "
                                              f"(it came back here {self._armed_why or 'without crossing an edge'})")
            else:
                self._pressing = None
            return
        offline = ""
        for ex, ey in pushes:
            crossing = self.layout.find_crossing(
                self.local_id, self.x, self.y, ex, ey, self.settings.gap_tolerance_px
            )
            if crossing and self.available(crossing.screen.device_id):
                self.switch_to(crossing.screen, crossing.x, crossing.y, "edge")
                return
            if crossing:
                offline = (f"{crossing.screen.device_id} is turned off" if crossing.screen.device_id in self.turned_off
                           else f"{crossing.screen.device_id} isn't connected")
        if pushes:
            self._pressed(screen, pushes, offline or (lambda: self._nowhere(screen, pushes)))
        else:
            self._pressed(screen, (), "")

    def _pressed(self, screen: Screen, pushes, why) -> None:
        """The local pointer reported at (self.x, self.y): count reports in a
        row at the same spot (the mouse moving, the cursor not), and after
        STOP_REPORTS say why it isn't crossing (``why``: a reason, or a
        function that works it out). Off an edge, that's an app
        holding the cursor in a box (ClipCursor), or nothing to say."""
        spot = (round(self.x), round(self.y), tuple(pushes))
        if self._pressing is None or self._pressing[0][2] != spot[2] or (
                not pushes and self._pressing[0][:2] != spot[:2]):
            self._pressing = (spot, 1)
            return
        count = self._pressing[1] + 1
        self._pressing = (spot, count)
        if count != STOP_REPORTS:
            return
        if callable(why):
            # Working out why is slow (hundreds of crossings tried along the
            # edge: 2-3 s of the PC host's loop in 0.4.101, every time the
            # pointer rested on an edge). Only when it'll be said.
            spot_key = (screen.device_id, screen.id, spot[2])
            now = self.clock()
            if now - self._stop_said.get(spot_key, -1e9) < STOP_SAY_S:
                return
            self._stop_said[spot_key] = now
            why = why()
        if not pushes:
            clip = self.cursor_clip()
            if clip is None:
                return
            why = (f"an app holds the cursor inside {clip[2]}x{clip[3]} at {clip[0]},{clip[1]} "
                   "(ClipCursor), so it can't reach the edge of the screen")
        sides = ", ".join(_SIDE_NAMES[p] for p in pushes) or "no"
        key = (screen.device_id, screen.id, spot[2], why)
        now = self.clock()
        if now - self._stop_said.get(key, -1e9) < STOP_SAY_S:
            return
        self._stop_said[key] = now
        ox, oy = screen.to_os(self.x, self.y)
        log.info("edge: the pointer is pushing on the %s edge of %s/%s at (%d, %d) and stays here: %s",
                 sides, screen.device_id, screen.id, ox, oy, why)

    def _nowhere(self, screen: Screen, pushes) -> str:
        """Why pushing on ``pushes`` from (self.x, self.y) leads nowhere: the
        parts of those edges that do lead somewhere, if any."""
        parts = []
        for ex, ey in pushes:
            along_x = ex == 0
            length = screen.width if along_x else screen.height
            step = max(1, length // NOWHERE_SAMPLES)
            runs: list[list] = []
            for i in range(0, length, step):
                if along_x:
                    px, py = screen.x + i, (screen.y if ey < 0 else screen.bottom - 1)
                else:
                    px, py = (screen.x if ex < 0 else screen.right - 1), screen.y + i
                c = self.layout.find_crossing(self.local_id, px, py, ex, ey, self.settings.gap_tolerance_px)
                to = f"{c.screen.device_id}/{c.screen.id}" if c else None
                if runs and runs[-1][0] == to:
                    runs[-1][2] = i
                else:
                    runs.append([to, i, i])
            leads = [f"{to} from {a * 100 // length}% to {min(100, (b + step) * 100 // length)}%"
                     for to, a, b in runs if to]
            name = _SIDE_NAMES[(ex, ey)]
            where = "left to right" if along_x else "top to bottom"
            parts.append(f"the {name} edge leads on only to {', '.join(leads)} of the way {where}"
                         if leads else f"nothing in the layout is beyond the {name} edge")
        return "; ".join(parts) + " (move the screens on the Layout page to change that)"

    def on_motion(self, dx: float, dy: float, ts: float | None = None) -> None:
        """``ts``: when the motion was captured (time.monotonic()). Sent with
        the move so the agent can play moves back as evenly as they happened
        (see curslink.smoothing)."""
        if self.is_local:
            return
        if dx or dy:
            self._moved_at = self.clock()
            dx, dy = self.feel.scale(self.active_device, dx, dy, self.clock() if ts is None else ts)
            if self.portal is not None:
                kx, ky = self.portal.scale(self.active_device)
                dx, dy = dx * kx, dy * ky
        nx, ny = self.x + dx, self.y + dy
        here = self.layout.screen_at(nx, ny, self.active_device, prefer=self.screen)
        if self.portal is not None and (dx or dy):
            hop = self.portal.hop(self.active_device, here, nx, ny)
            if hop is not None and hop[0].device_id == self.active_device:
                here, nx, ny = hop  # held in the app, at its edge
            elif hop is not None:
                self.switch_to(*hop, "app")
                return
            if here is None and hasattr(self.portal, "inside"):
                # In a mirrored app's window, past its screen's edge: still in
                # the app (the mirror can be bigger than its screen), never
                # out to the device beyond or stopped at the edge.
                here = self.portal.inside(self.active_device, nx, ny) or None
        if here is None and self.settings.edge_switching:
            crossing = self.layout.find_crossing(
                self.active_device, self.x, self.y, dx, dy, self.settings.gap_tolerance_px
            )
            if crossing and (self._just_came_from(crossing.screen.device_id, dx, dy) or not self._edge_armed):
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
        if self.settings.edge_switching:
            carried = self._carried_to_front(here, nx, ny)
            if carried is not None:
                self.switch_to(*carried, "edge")
                return
        self._set_screen(here)
        self.x, self.y = nx, ny
        self.on_moved()
        self.on_position(self.active_device, nx, ny)
        if not self._edge_armed:
            self._edge_armed = not self.layout.edge_pushes(self.active_device, nx, ny, ARM_PX)
        ox, oy = here.to_os(nx, ny)
        if ts is None:
            self.transport.send(self.active_device, "move", x=ox, y=oy)
        else:
            self.transport.send(self.active_device, "move", x=ox, y=oy, ts=round(ts * 1000.0, 2))

    def _carried_to_front(self, here, x: float, y: float):
        """Mirror mode keeps the pointer on the screen behind where another
        device's screen covers it (that one shows it). But holding something
        that can go on there (an Isolink chat), it goes to the screen in
        front, as when that part isn't mirrored: ``(screen, x, y)`` there."""
        if not self.layout.pointer_through:
            return None
        parts = [p for p in self.layout.covered_parts(here) if p[0].device_id != here.device_id]
        if not self._carry_armed:
            # Just landed beside the covered part (crossing an edge of the
            # screen in front lands a pixel or two from it): a wobble of the
            # hand mustn't carry the chat straight back. Kyle, 0.4.76: chats
            # dragged Windows -> Mac came back to the PC within 100 ms.
            if all(_outside_by(x, y, rect) >= ARM_PX for _f, rect, _t in parts):
                self._carry_armed = True
        for front, (bx, by, bw, bh), (tx, ty, tw, th) in parts:
            if not (bx <= x < bx + bw and by <= y < by + bh):
                continue
            if not self._carry_armed and _inside_by(x, y, (bx, by, bw, bh)) < ARM_PX:
                return None
            if not (self.available(front.device_id) and self.carries_drag(here.device_id, front.device_id)):
                return None
            fx, fy = tx + (x - bx) * tw / max(1, bw), ty + (y - by) * th / max(1, bh)
            return (front, *front.clamp(fx, fy))
        return None

    def _near_covered(self, screen, x: float, y: float) -> bool:
        """Is ``(x, y)`` on ``screen`` within ARM_PX of a part of it that
        another device's screen covers (mirror mode)?"""
        if not self.layout.pointer_through:
            return False
        return any(front.device_id != screen.device_id and _outside_by(x, y, rect) < ARM_PX
                   for front, rect, _t in self.layout.covered_parts(screen))

    def _hands_drag_to(self, from_device: str, to_device: str) -> bool:
        """A file drag held on agent ``from_device`` goes on on ``to_device``
        (this computer, or another agent that takes files), rather than
        being let go of where it is."""
        if from_device == to_device or from_device == self.local_id or not self.can_drag_out(from_device):
            return False
        return to_device == self.local_id or self.takes_drag(to_device)

    def _overlapping(self, a, b) -> bool:
        """Is one of these screens in front of the other (different devices)?"""
        if a is None or b is None or a.device_id == b.device_id or not self.layout.allow_overlap:
            return False
        return a.overlap(b) is not None

    def _just_came_from(self, device_id: str, dx: float = 0.0, dy: float = 0.0) -> bool:
        """Did input only just arrive from ``device_id``? For RETURN_GUARD_S
        always; for RETURN_NEAR_S while the pointer is still within
        RETURN_NEAR_PX of where it landed and the push back (``dx, dy``) is
        no more than RETURN_WOBBLE_PX: a hand wobbling on the join it just
        crossed went back and forth three times in 370 ms. A real pull back
        is a bigger move, and a slow one gets through once the time is up."""
        if self._arrived is None:
            return False
        when, source = self._arrived
        if source != device_id:
            return False
        since = self.clock() - when
        if since < RETURN_GUARD_S:
            return True
        at = self._arrived_at
        return (since < RETURN_NEAR_S and at is not None and max(abs(dx), abs(dy)) <= RETURN_WOBBLE_PX
                and abs(self.x - at[0]) <= RETURN_NEAR_PX and abs(self.y - at[1]) <= RETURN_NEAR_PX)

    def on_button(self, button: str, pressed: bool) -> None:
        try:
            self.on_any_button(button, pressed)
        except Exception:  # pragma: no cover - never lose a click to it
            log.exception("button hook")
        if self.is_local:
            return
        self._moved_at = self.clock()  # clicking counts as using the mouse
        if self.picker is not None and button == "left" and button not in self._remote_buttons:
            if pressed:
                self.picker({"device": self.active_device, "screen": self.screen.id,
                             "x": round(self.x - self.screen.x, 1), "y": round(self.y - self.screen.y, 1)})
            return
        if pressed:
            self._remote_buttons.add(button)
        else:
            self._remote_buttons.discard(button)
        # Where and when it happened, as the moves say it: a press goes by TCP
        # and can overtake the last moves (UDP) before it, which put it a few
        # pixels short, on the row above a file in Finder or on the text beside
        # it; and a Mac counts double clicks from the time between presses,
        # which the network can bunch up (see PynputSink.button_at).
        ox, oy = self.screen.to_os(self.x, self.y)
        self.transport.send(self.active_device, "button", button=button, pressed=pressed,
                            x=ox, y=oy, ts=round(self.clock() * 1000.0, 2))

    def hold_remote_button(self, button: str) -> None:
        """The device input is on now pressed ``button`` itself (to carry on a
        drag that came from elsewhere): let go of it there like a button
        pressed from here, on the release or when input moves on."""
        if not self.is_local:
            self._remote_buttons.add(button)

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
        if self.picker is not None and key in ("esc", "backspace", "tab") and key not in self._remote_keys:
            if pressed:
                self.picker({"key": key})
            return
        if pressed:
            self._remote_keys.add(key)
        else:
            self._remote_keys.discard(key)
        out = self.key_for(self.active_device, key)
        if out:  # "" is a key that computer has nothing for (a Mac's fn on a PC)
            self.transport.send(self.active_device, "key", key=out, pressed=pressed)

    # ------------------------------------------------------------------ gaze
    def set_gaze_paused(self, paused: bool) -> None:
        if paused == self.gaze_paused:
            return
        self.gaze_paused = paused
        log.info("gaze switching %s", "off" if paused else "on")
        self.on_gaze_paused(paused)

    def on_gaze(self, target: Target) -> None:
        if not self.settings.gaze_switching or self.gaze_paused:
            return
        if self._remote_buttons and not (
                self._remote_buttons == {"left"} and (
                    self._hands_drag_to(self.active_device, target.device_id)
                    or self.carries_drag(self.active_device, target.device_id))):
            return  # never yank the pointer away mid-drag (unless it carries a file home, or a chat)
        if self.clock() - self._moved_at < GAZE_HOLD_S:
            return  # the mouse is moving: it decides (gaze is asked again shortly)
        if self.portal is not None and self.portal.holds(target.device_id):
            return  # looking at the mirror of the app the pointer is in
        try:
            screen = self.layout.screen(target.device_id, target.screen_id)
        except KeyError:
            return
        if not self.available(screen.device_id):
            return
        if self._overlapping(screen, self.screen):
            # A screen in front of another: both are in the same direction,
            # so a face turned that way says nothing about which one is
            # meant. The pointer went where it is by hand; gaze pulling it
            # back to the other of the pair was Kyle's "the overlap edge
            # quits and the mouse bounces back" (0.4.114, gaze on).
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

    def go_home(self, reason: str, middle: bool = False) -> None:
        """Input back to this computer, where it left it (or with ``middle``,
        in the middle of its main screen: the safety nets, so the pointer
        is easy to find)."""
        home = self.screen_for_home().center
        x, y = home if middle else self._last_pos.get(self.local_id, home)
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
        if new in self.turned_off:
            return  # turned off on the Layout page (whoever asked: edge, gaze, app mirror)
        if old == self.local_id and new != self.local_id:
            why = self.blocked()
            if why:
                if why != self._blocked_said:
                    log.warning("not switching to %s: %s", new, why)
                    self._blocked_said = why
                return
        left_at = (self.x, self.y)
        self._last_pos[old] = left_at
        self._screen_pos[(self.screen.device_id, self.screen.id)] = left_at
        # Into and out of a mirrored app (curslink.appmirror): no animation,
        # it's one picture as far as the eye goes.
        effect = "none" if self.setup_quiet or reason == "app" else self.settings.effect
        # Where the pointer leaves from. Nothing plays there (the edge glow
        # shows the crossing); the animation only marks where it lands.
        exit_x, exit_y = self.screen.to_os(self.x, self.y)

        if old != new:
            if old == self.local_id:
                self._release_local_keys()
            elif self.transport.is_connected(old):
                for key in sorted(self._remote_keys):
                    out = self.key_for(old, key)
                    if out:
                        self.transport.send(old, "key", key=out, pressed=False)
                # A drag leaving that device (for this one, or for another that
                # takes files) may be a file: the device finds out and ends its
                # drag itself, rather than let go of the button wherever its
                # pointer stopped.
                # (A window dragged by its title bar isn't: App mirror casts it.)
                dragging_out = ("left" in self._remote_buttons and self._hands_drag_to(old, new)
                                and not (self.portal is not None and self.portal.window_drag(old)))
                for button in sorted(self._remote_buttons):
                    if not (dragging_out and button == "left"):
                        self.transport.send(old, "button", button=button, pressed=False)
                if dragging_out:
                    self.drag_out(old)
                    self.transport.send(old, "leave", effect="none", x=exit_x, y=exit_y)
                elif reason == "app":
                    # Into a mirrored app from the screen showing it: its
                    # cursor stays where it is, under the app's pointer drawn
                    # there (parked at the edge, macOS flashed it into view).
                    self.transport.send(old, "leave", effect="none", x=exit_x, y=exit_y)
                else:
                    # Its own cursor stays behind, hidden but still hovering:
                    # left by an edge it sits on title bar buttons and taskbar
                    # icons whose tooltips keep popping up. Put it where little
                    # hovers (the middle of that screen's right edge).
                    park = self.screen.to_os(self.screen.right - 1, self.screen.y + self.screen.height / 2)
                    self.transport.send(old, "leave", effect="none", x=exit_x, y=exit_y, park=list(park))
            self._remote_keys.clear()
            self._remote_buttons.clear()

        self.active_device, self.screen, self.x, self.y = new, screen, x, y
        if old != new:
            self._arrived = (self.clock(), old)
            self._arrived_at = (x, y)
        self._carry_armed = not self._near_covered(screen, x, y)
        ox, oy = screen.to_os(x, y)
        if new == self.local_id:
            if old != new:
                self.capture.set_grab(False)
                self._edge_armed = reason == "edge"
                self._armed_why = f"by {reason}"
                self._pressing = None
            self.local_sink.move_to(ox, oy)
            hide = getattr(self.capture, "set_cursor_hidden", None)
            if hide is not None:
                # In one of this computer's own apps through its mirror on
                # another screen: the pointer is drawn there; here it's hidden.
                hide(reason == "app" and getattr(self.portal, "through", None) is not None)
            if old != new:
                park = getattr(self.capture, "park_at", None)
                self._warped = (ox, oy, self.clock(), tuple(park) if park is not None else None)
            if old != new:
                # The screen as the layout has it: the helper places the
                # animation as a fraction of it (see curslink.overlay.screen_box).
                self.overlay.show(effect, ox, oy, (screen.os_x, screen.os_y, screen.width, screen.height))
        else:
            if old == self.local_id:
                self.capture.set_grab(True)
            if old != new:
                # Same as coming home: by gaze the pointer comes back where it
                # was left, often on the very edge it left by, and the next
                # nudge used to send input straight back (and blink twice).
                self._edge_armed = reason == "edge"
                # With the screen as the layout has it, in the same OS pixels:
                # the agent places the landing effect as a fraction of it, as
                # here (its own screen list can be in other pixels, Windows scaling).
                self.transport.send(new, "enter", x=ox, y=oy, effect=effect,
                                    box=[screen.os_x, screen.os_y, screen.width, screen.height])
            else:
                self.transport.send(new, "move", x=ox, y=oy)

        log.info("input -> %s/%s (%s): left %s at (%.0f, %.0f), lands at (%.0f, %.0f)",
                 new, screen.id, reason, old, left_at[0], left_at[1], ox, oy)
        self.on_moved()
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


def _outside_by(x: float, y: float, rect: tuple[int, int, int, int]) -> float:
    """How far ``(x, y)`` is outside ``rect`` (0 inside it)."""
    bx, by, bw, bh = rect
    dx = max(bx - x, 0.0, x - (bx + bw - 1))
    dy = max(by - y, 0.0, y - (by + bh - 1))
    return max(dx, dy)


def _inside_by(x: float, y: float, rect: tuple[int, int, int, int]) -> float:
    """How far ``(x, y)`` is inside ``rect`` from its nearest edge (0 outside)."""
    bx, by, bw, bh = rect
    return max(0.0, min(x - bx, bx + bw - 1 - x, y - by, by + bh - 1 - y))
