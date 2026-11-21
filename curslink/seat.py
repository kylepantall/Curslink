"""Where you sit: the eye point a desk's overlaps are aimed at.

A live line-up (curslink.lineup) leaves the desk solved in 3D: where every
screen stands, and where the phone was when the scan began, which it takes
for your eyes. But the phone was held out at arm's length, and with a big
setup you sit further back than that: a laptop 25 cm before a monitor is
seen over a part of it some centimetres off from there.

This module keeps that desk (``seat.json``, next to the pairing files) and
works with the one thing the line-up didn't know, the *eye point*:

* :class:`PhoneSeat`: the last step of a line-up. The patterns stay up, you
  sit back with the phone at your eyes, and the solver that placed the
  screens places the phone.
* :func:`reaim`: every overlap's fit (curslink.layout.Fit) worked out again
  for an eye elsewhere, from the saved desk: no new scan.
* :func:`summary`: what the "Where you sit" card on the Layout page says:
  how far back you are, your eye level against the main screen's top edge,
  what the screen in front hides, and what moving something would change.
* :func:`from_looks` and :func:`locate_camera`: without a phone. You look at
  the computer's camera, then at each screen; where your head is before the
  camera (from the size of your face) and how far you turn between screens
  whose places are known say where you are. Rougher than the phone.

The desk is a plain dict (it's JSON on disk)::

    {"reference": key, "mm": one world unit in mm (or None),
     "screens": {key: {"width", "height", "r": 3 rows, "t": [x, y, z], "scale", "bend"}},
     "eye": [x, y, z],       # where the overlaps are aimed from now
     "scan_eye": [x, y, z],  # where the phone was when the scan began
     "how": "scan" | "phone" | "camera", "when": ISO time,
     "camera": {"screen", "at": [x, y, z], "r": 3 rows, "how": "assumed" | "found"}}

The world is the reference screen's pixels: x across it, y down, z away from
you into it (curslink.lineup_scene). The geometry here is plain Python (the
service and the Layout page read it without numpy); only what solves
something (the phone's place, the looks) needs numpy.
"""

from __future__ import annotations

import json
import logging
import math
import time
from pathlib import Path

log = logging.getLogger(__name__)

LENS_TO_EYE_MM = 20.0  # the phone rests on the bridge of the nose: its lens is about this far before the eyes

# The phone step.
STILL_MM = 20.0  # the phone is still: every sighting this near the middle of them...
STILL_S = 1.0  # ...over this long...
STILL_FRAMES = 4  # ...and at least this many
KEPT_S = 10.0  # (sightings older than this don't count: a slow link sends a frame a second or fewer)
MOVED_MM = 60.0  # it's been moved from where the pattern step left it (to the eyes, sitting back)
WAIT_S = 6.0  # ...or it's stayed where it was this long (it was at the eyes all along)
GIVE_UP_S = 45.0  # no still sighting in this long: the overlaps stay aimed at where the scan began

# What the card suggests by (placeholders: common workstation guidance).
EYE_ABOVE_MM = (-20.0, 100.0)  # the eyes against the main screen's top edge: from a little below it to this far above
SPREAD_DEG = 35.0  # the whole setup within this of straight ahead, each side
OFF_CENTRE_MM = 40.0  # a curved screen: this far off its centre line is worth saying
HIDDEN_SHARE = 0.2  # the screen in front hides this much of the one behind


def seat_path() -> Path:
    from curslink.pairing.store import default_dir

    return default_dir() / "seat.json"


def load(path: Path | None = None) -> dict | None:
    """The saved desk, or None if there's none (or it's not one)."""
    try:
        desk = json.loads((path or seat_path()).read_text())
    except (OSError, ValueError):
        return None
    return desk if valid(desk) else None


def save(desk: dict, path: Path | None = None) -> bool:
    path = path or seat_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(desk, separators=(",", ":")))
        return True
    except OSError:
        log.warning("couldn't save where you sit", exc_info=True)
        return False


def _vec(v, n: int = 3) -> bool:
    return isinstance(v, list) and len(v) == n and all(isinstance(c, (int, float)) and math.isfinite(c) for c in v)


def valid(desk) -> bool:
    if not isinstance(desk, dict) or not isinstance(desk.get("screens"), dict) or not _vec(desk.get("eye")):
        return False
    for s in desk["screens"].values():
        if not isinstance(s, dict) or not _vec(s.get("t")) or not isinstance(s.get("r"), list) or len(s["r"]) != 3 \
                or not all(_vec(row) for row in s["r"]):
            return False
        if not all(isinstance(s.get(k), (int, float)) and s[k] > 0 for k in ("width", "height", "scale")):
            return False
    return desk.get("reference") in desk["screens"]


