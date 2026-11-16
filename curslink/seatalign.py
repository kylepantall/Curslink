"""Line up from your seat: an overlap joined by eye, a corner at a time.

A screen in front of another is only seen to carry on from it from one
place: where your eyes are. A camera can stand in for them (the live
line-up, curslink.lineup) but never quite is them, and what's left over is
there to see as soon as you sit down. So the last word is yours: at two
spots along the edge the two screens share, a ring is drawn half on the
screen behind and half on the mirror in front, and you nudge the mirror
until each closes.

* At the first spot a nudge moves the whole picture: that sets where it is.
* At the second the first stays where it is and the picture turns and
  grows about it: that sets its size and how it leans.

:class:`Align` keeps the picture's four corners (curslink.layout.Fit.quad)
through that; :func:`spots` picks the two spots; :func:`eye_from` says where
your eyes must be for the joined spots to be seen as they are, by a desk a
line-up solved (curslink.seat). No numpy: the Layout page uses it.
"""

from __future__ import annotations

import math

from curslink.layout import Fit

STEP = 1.0  # the screen behind's pixels a nudge
BIG_STEP = 8.0  # with Shift
EDGE_POINTS = 10  # places tried along each side of the front screen for a spot
INSIDE = 0.02  # a spot is at least this share of the screen behind's short side inside it
MIN_APART = 0.25  # the two spots: at least this share of the front screen's width apart, on the screen behind
RING = 0.035  # a ring's radius: this share of the screen behind's short side...
RING_MIN = 26.0  # ...and at least this many of its pixels


def rect_quad(x: float, y: float, scale: float, size: tuple[float, float]) -> tuple[float, ...]:
    """A plain fit's corners (top left, top right, bottom right, bottom left)."""
    w, h = size[0] * scale, size[1] * scale
    return (x, y, x + w, y, x + w, y + h, x, y + h)


def _area(quad) -> float:
    pts = list(zip(quad[0::2], quad[1::2]))
    return abs(sum(a[0] * b[1] - b[0] * a[1] for a, b in zip(pts, pts[1:] + pts[:1]))) / 2


def plain(quad, size: tuple[float, float]) -> tuple[float, float, float]:
    """The nearest plain rectangle to a leaning shape: the same middle, the
    same area (what an older Curslink goes by, and the size things are drawn at)."""
    scale = math.sqrt(max(1e-9, _area(quad)) / max(1e-9, size[0] * size[1]))
    cx, cy = sum(quad[0::2]) / 4, sum(quad[1::2]) / 4
    return cx - size[0] * scale / 2, cy - size[1] * scale / 2, scale


def ring_radius(behind_size: tuple[float, float]) -> float:
    return max(RING_MIN, RING * min(behind_size))


def spots(quad, front_size: tuple[float, float], behind_size: tuple[float, float]) -> list[tuple[float, float]]:
    """The two places on the front screen (its pixels) to join it at: on
    its rim, seen against the screen behind, as far apart as can be. Fewer
    than two if the screens hardly overlap."""
    w, h = front_size
    fit = Fit(("", ""), ("", ""), *plain(quad, front_size), quad=tuple(quad))
    # far enough inside the screen behind for most of its ring to be drawn there
    margin = max(INSIDE * min(behind_size), 0.6 * ring_radius(behind_size))

    def inside(points):
        out = []
        for u, v in points:
            x, y = fit.to_behind(front_size, u, v)
            if margin <= x <= behind_size[0] - margin and margin <= y <= behind_size[1] - margin:
                out.append(((u, v), (x, y)))
        return out

    def furthest(seen):
        best, apart = None, 0.0
        for i, (a, pa) in enumerate(seen):
            for b, pb in seen[i + 1:]:
                d = math.hypot(pa[0] - pb[0], pa[1] - pb[1])
                if d > apart:
                    best, apart = (a, b), d
        return best, apart

    rim: list[tuple[float, float]] = []
    for i in range(EDGE_POINTS):
        t = i / EDGE_POINTS
        rim += [(t * w, 0.0), (w, t * h), (w - t * w, h), (0.0, h - t * h)]
    seen = inside(rim)
    # Its corners where two of them are seen (a ring there crosses two of its
    # sides, and they're the easiest places to look at); else along its rim.
    best, apart = furthest(inside([(0.0, 0.0), (w, 0.0), (w, h), (0.0, h)]))
    if best is None or apart < MIN_APART * w * fit.scale:
        best, apart = furthest(seen)
    if best is None:
        return [seen[0][0]] if seen else []
    if apart < MIN_APART * w * fit.scale:
        return [best[0]]
    # left to right (top to bottom on a side): the order they're gone through
    a, b = sorted(best, key=lambda p: (fit.to_behind(front_size, *p)[0], fit.to_behind(front_size, *p)[1]))
    out = [a, b]
    if a[0] == b[0] or a[1] == b[1]:
        # Both on one side of the front screen: halfway between them is on its
        # rim too, and says how the picture is drawn out along that side (a
        # screen that's turned is seen squeezed up towards its far end).
        middle = ((a[0] + b[0]) / 2, (a[1] + b[1]) / 2)
        if inside([middle]):
            out.append(middle)
    return out


