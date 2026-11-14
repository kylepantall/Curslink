"""The desk in 3D, from many views of the screens' patterns (numpy only).

Auto line-up (curslink.lineup) used to read one photo and measure every
screen in the plane of one of them, which is only right when all the
screens stand in one plane. Real desks don't: side monitors are turned in,
a laptop stands nearer than the monitor behind it, and one photo can't tell
a small screen that's near from a big one that's far.

So this is the measuring done properly, as a camera calibration is:

* Each screen is a flat rectangle somewhere in space: a rotation, a place
  and a size (how big one of its pixels is, against the reference screen's).
  The reference screen is the world: its top left is the origin, x runs
  along its top edge, y down its left edge, z into it, in its own pixels.
* The camera is a pinhole with a focal length, a principal point and two
  terms of lens distortion, the same for every frame, and stands somewhere
  else in each frame.
* What's seen is where each pattern's corners are in each frame
  (:class:`Sighting`). :meth:`Scene.solve` finds the screens, the camera and
  where it stood in every frame that make all of those corners land where
  they were seen: a bundle adjustment (Levenberg-Marquardt with a Huber
  loss, so a misread corner pulls like a far smaller one).

Why moving the camera matters (the figure of 8): seen from one place, a
screen twice the size and twice as far looks the same. Seen from two, the
nearer one shifts more against the others (parallax), which fixes how far
each screen is and so how big; and the tilted views are what fix the
camera's focal length. Where a monitor says how big it is (its EDID, see
curslink.screensize) that is used too, as a measurement with an error bar.

The solve also says how sure it is: the covariance of every screen's place
(from the same equations), which is what "aligned" is decided on.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

MAX_FRAMES = 40  # views kept; beyond this the least different one makes way
MIN_MARKERS = 2  # patterns of a screen that must agree for it to count in a frame
# A frame is a new view when the screens' corners moved this far (a share of
# the frame's diagonal) from every view kept so far.
NEW_VIEW = 0.035
HUBER = 1.2  # px (in a 1600 px frame): corners further off than this pull less
# How well a monitor's own idea of its size is believed (EDIDs round to the
# mm or the cm, and a bezel hides a little of the panel).
SIZE_SIGMA = 0.03
# Without a size or any parallax, a screen is taken to stand near the
# reference screen's plane: within about this many reference widths.
DEPTH_SIGMA = 0.6


# -- rotations ------------------------------------------------------------------

def exp_so3(w: np.ndarray) -> np.ndarray:
    """Rotation matrices of the rotation vectors ``w`` (..., 3)."""
    w = np.asarray(w, np.float64)
    theta = np.linalg.norm(w, axis=-1)[..., None, None]
    k = np.zeros(w.shape[:-1] + (3, 3))
    k[..., 0, 1], k[..., 0, 2] = -w[..., 2], w[..., 1]
    k[..., 1, 0], k[..., 1, 2] = w[..., 2], -w[..., 0]
    k[..., 2, 0], k[..., 2, 1] = -w[..., 1], w[..., 0]
    small = theta < 1e-9
    t = np.where(small, 1.0, theta)
    a = np.where(small, 1.0 - theta ** 2 / 6, np.sin(t) / t)
    b = np.where(small, 0.5 - theta ** 2 / 24, (1 - np.cos(t)) / t ** 2)
    return np.eye(3) + a * k + b * (k @ k)


def nearest_rotation(m: np.ndarray) -> np.ndarray:
    u, _s, vt = np.linalg.svd(m)
    r = u @ vt
    if np.linalg.det(r) < 0:
        u[:, -1] *= -1
        r = u @ vt
    return r


def angle_between(a: np.ndarray, b: np.ndarray) -> float:
    c = (np.trace(a.T @ b) - 1) / 2
    return float(np.arccos(min(1.0, max(-1.0, c))))


# -- homographies ---------------------------------------------------------------

def fit_homography(src: np.ndarray, dst: np.ndarray) -> np.ndarray | None:
    """The homography taking ``src`` (n, 2) to ``dst`` (n, 2), n >= 4, by the
    normalised direct linear transform."""
    src, dst = np.asarray(src, np.float64), np.asarray(dst, np.float64)
    if len(src) < 4:
        return None

    def norm(p):
        c = p.mean(0)
        s = np.sqrt(2) / max(1e-12, np.sqrt(((p - c) ** 2).sum(1)).mean())
        return (p - c) * s, np.array([[s, 0, -s * c[0]], [0, s, -s * c[1]], [0, 0, 1]])

    a, ta = norm(src)
    b, tb = norm(dst)
    rows = np.zeros((2 * len(a), 9))
    rows[0::2, 0:2], rows[0::2, 2] = a, 1
    rows[0::2, 6:8], rows[0::2, 8] = -b[:, :1] * a, -b[:, 0]
    rows[1::2, 3:5], rows[1::2, 5] = a, 1
    rows[1::2, 6:8], rows[1::2, 8] = -b[:, 1:] * a, -b[:, 1]
    try:
        _u, _s, vt = np.linalg.svd(rows)
    except np.linalg.LinAlgError:
        return None
    h = np.linalg.inv(tb) @ vt[-1].reshape(3, 3) @ ta
    if abs(h[2, 2]) < 1e-12:
        return None
    return h / h[2, 2]


def apply_homography(h: np.ndarray, p: np.ndarray) -> np.ndarray:
    p = np.asarray(p, np.float64)
    q = p @ h[:2, :2].T + h[:2, 2]
    w = p @ h[2, :2] + h[2, 2]
    w = np.where(np.abs(w) < 1e-12, 1e-12, w)
    return q / w[:, None]


@dataclass
class Sighting:
    """One screen in one frame: where its patterns' corners were seen."""

    key: str
    uv: np.ndarray  # (n, 2) on the screen, in its own pixels
    xy: np.ndarray  # (n, 2) in the frame's pixels
    markers: int  # how many patterns those corners are of (4 corners each)
    h: np.ndarray  # the homography uv -> xy that fits them
    rms: float  # how well, in the frame's pixels