def from_solution(solution, mm: float | None = None) -> dict:
    """The desk a line-up solved (a curslink.lineup_scene.Solution), aimed
    from where the scan began."""
    eye = [float(v) for v in solution.eye]
    return {
        "reference": solution.reference, "mm": float(mm) if mm else None,
        "screens": {k: {"width": float(s.width), "height": float(s.height), "r": [[float(v) for v in row] for row in s.r],
                        "t": [float(v) for v in s.t], "scale": float(s.scale), "bend": float(s.bend)}
                    for k, s in solution.screens.items()},
        "eye": eye, "scan_eye": list(eye), "how": "scan", "when": now(),
    }


def now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


# -- plain geometry ---------------------------------------------------------------

def bent(t: float, bend: float) -> tuple[float, float]:
    """A curved screen's glass ``t`` of the way across it (curslink.lineup_scene.bent):
    (across, behind the plane of its corners), in its widths."""
    if not bend:
        return t, 0.0
    radius = 1.0 / bend
    a = (t - 0.5) * bend
    return 0.5 * math.sin(bend / 2) / (bend / 2) + radius * math.sin(a), radius * (math.cos(a) - math.cos(bend / 2))


def chord(screen: dict) -> float:
    """A screen's width in a straight line from side to side, in its pixels."""
    bend = screen.get("bend") or 0.0
    return screen["width"] * (math.sin(bend / 2) / (bend / 2) if bend else 1.0)


def local(screen: dict, point) -> tuple[float, float, float]:
    """A point of the world in a screen's terms: across from its top left
    corner, down, and how far *before* the plane of its corners, in its pixels."""
    r, t, k = screen["r"], screen["t"], screen["scale"]
    d = [point[i] - t[i] for i in range(3)]
    u, v, w = (sum(r[i][j] * d[i] for i in range(3)) / k for j in range(3))
    return u, v, -w


def world(screen: dict, uvw) -> list[float]:
    """The other way: (across, down, before) in a screen's pixels, in the world."""
    r, t, k = screen["r"], screen["t"], screen["scale"]
    p = (uvw[0] * k, uvw[1] * k, -uvw[2] * k)
    return [sum(r[i][j] * p[j] for j in range(3)) + t[i] for i in range(3)]


def at(screen: dict, u: float, v: float) -> list[float]:
    """A point of a screen's glass (its pixels) in the world, curved or flat."""
    bend = screen.get("bend") or 0.0
    if not bend:
        return world(screen, (u, v, 0.0))
    x, z = bent(u / screen["width"], bend)
    return world(screen, (x * screen["width"], v, -z * screen["width"]))


def centre(screen: dict) -> list[float]:
    return at(screen, screen["width"] / 2, screen["height"] / 2)


def _sub(a, b):
    return [a[i] - b[i] for i in range(3)]


def _norm(a) -> float:
    return math.sqrt(sum(c * c for c in a))


def mm_of(desk: dict) -> float | None:
    mm = desk.get("mm")
    return float(mm) if isinstance(mm, (int, float)) and mm > 0 else None


def main_screen(desk: dict, fits=()) -> str:
    """The screen the seat is told against: the one behind the biggest
    overlap, or else the biggest there is."""
    def area(key: str) -> float:
        s = desk["screens"][key]
        return s["width"] * s["height"] * s["scale"] ** 2

    behind = [f["behind"] for f in fits if f.get("behind") in desk["screens"]]
    return max(behind or desk["screens"], key=area)


def distance(desk: dict, key: str, eye=None) -> float:
    """How far the eye is from a screen's glass, straight out from it (world units)."""
    s = desk["screens"][key]
    u, _v, before = local(s, eye or desk["eye"])
    sag = bent(min(1.0, max(0.0, u / chord(s))), s.get("bend") or 0.0)[1] * s["width"]
    return (before + sag) * s["scale"]


def eye_above(desk: dict, key: str, eye=None) -> float:
    """How far the eye is above a screen's top edge (world units; below it: negative)."""
    s = desk["screens"][key]
    return -local(s, eye or desk["eye"])[1] * s["scale"]


