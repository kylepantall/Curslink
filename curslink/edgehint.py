"""The edge indicator: where an edge leads, shown as the pointer gets near it.

When the pointer comes within :data:`RANGE_PX` of a screen edge that leads
to another linked device, that edge glows with a soft bar of light (brightest
beside the pointer, fading out at both ends) and a small glass label shows
the device's icon and name. The same bar rises on the facing edge of the
screen the pointer is heading for, so you can see where it's about to land.
Crossing flashes both bars and lets them fade.

This module is the platform free part: :func:`find_hint` works out which
edge is near and where it leads, and :class:`EdgeIndicator` turns the
router's pointer positions into ``edge`` commands for each device's overlay
helper (the host's own, or an agent's over the link). The drawing is in
:mod:`curslink.edgeoverlay` (desktop) and the Android app's ``EdgeView``.

An ``edge`` command (a JSON object) is one of:

    {"hide": true}                     take it down (fades out)
    {"flash": true}                    the pointer just crossed: flash, then fade
    {"role": "from" | "to",            "from": the screen the pointer is on
                                       (bar and label); "to": the screen it's
                                       heading for (bar only)
     "rect": [x, y, width, height],    that screen, in its device's OS pixels
     "side": "left" | "right" | "top" | "bottom",
     "a": 0, "b": 1080,                the stretch of that side that leads
                                       across, pixels from the screen's top
                                       (left/right sides) or left (top/bottom)
     "at": 540,                        where the pointer is (or will arrive)
                                       along that side, same units
     "near": 0.0 .. 1.0,               how close: 1 is at the edge
     "name": "WKS82261",               the device across ("from" only)
     "kind": "mac" | "windows" | "linux" | "phone"}
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Callable

from curslink.layout import OPPOSITE, Layout, Screen

log = logging.getLogger(__name__)

# The indicator shows when the pointer is this close to a linked edge
# (pixels, the device's own), and is at full strength from FULL_PX in.
RANGE_PX = 140
FULL_PX = 18
# Just after crossing, the pointer sits on the edge it came in by, which
# leads straight back: that edge stays dark until the pointer has moved this
# far away from it.
REARM_PX = 60
# Commands are only sent again when something moved at least this much, so
# a pointer gliding along at 1000 Hz doesn't flood the link.
STEP_PX = 3
STEP_NEAR = 0.04

_PUSH = {"left": (-1, 0), "right": (1, 0), "top": (0, -1), "bottom": (0, 1)}


@dataclass(frozen=True)
class EdgeHint:
    """A linked edge the pointer is near, and where it leads."""

    here: Screen
    side: str
    a: float          # the stretch along ``here``'s side that leads across
    b: float
    at: float         # the pointer, along that side
    distance: float   # pixels from the edge
    target: Screen
    target_side: str
    ta: float         # the matching stretch on the target's facing side
    tb: float
    tat: float        # where the pointer would arrive, along that side

    @property
    def near(self) -> float:
        """0 at the edge of the range, 1 at (or within FULL_PX of) the edge."""
        span = RANGE_PX - FULL_PX
        return max(0.0, min(1.0, (RANGE_PX - self.distance) / span))


def _length(screen: Screen, side: str) -> int:
    return screen.height if side in ("left", "right") else screen.width


def _along(screen: Screen, side: str, x: float, y: float) -> float:
    return y - screen.y if side in ("left", "right") else x - screen.x


def _distance(screen: Screen, side: str, x: float, y: float) -> float:
    if side == "left":
        return x - screen.x
    if side == "right":
        return screen.right - 1 - x
    if side == "top":
        return y - screen.y
    return screen.bottom - 1 - y


def _edge_point(screen: Screen, side: str, x: float, y: float) -> tuple[float, float]:
    if side == "left":
        return float(screen.x), y
    if side == "right":
        return float(screen.right - 1), y
    if side == "top":
        return x, float(screen.y)
    return x, float(screen.bottom - 1)


def _stretch(layout: Layout, here: Screen, side: str, at: float, target: Screen,
             ) -> tuple[float, float, str, float, float]:
    """The stretch of ``here``'s side that leads to ``target``, and the
    matching stretch of the target's side: an edge link's, or else where the
    two screens line up."""
    for link in layout.links:
        for mine, other in link.ends():
            if (mine.device_id, mine.screen_id, mine.side) != (here.device_id, here.id, side):
                continue
            if (other.device_id, other.screen_id) != (target.device_id, target.id) or not mine.covers(at):
                continue
            lo, hi = sorted((mine.start, mine.end))
            # other.start lines up with mine.start: keep them in the same order.
            ta, tb = (other.start, other.end) if mine.start <= mine.end else (other.end, other.start)
            return lo, hi, other.side, ta, tb
    target_side = OPPOSITE[side]
    if side in ("left", "right"):
        lo, hi = max(here.y, target.y), min(here.bottom, target.bottom)
        if hi <= lo:  # only bridged across a gap at a corner: just the near bit
            return 0.0, float(here.height), target_side, 0.0, float(target.height)
        return lo - here.y, hi - here.y, target_side, lo - target.y, hi - target.y
    lo, hi = max(here.x, target.x), min(here.right, target.right)
    if hi <= lo:
        return 0.0, float(here.width), target_side, 0.0, float(target.width)
    return lo - here.x, hi - here.x, target_side, lo - target.x, hi - target.x


def find_hint(layout: Layout, device_id: str, x: float, y: float,
              available: Callable[[str], bool], gap_tolerance: int = 0) -> EdgeHint | None:
    """The nearest edge within :data:`RANGE_PX` of (x, y) that leads to
    another available device, or None."""
    here = layout.screen_at(x, y, device_id)
    if here is None:
        return None
    best: EdgeHint | None = None
    for side, (dx, dy) in _PUSH.items():
        distance = _distance(here, side, x, y)
        if distance > RANGE_PX or (best is not None and distance >= best.distance):
            continue
        ex, ey = _edge_point(here, side, x, y)
        crossing = layout.find_crossing(device_id, ex, ey, dx, dy, gap_tolerance)
        if crossing is None or not available(crossing.screen.device_id):
            continue
        at = _along(here, side, ex, ey)
        a, b, target_side, ta, tb = _stretch(layout, here, side, at, crossing.screen)
        tat = _along(crossing.screen, target_side, crossing.x, crossing.y)
        best = EdgeHint(here, side, a, b, at, max(0.0, distance), crossing.screen, target_side, ta, tb, tat)
    return best


def device_kind(platform: str) -> str:
    """``hello.platform`` (or sys.platform) -> the icon to show."""
    p = (platform or "").lower()
    if p.startswith("android") or "phone" in p or p.startswith("ios"):
        return "phone"
    if p.startswith("darwin") or p.startswith("mac"):
        return "mac"
    if p.startswith("win"):
        return "windows"
    return "linux"


def _rect(screen: Screen) -> list[int]:
    return [screen.os_x, screen.os_y, screen.width, screen.height]


class EdgeIndicator:
    """Turns pointer positions into ``edge`` commands for the devices involved.

    ``send(device_id, data)`` delivers one command (a dict, see the module
    docstring) to that device's overlay: the host's own helper or an agent.
    ``name(device_id)`` and ``kind(device_id)`` describe a device for its label.
    """

    def __init__(self, layout: Layout, available: Callable[[str], bool],
                 send: Callable[[str, dict], None], name: Callable[[str], str],
                 kind: Callable[[str], str], gap_tolerance: int = 0):
        self.layout = layout
        self.available = available
        self.send = send
        self.name = name
        self.kind = kind
        self.gap_tolerance = gap_tolerance
        self.enabled = True
        # What each device is showing now: {device_id: last command sent}.
        self._shown: dict[str, dict] = {}
        self._last: EdgeHint | None = None
        # (device, screen, side) the pointer just came in by, kept dark for now.
        self._quiet: tuple[str, str, str] | None = None

    def update(self, device_id: str, x: float, y: float) -> None:
        """The pointer is at global (x, y) on ``device_id``."""
        hint = find_hint(self.layout, device_id, x, y, self.available, self.gap_tolerance) if self.enabled else None
        if self._quiet is not None:
            q_device, q_screen, q_side = self._quiet
            here = self.layout.screen_at(x, y, device_id)
            if q_device != device_id or here is None or here.id != q_screen \
                    or _distance(here, q_side, x, y) >= REARM_PX:
                self._quiet = None
            elif hint is not None and (hint.here.id, hint.side) == (q_screen, q_side):
                hint = None
        self._last = hint
        wanted: dict[str, dict] = {}
        if hint is not None:
            target = hint.target.device_id
            near = round(hint.near, 3)
            wanted[device_id] = {
                "role": "from", "rect": _rect(hint.here), "side": hint.side,
                "a": round(hint.a), "b": round(hint.b), "at": round(hint.at), "near": near,
                "name": self.name(target), "kind": self.kind(target),
            }
            wanted[target] = {
                "role": "to", "rect": _rect(hint.target), "side": hint.target_side,
                "a": round(hint.ta), "b": round(hint.tb), "at": round(hint.tat), "near": near,
            }
        for device in list(self._shown):
            if device not in wanted:
                self._put(device, {"hide": True})
        for device, data in wanted.items():
            if self._changed(self._shown.get(device), data):
                self._put(device, data)

    def crossed(self, to_device: str) -> None:
        """Input just moved to ``to_device``: flash what's showing and let it
        fade, and keep the edge it came in by dark until it moves away."""
        last, self._last = self._last, None
        if last is not None and last.target.device_id == to_device:
            self._quiet = (to_device, last.target.id, last.target_side)
        for device in list(self._shown):
            self._put(device, {"flash": True})

    def clear(self) -> None:
        for device in list(self._shown):
            self._put(device, {"hide": True})

    def forget(self, device_id: str) -> None:
        """That device went away: it isn't showing anything now."""
        self._shown.pop(device_id, None)

    def _put(self, device: str, data: dict) -> None:
        if "role" in data:
            self._shown[device] = data
        else:
            self._shown.pop(device, None)
        try:
            self.send(device, data)
        except Exception:  # pragma: no cover - never let drawing break routing
            log.debug("edge indicator for %s", device, exc_info=True)

    @staticmethod
    def _changed(old: dict | None, new: dict) -> bool:
        if old is None:
            return True
        for key in ("role", "rect", "side", "a", "b", "name", "kind"):
            if old.get(key) != new.get(key):
                return True
        if abs(old["at"] - new["at"]) >= STEP_PX:
            return True
        if abs(old["near"] - new["near"]) >= STEP_NEAR or (new["near"] in (0.0, 1.0) and old["near"] != new["near"]):
            return True
        return False


def command_line(data: dict) -> str:
    """The overlay helper's stdin line for an ``edge`` command."""
    return "edge " + json.dumps(data, separators=(",", ":"))
