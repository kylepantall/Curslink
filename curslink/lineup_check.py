"""The last step of a live line-up: lines drawn across the screens that
should look continuous from where the person sits, checked by the camera.

The scan (curslink.lineup.Live) ends with the desk in 3D and a layout. This
checks that layout where it matters, at the edges the pointer will cross,
with something the person can see for themselves:

* For every pair of screens that meet (a link) or overlap (a fit), a few
  short bright lines are drawn straight across the join: each starts on one
  screen and carries on, as seen from the eye, on the other. If the layout
  is right the two halves look like one line.
* The phone keeps sending frames. In each, the camera's place is worked
  out from the few patterns still showing on the screens, which says
  where every half-line should appear in the frame *if the desk is as
  solved* (wherever the phone is: it needn't be back at the eye). Where
  each half really is, is measured across it to a fraction of a pixel. A
  step between the two halves of a line, beyond what's predicted, is the
  layout being off at that join by that much.
* The screen further from the reference (and whatever hangs off it) is
  moved by the step, the lines are drawn again, and so on until every join
  is within a pixel or two, or a few seconds have gone.

numpy only, like curslink.lineup_scene (the frame is a grey numpy array).
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from curslink.lineup_scene import ScreenPose, exp_so3, pose_from_homography, project_from

REACH = 0.16  # how far a line runs into each screen, in that screen's shorter sides
THICK = 0.008  # how thick it's drawn, same units
PER_JOIN = 4  # lines across each join (each edge of a screen in front of another: 3)
WINDOW = 14.0  # how far either side of where a line should be it's looked for (px in a 1600 px frame)
CONTRAST = 28.0  # a line is this much brighter than what's beside it, or it isn't there
GAIN = 0.7  # how much of a measured step is taken out at a time (a small one: less)
# A join is right when its step is under this share of the screen's shorter
# side (or this many of its pixels): about what a line's place can be told
# to in a phone's frame. Asking for less only chases the noise about.
DONE = 0.003
DONE_PX = 2.5
GAP = 6  # how far out from a screen's edge the other's half starts, in hundredths of its shorter side
AGREE = 4  # frames in a row every join must be right in
MAX_FRAMES = 28  # frames read before it's left as it is
UNSEEN = 8  # ...or this many without a single line seen


@dataclass
class Line:
    """One line across a join: the half on screen ``a`` and the half on ``b``,
    each a run of points in that screen's pixels, from the join outwards."""

    a: str
    b: str
    on_a: np.ndarray
    on_b: np.ndarray
    step: float | None = None  # as last measured, in b's pixels (None: not seen)


@dataclass
class Join:
    a: str
    b: str
    lines: list[Line] = field(default_factory=list)
    step: float | None = None  # how far off it was last measured, in b's pixels
    good: int = 0  # frames in a row it's been right


def _project(intr, r, t, pts) -> np.ndarray:
    xc = np.asarray(pts, np.float64) @ r.T + t
    z = np.where(xc[:, 2] < 1e-6, 1e-6, xc[:, 2])
    x, y = xc[:, 0] / z, xc[:, 1] / z
    r2 = x * x + y * y
    d = 1 + intr[3] * r2 + intr[4] * r2 * r2
    return np.column_stack([intr[0] * d * x + intr[1], intr[0] * d * y + intr[2]])


def _sample(gray: np.ndarray, xy: np.ndarray) -> np.ndarray:
    """``gray`` at the points ``xy`` (..., 2), between its pixels; NaN outside."""
    h, w = gray.shape[:2]
    x, y = xy[..., 0], xy[..., 1]
    ok = (x >= 0) & (x <= w - 1.001) & (y >= 0) & (y <= h - 1.001)
    x0 = np.clip(np.floor(x), 0, w - 2).astype(int)
    y0 = np.clip(np.floor(y), 0, h - 2).astype(int)
    fx, fy = x - x0, y - y0
    g = gray.astype(np.float64, copy=False)
    out = (g[y0, x0] * (1 - fx) * (1 - fy) + g[y0, x0 + 1] * fx * (1 - fy)
           + g[y0 + 1, x0] * (1 - fx) * fy + g[y0 + 1, x0 + 1] * fx * fy)
    return np.where(ok, out, np.nan)


