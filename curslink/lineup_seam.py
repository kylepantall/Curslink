"""The very end of a live line-up: one blocky pattern across a screen and the
screen in front of it, joined up by what the camera sees.

A laptop in front of a monitor has a *fit* (curslink.lineup): which part of
the monitor each of the laptop's pixels is seen over. The pointer crosses by
it and the monitor's picture is carried on by it (the mirror strip), so it's
the fit itself that has to be right, from where the person looks. The scan
and the lines across the joins (curslink.lineup_check) get the desk right in
3D; this tests the saved fit directly:

* The screen behind draws a pattern of random black and white blocks in a
  band just beyond the front screen's edge. The front screen draws the *same*
  pattern in a strip along that edge, working out which blocks go where by
  the fit: exactly what the mirror strip does with the monitor's picture.
* If the fit is right the blocks carry on unbroken from the monitor into
  the strip, as seen from the camera. If it's out, they break at the edge.
* Each frame, the picture is laid out flat in the back screen's pixels (by
  where the camera is, from the few patterns still up), both where the band
  is and where the strip is seen against it, and each is matched to the
  pattern as it should be there. The band says how far out that laying-out
  is; what the strip is out by beyond that is the break. The fit is moved by
  most of it, the strip is drawn again, and so on until the blocks join.

The fit ends up right for the eye: where the phone was when the scan began
(a frame from a little way off is allowed for, by the desk as solved). So
the phone should be held as near the eyes as can be.

The pattern (bit, blocks) is plain Python: the screens that draw it needn't
have numpy. Reading it (Seam) needs numpy and OpenCV.
"""

from __future__ import annotations

from dataclasses import dataclass, field

CELL = 0.034  # a block's side, in the back screen's shorter sides
DEEP = 3  # how many blocks deep the band and the strip are read
LONG = 4  # ...and how many along the edge each patch is
GAP = 0.06  # the band is read this far out from the front screen's edge (its bezel hides nearer), same units
REACH = 2.5  # how far out a break can be found, in blocks
SAMPLES = 8  # samples across a block when the picture is laid out flat
SCORE = 0.45  # how well a patch must match the pattern to count
GAIN = 0.7  # how much of a break is taken out at a time
DONE = 0.003  # joined: the break is under this share of the back screen's shorter side...
DONE_PX = 3.0  # ...or this many of its pixels (a hand-held phone moves the seam about that much)
AGREE = 4  # frames in a row it must be joined in
MAX_FRAMES = 24  # frames read before it's left as it is
UNSEEN = 8  # ...or this many without the pattern read at all
MAX_PATCHES = 6  # along one edge


def bit(i: int, j: int, seed: int) -> int:
    """Is block (i, j) of pattern ``seed`` white? The same on every computer."""
    h = (i * 73856093 ^ j * 19349663 ^ seed * 83492791) & 0xFFFFFFFF
    h ^= h >> 13
    h = (h * 0x5BD1E995) & 0xFFFFFFFF
    h ^= h >> 15
    return h & 1


def blocks(pattern: dict, width: float, height: float):
    """The blocks a screen draws for ``pattern`` (``{"cell", "seed", "map":
    [x, y, scale], "areas": [[x, y, w, h], ...]}``: its pixel (u, v) shows the
    pattern at (x + scale*u, y + scale*v); the areas are shares of the
    screen). Yields ``(x0, y0, x1, y1, white)`` in the screen's pixels
    (``width`` by ``height``), each clipped to its area."""
    import math

    cell, seed = float(pattern["cell"]), int(pattern["seed"])
    mx, my, ms = (float(v) for v in pattern["map"])
    if cell <= 0 or ms <= 0:
        return
    for fx, fy, fw, fh in pattern["areas"]:
        ax0, ay0, ax1, ay1 = fx * width, fy * height, (fx + fw) * width, (fy + fh) * height
        i0, i1 = math.floor((mx + ms * ax0) / cell), math.ceil((mx + ms * ax1) / cell)
        j0, j1 = math.floor((my + ms * ay0) / cell), math.ceil((my + ms * ay1) / cell)
        if (i1 - i0) * (j1 - j0) > 20000:
            continue  # not a pattern anyone asked for
        for i in range(i0, i1):
            x0, x1 = max(ax0, (i * cell - mx) / ms), min(ax1, ((i + 1) * cell - mx) / ms)
            if x1 <= x0:
                continue
            for j in range(j0, j1):
                y0, y1 = max(ay0, (j * cell - my) / ms), min(ay1, ((j + 1) * cell - my) / ms)
                if y1 > y0:
                    yield x0, y0, x1, y1, bit(i, j, seed)