def sight(key: str, marker_uv: list, marker_xy: list, tolerance: float) -> Sighting | None:
    """A screen's sighting from its patterns read in a frame, without the
    ones that don't agree with the rest (a misread pattern, a reflection).

    ``marker_uv[i]`` and ``marker_xy[i]`` are the 4 corners of pattern i on
    the screen and in the frame. Every pair of patterns proposes where the
    screen is (a homography from their 8 corners); the proposal most patterns
    agree with, to within ``tolerance`` px, wins. None if fewer than
    :data:`MIN_MARKERS` agree.
    """
    n = len(marker_uv)
    if n < MIN_MARKERS:
        return None
    uv = [np.asarray(m, np.float64).reshape(4, 2) for m in marker_uv]
    xy = [np.asarray(m, np.float64).reshape(4, 2) for m in marker_xy]

    def agreeing(h):
        return [i for i in range(n) if np.abs(apply_homography(h, uv[i]) - xy[i]).max() <= tolerance]

    best: list[int] = []
    h_all = fit_homography(np.concatenate(uv), np.concatenate(xy))
    if h_all is not None:
        best = agreeing(h_all)
    if len(best) < n:
        for i in range(n):
            for j in range(i + 1, n):
                h = fit_homography(np.concatenate([uv[i], uv[j]]), np.concatenate([xy[i], xy[j]]))
                if h is None:
                    continue
                inl = agreeing(h)
                if len(inl) > len(best):
                    best = inl
    if len(best) < MIN_MARKERS:
        return None
    for _ in range(2):  # refit on those that agree, and ask again
        a, b = np.concatenate([uv[i] for i in best]), np.concatenate([xy[i] for i in best])
        h = fit_homography(a, b)
        if h is None:
            return None
        again = agreeing(h)
        if len(again) < MIN_MARKERS:
            return None
        if again == best:
            break
        best = again
    a, b = np.concatenate([uv[i] for i in best]), np.concatenate([xy[i] for i in best])
    h = fit_homography(a, b)
    if h is None:
        return None
    rms = float(np.sqrt(((apply_homography(h, a) - b) ** 2).sum(1).mean()))
    return Sighting(key, a, b, len(best), h, rms)


def pose_from_homography(h: np.ndarray, f: float, cx: float, cy: float) -> tuple[np.ndarray, np.ndarray] | None:
    """Where a flat screen is from the camera, from its homography: (R, t)
    with a point (u, v, 0) of the screen at ``R @ (u, v, 0) + t`` in front of
    the camera, in the screen's own units."""
    a = np.array([[1 / f, 0, -cx / f], [0, 1 / f, -cy / f], [0, 0, 1]]) @ h
    n1, n2 = np.linalg.norm(a[:, 0]), np.linalg.norm(a[:, 1])
    if n1 < 1e-12 or n2 < 1e-12:
        return None
    lam = 2 / (n1 + n2)
    if a[2, 2] * lam < 0:
        lam = -lam
    r1, r2, t = a[:, 0] * lam, a[:, 1] * lam, a[:, 2] * lam
    r = nearest_rotation(np.column_stack([r1, r2, np.cross(r1, r2)]))
    if t[2] <= 0:
        return None
    return r, t


