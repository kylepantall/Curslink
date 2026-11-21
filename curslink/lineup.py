"""Auto line-up: arrange the layout from one photo of the desk.

Every screen in the layout shows a few square patterns (ArUco markers) at
its corners and the middles of its edges, each one telling which screen it
is on and where on that screen it sits (:func:`plan`). A phone (the
Curslink app, or any camera) takes one photo with all of them in view.
:func:`find_screens` finds the patterns in the photo and, from those that
are seen, where each screen is in the picture; :func:`solve` turns that into
the layout: where each screen goes, which edges really meet and over which
stretch (calibrated edges, ``[[layout.links]]``), and which screens stand in
front of others (overlap, ``z`` and ``[[layout.fit]]``).

The maths assumes the screens are roughly side by side in one plane, as on a
desk seen from the front: everything is measured in the plane of one screen
(the reference: this computer's main one, else the best seen). A screen
nearer the camera than the others (a laptop in front of a monitor) looks a
little bigger than it is; the layout is still right about which edges meet.

Only this module needs OpenCV (``cv2``, in the ``gaze`` extra and the
packaged app) and only :func:`find_screens` and :func:`marker_bits` use it.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field

# 4x4-bit markers: the coarsest there is, so they're read from furthest away.
DICTIONARY = "DICT_4X4_250"
DICTIONARY_SIZE = 250
# Four corners, then the middle of the top, right, bottom and left edges. The
# middle of the screen stays clear for its number and name (and, on the
# computer you're at, for the line-up window).
SPOTS = ((0.0, 0.0), (1.0, 0.0), (1.0, 1.0), (0.0, 1.0), (0.5, 0.0), (1.0, 0.5), (0.5, 1.0), (0.0, 0.5))
PER_SCREEN = len(SPOTS)
MAX_SCREENS = DICTIONARY_SIZE // PER_SCREEN
MARKER = 0.16  # a marker's side, as a share of the screen's shorter side
MARGIN = 0.03  # its distance from the screen's edge, same units
CELLS = 6  # 4x4 bits and a black border a cell wide
LONGEST = 3000  # photos are shrunk to this many pixels along their longest side

# Two screens closer than this (a share of the smaller one's size across the
# gap) still meet: monitor bezels and the gap between them.
JOIN = 0.18
# Screens sharing more than this share of the smaller one's area overlap; less
# is a bezel over a bezel, so they only meet.
OVERLAP = 0.03


log = logging.getLogger(__name__)


class LineupError(RuntimeError):
    """The photo couldn't be used (in words for the person)."""


def marker_bits(marker_id: int) -> int:
    """The 4x4 bits of a marker, row by row from the top left, the most
    significant first; a 1 is a white cell. What :func:`plan` sends to the
    screens, so the computers showing them don't need OpenCV."""
    import cv2

    dictionary = cv2.aruco.getPredefinedDictionary(getattr(cv2.aruco, DICTIONARY))
    img = cv2.aruco.generateImageMarker(dictionary, marker_id, CELLS, borderBits=1)
    bits = 0
    for r in range(4):
        for c in range(4):
            bits = (bits << 1) | (1 if img[1 + r, 1 + c] > 127 else 0)
    return bits


def marker_boxes(width: float, height: float) -> list[tuple[float, float, float, float]]:
    """Where a screen ``width`` x ``height`` shows its markers: ``(x, y, w, h)``
    as shares of its width and height, in the order of :data:`SPOTS`."""
    short = min(width, height)
    side, margin = MARKER * short, MARGIN * short
    out = []
    for fx, fy in SPOTS:
        x = margin + fx * (width - side - 2 * margin)
        y = margin + fy * (height - side - 2 * margin)
        out.append((x / width, y / height, side / width, side / height))
    return out


# The patterns don't stay put: after the first set (the corners and edges,
# which is all an older Curslink shows) each screen shows set after set in
# other places, a few times a second, until its whole face has been covered.
# Every set has its own marker ids, so a frame says by itself which set it
# caught: nothing to keep in time. That's some 40 measured points across a
# screen instead of 8 round its edge, from wherever the phone is held: what
# shows a curved monitor's curve, and what's left of a screen partly hidden
# behind another one.
MAX_SETS = 6  # per screen, the first included
CHECK_SPOTS = ((0.27, 0.3), (0.73, 0.3), (0.73, 0.7), (0.27, 0.7))  # the middles of the check's patterns
PERIOD_MS = 320  # how long each set shows
SPACING = 0.25  # between the middles of neighbouring places, in shorter sides


def marker_grid(width: float, height: float) -> list[tuple[float, float, float, float]]:
    """Every place a screen ``width`` x ``height`` can show a marker without
    two of them touching: ``(x, y, w, h)`` as shares of its width and height,
    in an order that jumps about (so any run of them is spread out)."""
    short = min(width, height)
    side, margin = MARKER * short, MARGIN * short
    cols = max(2, min(9, int((width - side - 2 * margin) / (SPACING * short)) + 1))
    rows = max(2, min(9, int((height - side - 2 * margin) / (SPACING * short)) + 1))
    cells = []
    for r in range(rows):
        for c in range(cols):
            x = margin + c / (cols - 1) * (width - side - 2 * margin)
            y = margin + r / (rows - 1) * (height - side - 2 * margin)
            cells.append((x / width, y / height, side / width, side / height))
    n = len(cells)
    stride = max(1, round(n * 0.618))
    while _gcd(stride, n) != 1:
        stride += 1
    return [cells[(i * stride) % n] for i in range(n)]


def _gcd(a: int, b: int) -> int:
    while b:
        a, b = b, a % b
    return a


