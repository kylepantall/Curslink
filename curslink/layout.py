"""Shared "virtual desktop" layout of every device's screens.

All screens, on every device, are placed in one global coordinate space
(pixels). Arranging them like your physical desk is what makes edge
switching work: pushing the pointer off the right edge of your right-hand
monitor lands it on whatever screen sits to its right in this space.

Each screen also records its origin in its own device's OS coordinate
space (``os_x``/``os_y``), which is where that monitor lives in the device's
display settings. That lets us translate a global point into the absolute
coordinates the device's OS expects when we warp its cursor.

Screens may overlap only when the layout allows it (``allow_overlap``), for
a laptop standing in front of part of a monitor. Then the screen in front
(higher ``z``; on a tie, the one listed later) owns the shared area, and the
covered part of the screen behind is blocked out: the pointer can't rest
there, and pushing into it crosses to the screen in front.

With ``pointer_through`` (mirror mode, see :mod:`curslink.mirror`) a covered
part still belongs to the device behind as far as that device is concerned:
the pointer on the screen behind carries on into it (the screen in front
shows it there), and only leaves for the screen in front past the covered
part. The screen in front still owns the area for its own pointer.

Screens are laid out in their own pixels, which aren't the same size on
every monitor, so where a screen in front covers one behind, the layout
only roughly says which part. A :class:`Fit` (calibrated on the real
screens, see :mod:`curslink.gui.fitcalib`) says exactly where the screen in
front stands on the one behind and how big it is there; mirror mode uses it
for what to capture and where to show it (:meth:`Layout.covered_parts`).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable


@dataclass(frozen=True)
class Screen:
    id: str
    device_id: str
    x: int
    y: int
    width: int
    height: int
    os_x: int = 0
    os_y: int = 0
    z: int = 0  # stacking order when screens overlap: higher is in front

    @property
    def right(self) -> int:
        return self.x + self.width

    @property
    def bottom(self) -> int:
        return self.y + self.height

    @property
    def center(self) -> tuple[float, float]:
        return (self.x + self.width / 2, self.y + self.height / 2)

    def contains(self, x: float, y: float) -> bool:
        return self.x <= x < self.right and self.y <= y < self.bottom

    def clamp(self, x: float, y: float) -> tuple[float, float]:
        return (
            min(max(x, self.x), self.right - 1),
            min(max(y, self.y), self.bottom - 1),
        )

    def to_os(self, x: float, y: float) -> tuple[int, int]:
        return (round(x - self.x + self.os_x), round(y - self.y + self.os_y))

    def contains_os(self, ox: float, oy: float) -> bool:
        return (
            self.os_x <= ox < self.os_x + self.width
            and self.os_y <= oy < self.os_y + self.height
        )

    def from_os(self, ox: float, oy: float) -> tuple[float, float]:
        return (ox - self.os_x + self.x, oy - self.os_y + self.y)

    def overlap(self, other: "Screen") -> tuple[int, int, int, int] | None:
        """The shared area ``(x, y, width, height)``, or None if they don't overlap."""
        x0, y0 = max(self.x, other.x), max(self.y, other.y)
        x1, y1 = min(self.right, other.right), min(self.bottom, other.bottom)
        if x0 < x1 and y0 < y1:
            return (x0, y0, x1 - x0, y1 - y0)
        return None


@dataclass(frozen=True)
class Device:
    id: str
    name: str
    screens: tuple[Screen, ...]


SIDES = ("left", "right", "top", "bottom")
OPPOSITE = {"left": "right", "right": "left", "top": "bottom", "bottom": "top"}


@dataclass(frozen=True)
class EdgeEnd:
    """One side of an edge link: a stretch of one screen's edge.

    ``start``/``end`` are pixels along that edge from the screen's top
    (left and right sides) or left (top and bottom sides); ``start`` lines up
    with the other end's ``start``, so either may be the larger.
    """

    device_id: str
    screen_id: str
    side: str
    start: float
    end: float

    def along(self, screen: "Screen", x: float, y: float) -> float:
        return y - screen.y if self.side in ("left", "right") else x - screen.x

    def covers(self, t: float) -> bool:
        lo, hi = sorted((self.start, self.end))
        return lo <= t <= hi

    def fraction(self, t: float) -> float:
        span = self.end - self.start
        return 0.0 if span == 0 else (t - self.start) / span

    def point(self, screen: "Screen", f: float, inset: float) -> tuple[float, float]:
        """The point a fraction ``f`` along this stretch, ``inset`` pixels inside the screen."""
        t = self.start + f * (self.end - self.start)
        if self.side == "left":
            px, py = screen.x + inset, screen.y + t
        elif self.side == "right":
            px, py = screen.right - 1 - inset, screen.y + t
        elif self.side == "top":
            px, py = screen.x + t, screen.y + inset
        else:
            px, py = screen.x + t, screen.bottom - 1 - inset
        return screen.clamp(px, py)