def focal_from_homographies(hs: list[np.ndarray], cx: float, cy: float) -> float | None:
    """The focal length that makes these views of flat screens views of
    rectangles with square pixels (Zhang's constraints, principal point
    known). None when the views are too head-on to tell."""
    num = den = 0.0
    for h in hs:
        c = np.array([[1, 0, -cx], [0, 1, -cy], [0, 0, 1.0]]) @ h
        c = c / max(1e-12, np.linalg.norm(c[:, 0]))
        h1, h2 = c[:, 0], c[:, 1]
        # a * f^2 = b, twice
        for a, b in ((h1[2] * h2[2], -(h1[0] * h2[0] + h1[1] * h2[1])),
                     (h1[2] ** 2 - h2[2] ** 2, h2[0] ** 2 + h2[1] ** 2 - h1[0] ** 2 - h1[1] ** 2)):
            num += a * b
            den += a * a
    if den < 1e-18:
        return None
    f2 = num / den
    return float(np.sqrt(f2)) if f2 > 0 else None


# -- the scene ------------------------------------------------------------------

@dataclass
class Frame:
    sightings: dict[str, Sighting]
    r: np.ndarray | None = None  # world -> camera, once solved
    t: np.ndarray | None = None
    rms: float = 0.0
    first: bool = False  # the view the scan began from: where the person's eyes are


@dataclass
class ScreenPose:
    """Where a screen is: a point (u, v) of it, in its pixels, is at
    ``r @ (scale * (u, v, 0)) + t`` in the world (the reference's pixels)."""

    key: str
    width: float
    height: float
    r: np.ndarray
    t: np.ndarray
    scale: float
    views: int = 0  # the frames it was seen in
    error: float = 0.0  # how unsure its place still is, in reference pixels (1 sigma)

    def at(self, uv) -> np.ndarray:
        uv = np.atleast_2d(np.asarray(uv, np.float64))
        return (uv * self.scale) @ self.r[:, :2].T + self.t

    def corners(self) -> np.ndarray:
        """Top left, top right, bottom right, bottom left, in the world."""
        return self.at([(0, 0), (self.width, 0), (self.width, self.height), (0, self.height)])

    @property
    def centre(self) -> np.ndarray:
        return self.at([(self.width / 2, self.height / 2)])[0]

    @property
    def normal(self) -> np.ndarray:
        """Out of its back (it faces along the opposite)."""
        return self.r[:, 2]


@dataclass
class Solution:
    reference: str
    screens: dict[str, ScreenPose]
    eye: np.ndarray  # where the person looks from: where the camera was when the scan began
    focal: float
    centre: tuple[float, float]
    distortion: tuple[float, float]
    rms: float  # how far the corners land from where they were seen, in frame pixels
    frames: int
    cameras: list[np.ndarray] = field(default_factory=list)  # where it stood in each view


class SceneError(RuntimeError):
    pass