@dataclass
class Patch:
    """One place the join is read: a piece of the band on the back screen and
    the piece of the strip across the edge from it, both as rectangles
    (x0, y0, x1, y1) in the back screen's pixels."""

    band: tuple[float, float, float, float]
    strip: tuple[float, float, float, float]
    out: float | None = None  # the break last read here, in the back screen's pixels (None: not read)


@dataclass
class Join:
    front: str
    behind: str
    fit: dict  # the fit being put right: behind, front, x, y, scale (changed in place)
    seed: int
    cell: float
    patches: list[Patch]
    draw_band: list[tuple[float, float, float, float]]  # what the back screen draws (its pixels)
    draw_strip: list[tuple[float, float, float, float]]  # what the front one does (the back screen's pixels)
    out: float | None = None  # the break last read, in the back screen's pixels
    good: int = 0
    read: int = 0  # frames it's been read in
    moved: float = 0.0
    wants: list = field(default_factory=list)  # the fit each frame read asked for: (x, y, scale)


def _sample(gray, xy):
    """``gray`` at the points ``xy`` (..., 2), between its pixels; NaN outside."""
    import numpy as np

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


class Seam:
    """The pattern for each screen in front of another, and their fits put
    right by what the camera sees of it.

    ``solution``: a curslink.lineup_scene.Solution. ``fits``: the fits from
    the curslink.lineup.Result made from it (copied; ``fits()`` gives them as
    they now are). ``planned``: what each screen shows meanwhile
    (curslink.lineup.check_plan: places read keep clear of its patterns).
    ``locate(sightings)`` gives the camera's place in a frame as (r, t)
    (curslink.lineup_check.Check.locate). ``intr``: the camera (f, cx, cy, k1, k2).
    """

    def __init__(self, solution, fits: list[dict], planned: dict[str, dict], intr, image_size: tuple[int, int]):
        import numpy as np

        self.sol = solution
        self.planned = planned
        self.intr = np.asarray(intr, np.float64)
        self.long_side = float(max(image_size))
        self.frames = 0
        self.measured = 0
        # What each screen draws meanwhile: its check patterns, less any a strip
        # covers. (``showing``, for reading a frame, keeps them all: which pattern
        # is which goes by its place in the list.)
        self.showing = planned
        self.drawing = {k: dict(v) for k, v in planned.items()}
        self.joins: list[Join] = []
        for n, fit in enumerate(fits):
            if fit["front"] in solution.screens and fit["behind"] in solution.screens and fit["scale"] > 0:
                join = self._join(dict(fit), 1 + n)
                if join is not None:
                    self.joins.append(join)

    # ---- where the pattern goes ----
    def _markers(self, key: str, to_behind=None) -> list[tuple[float, float, float, float]]:
        """The patterns ``key`` is showing, as rectangles in its pixels (or,
        with ``to_behind`` (x, y, scale), in the back screen's)."""
        sp = self.sol.screens[key]
        out = []
        for _bits, fx, fy, fw, fh in self.planned.get(key, {}).get("markers", []):
            x0, y0 = (fx - fw / 5) * sp.width, (fy - fh / 5) * sp.height  # (with the white round them)
            x1, y1 = (fx + fw * 1.2) * sp.width, (fy + fh * 1.2) * sp.height
            if to_behind is not None:
                x, y, s = to_behind
                x0, y0, x1, y1 = x + s * x0, y + s * y0, x + s * x1, y + s * y1
            out.append((x0, y0, x1, y1))
        return out

    def _join(self, fit: dict, seed: int) -> Join | None:
        F, B = self.sol.screens[fit["front"]], self.sol.screens[fit["behind"]]
        short = min(B.width, B.height)
        cell = max(8.0, round(CELL * short))
        deep, long_, gap, reach = DEEP * cell, LONG * cell, GAP * short, REACH * cell
        x, y, s = fit["x"], fit["y"], fit["scale"]
        fx0, fy0, fx1, fy1 = x, y, x + s * F.width, y + s * F.height  # the front screen, in the back one's pixels
        inset = 0.5 * cell
        on_b = self._markers(fit["behind"])
        on_f: list = []  # (the front screen takes down any of its patterns that are in the way: ``showing``)

        def hit(rect, others) -> bool:
            return any(rect[0] < o[2] and o[0] < rect[2] and rect[1] < o[3] and o[1] < rect[3] for o in others)

        patches: list[Patch] = []
        band_areas, strip_areas = [], []
        # Each edge of the front screen with the back one showing beyond it:
        # (is it a top/bottom edge, where it is, which way is out, and the
        # stretch of it that lies on the back screen's face).
        edges = [(True, fy0, -1, max(fx0, 0.0), min(fx1, B.width)), (True, fy1, 1, max(fx0, 0.0), min(fx1, B.width)),
                 (False, fx0, -1, max(fy0, 0.0), min(fy1, B.height)), (False, fx1, 1, max(fy0, 0.0), min(fy1, B.height))]
        for flat, at, way, lo, hi in edges:
            limit = B.height if flat else B.width
            far = at + way * (gap + deep + reach)  # the band's far side, with room to look about
            if not (0 <= far <= limit) or not (0 <= at <= limit) or hi - lo < long_ + 2 * reach:
                continue
            count = min(MAX_PATCHES, int((hi - lo - 2 * reach) // long_))
            spare = (hi - lo - 2 * reach - count * long_) / max(1, count)
            found = []
            for k in range(count):
                a0 = lo + reach + spare / 2 + k * (long_ + spare)
                a1 = a0 + long_
                o0, o1 = sorted((at + way * gap, at + way * (gap + deep)))  # the band, beyond the edge
                i0, i1 = sorted((at - way * inset, at - way * (inset + deep)))  # the strip, within it
                band = (a0, o0, a1, o1) if flat else (o0, a0, o1, a1)
                strip = (a0, i0, a1, i1) if flat else (i0, a0, i1, a1)
                m = 0.5 * cell
                grown = (band[0] - m, band[1] - m, band[2] + m, band[3] + m)
                grown_s = (strip[0] - m, strip[1] - m, strip[2] + m, strip[3] + m)
                if hit(grown, on_b) or hit(grown_s, on_f):
                    continue
                found.append(Patch(band, strip))
            if not found:
                continue
            patches.extend(found)
            # What's drawn: all along the stretch, from the far side of the
            # band right up to the edge, and on into the strip.
            o0, o1 = sorted((at, far))
            i0, i1 = sorted((at, at - way * (inset + deep + reach)))
            band_areas.append((lo, o0, hi, o1) if flat else (o0, lo, o1, hi))
            strip_areas.append((lo, i0, hi, i1) if flat else (i0, lo, i1, hi))
        if not patches:
            return None
        # The front screen's own patterns that its strip would run into come down.
        mine = self.drawing.get(fit["front"])
        if mine is not None:
            grown = [(r[0] - 0.5 * cell, r[1] - 0.5 * cell, r[2] + 0.5 * cell, r[3] + 0.5 * cell) for r in strip_areas]
            rects = dict(zip((tuple(m) for m in self.planned[fit["front"]]["markers"]),
                             self._markers(fit["front"], (x, y, s))))
            keep = [m for m in mine["markers"] if not hit(rects[tuple(m)], grown)]
            self.drawing[fit["front"]] = {**mine, "markers": keep}
        return Join(fit["front"], fit["behind"], fit, seed, cell, patches, band_areas, strip_areas)

    @property
    def any(self) -> bool:
        return bool(self.joins)

    def patterns(self) -> dict[str, dict]:
        """What each screen draws now: ``{key: {"cell", "seed", "map", "size",
        "areas"}}`` (see ``blocks``; ``size``: the screen's own pixels, which
        ``map`` is in). A screen in two joins draws the first."""
        out: dict[str, dict] = {}
        for join in self.joins:
            F, B = self.sol.screens[join.front], self.sol.screens[join.behind]
            x, y, s = join.fit["x"], join.fit["y"], join.fit["scale"]

            def shares(rects, sp, back) -> list[list[float]]:
                areas = []
                for x0, y0, x1, y1 in rects:
                    if back is not None:
                        x0, y0, x1, y1 = (x0 - back[0]) / back[2], (y0 - back[1]) / back[2], \
                            (x1 - back[0]) / back[2], (y1 - back[1]) / back[2]
                    x0, y0, x1, y1 = max(0.0, x0), max(0.0, y0), min(sp.width, x1), min(sp.height, y1)
                    if x1 - x0 > 1 and y1 - y0 > 1:
                        areas.append([round(x0 / sp.width, 5), round(y0 / sp.height, 5),
                                      round((x1 - x0) / sp.width, 5), round((y1 - y0) / sp.height, 5)])
                return areas

            if join.behind not in out:
                out[join.behind] = {"cell": join.cell, "seed": join.seed, "map": [0.0, 0.0, 1.0],
                                    "size": [B.width, B.height], "areas": shares(join.draw_band, B, None)}
            if join.front not in out:
                out[join.front] = {"cell": join.cell, "seed": join.seed,
                                   "map": [round(x, 2), round(y, 2), round(s, 5)],
                                   "size": [F.width, F.height], "areas": shares(join.draw_strip, F, (x, y, s))}
        return out

    def fits(self) -> list[dict]:
        return [dict(j.fit) for j in self.joins]

    # ---- a frame ----
    def _ideal(self, join: Join, rect, step: float):
        """The pattern over ``rect`` of the back screen, a sample every ``step`` pixels."""
        import numpy as np

        xs = np.arange(rect[0] + step / 2, rect[2], step)
        ys = np.arange(rect[1] + step / 2, rect[3], step)
        i = np.floor(xs / join.cell).astype(np.int64)
        j = np.floor(ys / join.cell).astype(np.int64)
        h = (i[None, :] * 73856093 ^ j[:, None] * 19349663 ^ join.seed * 83492791) & 0xFFFFFFFF
        h ^= h >> 13
        h = (h * 0x5BD1E995) & 0xFFFFFFFF
        h ^= h >> 15
        return (h & 1).astype(np.float32)

    def _shift(self, gray, join: Join, rect, view) -> tuple[float, float] | None:
        """How far the pattern seen over ``rect`` of the back screen is from
        where it should be there, in that screen's pixels (None: not seen)."""
        import cv2
        import numpy as np

        from curslink.lineup_check import _project

        r, t = view
        B = self.sol.screens[join.behind]
        step = join.cell / SAMPLES
        reach = REACH * join.cell
        xs = np.arange(rect[0] - reach + step / 2, rect[2] + reach, step)
        ys = np.arange(rect[1] - reach + step / 2, rect[3] + reach, step)
        grid = np.stack(np.meshgrid(xs, ys), axis=-1).reshape(-1, 2)
        seen = _sample(gray, _project(self.intr, r, t, B.at(grid)).reshape(len(ys), len(xs), 2))
        if np.isnan(seen).mean() > 0.02:
            return None
        seen = np.nan_to_num(seen, nan=float(np.nanmean(seen))).astype(np.float32)
        want = cv2.GaussianBlur(self._ideal(join, rect, step), (0, 0), max(0.8, 0.12 * SAMPLES))
        if want.shape[0] > seen.shape[0] or want.shape[1] > seen.shape[1] or float(want.std()) < 0.05:
            return None
        score = cv2.matchTemplate(seen, want, cv2.TM_CCOEFF_NORMED)
        _lo, best, _at, (px, py) = cv2.minMaxLoc(score)
        if best < SCORE or not (0 < px < score.shape[1] - 1 and 0 < py < score.shape[0] - 1):
            return None

        def fine(a: float, b: float, c: float) -> float:
            d = a - 2 * b + c
            return 0.0 if abs(d) < 1e-9 else float(np.clip(0.5 * (a - c) / d, -0.5, 0.5))

        dx = px + fine(score[py, px - 1], score[py, px], score[py, px + 1])
        dy = py + fine(score[py - 1, px], score[py, px], score[py + 1, px])
        middle_x, middle_y = (score.shape[1] - 1) / 2, (score.shape[0] - 1) / 2
        return (dx - middle_x) * step, (dy - middle_y) * step

    def frame(self, gray, sightings: list, locate) -> bool:
        """Read a frame and move each fit by the break that's seen. True if a
        fit moved (the strips want drawing again)."""
        import numpy as np

        from curslink.lineup_scene import project_from

        self.frames += 1
        moved = False
        any_read = False
        for join in self.joins:
            F, B = self.sol.screens[join.front], self.sol.screens[join.behind]
            mine = [s for s in sightings if s.key == join.behind] or [s for s in sightings if s.key == join.front]
            view = locate(mine)
            if view is None:
                join.out = None
                continue
            # Where the camera is, for that allowance: by both screens (two
            # planes at two depths place it far better than one does).
            both = locate([s for s in sightings if s.key in (join.front, join.behind)]) or view
            cam = -(both[0].T @ both[1])
            centres, breaks = [], []
            for patch in join.patches:
                band = self._shift(gray, join, patch.band, view)
                strip = self._shift(gray, join, patch.strip, view) if band is not None else None
                if band is None or strip is None:
                    patch.out = None
                    continue
                d = np.array(strip) - np.array(band)
                # That's the break from where the camera is now. From the
                # eye (where the scan began) the front screen's edge is seen
                # over a slightly different part of the back one: by the
                # desk as solved, how much (a small difference, so little
                # hangs on how right the desk is).
                middle = np.array([((patch.strip[0] + patch.strip[2]) / 2, (patch.strip[1] + patch.strip[3]) / 2)])
                there = F.at((middle - (join.fit["x"], join.fit["y"])) / join.fit["scale"])
                from_cam, from_eye = project_from(cam, B, there), project_from(self.sol.eye, B, there)
                if from_cam is not None and from_eye is not None:
                    d = d - (from_cam[0] - from_eye[0])
                patch.out = float(np.linalg.norm(d))
                centres.append(((patch.strip[0] + patch.strip[2]) / 2, (patch.strip[1] + patch.strip[3]) / 2))
                breaks.append(d)
            if not breaks:
                join.out = None
                continue
            any_read = True
            join.read += 1
            centres, breaks = np.array(centres), np.array(breaks)
            fit = join.fit
            # The strip shows, at the back screen's (x, y), the pattern from
            # (x, y) - break: the fit is out by the break. With patches well
            # spread, how the break changes along them is the scale being out.
            shift, grow = np.median(breaks, axis=0), 0.0
            u = (centres - (fit["x"], fit["y"])) / fit["scale"]  # where the patches are on the front screen
            spread = float(np.ptp(centres, axis=0).max())
            if len(breaks) >= 3 and spread > 0.2 * min(B.width, B.height):
                # break = shift + grow * u, least squares
                a = np.zeros((2 * len(u), 3))
                a[0::2, 0], a[1::2, 1] = 1.0, 1.0
                a[0::2, 2], a[1::2, 2] = u[:, 0], u[:, 1]
                sol, *_ = np.linalg.lstsq(a, breaks.ravel(), rcond=None)
                if abs(sol[2]) < 0.05 * fit["scale"]:
                    shift, grow = sol[:2], float(sol[2])
            at_patches = shift + grow * u.mean(axis=0)
            join.wants.append((fit["x"] + shift[0], fit["y"] + shift[1], fit["scale"] + grow))
            join.out = float(np.linalg.norm(at_patches))
            enough = max(DONE_PX, DONE * min(B.width, B.height))
            if join.out <= enough:
                join.good += 1
                continue
            join.good = 0
            gain = GAIN if join.out > 3 * enough else 0.6 * GAIN
            fit["x"] = float(fit["x"] + gain * shift[0])
            fit["y"] = float(fit["y"] + gain * shift[1])
            fit["scale"] = float(fit["scale"] + gain * grow)
            join.moved += gain * join.out
            moved = True
        if any_read:
            self.measured += 1
        if self.frames >= MAX_FRAMES and not self.joined:
            # Out of time with the seam still moving about (a phone in the
            # hand: every centimetre it moves, a laptop's edge moves
            # millimetres against the monitor behind). The middle of what
            # the last frames asked for.
            for join in self.joins:
                if len(join.wants) >= 3 and join.good < AGREE:
                    x, y, scale = np.median(np.array(join.wants[-8:]), axis=0)
                    join.fit.update(x=float(x), y=float(y), scale=float(scale))
                    moved = True
        return moved

    @property
    def joined(self) -> bool:
        """Has every join that can be read been unbroken, frames in a row?"""
        read = [j for j in self.joins if j.read]
        return bool(read) and all(j.good >= AGREE for j in read)

    @property
    def done(self) -> bool:
        if not self.joins or self.frames >= MAX_FRAMES:
            return True
        if self.frames >= UNSEEN and not self.measured:
            return True  # the pattern isn't there to be seen (older Curslink on the screens)
        return self.joined

    def worst(self) -> float | None:
        """The biggest break last read, in the back screen's pixels (None: none read)."""
        outs = [j.out for j in self.joins if j.out is not None]
        return max(outs) if outs else None

    def summary(self) -> dict:
        state = "unseen" if not self.measured else "joined" if self.joined else "part"
        return {"state": state, "joins": len(self.joins), "left": self.worst(),
                "moved": float(sum(j.moved for j in self.joins))}