@dataclass(frozen=True)
class EdgeLink:
    """Where two screens of different devices join, set by clicking the
    start and end of the shared stretch on each real monitor. Crossing maps
    the stretch on one onto the other end to end, both ways, so screens of
    different sizes and pixel densities line up the way they sit on the desk."""

    a: EdgeEnd
    b: EdgeEnd

    def ends(self) -> tuple[tuple[EdgeEnd, EdgeEnd], tuple[EdgeEnd, EdgeEnd]]:
        return ((self.a, self.b), (self.b, self.a))


@dataclass(frozen=True)
class Crossing:
    screen: Screen
    x: float
    y: float


@dataclass(frozen=True)
class Fit:
    """Where the screen in front really stands on the screen behind it:
    its top-left corner at (``x``, ``y``) in the screen behind's own pixels
    (from that screen's top-left), and ``scale`` of that screen's pixels to
    one of its own (the two monitors' pixel sizes compared)."""

    behind: tuple[str, str]  # (device, screen)
    front: tuple[str, str]
    x: float
    y: float
    scale: float


def overlap_message(a: Screen, b: Screen) -> str:
    return f"screens {a.device_id}/{a.id} and {b.device_id}/{b.id} overlap"


@dataclass
class Layout:
    devices: dict[str, Device] = field(default_factory=dict)
    allow_overlap: bool = False
    links: tuple[EdgeLink, ...] = ()
    # Mirror mode's pointer: see the module docstring.
    pointer_through: bool = False
    # Mirror mode (curslink.mirror): screens in front show what they cover.
    mirror: bool = False
    # Calibrated overlaps: see Fit.
    fits: tuple[Fit, ...] = ()

    @classmethod
    def from_devices(cls, devices: Iterable[Device], allow_overlap: bool = False,
                     links: Iterable[EdgeLink] = (), pointer_through: bool = False,
                     mirror: bool = False, fits: Iterable[Fit] = ()) -> "Layout":
        layout = cls({d.id: d for d in devices}, allow_overlap, tuple(links), pointer_through, mirror, tuple(fits))
        layout.validate()
        return layout

    def overlaps(self) -> list[tuple[Screen, Screen]]:
        """Every overlapping pair, as ``(behind, in front)``."""
        stack = self._stack()
        out = []
        for i, a in enumerate(stack):
            for b in stack[i + 1 :]:
                if a.overlap(b):
                    out.append((a, b))
        return out

    def _stack(self) -> list[Screen]:
        """All screens, back to front."""
        order = list(self.screens())
        return [s for _, s in sorted(enumerate(order), key=lambda p: (p[1].z, p[0]))]

    def covering(self, screen: Screen) -> list[tuple[Screen, tuple[int, int, int, int]]]:
        """Every screen standing in front of ``screen``, with the part of it
        that one covers (layout pixels), front most last."""
        if not self.allow_overlap:
            return []
        stack = self._stack()
        if screen not in stack:
            return []
        out = []
        for front in stack[stack.index(screen) + 1 :]:
            rect = screen.overlap(front)
            if rect is not None:
                out.append((front, rect))
        return out

    def fit(self, behind: Screen, front: Screen) -> Fit | None:
        for f in self.fits:
            if f.behind == (behind.device_id, behind.id) and f.front == (front.device_id, front.id):
                return f
        return None

    def covered_parts(self, screen: Screen) -> list[tuple[Screen, tuple[int, int, int, int], tuple[int, int, int, int]]]:
        """Like :meth:`covering`, as ``(front, part behind, part in front)``,
        both in layout pixels: the part of ``screen`` that ``front`` covers,
        and where that part is on ``front``. The same rectangle twice unless
        the pair is calibrated (a :class:`Fit`)."""
        out = []
        for front, rect in self.covering(screen):
            f = self.fit(screen, front)
            if f is None:
                out.append((front, rect, rect))
                continue
            k = f.scale
            fx, fy = screen.x + f.x, screen.y + f.y
            x0, y0 = max(screen.x, fx), max(screen.y, fy)
            x1, y1 = min(screen.right, fx + front.width * k), min(screen.bottom, fy + front.height * k)
            if x1 - x0 < 1 or y1 - y0 < 1:
                continue  # it doesn't really cover any of it
            behind = (round(x0), round(y0), round(x1 - x0), round(y1 - y0))
            there = (round(front.x + (x0 - fx) / k), round(front.y + (y0 - fy) / k),
                     max(1, round((x1 - x0) / k)), max(1, round((y1 - y0) / k)))
            out.append((front, behind, there))
        return out

    def in_front(self, a: Screen, b: Screen) -> bool:
        """Is ``a`` drawn in front of ``b``?"""
        stack = self._stack()
        return stack.index(a) > stack.index(b)

    def validate(self) -> None:
        seen: set[tuple[str, str]] = set()
        all_screens = list(self.screens())
        for s in all_screens:
            if s.width <= 0 or s.height <= 0:
                raise ValueError(f"screen {s.device_id}/{s.id} must have a positive size")
            if (s.device_id, s.id) in seen:
                raise ValueError(f"duplicate screen id {s.device_id}/{s.id}")
            seen.add((s.device_id, s.id))
        for link in self.links:
            for end in (link.a, link.b):
                if end.side not in SIDES:
                    raise ValueError(f"edge link side must be one of {', '.join(SIDES)}, not {end.side!r}")
                try:
                    self.screen(end.device_id, end.screen_id)
                except KeyError:
                    raise ValueError(f"edge link names an unknown screen {end.device_id}/{end.screen_id}") from None
            if link.a.device_id == link.b.device_id:
                raise ValueError("an edge link joins screens of two different devices")
        for f in self.fits:
            for ref in (f.behind, f.front):
                try:
                    self.screen(*ref)
                except KeyError:
                    raise ValueError(f"an overlap fit names an unknown screen {ref[0]}/{ref[1]}") from None
            if not f.scale > 0:
                raise ValueError("an overlap fit's scale must be more than 0")
        pairs = self.overlaps()
        if pairs and not self.allow_overlap:
            raise ValueError(overlap_message(*pairs[0]))
        for behind, front in pairs:
            if front.x <= behind.x and front.y <= behind.y and front.right >= behind.right \
                    and front.bottom >= behind.bottom:
                raise ValueError(
                    f"screen {behind.device_id}/{behind.id} is completely hidden behind "
                    f"{front.device_id}/{front.id}"
                )

    def screens(self, device_id: str | None = None) -> Iterable[Screen]:
        for device in self.devices.values():
            if device_id is None or device.id == device_id:
                yield from device.screens

    def screen(self, device_id: str, screen_id: str) -> Screen:
        for s in self.screens(device_id):
            if s.id == screen_id:
                return s
        raise KeyError(f"{device_id}/{screen_id}")

    def screen_at(self, x: float, y: float, device_id: str | None = None,
                  prefer: Screen | None = None) -> Screen | None:
        """The screen you'd see at a point: the front one where screens overlap.

        With ``device_id``, only if that device's screen is the one in front
        there, so a covered (blocked out) area belongs to nobody behind it;
        with ``pointer_through``, that device's front most screen there.
        ``prefer``: with ``pointer_through``, that screen if it's one of the
        device's there (the pointer stays on the screen it's on).
        """
        if not self.allow_overlap:
            for s in self.screens(device_id):
                if s.contains(x, y):
                    return s
            return None
        if device_id is not None and self.pointer_through:
            if prefer is not None and prefer.device_id == device_id and prefer.contains(x, y):
                return prefer
            for s in reversed(self._stack()):
                if s.device_id == device_id and s.contains(x, y):
                    return s
            return None
        for s in reversed(self._stack()):
            if s.contains(x, y):
                return s if device_id is None or s.device_id == device_id else None
        return None

    def covered_by(self, device_id: str, x: float, y: float) -> Screen | None:
        """Another device's screen standing in front of ``device_id`` at a point."""
        if not self.allow_overlap or self.pointer_through:
            return None
        front = self.screen_at(x, y)
        if front is None or front.device_id == device_id:
            return None
        if any(s.contains(x, y) for s in self.screens(device_id)):
            return front
        return None

    def screen_at_os(self, device_id: str, ox: float, oy: float) -> Screen | None:
        for s in self.screens(device_id):
            if s.contains_os(ox, oy):
                return s
        return None

    def clamp_to_device(self, device_id: str, x: float, y: float) -> tuple[Screen, float, float]:
        """Nearest point on any of the device's screens (lets the pointer slide along edges).

        A point blocked out behind another device's screen is only used when
        nothing visible is found; callers can check with :meth:`screen_at`.
        """
        best: tuple[float, Screen, float, float] | None = None
        blocked: tuple[float, Screen, float, float] | None = None
        for s in self.screens(device_id):
            cx, cy = s.clamp(x, y)
            dist = (cx - x) ** 2 + (cy - y) ** 2
            if self.allow_overlap and self.screen_at(cx, cy, device_id) is None:
                if blocked is None or dist < blocked[0]:
                    blocked = (dist, s, cx, cy)
                continue
            if best is None or dist < best[0]:
                best = (dist, s, cx, cy)
        best = best or blocked
        if best is None:
            raise KeyError(f"device {device_id!r} has no screens")
        return best[1], best[2], best[3]

    def find_crossing(
        self,
        from_device: str,
        x: float,
        y: float,
        dx: float,
        dy: float,
        gap_tolerance: int = 0,
        entry_inset: int = 2,
    ) -> Crossing | None:
        """Where does moving by (dx, dy) from (x, y) leave ``from_device``?

        Returns the screen of a *different* device the motion lands on, or
        ``None`` if it stays on the same device or hits empty space. Gaps of
        up to ``gap_tolerance`` pixels between screens are bridged, so users
        don't need a pixel-perfect arrangement. The entry point is pushed
        ``entry_inset`` pixels into the new screen so that it isn't itself
        sitting on an edge (which would bounce straight back).
        """
        nx, ny = x + dx, y + dy
        if self.screen_at(nx, ny, from_device) is not None:
            return None
        linked, skip = self._linked_crossing(from_device, x, y, nx, ny, entry_inset)
        if linked is not None:
            return linked
        length = max(abs(dx), abs(dy))
        ux, uy = (dx / length, dy / length) if length else (0.0, 0.0)
        for step in range(gap_tolerance + 1):
            px, py = nx + ux * step, ny + uy * step
            target = self.screen_at(px, py)
            if target is not None and target.device_id != from_device and (target.device_id, target.id) not in skip:
                ex, ey = target.clamp(px + ux * entry_inset, py + uy * entry_inset)
                return Crossing(target, ex, ey)
            if step and (ux, uy) == (0.0, 0.0):
                break
        return None

    def _linked_crossing(
        self, from_device: str, x: float, y: float, nx: float, ny: float, inset: float
    ) -> tuple[Crossing | None, set[tuple[str, str]]]:
        """Crossing by an edge link, if the motion leaves a linked side.

        Also returns the screens linked along that side: off the linked
        stretch, the plain geometry mustn't send the pointer onto them,
        since the link says where they really join."""
        skip: set[tuple[str, str]] = set()
        if not self.links:
            return None, skip
        here = self.screen_at(x, y, from_device)
        if here is None:
            return None, skip
        sides = []
        if nx < here.x:
            sides.append("left")
        if nx >= here.right:
            sides.append("right")
        if ny < here.y:
            sides.append("top")
        if ny >= here.bottom:
            sides.append("bottom")
        for link in self.links:
            for mine, other in link.ends():
                if (mine.device_id, mine.screen_id) != (here.device_id, here.id) or mine.side not in sides:
                    continue
                skip.add((other.device_id, other.screen_id))
                t = mine.along(here, x, y)
                if not mine.covers(t):
                    continue
                target = self.screen(other.device_id, other.screen_id)
                ex, ey = other.point(target, mine.fraction(t), inset)
                return Crossing(target, ex, ey), skip
        return None, skip

    def edge_pushes(self, device_id: str, x: float, y: float, margin: float = 0.0) -> list[tuple[int, int]]:
        """Outward directions for a pointer resting on an outer edge of a device.

        The OS clamps the real cursor at the edge of the desktop, so we never
        see it go further. Instead, a cursor sitting on an outer edge pixel
        (with none of the same device's screens beyond it) counts as pushing
        outward in that direction. ``margin``: also count a pointer within
        that many pixels of such an edge.
        """
        screen = self.screen_at(x, y, device_id)
        if screen is None:
            return []
        pushes = []
        for (ex, ey), on_edge, beyond in (
            ((-1, 0), x <= screen.x + margin, (screen.x - 1, y)),
            ((1, 0), x >= screen.right - 1 - margin, (screen.right, y)),
            ((0, -1), y <= screen.y + margin, (x, screen.y - 1)),
            ((0, 1), y >= screen.bottom - 1 - margin, (x, screen.bottom)),
        ):
            if on_edge and self.screen_at(*beyond, device_id) is None:
                pushes.append((ex, ey))
        return pushes