def _clip(poly: list[tuple[float, float]], w: float, h: float) -> list[tuple[float, float]]:
    """A polygon cut down to the rectangle (0, 0, w, h)."""
    def cut(points, inside, cross):
        out = []
        for i, p in enumerate(points):
            q = points[i - 1]
            if inside(p):
                if not inside(q):
                    out.append(cross(q, p))
                out.append(p)
            elif inside(q):
                out.append(cross(q, p))
        return out

    def x_at(x):
        return lambda a, b: (x, a[1] + (b[1] - a[1]) * (x - a[0]) / (b[0] - a[0]))

    def y_at(y):
        return lambda a, b: (a[0] + (b[0] - a[0]) * (y - a[1]) / (b[1] - a[1]), y)

    for inside, cross in ((lambda p: p[0] >= 0, x_at(0.0)), (lambda p: p[0] <= w, x_at(w)),
                          (lambda p: p[1] >= 0, y_at(0.0)), (lambda p: p[1] <= h, y_at(h))):
        poly = cut(poly, inside, cross)
        if not poly:
            return []
    return poly


def _area(poly) -> float:
    return abs(sum(poly[i - 1][0] * p[1] - p[0] * poly[i - 1][1] for i, p in enumerate(poly))) / 2


def _quad_of(fit: dict, front: dict | None) -> list[tuple[float, float]] | None:
    q = fit.get("quad")
    if q is not None and len(q) == 8:
        return [(q[0], q[1]), (q[2], q[3]), (q[4], q[5]), (q[6], q[7])]
    if front is None:
        return None
    x, y, k = fit["x"], fit["y"], fit["scale"]
    w, h = front["width"] * k, front["height"] * k
    return [(x, y), (x + w, y), (x + w, y + h), (x, y + h)]


def hidden(desk: dict, fit: dict) -> dict | None:
    """What the front screen of ``fit`` hides of the one behind:
    ``{"share": of its area, "height": how far up from its bottom edge (its
    pixels; 0 if it isn't the bottom that's hidden)}``."""
    behind = desk["screens"].get(fit.get("behind"))
    quad = _quad_of(fit, desk["screens"].get(fit.get("front")))
    if behind is None or quad is None:
        return None
    w, h = behind["width"], behind["height"]
    part = _clip(quad, w, h)
    if len(part) < 3:
        return {"share": 0.0, "height": 0.0}
    top, bottom = min(p[1] for p in part), max(p[1] for p in part)
    return {"share": _area(part) / (w * h), "height": h - top if bottom >= h - 1.0 else 0.0}


def spread(desk: dict, key: str, eye=None) -> tuple[float, float]:
    """How far to each side of straight ahead the screens reach, in degrees
    (left, right), looking square on at screen ``key``."""
    main = desk["screens"][key]
    eye = eye or desk["eye"]
    eu, _ev, eb = local(main, eye)
    lo = hi = 0.0
    for s in desk["screens"].values():
        for t in (0.0, 0.5, 1.0):
            for v in (0.0, s["height"]):
                u, _v, before = local(main, at(s, t * s["width"], v))
                ahead = (eb - before) * main["scale"]
                if ahead > 1e-6:
                    a = math.degrees(math.atan2((u - eu) * main["scale"], ahead))
                    lo, hi = min(lo, a), max(hi, a)
    return -lo, hi


def _cm(mm: float) -> str:
    cm = abs(mm) / 10.0
    return f"{cm:.0f} cm" if cm >= 9.5 else f"{cm:.1f} cm".replace(".0 cm", " cm")


def _share(x: float) -> str:
    for limit, word in ((0.07, "a sliver"), (0.15, "about a tenth"), (0.225, "about a fifth"), (0.29, "about a quarter"),
                        (0.42, "about a third"), (0.58, "about half"), (0.71, "about two thirds")):
        if x < limit:
            return word
    return "most"