class Align:
    """One overlap being joined by eye. ``quad``: where the front screen's
    corners are seen on the screen behind now (its pixels); ``spots``: the
    places being joined (the front screen's pixels); ``step``: which one now."""

    def __init__(self, quad, front_size: tuple[float, float], behind_size: tuple[float, float]):
        self.front_size = (float(front_size[0]), float(front_size[1]))
        self.behind_size = (float(behind_size[0]), float(behind_size[1]))
        self.quad = tuple(float(v) for v in quad)
        self.began = self.quad
        self.spots = spots(self.quad, self.front_size, self.behind_size)
        self.step = 0
        self.moved = False

    @classmethod
    def from_fit(cls, x: float, y: float, scale: float, quad, front_size, behind_size) -> "Align":
        return cls(quad if quad is not None and len(quad) == 8 else rect_quad(x, y, scale, front_size),
                   front_size, behind_size)

    # -- where things are ------------------------------------------------------
    @property
    def fit(self) -> Fit:
        return Fit(("", ""), ("", ""), *plain(self.quad, self.front_size), quad=self.quad)

    def seen(self, i: int) -> tuple[float, float]:
        """Spot ``i`` on the screen behind (its pixels), as the picture now stands."""
        return self.fit.to_behind(self.front_size, *self.spots[i])

    def rings(self) -> list[list[float]]:
        """What's drawn: ``[x, y, radius, 1 if it's the one being joined]`` per spot, the screen behind's pixels."""
        r = ring_radius(self.behind_size)
        return [[round(x, 2), round(y, 2), round(r, 1), 1 if i == self.step else 0]
                for i, (x, y) in enumerate(self.seen(j) for j in range(len(self.spots)))]

    def command(self) -> dict:
        """The fit as the running service takes it (the ``fit`` command)."""
        x, y, scale = plain(self.quad, self.front_size)
        return {"x": round(x, 3), "y": round(y, 3), "scale": round(scale, 6), "quad": [round(v, 3) for v in self.quad]}

    # -- changing it -----------------------------------------------------------
    def shift(self, dx: float, dy: float) -> None:
        self.quad = tuple(v + (dx, dy)[i % 2] for i, v in enumerate(self.quad))

    def nudge(self, dx: float, dy: float) -> None:
        """Move the spot being joined by (dx, dy) of the screen behind's
        pixels: the whole picture at the first, the picture about the first
        at any after it."""
        if not self.spots or not (dx or dy):
            return
        self.moved = True
        if self.step == 0 or len(self.spots) < 2:
            self.shift(dx, dy)
            return
        if self.step == 2:
            self._draw_out(dx, dy)
            return
        ax, ay = self.seen(0)
        bx, by = self.seen(self.step)
        ox, oy = bx - ax, by - ay
        d = ox * ox + oy * oy
        if d < 1e-6:
            return
        nx, ny = ox + dx, oy + dy
        # the turn-and-grow about the first spot that takes the second where it's wanted
        re, im = (nx * ox + ny * oy) / d, (ny * ox - nx * oy) / d
        if math.hypot(re, im) < 0.2 or math.hypot(re, im) > 5:
            return
        out = []
        for px, py in zip(self.quad[0::2], self.quad[1::2]):
            qx, qy = px - ax, py - ay
            out += [ax + re * qx - im * qy, ay + im * qx + re * qy]
        self.quad = tuple(out)

    def _frame(self):
        """The first two spots as (0, 0) and (1, 0): (to that frame, back from it)."""
        ax, ay = self.seen(0)
        bx, by = self.seen(1)
        ox, oy = bx - ax, by - ay
        d = ox * ox + oy * oy

        def to(px, py):
            qx, qy = px - ax, py - ay
            return (qx * ox + qy * oy) / d, (qy * ox - qx * oy) / d

        def back(x, y):
            return ax + x * ox - y * oy, ay + x * oy + y * ox

        return to, back, math.sqrt(d)

    def _draw_out(self, dx: float, dy: float) -> None:
        """The third spot (between the first two) moved along their line,
        those two staying put: the picture drawn out towards one of them
        and squeezed up towards the other, as a turned screen is seen."""
        to, back, length = self._frame()
        if length < 1e-3:
            return
        m = to(*self.seen(2))[0]
        ax, ay = self.seen(0)
        bx, by = self.seen(1)
        along = (dx * (bx - ax) + dy * (by - ay)) / length  # the part of the nudge along the line
        if not along and (dx or dy):
            along = dx if abs(bx - ax) >= abs(by - ay) else dy
        want = m + along / length
        if not 0.05 < m < 0.95 or not 0.05 < want < 0.95:
            return
        c = (m - want) / (m * (want - 1))
        pts = [to(px, py) for px, py in zip(self.quad[0::2], self.quad[1::2])]
        if any(1 + c * x <= 0.2 for x, _y in pts):
            return
        out = []
        for x, y in pts:
            out += back((1 + c) * x / (1 + c * x), y / (1 + c * x))
        self.quad = tuple(out)

    def adopt(self, quad) -> bool:
        """Take a better idea of the picture's shape (how the desk as solved
        says it's seen from where the joined spots put the eyes), turned and
        sized so the first two spots stay exactly where they were joined.
        False if it isn't a shape to go by."""
        if len(self.spots) < 2 or quad is None or len(quad) != 8:
            return False
        model = Fit(("", ""), ("", ""), *plain(quad, self.front_size), quad=tuple(float(v) for v in quad))
        a0, b0 = model.to_behind(self.front_size, *self.spots[0]), model.to_behind(self.front_size, *self.spots[1])
        a1, b1 = self.seen(0), self.seen(1)
        ox, oy = b0[0] - a0[0], b0[1] - a0[1]
        d = ox * ox + oy * oy
        if d < 1e-6:
            return False
        nx, ny = b1[0] - a1[0], b1[1] - a1[1]
        re, im = (nx * ox + ny * oy) / d, (ny * ox - nx * oy) / d
        if not 0.5 < math.hypot(re, im) < 2:
            return False
        out = []
        for px, py in zip(quad[0::2], quad[1::2]):
            qx, qy = px - a0[0], py - a0[1]
            out += [a1[0] + re * qx - im * qy, a1[1] + im * qx + re * qy]
        self.quad = tuple(out)
        return True

    def follow(self, x: float, y: float, scale: float) -> bool:
        """The service says the fit is now this plain rectangle (the mirror
        was dragged or zoomed by hand): the corners go along. True if that
        was news."""
        mx, my, mk = plain(self.quad, self.front_size)
        if abs(x - mx) < 0.6 and abs(y - my) < 0.6 and abs(scale - mk) < 2e-4 * mk + 1e-4:
            return False
        k = scale / mk
        self.quad = tuple((v - (mx, my)[i % 2]) * k + (x, y)[i % 2] for i, v in enumerate(self.quad))
        self.moved = True
        return True

    def next(self) -> bool:
        """On to the next spot. False when that was the last."""
        if self.step + 1 >= len(self.spots):
            return False
        self.step += 1
        return True

    def back(self) -> bool:
        if self.step == 0:
            return False
        self.step -= 1
        return True

    def joins(self, upto: int | None = None) -> list[tuple[tuple[float, float], tuple[float, float]]]:
        """Each spot (the first ``upto`` of them): (where on the front screen, where it's seen on the one behind)."""
        n = len(self.spots) if upto is None else min(upto, len(self.spots))
        return [(self.spots[i], self.seen(i)) for i in range(n)]