def measure(gray: np.ndarray, path: np.ndarray, normal: np.ndarray, window: float) -> float | None:
    """How far, along ``normal``, the bright line in ``gray`` is from the
    ``path`` (n, 2) it should be on: the middle of its brightness across it,
    at points along it, the median of those. None if it isn't there."""
    path = np.asarray(path, np.float64)
    if len(path) < 2:
        return None
    seg = np.linalg.norm(np.diff(path, axis=0), axis=1)
    length = float(seg.sum())
    if length < 8:
        return None
    # A point every couple of pixels along it, clear of its two ends.
    at = np.linspace(0.12 * length, 0.88 * length, max(6, int(length / 2.5)))
    cum = np.concatenate([[0.0], np.cumsum(seg)])
    pts = np.column_stack([np.interp(at, cum, path[:, 0]), np.interp(at, cum, path[:, 1])])
    offs = np.arange(-window, window + 0.5, 0.5)
    prof = _sample(gray, pts[:, None, :] + offs[None, :, None] * normal[None, None, :])
    found = []
    for row in prof:
        if np.isnan(row).any():
            continue
        base = float(np.percentile(row, 30))
        top = int(np.argmax(row))
        if row[top] - base < CONTRAST or top < 3 or top > len(row) - 4:
            continue
        # The middle of the bump the peak is in.
        level = base + 0.35 * (row[top] - base)
        lo = top
        while lo > 0 and row[lo - 1] > level:
            lo -= 1
        hi = top
        while hi < len(row) - 1 and row[hi + 1] > level:
            hi += 1
        if lo == 0 or hi == len(row) - 1:
            continue  # runs off the window: something bigger than a line (a bright window behind)
        wgt = row[lo:hi + 1] - level
        found.append(float((offs[lo:hi + 1] * wgt).sum() / wgt.sum()))
    if len(found) < max(4, len(prof) // 3):
        return None
    return float(np.median(found))


class Check:
    """The lines for a solved desk, and the layout nudged by what the camera
    sees of them.

    ``solution``: a curslink.lineup_scene.Solution (its screens are moved in
    place). ``result``: the curslink.lineup.Result made from it (for which
    screens meet). ``planned``: what each screen shows through the check
    (curslink.lineup.check_plan: the lines keep clear of its patterns).
    ``intr``: the camera, as the scan found it (f, cx, cy, k1, k2).
    """

    def __init__(self, solution, result, planned: dict[str, dict], intr, image_size: tuple[int, int]):
        self.sol = solution
        self.planned = planned
        self.intr = np.asarray(intr, np.float64)
        self.long_side = float(max(image_size))
        self.frames = 0
        self.measured = 0  # frames in which a join was measured
        self.moved = 0.0  # how far the screens have been moved in all (their own px)
        joins = [j for j in result.joins if j[0] in solution.screens and j[1] in solution.screens]
        self.kinds = {(a, b): "fit" if side is None else "link" for a, b, side in joins}
        self.sides = {(a, b): side for a, b, side in joins if side is not None}
        self.joins = [Join(a, b) for (a, b) in self.kinds]
        # Which screen of a join moves: the one further from the reference
        # along the joins (and everything beyond it moves with it).
        self.depth = {solution.reference: 0}
        grew = True
        while grew:
            grew = False
            for a, b in self.kinds:
                for near, far in ((a, b), (b, a)):
                    if near in self.depth and far not in self.depth:
                        self.depth[far] = self.depth[near] + 1
                        grew = True
        self.parent: dict[str, str] = {}
        for a, b in self.kinds:
            for near, far in ((a, b), (b, a)):
                if self.depth.get(far) == self.depth.get(near, -9) + 1 and far not in self.parent:
                    self.parent[far] = near
        self.draw()

    # ---- the lines ----
    def _clear(self, key: str, pts: np.ndarray) -> bool:
        """Are ``pts`` (in the screen's pixels) on it, and clear of its patterns?"""
        sp = self.sol.screens[key]
        pad = 1.5 * THICK * min(sp.width, sp.height)
        if (pts[:, 0] < pad).any() or (pts[:, 0] > sp.width - pad).any() \
                or (pts[:, 1] < pad).any() or (pts[:, 1] > sp.height - pad).any():
            return False
        for _bits, fx, fy, fw, fh in self.planned.get(key, {}).get("markers", []):
            x0, y0 = (fx - fw / 3) * sp.width - pad, (fy - fh / 3) * sp.height - pad
            x1, y1 = (fx + fw * 4 / 3) * sp.width + pad, (fy + fh * 4 / 3) * sp.height + pad
            if ((pts[:, 0] > x0) & (pts[:, 0] < x1) & (pts[:, 1] > y0) & (pts[:, 1] < y1)).any():
                return False
        return True

    def _line(self, a: str, b: str, side: str, along: float) -> Line | None:
        """A line across ``a``'s ``side`` edge, ``along`` it (0 to 1), carried
        on as the eye sees it onto ``b``."""
        A, B = self.sol.screens[a], self.sol.screens[b]
        face = ScreenPose(a, A.chord, A.height, A.r, A.t, A.scale)  # a's corners' plane
        w, h = face.width, face.height
        edge, inward = {
            "left": ((0.0, along * h), (1.0, 0.0)), "right": ((w, along * h), (-1.0, 0.0)),
            "top": ((along * w, 0.0), (0.0, 1.0)), "bottom": ((along * w, h), (0.0, -1.0)),
        }[side]
        edge, inward = np.array(edge), np.array(inward)
        reach_a = REACH * min(A.width, A.height)
        inset = 2.5 * THICK * min(A.width, A.height)  # a hair in from the very edge (bezels, rounded corners)
        taus = np.linspace(inset, reach_a, 12)
        world_a = face.at(edge + taus[:, None] * inward)
        on_a = project_from(self.sol.eye, A, world_a) if A.bend else edge + taus[:, None] * inward
        if on_a is None or not self._clear(a, on_a):
            return None
        # Outwards, in a's plane, until it has run far enough over b.
        reach_b = REACH * min(B.width, B.height)
        inset_b = 2.5 * THICK * min(B.width, B.height)
        step = 0.01 * min(A.width, A.height)
        out = -(np.arange(1, 500)[:, None] * step) * inward + edge
        seen = project_from(self.sol.eye, B, face.at(out))
        if seen is None:
            return None
        inside = ((seen[:, 0] > inset_b) & (seen[:, 0] < B.width - inset_b)
                  & (seen[:, 1] > inset_b) & (seen[:, 1] < B.height - inset_b))
        inside[:GAP] = False  # a's own bezel hides b right beside it
        if not inside.any():
            return None
        first = int(np.argmax(inside))
        run = [seen[first]]
        for p in seen[first + 1:]:
            if not (inset_b < p[0] < B.width - inset_b and inset_b < p[1] < B.height - inset_b):
                break
            run.append(p)
            if np.linalg.norm(p - run[0]) >= reach_b:
                break
        on_b = np.array(run)
        if len(on_b) < 3 or np.linalg.norm(on_b[-1] - on_b[0]) < 0.4 * reach_b or not self._clear(b, on_b):
            return None
        return Line(a, b, np.asarray(on_a, np.float64), on_b)

    def draw(self) -> None:
        """The lines for the desk as it's now solved."""
        tries = np.linspace(0.06, 0.94, 23)

        def spread(found: list[tuple[float, Line]], n: int) -> list[Line]:
            """``n`` of them, as far apart along the edge as can be."""
            if len(found) <= n:
                return [li for _a, li in found]
            picks = np.linspace(0, len(found) - 1, n).round().astype(int)
            return [found[i][1] for i in picks]

        for join in self.joins:
            kind = self.kinds[(join.a, join.b)]
            lines: list[Line] = []
            # a link: across the one edge; a screen in front of another:
            # across each of its edges that has the other showing beyond it
            sides = [(self.sides[(join.a, join.b)], PER_JOIN)] if kind == "link" else \
                [(side, 3) for side in ("top", "left", "right", "bottom")]
            for side, n in sides:
                found = []
                for along in tries:
                    line = self._line(join.a, join.b, side, float(along))
                    if line is not None:
                        found.append((float(along), line))
                lines.extend(spread(found, n))
            old = {(round(float(li.on_a[0][0]), -1), round(float(li.on_a[0][1]), -1)): li.step for li in join.lines}
            for li in lines:
                li.step = old.get((round(float(li.on_a[0][0]), -1), round(float(li.on_a[0][1]), -1)))
            join.lines = lines

    def lines(self) -> dict[str, list[list[list[float]]]]:
        """What each screen draws: ``{key: [[[x, y], ...], ...]}``, runs of
        points as shares of its width and height."""
        out: dict[str, list] = {}
        for join in self.joins:
            for li in join.lines:
                for key, pts in ((li.a, li.on_a), (li.b, li.on_b)):
                    sp = self.sol.screens[key]
                    keep = pts[np.linspace(0, len(pts) - 1, min(len(pts), 9)).round().astype(int)]
                    out.setdefault(key, []).append([[round(float(x / sp.width), 5), round(float(y / sp.height), 5)]
                                                    for x, y in keep])
        return out

    @property
    def any(self) -> bool:
        return any(j.lines for j in self.joins)

    # ---- where the camera is in a frame ----
    def locate(self, sightings: list) -> tuple[np.ndarray, np.ndarray] | None:
        known = [s for s in sightings if s.key in self.sol.screens]
        if not known:
            return None
        s = max(known, key=lambda s: (s.markers, -s.rms))
        view = pose_from_homography(s.h, self.intr[0], self.intr[1], self.intr[2])
        if view is None:
            return None
        sp = self.sol.screens[s.key]
        r = view[0] @ sp.r.T
        t = sp.scale * view[1] - r @ sp.t
        world = np.concatenate([self.sol.screens[s.key].at(s.uv) for s in known])
        xy = np.concatenate([s.xy for s in known])
        span = float(np.abs(t).max() + 1.0)
        steps = np.array([1e-6] * 3 + [1e-6 * span] * 3)
        huber = 2.0 * self.long_side / 1600

        def res(r_, t_):
            return _project(self.intr, r_, t_, world) - xy

        for _ in range(12):
            e = res(r, t)
            norm = np.sqrt((e ** 2).sum(1))
            wgt = np.repeat(np.minimum(1.0, huber / np.maximum(norm, 1e-9)), 2)
            j = np.zeros((2 * len(xy), 6))
            for k in range(6):
                d = np.zeros(6)
                d[k] = steps[k]
                j[:, k] = ((res(exp_so3(d[:3]) @ r, t + d[3:]) - e) / steps[k]).ravel()
            jw = j * wgt[:, None]
            try:
                dx = np.linalg.solve(jw.T @ jw + 1e-9 * np.eye(6), -jw.T @ (e.ravel() * wgt))
            except np.linalg.LinAlgError:
                return None
            r, t = exp_so3(dx[:3]) @ r, t + dx[3:]
            if np.abs(dx[:3]).max() < 1e-7 and np.abs(dx[3:]).max() < 1e-6 * span:
                break
        return r, t

    # ---- a frame ----
    def _jacobian(self, key: str, uv: np.ndarray, r, t) -> np.ndarray:
        """Frame pixels per pixel of the screen, at ``uv`` on it (2x2)."""
        sp = self.sol.screens[key]
        p = _project(self.intr, r, t, sp.at([uv, uv + (1.0, 0.0), uv + (0.0, 1.0)]))
        return np.column_stack([p[1] - p[0], p[2] - p[0]])

    def _move(self, key: str, by: np.ndarray) -> None:
        """Move a screen across its own face by ``by`` (its pixels)."""
        sp = self.sol.screens[key]
        sp.t = sp.t + sp.r[:, :2] @ (np.asarray(by, np.float64) * sp.scale)

    def _beyond(self, key: str) -> list[str]:
        out, todo = [], [key]
        while todo:
            k = todo.pop()
            for child, parent in self.parent.items():
                if parent == k and child not in out:
                    out.append(child)
                    todo.append(child)
        return out

    def frame(self, gray: np.ndarray, sightings: list) -> bool:
        """Read a frame: measure every line in it and move the screens by
        what's found. True if any screen moved (the lines want drawing again)."""
        self.frames += 1
        window = WINDOW * self.long_side / 1600
        moved = False
        any_measured = False
        for join in self.joins:
            # Where the camera is, by this join's own two screens: a screen
            # elsewhere that's a little out (and has no join to be put right
            # by) would pull the camera's place off with it. (Placed by both,
            # the camera splits their difference; the step between the two
            # halves of a line is still all of it.)
            moves_a = join.b == self.sol.reference or self.parent.get(join.a) == join.b
            view = self.locate([s for s in sightings if s.key in (join.a, join.b)])
            if view is None:
                join.step = None
                continue
            r, t = view
            rows, rhs = [], []
            for li in join.lines:
                A, B = self.sol.screens[li.a], self.sol.screens[li.b]
                pa = _project(self.intr, r, t, A.at(li.on_a))
                pb = _project(self.intr, r, t, B.at(li.on_b))
                along = pb[-1] - pa[-1]  # from deep in a to deep in b
                if np.linalg.norm(along) < 1e-6:
                    continue
                along = along / np.linalg.norm(along)
                normal = np.array([-along[1], along[0]])
                da, db = measure(gray, pa, normal, window), measure(gray, pb, normal, window)
                if da is None or db is None:
                    li.step = None
                    continue
                rho = db - da
                g = self._jacobian(li.b, li.on_b[0], r, t)
                rows.append(normal @ g)
                rhs.append(rho)
                li.step = float(rho / max(1e-9, np.linalg.norm(normal @ g)))
            if not rows:
                join.step = None
                continue
            any_measured = True
            # b's content is seen that far from where the desk as solved puts
            # it: b is really over there (as far as these lines can tell).
            # (Lines across one edge only say how far along it: what they can't
            # tell is left alone, not guessed from their noise.)
            delta, *_ = np.linalg.lstsq(np.array(rows), np.array(rhs), rcond=0.3)
            join.step = float(np.linalg.norm(delta))
            B = self.sol.screens[join.b]
            enough = max(DONE_PX, DONE * min(B.width, B.height))
            if join.step <= enough:
                join.good += 1
                continue
            join.good = 0
            gain = GAIN if join.step > 3 * enough else 0.6 * GAIN
            # Move the one further from the reference, and all beyond it,
            # by the same amount as the camera sees it.
            mid_b = np.array([B.width / 2, B.height / 2])
            shift = self._jacobian(join.b, mid_b, r, t) @ (gain * delta)  # in the frame
            mover = join.b
            if moves_a:
                mover, shift = join.a, -shift
            for key in [mover, *self._beyond(mover)]:
                sp = self.sol.screens[key]
                g = self._jacobian(key, np.array([sp.width / 2, sp.height / 2]), r, t)
                try:
                    by = np.linalg.solve(g, shift)
                except np.linalg.LinAlgError:
                    continue
                self._move(key, by)
                if key == mover:
                    self.moved += float(np.linalg.norm(by))
            moved = True
        if any_measured:
            self.measured += 1
        if moved:
            self.draw()
        return moved

    @property
    def done(self) -> bool:
        seen = [j for j in self.joins if j.lines]
        if not seen or self.frames >= MAX_FRAMES:
            return True
        if self.frames >= UNSEEN and not self.measured:
            return True  # the lines aren't there to be seen (older Curslink on the screens)
        return self.agreed

    @property
    def agreed(self) -> bool:
        """Has every join the camera can see been right, frames in a row?"""
        seen = [j for j in self.joins if j.lines]
        return self.measured >= AGREE and all(j.good >= AGREE for j in seen if j.step is not None) \
            and any(j.step is not None for j in seen)

    def worst(self) -> float | None:
        """The biggest step still measured at a join, in pixels (None: none measured)."""
        steps = [j.step for j in self.joins if j.step is not None]
        return max(steps) if steps else None