def summary(desk: dict, fits=(), names: dict[str, str] | None = None) -> dict | None:
    """What the "Where you sit" card says, or None when no size is known
    (no centimetres to give). ``fits``: the overlaps as dicts (``behind``,
    ``front``, ``quad``...), as they're aimed now. ``names``: what to call
    each screen.

    ``{"main", "distance", "eye", "hidden", "moved", "tips": [..]}``: each
    but ``tips`` and ``main`` a ``(value, detail)`` pair of strings (or None
    where there's nothing to say)."""
    if not valid(desk) or mm_of(desk) is None:
        return None
    mm = mm_of(desk)
    names = names or {}
    fits = [f for f in fits if f.get("behind") in desk["screens"] and f.get("front") in desk["screens"]]
    main = main_screen(desk, fits)
    name = names.get(main, "your main screen")
    eye = desk["eye"]
    out: dict = {"main": main, "tips": [], "hidden": None, "moved": None}
    far = distance(desk, main) * mm
    out["distance"] = (_cm(far), f"Your eyes to {name}")
    scan = desk.get("scan_eye")
    if desk.get("how") != "scan" and _vec(scan):
        back = far - distance(desk, main, scan) * mm
        if abs(back) >= 20:
            out["distance"] = (_cm(far), f"Your eyes to {name}: {_cm(back)} {'further back' if back > 0 else 'nearer'} "
                                         "than where the phone was held for the scan")
    above = eye_above(desk, main) * mm
    out["eye"] = ("Level with the top edge" if abs(above) < 10 else
                  f"{_cm(above)} {'above' if above > 0 else 'below'} the top edge", f"Against the top edge of {name}")
    if above < EYE_ABOVE_MM[0]:
        out["tips"].append(f"The top of {name} is {_cm(above)} above your eyes. Lowering it by about {_cm(above)}, or "
                           "raising your chair, would put its top edge at eye level.")
    elif above > EYE_ABOVE_MM[1]:
        out["tips"].append(f"You look down on {name}: its top edge is {_cm(above)} below your eyes. Raising it by about "
                           f"{_cm(above - EYE_ABOVE_MM[1] / 2)} would bring it up to eye level.")
    # the screen in front, and what it hides
    worst = None
    for f in fits:
        if f["behind"] != main:
            continue
        h = hidden(desk, f)
        if h is not None and (worst is None or h["share"] > worst[1]["share"]):
            worst = (f, h)
    if worst is not None:
        f, h = worst
        front = names.get(f["front"], "the screen in front")
        s = desk["screens"][main]
        tall = h["height"] * s["scale"] * mm
        out["hidden"] = (_share(h["share"]).capitalize() if h["share"] > 0.005 else "None of it",
                         f"How much of {name} {front} hides from here"
                         + (f": the bottom {_cm(tall)}" if tall >= 10 else ""))
        if h["share"] >= HIDDEN_SHARE and tall >= 10:
            out["tips"].append(f"{front[:1].upper() + front[1:]} hides {_share(h['share'])} of {name} from where you "
                               f"sit. Raising {name} by {_cm(tall)}, or lowering {front}, would show all of it.")
    moved = desk.get("moved_mm")
    if isinstance(moved, (int, float)) and moved >= 1 and desk.get("how") != "scan":
        out["moved"] = (_cm(moved) if moved >= 10 else f"{moved:.0f} mm", "How far the overlap moved when it was aimed "
                                                                           "at your eyes; saved")
    left, right = spread(desk, main)
    wide = max(left, right)
    if wide > SPREAD_DEG + 2:
        # how much further back brings it all within that
        s = desk["screens"][main]
        u, v, before = local(s, eye)
        step = 10.0 / mm / s["scale"]
        back = 0
        for n in range(1, 151):
            lo, hi = spread(desk, main, world(s, (u, v, before + n * step)))
            if max(lo, hi) <= SPREAD_DEG:
                back = n
                break
        side = "left" if left > right else "right"
        tip = f"Your screens reach {wide:.0f}° to your {side}, further than is easy to take in without turning."
        if back:
            tip += f" Sitting about {back} cm further back would bring them all within {SPREAD_DEG:.0f}° each side."
        out["tips"].append(tip)
    s = desk["screens"][main]
    if (s.get("bend") or 0.0) > 0.05:
        off = (local(s, eye)[0] - chord(s) / 2) * s["scale"] * mm
        if abs(off) > OFF_CENTRE_MM:
            out["tips"].append(f"You're sitting {_cm(off)} to the {'right' if off > 0 else 'left'} of the middle of "
                               f"{name}. A curved screen looks right from its centre line.")
    return out


# -- aiming the overlaps at an eye elsewhere ----------------------------------------

def _poses(desk: dict):
    import numpy as np

    from curslink.lineup_scene import ScreenPose

    return {k: ScreenPose(k, s["width"], s["height"], np.array(s["r"], np.float64), np.array(s["t"], np.float64),
                          float(s["scale"]), 9, bend=float(s.get("bend") or 0.0)) for k, s in desk["screens"].items()}


def _leans(desk: dict, pairs: list[tuple[str, str]], eye) -> dict[tuple[str, str], dict]:
    """How each (behind, front) is seen from ``eye`` by the desk as solved
    (curslink.lineup._Desk.lean): needs numpy."""
    from types import SimpleNamespace

    import numpy as np

    from curslink.lineup import _Desk

    solved = _Desk(SimpleNamespace(eye=np.array(eye, np.float64), screens=_poses(desk), reference=desk["reference"]))
    return {(b, f): solved.lean(b, f) for b, f in pairs}