class Scene:
    """Views of the screens' patterns, and the desk they're views of.

    ``screens``: ``{key: (width, height)}`` in each screen's pixels.
    ``sizes``: ``{key: (width_mm, height_mm)}`` where known. ``focal``: the
    camera's focal length in frame pixels where the phone says.
    """

    def __init__(self, screens: dict[str, tuple[float, float]], reference: str | None = None,
                 sizes: dict[str, tuple[float, float]] | None = None,
                 image_size: tuple[int, int] = (1600, 1200), focal: float | None = None):
        self.dims = {k: (float(w), float(h)) for k, (w, h) in screens.items()}
        self.wanted = reference if reference in self.dims else None
        self.reference = self.wanted
        self._f0 = 0.0
        self.pitch = {}  # mm per pixel
        for k, (wmm, hmm) in (sizes or {}).items():
            if k in self.dims and wmm > 0 and hmm > 0:
                w, h = self.dims[k]
                px, py = wmm / w, hmm / h
                if abs(px / py - 1) < 0.08:  # square pixels, or the size is made up
                    self.pitch[k] = (px + py) / 2
        self.image_size = (int(image_size[0]), int(image_size[1]))
        self.focal_hint = float(focal) if focal and focal > 0 else None
        self.frames: list[Frame] = []
        self.intr: np.ndarray | None = None  # f, cx, cy, k1, k2
        self.poses: dict[str, tuple[np.ndarray, np.ndarray, float]] = {}
        self.cov: dict[str, np.ndarray] = {}
        self.sigma = 1.0
        self.dropped = 0
        self.started = False

    # ---- taking frames in ----
    @property
    def long_side(self) -> float:
        return float(max(self.image_size))

    @property
    def tolerance(self) -> float:
        """How far a pattern may be from where the others put it, in frame px."""
        return max(2.5, 0.004 * self.long_side)

    def _signature(self, frame: Frame) -> dict[str, np.ndarray]:
        out = {}
        for key, s in frame.sightings.items():
            w, h = self.dims[key]
            out[key] = apply_homography(s.h, np.array([(0, 0), (w, 0), (w, h), (0, h)], np.float64))
        return out

    def _distance(self, a: dict, b: dict) -> float:
        common = [k for k in a if k in b]
        if not common or set(a) != set(b):
            return 1.0  # other screens in view: another view altogether
        diag = float(np.hypot(*self.image_size))
        return float(np.mean([np.linalg.norm(a[k] - b[k], axis=1).mean() for k in common])) / diag

    def add(self, sightings: list[Sighting]) -> bool:
        """Take a frame's sightings; True if it was kept as a new view."""
        ok = {s.key: s for s in sightings
              if s.key in self.dims and s.markers >= MIN_MARKERS and s.rms <= self.tolerance}
        if not ok:
            return False
        frame = Frame(ok, first=not self.frames and not self.started)
        self.started = True
        sig = self._signature(frame)
        sigs = [self._signature(f) for f in self.frames]
        if sigs and min(self._distance(sig, s) for s in sigs) < NEW_VIEW:
            return False
        self.frames.append(frame)
        if len(self.frames) > MAX_FRAMES:
            sigs.append(sig)
            # The view most like another one makes way (never the newest).
            nearest = []
            for i, a in enumerate(sigs[:-1]):
                near = min(self._distance(a, b) for j, b in enumerate(sigs) if j != i)
                nearest.append(2.0 if self.frames[i].first else near)  # (nor the first)
            del self.frames[int(np.argmin(nearest))]
        return True

    def seen(self) -> dict[str, int]:
        """How many kept views each screen is in."""
        out: dict[str, int] = {}
        for f in self.frames:
            for k in f.sightings:
                out[k] = out.get(k, 0) + 1
        return out

    def together(self) -> dict[frozenset, int]:
        """How many kept views each pair of screens share."""
        out: dict[frozenset, int] = {}
        for f in self.frames:
            keys = sorted(f.sightings)
            for i, a in enumerate(keys):
                for b in keys[i + 1:]:
                    out[frozenset((a, b))] = out.get(frozenset((a, b)), 0) + 1
        return out

    # ---- first guesses ----
    def _first_intrinsics(self) -> np.ndarray:
        w, h = self.image_size
        cx, cy = w / 2, h / 2
        f = self.focal_hint
        if f is None:
            hs = [s.h for fr in self.frames for s in fr.sightings.values()]
            f = focal_from_homographies(hs, cx, cy)
            if f is None or not 0.35 * self.long_side < f < 3.0 * self.long_side:
                f = 0.8 * self.long_side  # a phone's main camera, about
        return np.array([f, cx, cy, 0.0, 0.0])

    def _view(self, s: Sighting) -> tuple[np.ndarray, np.ndarray] | None:
        f, cx, cy = self.intr[:3]
        return pose_from_homography(s.h, f, cx, cy)

    def _pair(self, a: str, b: str) -> tuple[np.ndarray, np.ndarray, float] | None:
        """Screen b from screen a, from the frames with both: (R, t in a's
        pixels, how many of a's pixels one of b's is)."""
        rs, ps, qs = [], [], []
        for fr in self.frames:
            if a in fr.sightings and b in fr.sightings:
                va, vb = self._view(fr.sightings[a]), self._view(fr.sightings[b])
                if va is None or vb is None:
                    continue
                rs.append(va[0].T @ vb[0])
                ps.append(va[0].T @ vb[1])
                qs.append(va[0].T @ va[1])
        if not rs:
            return None
        # The rotation most of the frames agree on (a flat screen seen nearly
        # head-on can come out tilted the wrong way in a frame or two).
        cost = [sum(angle_between(r, o) for o in rs) for r in rs]
        mid = rs[int(np.argmin(cost))]
        near = [r for r in rs if angle_between(r, mid) < np.radians(8)]
        rot = nearest_rotation(np.mean(near, axis=0))
        ps, qs = np.array(ps), np.array(qs)
        wb, hb = self.dims[b]
        cb = rot @ np.array([wb / 2, hb / 2, 0.0])
        # In the reference's plane, about: the size that puts b's middle there.
        flat = np.median(qs[:, 2] / np.where(np.abs(ps[:, 2] + cb[2]) < 1e-9, 1e-9, ps[:, 2] + cb[2]))
        rho = None
        if a in self.pitch and b in self.pitch:
            rho = self.pitch[b] / self.pitch[a]
        elif len(ps) >= 3:
            # tau - rho * p = -q in every frame: the camera's moving about
            # (parallax) is what tells rho. Solved with a pull towards "flat".
            spread = np.linalg.norm(ps - ps.mean(0), axis=1).mean() / max(1e-9, np.linalg.norm(ps.mean(0)))
            if spread > 0.03:
                w = np.ones(len(ps))
                for _ in range(4):
                    m = np.zeros((3 * len(ps) + 1, 4))
                    y = np.zeros(3 * len(ps) + 1)
                    for i, (p, q) in enumerate(zip(ps, qs)):
                        m[3 * i:3 * i + 3, :3] = np.eye(3) * w[i]
                        m[3 * i:3 * i + 3, 3] = -p * w[i]
                        y[3 * i:3 * i + 3] = -q * w[i]
                    pull = 0.05 * np.linalg.norm(ps.mean(0))
                    m[-1, 3], y[-1] = pull, pull * flat
                    sol, *_ = np.linalg.lstsq(m, y, rcond=None)
                    res = np.linalg.norm(sol[:3] - sol[3] * ps + qs, axis=1)
                    scale = max(1e-9, 1.5 * np.median(res))
                    w = np.minimum(1.0, scale / np.maximum(res, 1e-12))
                if 0.05 < sol[3] < 20:
                    rho = float(sol[3])
        if rho is None:
            rho = float(flat) if 0.05 < flat < 20 else 1.0
        tau = np.median(rho * ps - qs, axis=0)
        return rot, tau, rho

    def _grow(self) -> None:
        """First guesses for the screens that have none, from those that do."""
        ref = self.reference
        if ref not in self.poses:
            self.poses[ref] = (np.eye(3), np.zeros(3), 1.0)
        shared = self.together()
        while True:
            best = None
            for pair, n in shared.items():
                a, b = tuple(pair)
                for known, new in ((a, b), (b, a)):
                    if known in self.poses and new not in self.poses:
                        if best is None or n > best[0]:
                            best = (n, known, new)
            if best is None:
                return
            _n, a, b = best
            rel = self._pair(a, b)
            if rel is None:
                shared.pop(frozenset((a, b)))
                continue
            rot, tau, rho = rel
            ra, ta, sa = self.poses[a]
            self.poses[b] = (ra @ rot, ra @ (sa * tau) + ta, sa * rho)

    def _place_cameras(self) -> None:
        for fr in self.frames:
            if fr.r is not None:
                continue
            known = [s for s in fr.sightings.values() if s.key in self.poses]
            if not known:
                continue
            s = max(known, key=lambda s: (s.markers, -s.rms))
            view = self._view(s)
            if view is None:
                continue
            ra, ta, sa = self.poses[s.key]
            fr.r = view[0] @ ra.T
            fr.t = sa * view[1] - fr.r @ ta

    # ---- the bundle adjustment ----
    def _pack(self):
        keys = [k for k in self.poses]
        keys.sort(key=lambda k: (k != self.reference, k))
        frames = [fr for fr in self.frames if fr.r is not None and any(k in self.poses for k in fr.sightings)]
        index = {k: i for i, k in enumerate(keys)}
        fi, si, uv, xy = [], [], [], []
        for i, fr in enumerate(frames):
            for k, s in fr.sightings.items():
                if k in index:
                    fi.append(np.full(len(s.uv), i))
                    si.append(np.full(len(s.uv), index[k]))
                    uv.append(s.uv)
                    xy.append(s.xy)
        return keys, frames, np.concatenate(fi), np.concatenate(si), np.concatenate(uv), np.concatenate(xy)

    @staticmethod
    def _project(intr, rc, tc, rs, ts, ss, fi, si, uv):
        r = rs[si]
        s = ss[si]
        xw = r[:, :, 0] * (uv[:, :1] * s[:, None]) + r[:, :, 1] * (uv[:, 1:] * s[:, None]) + ts[si]
        xc = np.einsum("nij,nj->ni", rc[fi], xw) + tc[fi]
        z = np.where(xc[:, 2] < 1e-6, 1e-6, xc[:, 2])
        x, y = xc[:, 0] / z, xc[:, 1] / z
        r2 = x * x + y * y
        d = 1 + intr[3] * r2 + intr[4] * r2 * r2
        return np.column_stack([intr[0] * d * x + intr[1], intr[0] * d * y + intr[2]])

    def _priors(self, keys, intr, rs, ts, ss):
        """What's believed before looking, as residuals (each already over its sigma)."""
        w, h = self.image_size
        long = self.long_side
        out = []
        if self.focal_hint is not None:
            out.append((intr[0] - self.focal_hint) / (0.08 * self.focal_hint))
        else:
            out.append((intr[0] - self._f0) / (0.6 * self._f0))
        out.append((intr[1] - w / 2) / (0.03 * long))
        out.append((intr[2] - h / 2) / (0.03 * long))
        out.append(intr[3] / 0.08)
        out.append(intr[4] / 0.08)
        ref = self.reference
        ref_w = self.dims[ref][0]
        for i, k in enumerate(keys):
            if i == 0:
                continue
            if k in self.pitch and ref in self.pitch:
                out.append(np.log(ss[i] / (self.pitch[k] / self.pitch[ref])) / SIZE_SIGMA)
            else:
                wk, hk = self.dims[k]
                mid = rs[i] @ (ss[i] * np.array([wk / 2, hk / 2, 0.0])) + ts[i]
                out.append(mid[2] / (DEPTH_SIGMA * ref_w))
        return np.array(out)

    def _adjust(self, rounds: int = 25) -> tuple[float, np.ndarray, list, list]:
        keys, frames, fi, si, uv, xy = self._pack()
        nf, ns, n = len(frames), len(keys), len(uv)
        intr = self.intr.copy()
        rc = np.array([f.r for f in frames])
        tc = np.array([f.t for f in frames])
        rs = np.array([self.poses[k][0] for k in keys])
        ts = np.array([self.poses[k][1] for k in keys])
        ss = np.array([self.poses[k][2] for k in keys])
        huber = HUBER * self.long_side / 1600
        cam0, scr0 = 5, 5 + 6 * nf
        size = scr0 + 7 * (ns - 1)
        rows = np.arange(n)
        free = si > 0  # the reference screen isn't moved

        def state(dx):
            i2 = intr + dx[:5]
            dc = dx[cam0:scr0].reshape(nf, 6)
            rc2 = exp_so3(dc[:, :3]) @ rc
            tc2 = tc + dc[:, 3:]
            ds = np.zeros((ns, 7))
            ds[1:] = dx[scr0:].reshape(ns - 1, 7)
            rs2 = exp_so3(ds[:, :3]) @ rs
            ts2 = ts + ds[:, 3:6]
            ss2 = ss * np.exp(ds[:, 6])
            return i2, rc2, tc2, rs2, ts2, ss2

        def residuals(st):
            return self._project(st[0], st[1], st[2], st[3], st[4], st[5], fi, si, uv) - xy

        def priors(st):
            return self._priors(keys, st[0], st[3], st[4], st[5])

        # Steps for the numeric derivatives, per kind of unknown.
        span = float(np.abs(ts).max() + np.abs(tc).max() + 1.0)
        step_i = np.array([1e-4 * intr[0], 1e-4 * intr[0], 1e-4 * intr[0], 1e-6, 1e-6])
        step_c = np.array([1e-7] * 3 + [1e-7 * span] * 3)
        step_s = np.array([1e-7] * 3 + [1e-7 * span] * 3 + [1e-7])

        def jacobian(st0, r0, p0):
            j = np.zeros((2 * n + len(p0), size))
            # Every corner is seen by one camera and is on one screen, so one
            # nudge of (say) every camera's x at once gives all of those columns.
            for k in range(5):
                dx = np.zeros(size)
                dx[k] = step_i[k]
                st = state(dx)
                j[:2 * n, k] = ((residuals(st) - r0) / step_i[k]).ravel()
                j[2 * n:, k] = (priors(st) - p0) / step_i[k]
            for k in range(6):
                dx = np.zeros(size)
                dx[cam0 + k:scr0:6] = step_c[k]
                d = (residuals(state(dx)) - r0) / step_c[k]
                j[2 * rows, cam0 + 6 * fi + k] = d[:, 0]
                j[2 * rows + 1, cam0 + 6 * fi + k] = d[:, 1]
            for k in range(7):
                if ns < 2:
                    break
                dx = np.zeros(size)
                dx[scr0 + k::7] = step_s[k]
                st = state(dx)
                d = (residuals(st) - r0) / step_s[k]
                cols = scr0 + 7 * (si[free] - 1) + k
                j[2 * rows[free], cols] = d[free, 0]
                j[2 * rows[free] + 1, cols] = d[free, 1]
                # each screen's own prior row
                dp = (priors(st) - p0) / step_s[k]
                for i in range(1, ns):
                    j[2 * n + 5 + (i - 1), scr0 + 7 * (i - 1) + k] = dp[5 + (i - 1)]
            return j

        def weights(r):
            e = np.sqrt((r ** 2).sum(1))
            return np.minimum(1.0, huber / np.maximum(e, 1e-12))

        def cost(r, p):
            e = np.sqrt((r ** 2).sum(1))
            rho = np.where(e <= huber, 0.5 * e ** 2, huber * (e - 0.5 * huber))
            return float(rho.sum() + 0.5 * (p ** 2).sum())

        lam = 1e-3
        zero = np.zeros(size)
        st = state(zero)
        r, p = residuals(st), priors(st)
        c = cost(r, p)
        jac = None
        for _ in range(rounds):
            jac = jacobian(st, r, p)
            w = np.sqrt(np.repeat(weights(r), 2))
            jw = jac.copy()
            jw[:2 * n] *= w[:, None]
            rw = np.concatenate([r.ravel() * w, p])
            hess = jw.T @ jw
            grad = jw.T @ rw
            diag = np.maximum(np.diag(hess), 1e-12)
            moved = False
            for _try in range(8):
                try:
                    dx = np.linalg.solve(hess + lam * np.diag(diag), -grad)
                except np.linalg.LinAlgError:
                    lam *= 10
                    continue
                st2 = state(dx)
                r2, p2 = residuals(st2), priors(st2)
                c2 = cost(r2, p2)
                if np.isfinite(c2) and c2 < c:
                    gain = (c - c2) / max(c, 1e-12)
                    intr, rc, tc, rs, ts, ss = st2
                    st, r, p, c = st2, r2, p2, c2
                    lam = max(lam / 4, 1e-9)
                    moved = True
                    break
                lam *= 5
            if not moved or gain < 1e-7:
                break
        self.intr = intr
        for i, fr in enumerate(frames):
            fr.r, fr.t = rc[i], tc[i]
            mine = fi == i
            fr.rms = float(np.sqrt((r[mine] ** 2).sum(1).mean())) if mine.any() else 0.0
        for i, k in enumerate(keys):
            self.poses[k] = (rs[i], ts[i], float(ss[i]))
        # How sure: the noise that's really there, through the same equations.
        e = np.abs(r).ravel()
        sigma = max(0.05, 1.4826 * float(np.median(e)))
        self.sigma = sigma
        self.cov = {}
        if jac is not None and ns > 1:
            jac = jacobian(st, r, p)
            w = np.sqrt(np.repeat(weights(r), 2))
            jac[:2 * n] *= (w / sigma)[:, None]
            try:
                cov = np.linalg.inv(jac.T @ jac + 1e-12 * np.eye(size))
                for i, k in enumerate(keys):
                    if i:
                        a = scr0 + 7 * (i - 1)
                        self.cov[k] = cov[a:a + 7, a:a + 7]
            except np.linalg.LinAlgError:
                pass
        rms = float(np.sqrt((r ** 2).sum(1).mean()))
        return rms, r, frames, keys

    def solve(self) -> Solution:
        """The desk, from the views so far."""
        if not self.frames:
            raise SceneError("none of the screens' patterns have been seen yet")
        counts = self.seen()
        # The screen in the most views is the world until the one asked for turns up.
        best = self.wanted if self.wanted in counts else self.reference
        if best not in counts:
            best = max(counts, key=lambda k: (counts[k], k))
        if best != self.reference:
            self._forget()
            self.reference = best
        for attempt in range(2):
            if self.intr is None:
                self.intr = self._first_intrinsics()
                self._f0 = float(self.intr[0])
            self._grow()
            self._place_cameras()
            if not any(fr.r is not None for fr in self.frames):
                self._forget()
                raise SceneError("the patterns are too slanted or too small in the picture to use")
            rms, _r, frames, _keys = self._adjust()
            # Frames that don't fit (blurred as the phone moved, or half read) go.
            if len(frames) > 3:
                med = float(np.median([f.rms for f in frames]))
                bad = [f for f in frames if f.rms > max(3 * med, 2.0 * self.long_side / 1600) and not f.first]
                if bad and len(bad) <= len(frames) // 3:
                    self.frames = [f for f in self.frames if not any(f is b for b in bad)]
                    self.dropped += len(bad)
                    rms, _r, frames, _keys = self._adjust(rounds=10)
            if rms <= max(2.5, 3 * self.sigma) * self.long_side / 1600 or attempt:
                break
            self._forget()  # a bad start: begin again from the frames alone
        return self._solution(rms, frames)

    def _forget(self) -> None:
        self.intr = None
        self.poses = {}
        self.cov = {}
        for fr in self.frames:
            fr.r = fr.t = None

    def _solution(self, rms: float, frames: list[Frame]) -> Solution:
        counts = self.seen()
        cams = [-(f.r.T @ f.t) for f in frames]
        # The eye: where the scan began (the phone held up in front of the
        # person's face), else the middle of where the camera went.
        began = [-(f.r.T @ f.t) for f in frames if f.first]
        eye = began[0] if began else np.mean(np.array(cams), axis=0)
        screens = {}
        for k, (r, t, s) in self.poses.items():
            w, h = self.dims[k]
            screens[k] = ScreenPose(k, w, h, r.copy(), t.copy(), float(s), counts.get(k, 0))
        sol = Solution(self.reference, screens, eye, float(self.intr[0]), (float(self.intr[1]), float(self.intr[2])),
                       (float(self.intr[3]), float(self.intr[4])), rms, len(frames), cams)
        for k, sp in screens.items():
            sp.error = self._error(sol, sp)
        return sol

    def _error(self, sol: Solution, sp: ScreenPose) -> float:
        """How unsure a screen's place is, as seen from the eye against the
        reference screen: 1 sigma of its corners there, in reference pixels."""
        if sp.key == sol.reference:
            return 0.0
        cov = self.cov.get(sp.key)
        if cov is None:
            return float("inf")
        ref = sol.screens[sol.reference]

        def corners(d):
            moved = ScreenPose(sp.key, sp.width, sp.height, exp_so3(d[:3]) @ sp.r, sp.t + d[3:6],
                               sp.scale * float(np.exp(d[6])))
            q = project_from(sol.eye, ref, moved.corners())
            return None if q is None else q.ravel()

        base = corners(np.zeros(7))
        if base is None:
            # Not in front of the reference from here: its middle's own doubt will do.
            return float(np.sqrt(np.trace(cov[3:6, 3:6])))
        span = float(np.abs(sp.t).max() + ref.width)
        steps = np.array([1e-6] * 3 + [1e-6 * span] * 3 + [1e-6])
        g = np.zeros((8, 7))
        for i in range(7):
            d = np.zeros(7)
            d[i] = steps[i]
            c = corners(d)
            if c is None:
                return float(np.sqrt(np.trace(cov[3:6, 3:6])))
            g[:, i] = (c - base) / steps[i]
        var = np.diag(g @ cov @ g.T)
        return float(np.sqrt(max(0.0, var.max())))


def project_from(eye: np.ndarray, onto: ScreenPose, points: np.ndarray) -> np.ndarray | None:
    """Where ``points`` (n, 3, in the world) are seen against the plane of
    the screen ``onto`` from ``eye``, in that screen's pixels (n, 2). None if
    any isn't in front of the eye, looking at that screen's face."""
    e = (onto.r.T @ (np.asarray(eye, np.float64) - onto.t)) / onto.scale
    p = ((np.asarray(points, np.float64) - onto.t) @ onto.r) / onto.scale
    if e[2] >= -1e-9:
        return None  # behind the screen
    den = e[2] - p[:, 2]
    if np.any(den >= -1e-9 * abs(e[2])):
        return None  # level with the eye or behind it
    lam = e[2] / den
    return e[:2] + lam[:, None] * (p[:, :2] - e[:2])