def plan(screens: list[dict]) -> dict[str, dict]:
    """What each screen shows: ``{key: {"slot": n, "markers": [[bits, x, y, w, h], ...],
    "sets": [markers, markers, ...]}}`` for ``screens`` (``{"key", "width",
    "height"}``), in the order given. ``markers`` is the first set (the
    corners and edges), ``sets`` all of them in the order they show.

    A screen's sets take slots ``slot, slot + 1, ...``; a set's marker ids
    are ``its slot * 8 .. + 7``. At most :data:`MAX_SCREENS` slots in all,
    so many screens get fewer sets each. ``"check"``: ``{"slot", "markers"}``,
    the four patterns shown while the result is checked (see :func:`check_plan`).
    """
    screens = screens[:MAX_SCREENS // 2]
    grids = [marker_grid(float(s["width"]), float(s["height"])) for s in screens]
    # (One slot each is kept for the check's patterns.)
    most = max(1, min(MAX_SETS, MAX_SCREENS // max(1, len(screens)) - 1))
    out = {}
    slot = 0
    for s, grid in zip(screens, grids):
        sets = [marker_boxes(float(s["width"]), float(s["height"]))]
        for k in range(min(most - 1, -(-len(grid) // PER_SCREEN))):
            sets.append([grid[(k * PER_SCREEN + i) % len(grid)] for i in range(PER_SCREEN)])
        coded = [[[marker_bits((slot + k) * PER_SCREEN + i), *(round(v, 5) for v in box)]
                  for i, box in enumerate(boxes)] for k, boxes in enumerate(sets)]
        slot += len(sets)
        # For the check at the end (curslink.lineup_check): four patterns in
        # from the edges, which are left clear for the lines across the joins.
        w, h = float(s["width"]), float(s["height"])
        side = MARKER * min(w, h)
        check = [[marker_bits(slot * PER_SCREEN + i), round(fx - side / w / 2, 5), round(fy - side / h / 2, 5),
                  round(side / w, 5), round(side / h, 5)]
                 for i, (fx, fy) in enumerate(CHECK_SPOTS)]
        out[s["key"]] = {"slot": slot - len(sets), "markers": coded[0], "sets": coded,
                         "check": {"slot": slot, "markers": check}}
        slot += 1
    return out


def check_plan(planned: dict[str, dict]) -> dict[str, dict]:
    """``planned`` as it is during the check: each screen showing its check patterns only."""
    return {k: {"slot": p["check"]["slot"], "markers": p["check"]["markers"]} for k, p in planned.items() if "check" in p}


# -- finding the patterns in a frame ---------------------------------------------

def load_photo(data: bytes):
    """The photo as a grey image, shrunk to at most :data:`LONGEST` pixels."""
    import cv2
    import numpy as np

    img = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_GRAYSCALE)
    if img is None:
        raise LineupError("that file isn't a photo Curslink can open (try a JPEG or PNG)")
    h, w = img.shape[:2]
    scale = LONGEST / max(h, w)
    if scale < 1:
        img = cv2.resize(img, (round(w * scale), round(h * scale)), interpolation=cv2.INTER_AREA)
    return img


def _detector():
    import cv2

    dictionary = cv2.aruco.getPredefinedDictionary(getattr(cv2.aruco, DICTIONARY))
    params = cv2.aruco.DetectorParameters()
    # Corners to a fraction of a pixel: everything after is only as good as these.
    params.cornerRefinementMethod = cv2.aruco.CORNER_REFINE_SUBPIX
    params.cornerRefinementWinSize = 4
    params.cornerRefinementMaxIterations = 40
    params.cornerRefinementMinAccuracy = 0.02
    # Screens glow: under a reflection or in a dim room a pattern's white
    # runs into its black. Bigger windows for the local threshold, a slacker
    # outline, and cells read from their middles.
    params.adaptiveThreshWinSizeMin = 5
    params.adaptiveThreshWinSizeMax = 45
    params.adaptiveThreshWinSizeStep = 10
    params.minMarkerPerimeterRate = 0.012
    params.polygonalApproxAccuracyRate = 0.05
    params.perspectiveRemoveIgnoredMarginPerCell = 0.2
    return cv2.aruco.ArucoDetector(dictionary, params)


def find_markers(gray, thorough: bool = True) -> dict[int, "object"]:
    """The patterns in ``gray``: ``{marker id: its 4 corners (4, 2), in pixels}``.

    ``thorough`` looks a second time with the contrast stretched (a photo;
    live frames only do when the first look finds next to nothing).
    """
    import cv2
    import numpy as np

    detector = _detector()
    # A screen right at the picture's edge: its patterns' white border runs into
    # the edge and they aren't found, so give the picture a grey frame.
    pad = max(16, round(0.02 * max(gray.shape[:2])))
    framed = cv2.copyMakeBorder(gray, pad, pad, pad, pad, cv2.BORDER_CONSTANT, value=int(np.median(gray)))
    found: dict[int, object] = {}

    def look(img) -> None:
        corners, ids, _rejected = detector.detectMarkers(img)
        if ids is None:
            return
        for quad, marker_id in zip(corners, ids.flatten()):
            found.setdefault(int(marker_id), quad.reshape(4, 2).astype(np.float64) - pad)

    look(framed)
    if thorough or len(found) < 2:
        # A dim picture of bright screens, or a reflection across one.
        look(cv2.createCLAHE(clipLimit=3.0, tileGridSize=(8, 8)).apply(framed))
    return found


def read_frame(gray, planned: dict[str, dict], sizes: dict[str, tuple[float, float]], tolerance: float,
               thorough: bool = True, curved: float = 0.0) -> list:
    """The screens of ``planned`` (as :func:`plan` gives) seen in ``gray``:
    a curslink.lineup_scene.Sighting for each with at least two patterns
    that agree. ``sizes``: ``{key: (width, height)}`` in pixels."""
    import numpy as np

    from curslink.lineup_scene import sight

    by_slot = {}
    for key, p in planned.items():
        for k, markers in enumerate(p.get("sets") or [p["markers"]]):
            by_slot[p["slot"] + k] = (key, markers, k)
    per_screen: dict[str, tuple[list, list, set]] = {}
    for marker_id, quad in find_markers(gray, thorough).items():
        slot, index = divmod(marker_id, PER_SCREEN)
        if slot not in by_slot:
            continue  # not one of ours: something on the desk that looks like a pattern
        key, markers, which = by_slot[slot]
        if key not in sizes:
            continue
        sw, sh = sizes[key]
        _bits, x, y, w, h = markers[index]
        # As ArUco orders a marker's corners: clockwise from its top left.
        uv = np.array([(x, y), (x + w, y), (x + w, y + h), (x, y + h)], np.float64) * (sw, sh)
        on, at, sets = per_screen.setdefault(key, ([], [], set()))
        on.append(uv)
        at.append(quad)
        sets.add(which)
    out = []
    for key, (on, at, sets) in per_screen.items():
        s = sight(key, on, at, tolerance, curved)
        if s is not None:
            s.sets = frozenset(sets)
            out.append(s)
    return out


# -- from the desk in 3D to the layout -----------------------------------------

@dataclass
class Placed:
    """A screen as it's seen against another screen's plane, in that screen's pixels."""

    key: str
    x0: float
    y0: float
    x1: float
    y1: float
    k: float  # its own pixels per pixel of the plane

    @property
    def w(self) -> float:
        return self.x1 - self.x0

    @property
    def h(self) -> float:
        return self.y1 - self.y0

    @property
    def area(self) -> float:
        return self.w * self.h


@dataclass
class Result:
    """What the camera says, for the Layout page to show before it's saved."""

    positions: dict[str, tuple[int, int]]  # key -> new (x, y) in the layout
    fronts: list[tuple[str, str]]  # (behind, front) for each overlap: front goes in front
    links: list[dict]  # for ConfigFile.set_link: a, a_side, a_span, b, b_side, b_span
    fits: list[dict]  # for ConfigFile.set_fit: behind, front, x, y, scale (and quad, eye, depth, mm)
    found: list[str]  # the screens placed
    missing: list[str]  # the screens that weren't
    reference: str
    # How unsure each placed screen still is (1 sigma, in the reference's pixels).
    errors: dict[str, float] = field(default_factory=dict)
    # How each stands: {"yaw", "pitch"} in degrees against the reference, "scale"
    # (its pixel's size against the reference's), "depth" (reference pixels nearer).
    stance: dict[str, dict] = field(default_factory=dict)
    views: int = 1  # the frames it's from
    rms: float = 0.0  # how well the patterns fit, in frame pixels
    # Every pair of screens that meet or overlap as the person sees them, a
    # computer's own two screens as well (which have no link or fit: its OS
    # moves the pointer between them): (a, b, side of a that b is at) or
    # (front, behind, None). The check at the end draws its lines across these.
    joins: list[tuple[str, str, str | None]] = field(default_factory=list)
    # How the check at the end went (Live.check_summary), None if there wasn't one.
    check: dict | None = None
    # The desk in 3D and the eye point the fits are aimed from (curslink.seat),
    # kept with the layout so the overlaps can be aimed again without a scan.
    # None from a single photo (which can't tell how far away anything is).
    desk: dict | None = None


def _span(a0: float, a1: float, b0: float, b1: float) -> tuple[float, float] | None:
    lo, hi = max(a0, b0), min(a1, b1)
    return (lo, hi) if hi > lo else None


def relation(a: Placed, b: Placed) -> tuple[str, ...] | None:
    """How ``b`` stands to ``a``: ``("overlap",)``, ``("side", side of a it's on)``,
    or None when they don't meet."""
    shared_x = _span(a.x0, a.x1, b.x0, b.x1)
    shared_y = _span(a.y0, a.y1, b.y0, b.y1)
    if shared_x and shared_y:
        area = (shared_x[1] - shared_x[0]) * (shared_y[1] - shared_y[0])
        if area > OVERLAP * min(a.area, b.area):
            return ("overlap",)
    # Across a gap (or a sliver of overlap): which way is the gap smallest?
    gaps = {"right": b.x0 - a.x1, "left": a.x0 - b.x1, "bottom": b.y0 - a.y1, "top": a.y0 - b.y1}
    side = max(gaps, key=lambda s: gaps[s])
    across = (min(a.w, b.w) if side in ("left", "right") else min(a.h, b.h))
    if gaps[side] > JOIN * across:
        return None
    along = shared_y if side in ("left", "right") else shared_x
    if along is None:
        return None
    small = min(a.h, b.h) if side in ("left", "right") else min(a.w, b.w)
    if along[1] - along[0] < 0.1 * small:
        return None  # only corners near each other
    return ("side", side)


OPP = {"left": "right", "right": "left", "top": "bottom", "bottom": "top"}


class _Desk:
    """The solved desk, asked how one screen looks against another from
    where the person sits (the eye: where the camera was held)."""

    def __init__(self, solution):
        self.sol = solution
        self.screens = solution.screens

    def _quad(self, plane: str, other: str):
        from curslink.lineup_scene import project_from

        return project_from(self.sol.eye, self.screens[plane], self.screens[other].corners())

    @staticmethod
    def _whole(key: str, quad, w: float, h: float) -> Placed | None:
        (tlx, tly), (trx, try_), (brx, bry), (blx, bly) = quad
        x0, x1 = (tlx + blx) / 2, (trx + brx) / 2
        y0, y1 = (tly + try_) / 2, (bly + bry) / 2
        if x1 - x0 < 1e-6 or y1 - y0 < 1e-6:
            return None
        return Placed(key, x0, y0, x1, y1, (w / (x1 - x0) + h / (y1 - y0)) / 2)

    @staticmethod
    def _by_edge(key: str, quad, w: float, h: float, edge: str) -> Placed | None:
        """From the one edge that meets the other screen: where that edge is
        seen, and the screen's size as it is right there. A monitor turned
        in looks taller at its near edge than at its far one, and it's the
        near one the pointer crosses."""
        (tlx, tly), (trx, try_), (brx, bry), (blx, bly) = quad
        if edge in ("left", "right"):
            (ax, ay), (bx, by) = ((tlx, tly), (blx, bly)) if edge == "left" else ((trx, try_), (brx, bry))
            if by - ay < 1e-6:
                return None
            k = h / (by - ay)
            x = (ax + bx) / 2
            x0, x1 = (x, x + w / k) if edge == "left" else (x - w / k, x)
            return Placed(key, x0, ay, x1, by, k)
        (ax, ay), (bx, by) = ((tlx, tly), (trx, try_)) if edge == "top" else ((blx, bly), (brx, bry))
        if bx - ax < 1e-6:
            return None
        k = w / (bx - ax)
        y = (ay + by) / 2
        y0, y1 = (y, y + h / k) if edge == "top" else (y - h / k, y)
        return Placed(key, ax, y0, bx, y1, k)

    def pair(self, p: str, n: str, side: str | None = None) -> tuple[Placed, Placed] | None:
        """``p`` and ``n`` against one plane (p's, else n's): with ``side``
        (the side of p that n is on), measured at the edges that meet."""
        P, N = self.screens[p], self.screens[n]
        quad = self._quad(p, n)
        if quad is not None:
            other = self._whole(n, quad, N.width, N.height) if side is None else \
                self._by_edge(n, quad, N.width, N.height, OPP[side])
            if other is not None:
                return Placed(p, 0.0, 0.0, P.width, P.height, 1.0), other
        quad = self._quad(n, p)
        if quad is not None:
            other = self._whole(p, quad, P.width, P.height) if side is None else \
                self._by_edge(p, quad, P.width, P.height, side)
            if other is not None:
                return other, Placed(n, 0.0, 0.0, N.width, N.height, 1.0)
        return None

    def edge_fit(self, behind: str, front: str) -> tuple[float, float, float] | None:
        """Where ``front`` covers ``behind``, for a fit: ``(x, y, scale)`` with
        front's pixel (u, v) seen over behind's (x + scale*u, y + scale*v).

        Fitted along the stretches of front's edges that are seen against
        behind's face: that's where the pointer crosses and where behind's
        picture carries on (the mirror strip), so that's where it has to be
        right. (A laptop turned in and tipped back makes a skewed outline
        against the monitor behind: one rectangle for the whole of it is
        centimetres out at its top edge.) None if hardly any edge is."""
        import numpy as np

        from curslink.lineup_scene import project_from

        F, B = self.screens[front], self.screens[behind]
        along = np.linspace(0.0, 1.0, 41)
        w, h = F.width, F.height
        uv = np.concatenate([
            np.column_stack([along * w, np.zeros_like(along)]), np.column_stack([along * w, np.full_like(along, h)]),
            np.column_stack([np.zeros_like(along), along * h]), np.column_stack([np.full_like(along, w), along * h])])
        seen = project_from(self.sol.eye, B, F.at(uv))
        if seen is None:
            return None
        ok = (seen[:, 0] >= 0) & (seen[:, 0] <= B.width) & (seen[:, 1] >= 0) & (seen[:, 1] <= B.height)
        if ok.sum() < 8:
            return None
        uv, seen = uv[ok], seen[ok]
        # x + s*u = X, y + s*v = Y: least squares over (x, y, s)
        n = len(uv)
        a = np.zeros((2 * n, 3))
        a[:n, 0], a[:n, 2] = 1.0, uv[:, 0]
        a[n:, 1], a[n:, 2] = 1.0, uv[:, 1]
        (x, y, scale), *_ = np.linalg.lstsq(a, np.concatenate([seen[:, 0], seen[:, 1]]), rcond=None)
        if not 0.05 < scale < 20:
            return None
        return float(x), float(y), float(scale)

    def lean(self, behind: str, front: str) -> dict:
        """How ``front`` leans against ``behind`` and where that's seen from,
        for a fit (curslink.layout.Fit): ``{"quad", "eye", "depth"}``, or
        nothing if it can't be told. ``quad``: front's four corners as seen
        on behind (its pixels). ``eye``: where the eye is, across and down
        behind and how far before it, and ``depth``: how far before it each
        of those corners is, all in behind's pixels."""
        import numpy as np

        from curslink.layout import homography
        from curslink.lineup_scene import project_from

        F, B = self.screens[front], self.screens[behind]
        quad = project_from(self.sol.eye, B, F.corners())
        if quad is None or not np.all(np.isfinite(quad)):
            return {}
        # That's right for a flat screen behind. On a curved one a corner
        # that's seen off the side of the glass is carried on in the plane
        # of its corners, which isn't how the glass itself runs: so the
        # shape is fitted to where the part of the front screen that *is*
        # seen against the glass is seen, its edges most of all (where the
        # pointer crosses and the mirror has to carry on from).
        if B.bend:
            fitted = self._fitted_quad(F, B)
            if fitted is not None:
                quad = fitted
        flat = tuple(float(v) for v in quad.ravel())
        rect = (0.0, 0.0, float(F.width), 0.0, float(F.width), float(F.height), 0.0, float(F.height))
        if homography(rect, flat) is None or homography(flat, rect) is None:
            return {}
        # a shape the right way round, none of it folded over
        sides = [(quad[(i + 1) % 4] - quad[i], quad[(i + 2) % 4] - quad[(i + 1) % 4]) for i in range(4)]
        turn = [float(a[0] * b[1] - a[1] * b[0]) for a, b in sides]
        if not all(t > 0 for t in turn):
            return {}
        out: dict = {"quad": list(flat)}
        e = (B.r.T @ (np.asarray(self.sol.eye, np.float64) - B.t)) / B.scale
        p = ((F.corners() - B.t) @ B.r) / B.scale
        depth = -p[:, 2]
        if B.bend:
            # A curved screen's corners are in that plane and its glass is
            # behind it, the middle most (centimetres on a wide one): a
            # laptop's top edge near the middle of the glass is *behind* the
            # plane, and well in front of the glass it's seen against. How
            # far before the glass there, then (0.4.100 left these out for
            # Kyle's curved monitor, so head tracking had nothing to follow).
            from curslink.lineup_scene import bent

            across = np.clip(quad[:, 0] / B.width, 0.0, 1.0)
            depth = depth + bent(across, B.bend)[1] * B.width
        if -e[2] > 0 and np.all(depth > -0.1 * -e[2]) and np.all(depth < 0.9 * -e[2]):
            out["eye"] = [float(e[0]), float(e[1]), float(-e[2])]
            out["depth"] = [float(max(0.0, d)) for d in depth]
        else:
            log.info("line-up: %s before %s: can't say where it's seen from (eye %.0f px away, corners %s px before "
                     "it), so head tracking can't follow it", front, behind, -e[2], [round(float(d)) for d in depth])
        return out

    def _fitted_quad(self, F, B):
        """``F``'s four corners on ``B`` by the perspective map that best
        fits where the points of ``F`` seen against ``B``'s face are seen
        (4, 2), or None if too little of it is."""
        import cv2
        import numpy as np

        from curslink.lineup_scene import project_from

        w, h = F.width, F.height
        along = np.linspace(0.0, 1.0, 41)
        edges = np.concatenate([
            np.column_stack([along * w, np.zeros_like(along)]), np.column_stack([along * w, np.full_like(along, h)]),
            np.column_stack([np.zeros_like(along), along * h]), np.column_stack([np.full_like(along, w), along * h])])
        gx, gy = np.meshgrid(np.linspace(0.0, w, 17), np.linspace(0.0, h, 13))
        uv = np.concatenate([edges, edges, edges, np.column_stack([gx.ravel(), gy.ravel()])])  # (edges count three times)
        seen = project_from(self.sol.eye, B, F.at(uv))
        if seen is None:
            return None
        ok = (seen[:, 0] >= 0) & (seen[:, 0] <= B.width) & (seen[:, 1] >= 0) & (seen[:, 1] <= B.height)
        if ok.sum() < 40 or np.ptp(uv[ok, 0]) < 0.1 * w or np.ptp(uv[ok, 1]) < 0.05 * h:
            return None
        m, _ = cv2.findHomography(uv[ok].astype(np.float64), seen[ok].astype(np.float64), 0)
        if m is None or not np.all(np.isfinite(m)):
            return None
        corners = np.array([(0.0, 0.0, 1.0), (w, 0.0, 1.0), (w, h, 1.0), (0.0, h, 1.0)]) @ m.T
        if np.any(corners[:, 2] <= 1e-9):
            return None
        return corners[:, :2] / corners[:, 2:3]

    def in_front(self, a: str, b: str) -> bool:
        """Is ``a`` nearer the person than ``b`` where they overlap? The
        smaller one's middle, against the bigger one's plane; when that
        can't be told (one photo, sizes unknown: both come out in one
        plane), the smaller one is, like a laptop in front of a monitor."""
        A, B = self.screens[a], self.screens[b]
        small, big = (A, B) if A.width * A.height * A.scale ** 2 <= B.width * B.height * B.scale ** 2 else (B, A)
        depth = float((small.centre - big.t) @ big.normal)  # < 0: on the eye's side of big
        if abs(depth) < 0.02 * big.width * big.scale:
            return small is A
        return (depth < 0) == (small is A)

    def stance(self, key: str) -> dict:
        import math

        ref = self.screens[self.sol.reference]
        s = self.screens[key]
        n = ref.r.T @ s.normal
        mid = ref.r.T @ (s.centre - ref.centre)
        # yaw: turned to face the person's right; pitch: tipped back.
        return {"yaw": -math.degrees(math.atan2(n[0], n[2])), "pitch": math.degrees(math.atan2(n[1], n[2])),
                "scale": s.scale / ref.scale, "depth": -float(mid[2]) / ref.scale}


def arrange(screens: list[dict], solution) -> Result:
    """The layout from the desk in 3D (a curslink.lineup_scene.Solution).

    ``screens``: every screen in the layout, ``{"key", "device", "x", "y",
    "width", "height"}``. What matters to the pointer is what the person
    sees: which edges look as if they meet, and where along them. So each
    pair of screens is measured as it's seen from where the camera was held
    (in front of the person's face), one against the plane of the other.

    The reference screen stays where it is; the others are placed from it,
    each beside or over a screen already placed, in that screen's pixels.
    Screens not seen move with their own computer's screens that were (or
    stay put), and nothing ends up overlapping that doesn't on the desk.
    """
    import numpy as np

    info = {s["key"]: s for s in screens}
    mm = _sizes_mm(screens)
    desk = _Desk(solution)
    ref = solution.reference
    poses = {k: v for k, v in solution.screens.items() if k in info}
    if ref not in poses:
        raise LineupError("none of the screens' patterns were seen")
    keys = sorted(poses, key=lambda k: (k != ref, -poses[k].views, k))
    rel = {}
    for i, a in enumerate(keys):
        for b in keys[i + 1:]:
            both = desk.pair(a, b)
            r = relation(*both) if both is not None else None
            if r is not None:
                rel[(a, b)] = r
                rel[(b, a)] = ("overlap",) if r[0] == "overlap" else ("side", OPP[r[1]])

    pos: dict[str, tuple[float, float]] = {ref: (float(info[ref]["x"]), float(info[ref]["y"]))}

    def place(p: str, n: str) -> tuple[float, float]:
        """Where ``n`` goes, from ``p`` already placed."""
        px, py = pos[p]
        pw, ph = float(info[p]["width"]), float(info[p]["height"])
        nw, nh = float(info[n]["width"]), float(info[n]["height"])
        r = rel.get((p, n))
        side = r[1] if r is not None and r[0] == "side" else None
        both = desk.pair(p, n, side) or desk.pair(p, n)
        if both is None:
            return px + pw, py  # can't be seen against each other at all: just beside it
        P, N = both
        if side == "right":
            return px + pw, py + (N.y0 - P.y0) * P.k
        if side == "left":
            return px - nw, py + (N.y0 - P.y0) * P.k
        if side == "bottom":
            return px + (N.x0 - P.x0) * P.k, py + ph
        if side == "top":
            return px + (N.x0 - P.x0) * P.k, py - nh
        if r is not None and desk.in_front(p, n):
            # n is behind p: p sits on n at (P - N) in n's pixels.
            at = desk.edge_fit(n, p)
            if at is not None:
                return px - at[0], py - at[1]
            return px - (P.x0 - N.x0) * N.k, py - (P.y0 - N.y0) * N.k
        # n is in front of p (where it covers p, in p's pixels), or apart from it.
        at = desk.edge_fit(p, n) if r is not None else None
        if at is not None:
            return px + at[0], py + at[1]
        return px + (N.x0 - P.x0) * P.k, py + (N.y0 - P.y0) * P.k

    # Breadth first along the screens that meet, nearest first.
    todo = [k for k in keys if k != ref]
    while todo:
        best = None
        for n in todo:
            for p in pos:
                if (p, n) in rel:
                    score = (0, keys.index(p), keys.index(n))
                else:
                    d = float(np.linalg.norm(poses[p].centre - poses[n].centre))
                    score = (1, d, keys.index(n))
                if best is None or score < best[0]:
                    best = (score, p, n)
        _score, p, n = best
        pos[n] = place(p, n)
        todo.remove(n)

    overlapping = {frozenset(pair) for pair, r in rel.items() if r[0] == "overlap"}
    final: dict[str, tuple[int, int]] = {}
    rects: dict[str, tuple[int, int, int, int]] = {}

    def clear(key: str, x: int, y: int) -> tuple[int, int]:
        w, h = int(info[key]["width"]), int(info[key]["height"])
        others = [r for k, r in rects.items() if frozenset((k, key)) not in overlapping]
        return push_apart((x, y, w, h), others)

    for key in keys:  # seen: as placed, kept clear of what they don't really overlap
        x, y = clear(key, round(pos[key][0]), round(pos[key][1]))
        final[key] = (x, y)
        rects[key] = (x, y, int(info[key]["width"]), int(info[key]["height"]))
    missing = [s["key"] for s in screens if s["key"] not in final]
    for key in missing:  # not seen: along with its computer's screens that were
        s = info[key]
        moved = [k for k in keys if info[k]["device"] == s["device"]]
        dx = dy = 0
        if moved:
            m = info[moved[0]]
            dx, dy = final[moved[0]][0] - int(m["x"]), final[moved[0]][1] - int(m["y"])
        x, y = clear(key, int(s["x"]) + dx, int(s["y"]) + dy)
        final[key] = (x, y)
        rects[key] = (x, y, int(s["width"]), int(s["height"]))

    fronts, links, fits, joins = [], [], [], []
    for i, a in enumerate(keys):
        for b in keys[i + 1:]:
            r = rel.get((a, b))
            if r is None:
                continue
            if r[0] == "overlap":
                behind, front = (a, b) if desk.in_front(b, a) else (b, a)
                fronts.append((behind, front))
                both = desk.pair(behind, front)
                if both is not None:
                    joins.append((front, behind, None))
                if info[a]["device"] != info[b]["device"] and both is not None:
                    Bh, Fr = both
                    at = desk.edge_fit(behind, front) or (
                        float((Fr.x0 - Bh.x0) * Bh.k), float((Fr.y0 - Bh.y0) * Bh.k), float(Bh.k / Fr.k))
                    fit = {"behind": behind, "front": front, "x": at[0], "y": at[1], "scale": at[2],
                           **desk.lean(behind, front)}
                    if "eye" in fit and behind in mm:
                        fit["mm"] = mm[behind][0] / float(info[behind]["width"])
                    fits.append(fit)
                continue
            side = r[1]
            both = desk.pair(a, b, side)
            if both is None:
                continue
            if info[a]["device"] == info[b]["device"]:
                A, B = both
                shared = _span(A.y0, A.y1, B.y0, B.y1) if side in ("left", "right") else _span(A.x0, A.x1, B.x0, B.x1)
                if shared is not None:
                    joins.append((a, b, side))
                continue  # its own OS moves the pointer between them
            A, B = both
            if side in ("left", "right"):
                shared = _span(A.y0, A.y1, B.y0, B.y1)
                if shared is None:
                    continue
                lo, hi = shared
                a_span = ((lo - A.y0) * A.k, (hi - A.y0) * A.k)
                b_span = ((lo - B.y0) * B.k, (hi - B.y0) * B.k)
                a_len, b_len = float(info[a]["height"]), float(info[b]["height"])
            else:
                shared = _span(A.x0, A.x1, B.x0, B.x1)
                if shared is None:
                    continue
                lo, hi = shared
                a_span = ((lo - A.x0) * A.k, (hi - A.x0) * A.k)
                b_span = ((lo - B.x0) * B.k, (hi - B.x0) * B.k)
                a_len, b_len = float(info[a]["width"]), float(info[b]["width"])
            a_span = tuple(float(min(max(v, 0.0), a_len)) for v in a_span)
            b_span = tuple(float(min(max(v, 0.0), b_len)) for v in b_span)
            if a_span[1] - a_span[0] < 1 or b_span[1] - b_span[0] < 1:
                continue
            links.append({"a": a, "a_side": side, "a_span": a_span, "b": b, "b_side": OPP[side], "b_span": b_span})
            joins.append((a, b, side))
    from curslink import seat

    return Result(final, fronts, links, fits, found=keys, missing=missing, reference=ref, joins=joins,
                  errors={k: float(poses[k].error) for k in keys},
                  stance={k: desk.stance(k) for k in keys},
                  views=solution.frames, rms=solution.rms,
                  desk=seat.from_solution(solution, _world_mm(solution, mm, info)))


def overlap_of(a: tuple[int, int, int, int], b: tuple[int, int, int, int]) -> tuple[int, int, int, int] | None:
    """The area two ``(x, y, w, h)`` rects share, or None."""
    x0, y0 = max(a[0], b[0]), max(a[1], b[1])
    x1, y1 = min(a[0] + a[2], b[0] + b[2]), min(a[1] + a[3], b[1] + b[3])
    return (x0, y0, x1 - x0, y1 - y0) if x0 < x1 and y0 < y1 else None


def push_apart(rect: tuple[int, int, int, int], others: list[tuple[int, int, int, int]]) -> tuple[int, int]:
    """Move ``rect`` the shortest way out of every screen it overlaps, so it
    ends up touching instead."""
    x, y, w, h = rect
    for _ in range(8):
        hit = next((o for o in others if overlap_of((x, y, w, h), o)), None)
        if hit is None:
            break
        ox, oy, ow, oh = hit
        moves = [(ox - (x + w), 0), (ox + ow - x, 0), (0, oy - (y + h)), (0, oy + oh - y)]
        dx, dy = min(moves, key=lambda m: abs(m[0]) + abs(m[1]))
        x, y = x + dx, y + dy
    return x, y


def _world_mm(solution, mm: dict[str, tuple[float, float]], info: dict[str, dict]) -> float | None:
    """The size of one unit of the solved desk (a pixel of its reference
    screen) in mm, from whichever screens say how big they are."""
    each = sorted(mm[k][0] / float(info[k]["width"]) / float(sp.scale) for k, sp in solution.screens.items()
                  if k in mm and k in info and sp.scale > 0)
    if solution.reference in mm and solution.reference in info:
        return mm[solution.reference][0] / float(info[solution.reference]["width"])
    return each[len(each) // 2] if each else None


def _sizes_mm(screens: list[dict]) -> dict[str, tuple[float, float]]:
    out = {}
    for s in screens:
        try:
            w, h = float(s["width_mm"]), float(s["height_mm"])
        except (KeyError, TypeError, ValueError):
            continue
        if w > 0 and h > 0:
            out[s["key"]] = (w, h)
    return out


def line_up(photo: bytes, screens: list[dict], planned: dict[str, dict], reference: str | None = None) -> Result:
    """One photo's worth: its bytes and the screens (see :func:`arrange`; with
    ``width_mm`` and ``height_mm`` where their real size is known) to the result.

    One view can't tell how far away a screen is unless its size is known,
    so without that the screens are taken to stand about in one plane; the
    live line-up (:class:`Live`) is the one that measures.
    """
    try:
        import cv2  # noqa: F401
    except ImportError as exc:
        raise LineupError("this copy of Curslink has no camera support (install curslink[gaze])") from exc
    from curslink.lineup_scene import Scene, SceneError

    gray = load_photo(photo)
    dims = {s["key"]: (float(s["width"]), float(s["height"])) for s in screens if s["key"] in planned}
    scene = Scene(dims, reference, _sizes_mm(screens), (gray.shape[1], gray.shape[0]))
    scene.add(read_frame(gray, planned, dims, scene.tolerance, curved=scene.curved))
    if not scene.frames:
        raise LineupError("Curslink couldn't find the patterns in that photo. Get every screen in the picture, "
                          "close enough that the patterns are sharp, and try again")
    try:
        result = arrange(screens, scene.solve())
    except SceneError as exc:
        raise LineupError(str(exc)) from exc
    result.desk = None  # (one photo: no depth worth keeping)
    return result


# -- live: frame after frame from a phone moved about ------------------------------

# A screen is lined up when the sums put its place to within this share of
# the reference screen's shorter side (10 px on a 1620 px one): well under
# what a pointer crossing an edge shows.
ALIGNED = 0.006
# ...or when that's as sure as this camera gets: a real phone's frames (focus
# breathing, a rolling shutter, a smeared pattern) leave more doubt than the
# sums can average away in a scan anyone would sit through, while the layout
# itself stopped moving long before. So: enough views (or enough frames since
# every screen was placed), the solves agreeing, and the doubt not silly.
SETTLED_VIEWS = 12
SETTLED_FRAMES = 30
ROUGH = 0.03
MIN_VIEWS = 6  # either way, from at least this many different places
STEADY = 3  # and when this many solves in a row agree...
STILL = 0.004  # ...to this share of the reference's shorter side


class Live:
    """A live line-up: frames come in from the phone as it's moved about
    (:meth:`frame`), the desk is solved again whenever there's a new view
    (:meth:`update`), and :meth:`status` says how it's going, for the
    phone's screen and the computer's.

    ``screens`` as for :func:`arrange` (with ``width_mm``/``height_mm``
    where known); ``planned`` as :func:`plan` gives: the screens showing
    patterns, which are the ones to find.
    """

    def __init__(self, screens: list[dict], planned: dict[str, dict], reference: str | None = None):
        self.screens = screens
        self.planned = planned
        self.reference = reference
        self.dims = {s["key"]: (float(s["width"]), float(s["height"])) for s in screens if s["key"] in planned}
        self.wanted = [s["key"] for s in screens if s["key"] in planned]
        self.scene = None
        self.focal: float | None = None
        self.frames = 0  # every frame taken in
        self.last_seen: list[str] = []  # the screens in the latest one
        self.result: Result | None = None
        self.solution = None  # the curslink.lineup_scene.Solution it's from
        self.check = None  # curslink.lineup_check.Check, once the scan is done
        self.seam = None  # curslink.lineup_seam.Seam, after that: each overlap's pattern joined up
        self.seat = None  # curslink.seat.PhoneSeat, last: the phone at the eyes of someone sitting back
        self._locator = None
        self.error = ""
        self.fresh = False  # a new view since the last solve
        self.history: list[dict[str, tuple[int, int]]] = []
        self.solves = 0
        self.placed_at: int | None = None  # the frame at which every screen first had a place

    @property
    def short_side(self) -> float:
        ref = self.result.reference if self.result is not None else self.reference
        w, h = self.dims.get(ref) or next(iter(self.dims.values()), (1920.0, 1080.0))
        return float(min(w, h))

    @property
    def tolerance(self) -> float:
        """What "lined up" asks of the reference screen's place, in its pixels."""
        return max(3.0, ALIGNED * self.short_side)

    def asked(self, key: str, share: float = ALIGNED) -> float:
        """What "lined up" asks of a screen's place, in its own pixels (which
        is what its error is in)."""
        w, h = self.dims.get(key) or (1920.0, 1080.0)
        return max(3.0, share * min(w, h))

    @property
    def settled(self) -> bool:
        """Has the scan gone on long enough that more of it won't change the
        layout (if the solves agree)?"""
        if self.result is None or self.result.views < MIN_VIEWS:
            return False
        return self.result.views >= SETTLED_VIEWS or (
            self.placed_at is not None and self.frames - self.placed_at >= SETTLED_FRAMES)

    def frame(self, gray, focal: float | None = None) -> bool:
        """Take a frame in (a grey image); True if it gave a new view."""
        from curslink.lineup_scene import Scene

        size = (gray.shape[1], gray.shape[0])
        if focal and focal > 0:
            self.focal = float(focal)
        if self.scene is None or self.scene.image_size != size:
            # (A phone turned from upright to sideways sends frames the other
            # way up: a different camera, as far as the sums go. Start again.)
            self.scene = Scene(self.dims, self.reference, _sizes_mm(self.screens), size, self.focal)
            self.result = None
            self.history = []
            self.placed_at = None
        self.frames += 1
        sightings = read_frame(gray, self.planned, self.dims, self.scene.tolerance, thorough=False,
                               curved=self.scene.curved)
        self.last_seen = [s.key for s in sightings]
        kept = self.scene.add(sightings)
        self.fresh = self.fresh or kept
        return kept

    def update(self) -> Result | None:
        """Solve again if there's a new view; the latest result (or None)."""
        from curslink.lineup_scene import SceneError

        if not self.fresh or self.scene is None:
            return self.result
        self.fresh = False
        try:
            self.solution = self.scene.solve()
            self.result = arrange(self.screens, self.solution)
            self.error = ""
        except (SceneError, LineupError) as exc:
            self.error = str(exc)
            return self.result
        self.solves += 1
        self.history = (self.history + [dict(self.result.positions)])[-STEADY:]
        if self.placed_at is None and all(k in self.result.errors for k in self.wanted):
            self.placed_at = self.frames
        return self.result

    def steady(self) -> bool:
        if len(self.history) < STEADY:
            return False
        tol = max(2.0, STILL * self.short_side)
        first = self.history[0]
        return all(k in h and abs(h[k][0] - first[k][0]) <= tol and abs(h[k][1] - first[k][1]) <= tol
                   for h in self.history[1:] for k in first)

    def states(self) -> dict[str, str]:
        """Each wanted screen: "unseen", "seen" (but not yet with a screen
        that's placed), "placing" or "locked"."""
        seen = self.scene.seen() if self.scene is not None else {}
        out = {}
        enough = self.result is not None and self.result.views >= MIN_VIEWS
        share = ROUGH if enough and self.settled and self.steady() else ALIGNED
        for k in self.wanted:
            if self.result is not None and k in self.result.errors:
                out[k] = "locked" if enough and self.result.errors[k] <= self.asked(k, share) else "placing"
            else:
                out[k] = "seen" if k in seen else "unseen"
        return out

    @property
    def aligned(self) -> bool:
        states = self.states()
        return bool(states) and all(v == "locked" for v in states.values()) and self.steady()

    @property
    def usable(self) -> bool:
        """Is there a result worth taking as it is (if the person says so)?"""
        return self.result is not None and len(self.result.found) >= 2

    def progress(self) -> float:
        states = self.states()
        if not states:
            return 0.0
        tol = self.tolerance
        views = min(1.0, (self.result.views if self.result is not None else 0) / MIN_VIEWS)
        total = 0.0
        for k, state in states.items():
            if state == "unseen":
                continue
            if state == "seen":
                total += 0.15
                continue
            err = self.result.errors.get(k, float("inf"))
            if state == "locked":
                sure = 1.0
            else:
                # Either the doubt comes down to what's asked, or the scan settles.
                sure = max(0.0, min(1.0, self.asked(k) / err)) if err > 0 else 1.0
                if self.placed_at is not None:
                    sure = max(sure, min(0.95, (self.frames - self.placed_at) / SETTLED_FRAMES))
            total += 0.3 + 0.7 * views * sure
        p = total / len(states)
        if p >= 0.999 and not self.aligned:
            p = 0.97  # all sure, waiting for the solves to agree
        return p

    # ---- the check at the end: lines across the joins (curslink.lineup_check) ----
    def start_check(self):
        """Once it's lined up: the lines to draw across the screens' joins, to
        check the layout by. None if no two screens meet (nothing to check)."""
        from curslink.lineup_check import Check

        if self.check is None and self.aligned and self.solution is not None and self.scene is not None:
            check = Check(self.solution, self.result, check_plan(self.planned), self.scene.intr, self.scene.image_size)
            if check.any:
                self.check = check
        return self.check

    def check_frame(self, gray) -> bool:
        """A frame taken while the lines are up: the layout is nudged by any
        step the camera sees in them. True if it moved (the lines have too)."""
        if self.check is None or self.scene is None:
            return False
        sightings = read_frame(gray, check_plan(self.planned), self.dims, self.scene.tolerance, thorough=False,
                               curved=self.scene.curved)
        self.frames += 1
        self.last_seen = [s.key for s in sightings]
        moved = self.check.frame(gray, sightings)
        if moved:
            errors, views, rms = self.result.errors, self.result.views, self.result.rms
            self.result = arrange(self.screens, self.check.sol)
            self.result.errors, self.result.views, self.result.rms = errors, views, rms
        self.result.check = self.check_summary()
        return moved

    # ---- and last: a pattern across each overlap, joined up (curslink.lineup_seam) ----
    def start_seam(self):
        """After the check: the pattern each screen in front of another and
        the one behind it draw, to put that overlap's fit right by. None if
        no screen stands in front of another (or too little of one shows)."""
        from curslink.lineup_check import Check
        from curslink.lineup_seam import Seam

        if self.seam is None and self.aligned and self.solution is not None and self.scene is not None \
                and self.result is not None and self.result.fits:
            solution = self.check.sol if self.check is not None else self.solution
            planned = check_plan(self.planned)
            seam = Seam(solution, self.result.fits, planned, self.scene.intr, self.scene.image_size)
            if seam.any:
                self.seam = seam
                self._locator = self.check or Check(solution, self.result, planned, self.scene.intr,
                                                    self.scene.image_size)
        return self.seam

    def seam_frame(self, gray) -> bool:
        """A frame taken while the pattern is up: each overlap's fit is moved
        by the break the camera sees in it. True if one moved (the strips
        want drawing again)."""
        if self.seam is None or self.scene is None:
            return False
        sightings = read_frame(gray, self.seam.showing, self.dims, self.scene.tolerance, thorough=False,
                               curved=self.scene.curved)
        self.frames += 1
        self.last_seen = [s.key for s in sightings]
        moved = self.seam.frame(gray, sightings, self._locator.locate)
        mine = {(f["behind"], f["front"]): f for f in self.seam.fits()}
        self.result.fits = [mine.get((f["behind"], f["front"]), f) for f in self.result.fits]
        self.result.check = self.check_summary()
        return moved

    # ---- last of all: where you sit (curslink.seat) ----
    def start_seat(self):
        """After the pattern: the phone is to be held at the eyes of someone
        sitting as they usually sit, and found by the patterns still up
        (:func:`check_plan`). None if there's no overlap to aim (nothing
        depends on where the eyes are) or it can't be done."""
        from curslink import seat
        from curslink.lineup_check import Check

        if self.seat is None and self.aligned and self.solution is not None and self.scene is not None \
                and self.result is not None and self.result.desk is not None \
                and any(f.get("quad") is not None for f in self.result.fits):
            solution = self.check.sol if self.check is not None else self.solution
            if self._locator is None:
                self._locator = self.check or Check(solution, self.result, check_plan(self.planned), self.scene.intr,
                                                    self.scene.image_size)
            self.seat = seat.PhoneSeat(solution, seat.mm_of(self.result.desk))
        return self.seat

    def seat_frame(self, gray) -> bool:
        """A frame taken in that last step. True once the eyes are found:
        every overlap's fit is then aimed from there (``result.fits``), and
        ``result.desk`` says where that is."""
        from curslink import seat

        if self.seat is None or self.scene is None or self.seat.done:
            return False
        sightings = read_frame(gray, check_plan(self.planned), self.dims, self.scene.tolerance, thorough=False,
                               curved=self.scene.curved)
        self.frames += 1
        self.last_seen = [s.key for s in sightings]
        if not self.seat.sight(self._locator.locate(sightings) if sightings else None):
            return False
        # (The desk as the check left it; the fits as the pattern step did.)
        check = self.result.check
        desk = self.result.desk
        self.result.desk, self.result.fits = seat.seated(desk, self.result.fits, self.seat.eye, "phone")
        self.result.check = check
        return True

    def seat_summary(self, names: dict[str, str] | None = None) -> str:
        """Where the eyes were found, in a few words (for the phone and the computer)."""
        from curslink import seat

        desk = self.result.desk if self.result is not None else None
        if self.seat is None or self.seat.eye is None or desk is None:
            return ""
        mm = seat.mm_of(desk)
        if mm is None:
            return "Got you"
        main = seat.main_screen(desk, self.result.fits)
        return f"Got you: {seat.distance(desk, main) * mm / 10:.0f} cm from {(names or {}).get(main, 'your main screen')}"

    def check_summary(self) -> dict | None:
        """How the check went, for the Layout page: ``{"state", "lines",
        "joins", "step"}``. ``state``: "none" (no two screens meet where a
        line could be drawn), "unseen" (lines were drawn, the camera never
        saw one), "checked" (every join came within a pixel or two) or
        "part" (it stopped before that). ``step``: the biggest step left."""
        if not self.aligned:
            return None
        seam = {"seam": self.seam.summary()} if self.seam is not None else {}
        if self.check is None:
            return {"state": "none", "lines": 0, "joins": 0, "step": None, **seam}
        c = self.check
        drawn = [j for j in c.joins if j.lines]
        state = "unseen" if not c.measured else "checked" if c.agreed else "part"
        return {"state": state, "lines": sum(len(j.lines) for j in drawn), "joins": len(drawn), "step": c.worst(),
                **seam}

    def hint(self, numbers: dict[str, object] | None = None) -> str:
        """What to do next, in a few words (for the phone's screen)."""
        numbers = numbers or {}
        if self.seat is not None:
            if self.seat.eye is not None:
                return self.seat_summary({k: f"screen {n}" for k, n in numbers.items() if n})
            if self.seat.gave_up:
                return "Lined up"
            return ("Last step: sit back as you normally sit and rest the top of the phone on the bridge of your "
                    "nose, camera facing your screens. It buzzes when it has you")
        if self.seam is not None:
            if self.seam.done:
                return "Lined up"
            return "Hold the phone close to your eyes: the pattern is being joined up"
        if self.check is not None:
            if self.check.done:
                return "Lined up"
            return "Checking: keep the screens in the picture while the lines are lined up"

        def name(keys: list[str]) -> str:
            ns = [str(numbers.get(k) or "") for k in keys]
            ns = [n for n in ns if n]
            if not ns:
                return "every screen"
            return ("screen " + ns[0]) if len(ns) == 1 else "screens " + ", ".join(ns[:-1]) + " and " + ns[-1]

        states = self.states()
        if self.aligned:
            return "Lined up"
        if not self.frames or all(v == "unseen" for v in states.values()):
            return "Hold the phone up in front of your eyes, pointing at your screens"
        unseen = [k for k, v in states.items() if v == "unseen"]
        if unseen:
            return f"Get {name(unseen)} in the picture"
        loose = [k for k, v in states.items() if v == "seen"]
        if loose:
            return f"Get {name(loose)} in the picture together with the screen next to it"
        if all(v == "locked" for v in states.values()):
            return "Nearly there: hold it there"
        # A monitor that doesn't say how big it is can't be told from a
        # bigger one further off without seeing it from somewhere else.
        sized = self.scene is not None and all(k in self.scene.pitch for k in self.wanted)
        if not sized and self.result is not None and self.result.views >= MIN_VIEWS:
            return "Move the phone slowly from side to side"
        return "Hold the phone steady: the patterns are measuring each screen"


# -- the host's end: asking phones, taking their photos -------------------------

class PhotoInbox:
    """The host service's part of a line-up: puts the patterns up on the
    paired computers, asks the phones for their camera and gathers what
    they send (one photo, or frame after frame of a live line-up).

    ``server.send(device_id, t, **data)`` sends a frame; ``emit(event,
    **data)`` tells the app (see curslink.events): ``lineup_sizes`` (the
    paired computers' monitors' real sizes), ``lineup_camera`` (which phones
    were asked), ``lineup_photo`` (``{device, id, path}``, with ``seq`` and
    maybe ``focal`` for a live frame; or ``{device, id, cancelled}``,
    ``{device, id, done}`` or ``{device, id, error}``) and ``lineup_request``.
    """

    def __init__(self, server, emit, folder=None):
        self.server = server
        self.emit = emit
        self.folder = folder
        self.parts: dict[tuple[str, str], tuple[int, list[bytes], dict]] = {}

    def _send(self, device_id: str, t: str, **data) -> None:
        self.server.send(device_id, t, **data)

    def markers(self, payload: str) -> None:
        """``lineup {"devices": {device_id: {"screens": [...]}}}`` (or ``{"hide": true}``)."""
        import json

        try:
            data = json.loads(payload)
            if not isinstance(data, dict):
                raise ValueError("not an object")
        except ValueError:
            return
        devices = data.get("devices") if isinstance(data.get("devices"), dict) else {}
        sizes = {}
        for device_id in list(self.server.connections):
            if not self.server.supports(device_id, "lineup"):
                continue
            entry = devices.get(device_id)
            if data.get("hide") or not isinstance(entry, dict) or not isinstance(entry.get("screens"), list):
                self._send(device_id, "lineup", hide=True)
            else:
                seconds = entry.get("seconds", 300)
                more = {"period": entry["period"]} if isinstance(entry.get("period"), (int, float)) else {}
                self._send(device_id, "lineup", screens=entry["screens"], seconds=seconds, **more)
                mm = getattr(self.server.connections[device_id], "screen_mm", None)
                if mm:
                    sizes[device_id] = list(mm)
        if sizes:
            # How big those monitors really are (curslink.screensize), for the sums.
            self.emit("lineup_sizes", devices=sizes)

    def camera(self, payload: str) -> None:
        """``lineup_camera {"id": ..., "live": true, "cancel": false}``: ask every connected phone."""
        import json

        from curslink.protocol import LINEUP_CAMERA_FEATURE

        try:
            data = json.loads(payload)
            lineup_id = str(data["id"])
        except (ValueError, KeyError, TypeError):
            return
        asked = []
        for device_id, conn in list(self.server.connections.items()):
            if LINEUP_CAMERA_FEATURE not in conn.features:
                continue
            if data.get("cancel"):
                self._send(device_id, "lineup_camera", id=lineup_id, cancel=True)
            else:
                if data.get("live"):
                    self._send(device_id, "lineup_camera", id=lineup_id, live=True)
                else:
                    self._send(device_id, "lineup_camera", id=lineup_id)
                asked.append({"id": device_id, "name": conn.name})
        if data.get("cancel"):
            self.parts = {k: v for k, v in self.parts.items() if k[1] != lineup_id}
        else:
            self.emit("lineup_camera", id=lineup_id, devices=asked)

    def live(self, payload: str) -> None:
        """``lineup_live {"device": ..., "id": ..., "seq", "progress", "hint",
        "screens", "buzz", "done"}``: how it's going, to the phone sending the frames."""
        import json

        from curslink.protocol import LINEUP_CAMERA_FEATURE

        try:
            data = json.loads(payload)
            device_id, lineup_id = str(data["device"]), str(data["id"])
        except (ValueError, KeyError, TypeError):
            return
        conn = self.server.connections.get(device_id)
        if conn is None or LINEUP_CAMERA_FEATURE not in conn.features:
            return
        out = {"id": lineup_id}
        if isinstance(data.get("seq"), int) and not isinstance(data.get("seq"), bool):
            out["seq"] = data["seq"]
        try:
            out["progress"] = round(min(1.0, max(0.0, float(data.get("progress", 0)))), 3)
        except (TypeError, ValueError):
            out["progress"] = 0.0
        out["hint"] = str(data.get("hint", ""))[:200]
        screens = data.get("screens") if isinstance(data.get("screens"), list) else []
        out["screens"] = [[s[0], str(s[1])] for s in screens[:MAX_SCREENS]
                          if isinstance(s, list) and len(s) == 2 and isinstance(s[0], int)]
        if data.get("buzz"):
            out["buzz"] = True  # the phone is against a face: it says "got you" by shaking
        if data.get("done"):
            out["done"] = True
        self._send(device_id, "lineup_live", **out)

    def handle(self, device_id: str, t: str, data: dict) -> None:
        import base64
        import os
        import tempfile

        from curslink.protocol import LINEUP_PHOTO_MAX

        if t == "lineup_request":
            self.emit("lineup_request", device=device_id)
            return
        lineup_id = str(data.get("id"))
        key = (device_id, lineup_id)
        if t == "lineup_photo":
            if data.get("cancel"):
                self.parts.pop(key, None)
                self.emit("lineup_photo", device=device_id, id=lineup_id, cancelled=True)
                return
            if data.get("done"):
                self.parts.pop(key, None)
                self.emit("lineup_photo", device=device_id, id=lineup_id, done=True)
                return
            size = data.get("size")
            if not isinstance(size, int) or isinstance(size, bool) or not 0 < size <= LINEUP_PHOTO_MAX:
                self.emit("lineup_photo", device=device_id, id=lineup_id, error="the photo is too big")
                return
            extra = {}
            seq = data.get("seq")
            if isinstance(seq, int) and not isinstance(seq, bool):
                extra["seq"] = seq  # a frame of a live line-up
                focal = data.get("focal")
                if isinstance(focal, (int, float)) and not isinstance(focal, bool) and 50 < focal < 1e5:
                    extra["focal"] = float(focal)
            self.parts[key] = (size, [], extra)
            return
        if key not in self.parts:
            return
        size, chunks, extra = self.parts[key]
        try:
            chunks.append(base64.b64decode(data.get("data", ""), validate=True))
        except ValueError:
            self.parts.pop(key, None)
            self.emit("lineup_photo", device=device_id, id=lineup_id, error="the photo arrived garbled", **extra)
            return
        got = sum(len(c) for c in chunks)
        if got > size:
            self.parts.pop(key, None)
            self.emit("lineup_photo", device=device_id, id=lineup_id, error="the photo arrived garbled", **extra)
            return
        if not data.get("end"):
            return
        del self.parts[key]
        fd, path = tempfile.mkstemp(prefix="curslink-lineup-", suffix=".jpg", dir=self.folder)
        with os.fdopen(fd, "wb") as f:
            for c in chunks:
                f.write(c)
        self.emit("lineup_photo", device=device_id, id=lineup_id, path=path, **extra)