def reaim(desk: dict, fits: list[dict], eye) -> tuple[list[dict], float]:
    """``fits`` (dicts as curslink.lineup.Result.fits, aimed from
    ``desk["eye"]``) worked out again for ``eye``, and how far the overlaps
    moved by it at most (world units). A fit that isn't on the desk, or
    doesn't say how it leans, is left as it is.

    By the desk in 3D where numpy is to hand: each fit is moved by the
    *change* in how the desk says its front screen is seen, so what the
    pattern step (or a hand) corrected in it stays corrected. Without numpy,
    by the fit's own corners and depths (curslink.layout.Fit.seen_from: a
    curved screen behind is taken as flat where each corner is seen)."""
    eye = [float(v) for v in eye]
    pairs = [(f["behind"], f["front"]) for f in fits
             if f.get("behind") in desk["screens"] and f.get("front") in desk["screens"] and f.get("quad") is not None]
    try:
        old, new = _leans(desk, pairs, desk["eye"]), _leans(desk, pairs, eye)
    except ImportError:
        old = new = {}
    out, most = [], 0.0
    for fit in fits:
        key = (fit.get("behind"), fit.get("front"))
        if key not in pairs:
            out.append(fit)
            continue
        behind, front = desk["screens"][key[0]], desk["screens"][key[1]]
        a, b = old.get(key) or {}, new.get(key) or {}
        moved = dict(fit)
        if a.get("quad") and b.get("quad"):
            quad = [q + (n - o) for q, o, n in zip(fit["quad"], a["quad"], b["quad"])]
            if fit.get("eye") is not None and a.get("eye") and b.get("eye"):
                moved["eye"] = [e + (n - o) for e, o, n in zip(fit["eye"], a["eye"], b["eye"])]
                moved["depth"] = list(b["depth"])
            elif b.get("eye"):
                moved["eye"], moved["depth"] = list(b["eye"]), list(b["depth"])
        elif fit.get("eye") is not None and fit.get("depth") is not None:
            from curslink.layout import Fit

            was, will = local(behind, desk["eye"]), local(behind, eye)
            seen = [fit["eye"][i] + will[i] - was[i] for i in range(3)]
            plain = Fit(tuple(key[0].split("/", 1)), tuple(key[1].split("/", 1)), fit["x"], fit["y"], fit["scale"],
                        quad=tuple(fit["quad"]), eye=tuple(fit["eye"]), depth=tuple(fit["depth"]))
            quad = list(plain.seen_from(tuple(seen)).quad)
            if quad != list(fit["quad"]):
                moved["eye"] = seen
        else:
            out.append(fit)
            continue
        # the plain rectangle goes with it: the same middle, the same share of the shape's size
        size = math.sqrt(max(1e-9, _area(list(zip(quad[0::2], quad[1::2])))) /
                         max(1e-9, _area(list(zip(fit["quad"][0::2], fit["quad"][1::2])))))
        w, h = front["width"], front["height"]
        cx = fit["x"] + w * fit["scale"] / 2 + sum(quad[0::2]) / 4 - sum(fit["quad"][0::2]) / 4
        cy = fit["y"] + h * fit["scale"] / 2 + sum(quad[1::2]) / 4 - sum(fit["quad"][1::2]) / 4
        scale = fit["scale"] * size
        moved.update(quad=[float(v) for v in quad], scale=float(scale), x=float(cx - w * scale / 2),
                     y=float(cy - h * scale / 2))
        most = max(most, max(math.hypot(quad[2 * i] - fit["quad"][2 * i], quad[2 * i + 1] - fit["quad"][2 * i + 1])
                             for i in range(4)) * behind["scale"])
        out.append(moved)
    return out, most


def seated(desk: dict, fits: list[dict], eye, how: str) -> tuple[dict, list[dict]]:
    """The desk and its fits with the eye now at ``eye`` (found ``how``)."""
    new, most = reaim(desk, fits, eye)
    mm = mm_of(desk)
    out = {**desk, "eye": [float(v) for v in eye], "how": how, "when": now(),
           "moved_mm": float(most * mm) if mm else 0.0}
    return out, new


# -- the phone at your eyes -----------------------------------------------------------