# -- where that says your eyes are ----------------------------------------------------
def eye_from(desk: dict, behind: str, front: str, joins, near=None) -> list[float] | None:
    """Where the eyes are (the desk's world), going by spots of ``front``
    seen at known places on ``behind``: each is a line from the place on the
    screen behind out through the spot on the front one, and the eyes are
    where those lines come nearest to meeting.

    Two spots along one edge fix how far across and how far up you are well,
    and how far *back* only as well as their lines cross (they're near enough
    parallel when you sit far back): so the answer is kept near ``near`` (the
    eye point there was) in the directions the lines don't decide. None if
    the desk doesn't have the screens, or the lines don't point at a place a
    person could be."""
    from curslink import seat

    screens = desk.get("screens") or {}
    if behind not in screens or front not in screens or len(joins) < 1:
        return None
    lines = []
    for (u, v), (x, y) in joins:
        a = seat.at(screens[behind], x, y)
        b = seat.at(screens[front], u, v)
        d = [b[i] - a[i] for i in range(3)]
        n = math.sqrt(sum(c * c for c in d))
        if n < 1e-9:
            return None
        lines.append((b, [c / n for c in d]))
    near = [float(v) for v in (near if near is not None else desk["eye"])]
    # Least squares: the point nearest every line, held to ``near`` lightly
    # (so one line, or lines that hardly cross, still give an answer: the
    # point on them nearest where the eyes were thought to be).
    hold = 1e-3
    m = [[hold if i == j else 0.0 for j in range(3)] for i in range(3)]
    rhs = [hold * near[i] for i in range(3)]
    for p, d in lines:
        for i in range(3):
            for j in range(3):
                c = (1.0 if i == j else 0.0) - d[i] * d[j]
                m[i][j] += c
                rhs[i] += c * p[j]
    eye = _solve3(m, rhs)
    if eye is None:
        return None
    # Before the front screen (further along each line than the spot), and
    # not absurdly far from where the eyes were.
    for p, d in lines:
        if sum((eye[i] - p[i]) * d[i] for i in range(3)) <= 0:
            return None
    mm = seat.mm_of(desk)
    if mm:
        back = math.sqrt(sum((eye[i] - near[i]) ** 2 for i in range(3))) * mm
        if back > 1500:
            return None
    return eye