class PhoneSeat:
    """The last step of a live line-up: the phone, held at the eyes of
    someone sitting as they usually sit, is placed by the patterns still up.

    :meth:`sight` takes where the camera is in each frame (world -> camera,
    as curslink.lineup_check.Check.locate gives it; None if it couldn't
    say). :attr:`eye` is set once it's been held still somewhere new (or
    where it already was, for a while)."""

    def __init__(self, solution, mm: float | None = None, clock=time.monotonic):
        import numpy as np

        self.clock = clock
        self.began = clock()
        ref = solution.screens[solution.reference]
        if not mm:
            # No screen says how big it is: go by a phone held about 60 cm from the screens.
            mm = 600.0 / max(1.0, float(np.linalg.norm(np.asarray(solution.eye, np.float64) - ref.centre)))
        self.mm = float(mm)
        self.start = None  # where the phone was when this began
        self.sightings: list[tuple[float, object, object]] = []  # (when, where, looking which way)
        self.frames = self.located = 0
        self.eye = None  # where the eyes are (the world), once found
        self.camera = None  # ...and where the phone's lens was
        self.moved = False  # it was somewhere new (not where the pattern step left it)
        self.gave_up = False

    @property
    def done(self) -> bool:
        return self.eye is not None or self.gave_up

    def sight(self, pose, now: float | None = None) -> bool:
        """A frame's worth. True once the eyes are found."""
        import numpy as np

        now = self.clock() if now is None else now
        self.frames += 1
        if self.done:
            return self.eye is not None
        if pose is not None:
            r, t = pose
            where = -r.T @ t
            if np.all(np.isfinite(where)):
                self.located += 1
                if self.start is None:
                    self.start = where
                self.sightings = [s for s in self.sightings if now - s[0] <= KEPT_S] + [(now, where, r[2])]
        # The latest sightings, back as far as it takes to have enough of them over long enough
        # (a slow link sends a frame or two a second).
        still: list = []
        for s in reversed(self.sightings):
            still.insert(0, s)
            if len(still) >= STILL_FRAMES and still[-1][0] - s[0] >= STILL_S:
                break
        if len(still) >= STILL_FRAMES and still[-1][0] - still[0][0] >= STILL_S and now - still[-1][0] < 0.5:
            middle = np.median(np.array([s[1] for s in still]), axis=0)
            if all(np.linalg.norm(s[1] - middle) * self.mm <= STILL_MM for s in still):
                moved = np.linalg.norm(middle - self.start) * self.mm >= MOVED_MM
                if moved or now - self.began >= WAIT_S:
                    axis = np.mean(np.array([s[2] for s in still]), axis=0)
                    axis /= max(1e-9, float(np.linalg.norm(axis)))
                    self.camera = middle
                    self.eye = middle - axis * (LENS_TO_EYE_MM / self.mm)
                    self.moved = bool(moved)
                    return True
        if now - self.began >= GIVE_UP_S:
            self.gave_up = True
        return False


# -- without a phone: the computer's camera, and where you look ---------------------------

def assumed_camera(desk: dict, key: str) -> dict:
    """A camera taken to be where most are: at the middle of the top edge of
    screen ``key``, looking straight out of it. Its ``r``: the world's
    directions of the camera's right (as *you* see it, facing it: the way
    curslink.headfollow.mediapipe_head counts), down, and out towards you."""
    s = desk["screens"][key]
    r = s["r"]
    where = at(s, s["width"] / 2, 0.0)
    up = [-r[i][1] * 8.0 / (mm_of(desk) or 0.25) for i in range(3)]  # (8 mm above the glass)
    return {"screen": key, "at": [where[i] + up[i] for i in range(3)], "how": "assumed",
            "r": [[r[i][0] for i in range(3)], [r[i][1] for i in range(3)], [-r[i][2] for i in range(3)]]}


def from_camera(desk: dict, camera: dict, head_mm) -> list[float] | None:
    """Where the eyes are from where the head is before a camera whose place
    is known: ``head_mm`` as curslink.headfollow.mediapipe_head gives it (to
    the right as you look at the screen, down, away from the camera)."""
    mm = mm_of(desk)
    if mm is None:
        return None
    axes = camera["r"]
    return [camera["at"][i] + sum(axes[a][i] * head_mm[a] for a in range(3)) / mm for i in range(3)]


def look_direction(yaw: float, pitch: float) -> tuple[float, float, float]:
    """Which way a head turned (yaw, pitch) degrees points, as a direction.
    Which way round the angles count doesn't matter here: only the angles
    *between* looks are used."""
    y, p = math.radians(yaw), math.radians(pitch)
    return (math.sin(y) * math.cos(p), math.sin(p), math.cos(y) * math.cos(p))


def _angle(a, b) -> float:
    na, nb = _norm(a), _norm(b)
    if na < 1e-12 or nb < 1e-12:
        return 0.0
    return math.acos(max(-1.0, min(1.0, sum(x * y for x, y in zip(a, b)) / (na * nb))))


def from_looks(desk: dict, camera: dict, head_mm, looks: dict[str, tuple[float, float]],
               camera_look: tuple[float, float] | None = None) -> dict | None:
    """Where the eyes are, with no phone. ``head_mm``: where the head is
    before the camera (from the size of the face: sure of the direction,
    less so of how far). ``looks``: the head's (yaw, pitch) while looking
    at the middle of each screen (by key); ``camera_look``: while looking at
    the camera itself.

    The angle the head turns between two things is the angle between them as
    seen from where it is, less a share (people turn their eyes too): so
    the further back, the smaller the turns. The place along the camera's
    line of sight to the face, and that share, are fitted to the turns,
    held to the face's size where the turns can't say.

    ``{"eye": [x, y, z], "by_face": mm from the camera by the face's size,
    "by_turns": the same as fitted, "turn": the share of each look the head
    made, "pairs": how many pairs of looks it's from}``, or None."""
    mm = mm_of(desk)
    start = from_camera(desk, camera, head_mm)
    if mm is None or start is None:
        return None
    ray = _sub(start, camera["at"])
    far = _norm(ray)
    if far * mm < 150:
        return None
    ray = [c / far for c in ray]
    targets = [(centre(desk["screens"][k]), look_direction(*a)) for k, a in looks.items() if k in desk["screens"]]
    if camera_look is not None:
        targets.append((list(camera["at"]), look_direction(*camera_look)))
    pairs = [(targets[i], targets[j]) for i in range(len(targets)) for j in range(i + 1, len(targets))]
    pairs = [(a, b) for a, b in pairs if _angle(a[1], b[1]) > math.radians(3)]

    def cost(rho: float, turn: float) -> float:
        eye = [camera["at"][i] + ray[i] * rho for i in range(3)]
        total = ((rho - far) / (0.15 * far)) ** 2 + ((turn - 0.85) / 0.2) ** 2
        for a, b in pairs:
            real = _angle(_sub(a[0], eye), _sub(b[0], eye))
            total += ((turn * real - _angle(a[1], b[1])) / math.radians(2.0)) ** 2
        return total

    best = (cost(far, 0.85), far, 0.85)
    if pairs:
        for i in range(61):
            rho = far * (0.6 + 0.8 * i / 60)
            for j in range(31):
                turn = 0.5 + 0.6 * j / 30
                c = cost(rho, turn)
                if c < best[0]:
                    best = (c, rho, turn)
    _c, rho, turn = best
    return {"eye": [camera["at"][i] + ray[i] * rho for i in range(3)], "by_face": far * mm, "by_turns": rho * mm,
            "turn": turn, "pairs": len(pairs)}


def locate_camera(desk: dict, head_mm, key: str | None = None) -> dict | None:
    """Where a camera is, from an eye point that's known (the phone found
    it) and where it sees the head: it faces the way the screen ``key`` it's
    on does (or the main one), and stands back from the eyes by where the
    head is before it. For a camera that isn't built into a lined-up screen
    (one perched on a monitor, or on a stand)."""
    mm = mm_of(desk)
    if mm is None:
        return None
    key = key if key in desk["screens"] else main_screen(desk)
    camera = assumed_camera(desk, key)
    axes = camera["r"]
    camera["at"] = [desk["eye"][i] - sum(axes[a][i] * head_mm[a] for a in range(3)) / mm for i in range(3)]
    camera["how"] = "found"
    return camera


# -- the saved layout ---------------------------------------------------------------

def apply(cfg, desk: dict, eye, how: str, path: Path | None = None) -> dict:
    """Aim every saved overlap from ``eye`` and save both the layout
    (``cfg``: a curslink.configedit.ConfigFile) and the desk. The desk as
    it now is."""
    fits = _config_fits_full(cfg)
    new_desk, aimed = seated(desk, fits, eye, how)
    changed = False
    for was, now_ in zip(fits, aimed):
        if now_ is not was:
            cfg.set_fit(now_["behind"], now_["front"], now_["x"], now_["y"], now_["scale"], quad=now_.get("quad"),
                        eye=now_.get("eye"), depth=now_.get("depth"), mm=now_.get("mm"))
            changed = True
    if changed:
        cfg.save()
    save(new_desk, path)
    log.info("where you sit: eye now %s (%s); %d overlap(s) aimed from there, moved up to %.0f mm",
             [round(v) for v in new_desk["eye"]], how, sum(1 for a, b in zip(fits, aimed) if a is not b),
             new_desk.get("moved_mm") or 0.0)
    return new_desk