def miss(desk: dict, behind: str, front: str, joins, eye) -> float:
    """How far the lines through ``joins`` pass from ``eye`` at most (world units)."""
    from curslink import seat

    screens = desk["screens"]
    worst = 0.0
    for (u, v), (x, y) in joins:
        a, b = seat.at(screens[behind], x, y), seat.at(screens[front], u, v)
        d = [b[i] - a[i] for i in range(3)]
        n = math.sqrt(sum(c * c for c in d)) or 1.0
        d = [c / n for c in d]
        e = [eye[i] - b[i] for i in range(3)]
        t = sum(e[i] * d[i] for i in range(3))
        worst = max(worst, math.sqrt(max(0.0, sum(c * c for c in e) - t * t)))
    return worst


def _solve3(m, rhs) -> list[float] | None:
    a = [list(m[i]) + [rhs[i]] for i in range(3)]
    for c in range(3):
        pivot = max(range(c, 3), key=lambda r: abs(a[r][c]))
        if abs(a[pivot][c]) < 1e-12:
            return None
        a[c], a[pivot] = a[pivot], a[c]
        for r in range(3):
            if r != c:
                f = a[r][c] / a[c][c]
                a[r] = [x - f * y for x, y in zip(a[r], a[c])]
    return [a[i][3] / a[i][i] for i in range(3)]