def config_fits(cfg) -> list[dict]:
    """The saved overlaps as :func:`summary` and :func:`hidden` take them."""
    out = []
    for t in cfg.fits():
        try:
            fit = {"behind": str(t["behind"]), "front": str(t["front"]), "x": float(t["x"]), "y": float(t["y"]),
                   "scale": float(t["scale"])}
            if "quad" in t and len(t["quad"]) == 8:
                fit["quad"] = [float(v) for v in t["quad"]]
        except (KeyError, TypeError, ValueError):
            continue
        out.append(fit)
    return out


# -- joined by eye, from the seat (curslink.seatalign) ------------------------------------
def apply_joined(cfg, behind: str, front: str, fit: dict, joins, desk: dict | None = None,
                 path: Path | None = None) -> dict:
    """Keep an overlap joined by eye from the seat: ``fit`` (``x``, ``y``,
    ``scale``, ``quad``: curslink.seatalign.Align.command) for ``front``
    before ``behind``, as it stands. With a desk a line-up solved, the spots
    that were joined (``joins``) also say where the eyes are: the desk is
    aimed from there, every *other* overlap with it, and this one is given
    the eye and depths head tracking follows it by.

    ``{"eye": the eye found or None, "moved_mm": how far that is from where
    it was thought to be, "others": how many other overlaps were re-aimed}``."""
    from curslink import seatalign

    out: dict = {"eye": None, "moved_mm": 0.0, "others": 0}
    lean: dict = {}
    eye = None
    if desk is not None and valid(desk):
        try:
            eye = seatalign.eye_from(desk, behind, front, joins)
        except (KeyError, TypeError, ValueError, ZeroDivisionError):
            eye = None
    if eye is not None:
        mm = mm_of(desk)
        out["eye"] = [float(v) for v in eye]
        out["moved_mm"] = float(_norm(_sub(eye, desk["eye"])) * mm) if mm else 0.0
        others = [f for f in _config_fits_full(cfg) if (f["behind"], f["front"]) != (behind, front)]
        new_desk, aimed = seated(desk, others, eye, "joined")
        for was, now_ in zip(others, aimed):
            if now_ is not was:
                cfg.set_fit(now_["behind"], now_["front"], now_["x"], now_["y"], now_["scale"], quad=now_.get("quad"),
                            eye=now_.get("eye"), depth=now_.get("depth"), mm=now_.get("mm"))
                out["others"] += 1
        try:
            seen = _leans(desk, [(behind, front)], eye).get((behind, front)) or {}
        except ImportError:
            seen = {}
        if seen.get("eye") and seen.get("depth"):
            lean = {"eye": seen["eye"], "depth": seen["depth"], "mm": mm}
        save(new_desk, path)
        log.info("joined by eye: %s before %s; eyes at %s, %.0f mm from where they were thought to be; %d other "
                 "overlap(s) re-aimed", front, behind, [round(v) for v in eye], out["moved_mm"], out["others"])
    cfg.set_fit(behind, front, float(fit["x"]), float(fit["y"]), float(fit["scale"]), quad=fit.get("quad"), **lean)
    cfg.save()
    return out


def joined_shape(desk: dict | None, behind: str, front: str, joins) -> list[float] | None:
    """How ``front`` is seen on ``behind`` (its four corners there) from
    where spots joined by eye put the eyes, by the desk as solved: the shape
    for curslink.seatalign.Align.adopt. None without a desk that can say
    (or without numpy)."""
    from curslink import seatalign

    if desk is None or not valid(desk):
        return None
    try:
        eye = seatalign.eye_from(desk, behind, front, joins)
        if eye is None:
            return None
        seen = _leans(desk, [(behind, front)], eye).get((behind, front)) or {}
    except (ImportError, KeyError, TypeError, ValueError, ZeroDivisionError):
        return None
    return list(seen["quad"]) if seen.get("quad") else None


def _config_fits_full(cfg) -> list[dict]:
    """The saved overlaps with all that says how they lean (as :func:`reaim` takes them)."""
    fits = []
    for t in cfg.fits():
        try:
            fit = {"behind": str(t["behind"]), "front": str(t["front"]), "x": float(t["x"]), "y": float(t["y"]),
                   "scale": float(t["scale"])}
            for key, n in (("quad", 8), ("eye", 3), ("depth", 4)):
                if key in t:
                    fit[key] = [float(v) for v in t[key]]
                    if len(fit[key]) != n:
                        del fit[key]
            if "mm" in t:
                fit["mm"] = float(t["mm"])
        except (KeyError, TypeError, ValueError):
            continue
        fits.append(fit)
    return fits
