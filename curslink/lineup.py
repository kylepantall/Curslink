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


def plan(screens: list[dict]) -> dict[str, dict]:
    """What each screen shows: ``{key: {"slot": n, "markers": [[bits, x, y, w, h], ...]}}``
    for ``screens`` (``{"key", "width", "height"}``), in the order given.

    Marker ids ``slot * 8 .. slot * 8 + 7``; at most :data:`MAX_SCREENS` screens.
    """
    out = {}
    for slot, s in enumerate(screens[:MAX_SCREENS]):
        boxes = marker_boxes(float(s["width"]), float(s["height"]))
        out[s["key"]] = {
            "slot": slot,
            "markers": [[marker_bits(slot * PER_SCREEN + i), *(round(v, 5) for v in box)]
                        for i, box in enumerate(boxes)],
        }
    return out


# -- finding the screens in a photo --------------------------------------------

@dataclass
class Seen:
    """A screen found in the photo."""

    key: str
    homography: list[list[float]]  # its own 0..1 coordinates -> the photo's pixels
    markers: int  # how many of its markers were read
    # Where each marker read was, in the photo's pixels (its middle).
    centres: list[tuple[float, float]] = field(default_factory=list)


def _apply(h, x: float, y: float) -> tuple[float, float]:
    w = h[2][0] * x + h[2][1] * y + h[2][2]
    if abs(w) < 1e-12:
        w = 1e-12
    return ((h[0][0] * x + h[0][1] * y + h[0][2]) / w, (h[1][0] * x + h[1][1] * y + h[1][2]) / w)


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


def find_screens(gray, planned: dict[str, dict]) -> dict[str, Seen]:
    """The screens of ``planned`` (as :func:`plan` gives) found in ``gray``."""
    import cv2
    import numpy as np

    dictionary = cv2.aruco.getPredefinedDictionary(getattr(cv2.aruco, DICTIONARY))
    params = cv2.aruco.DetectorParameters()
    params.cornerRefinementMethod = cv2.aruco.CORNER_REFINE_SUBPIX
    detector = cv2.aruco.ArucoDetector(dictionary, params)
    # A screen right at the photo's edge: its patterns' white border runs into
    # the edge and they aren't found, so give the photo a grey frame.
    pad = max(16, round(0.02 * max(gray.shape[:2])))
    framed = cv2.copyMakeBorder(gray, pad, pad, pad, pad, cv2.BORDER_CONSTANT, value=int(np.median(gray)))
    corners, ids, _rejected = detector.detectMarkers(framed)
    if ids is None or not len(ids):
        # A dim photo of bright screens: stretch the contrast and look again.
        clahe = cv2.createCLAHE(clipLimit=3.0, tileGridSize=(8, 8))
        corners, ids, _rejected = detector.detectMarkers(clahe.apply(framed))
    corners = [c - pad for c in corners] if ids is not None else []
    by_slot = {p["slot"]: (key, p) for key, p in planned.items()}
    points: dict[str, tuple[list, list, list]] = {}
    for quad, marker_id in zip(corners, ids.flatten() if ids is not None else []):
        slot, index = divmod(int(marker_id), PER_SCREEN)
        if slot not in by_slot:
            continue
        key, p = by_slot[slot]
        _bits, x, y, w, h = p["markers"][index]
        src, dst, centres = points.setdefault(key, ([], [], []))
        src.extend([(x, y), (x + w, y), (x + w, y + h), (x, y + h)])  # as ArUco orders a marker's corners
        q = quad.reshape(4, 2)
        dst.extend(q.tolist())
        centres.append((float(q[:, 0].mean()), float(q[:, 1].mean())))
    out = {}
    for key, (src, dst, centres) in points.items():
        method = cv2.RANSAC if len(src) >= 8 else 0
        h, _mask = cv2.findHomography(np.array(src, np.float64), np.array(dst, np.float64), method, 3.0)
        if h is None:
            continue
        out[key] = Seen(key, h.tolist(), len(centres), centres)
    return out


# -- from where they are in the photo to the layout ---------------------------

@dataclass
class Placed:
    """Where a screen really is, in the reference screen's pixels."""

    key: str
    x0: float
    y0: float
    x1: float
    y1: float
    k: float  # its own pixels per reference pixel
    markers: list[tuple[float, float]]  # where its markers were read, same units

    @property
    def w(self) -> float:
        return self.x1 - self.x0

    @property
    def h(self) -> float:
        return self.y1 - self.y0

    @property
    def area(self) -> float:
        return self.w * self.h

    def inside(self, x: float, y: float) -> bool:
        return self.x0 <= x <= self.x1 and self.y0 <= y <= self.y1


@dataclass
class Result:
    """What the photo says, for the Layout page to show before it's saved."""

    positions: dict[str, tuple[int, int]]  # key -> new (x, y) in the layout
    fronts: list[tuple[str, str]]  # (behind, front) for each overlap: front goes in front
    links: list[dict]  # for ConfigFile.set_link: a, a_side, a_span, b, b_side, b_span
    fits: list[dict]  # for ConfigFile.set_fit: behind, front, x, y, scale
    found: list[str]  # the screens seen in the photo
    missing: list[str]  # the screens that weren't
    reference: str


def _invert(h):
    import numpy as np

    return np.linalg.inv(np.array(h, np.float64)).tolist()


def _matmul(a, b):
    return [[sum(a[i][k] * b[k][j] for k in range(3)) for j in range(3)] for i in range(3)]


def _convex(quad: list[tuple[float, float]]) -> bool:
    """Is the quad convex and clockwise on screen (y down), as a screen seen from the front is?"""
    signs = []
    for i in range(4):
        (ax, ay), (bx, by), (cx, cy) = quad[i], quad[(i + 1) % 4], quad[(i + 2) % 4]
        signs.append((bx - ax) * (cy - by) - (by - ay) * (cx - bx))
    return all(s > 0 for s in signs)


def measure(screens: list[dict], seen: dict[str, Seen], reference: str | None = None) -> tuple[str, dict[str, Placed]]:
    """Each seen screen's real rectangle, in the reference screen's pixels.

    ``screens``: ``{"key", "width", "height"}``. The reference is
    ``reference`` if it was seen, else the screen with the most markers read.
    """
    sizes = {s["key"]: (float(s["width"]), float(s["height"])) for s in screens}
    seen = {k: v for k, v in seen.items() if k in sizes}
    if not seen:
        raise LineupError("none of the screens' patterns are in the photo")
    if reference not in seen:
        reference = max(seen.values(), key=lambda s: (s.markers, s.key)).key
    rw, rh = sizes[reference]
    to_ref = _invert(seen[reference].homography)  # photo -> the reference's 0..1
    out = {}
    for key, s in seen.items():
        w, h = sizes[key]
        m = _matmul(to_ref, s.homography)
        quad = [_apply(m, x, y) for x, y in ((0, 0), (1, 0), (1, 1), (0, 1))]
        quad = [(x * rw, y * rh) for x, y in quad]
        if not _convex(quad):
            continue  # seen at a slant no screen stands at: a misread
        (tlx, tly), (trx, try_), (brx, bry), (blx, bly) = quad
        x0, x1 = (tlx + blx) / 2, (trx + brx) / 2
        y0, y1 = (tly + try_) / 2, (bly + bry) / 2
        if x1 - x0 < 1 or y1 - y0 < 1:
            continue
        k = (w / (x1 - x0) + h / (y1 - y0)) / 2
        if not 0.02 < k * (rw / w) < 50:  # wildly bigger or smaller than the reference: a misread
            continue
        to_ref_px = [_apply(to_ref, cx, cy) for cx, cy in s.centres]
        out[key] = Placed(key, x0, y0, x1, y1, k, [(x * rw, y * rh) for x, y in to_ref_px])
    if reference not in out:
        raise LineupError("the photo is too slanted to use: take it from in front of the screens")
    return reference, out


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


def in_front(a: Placed, b: Placed) -> bool:
    """Is ``a`` in front of ``b`` where they overlap? Its markers were read
    inside ``b`` (which they'd be hidden by otherwise); else the smaller one
    is, like a laptop in front of a monitor."""
    a_in_b = sum(1 for x, y in a.markers if b.inside(x, y))
    b_in_a = sum(1 for x, y in b.markers if a.inside(x, y))
    if a_in_b != b_in_a:
        return a_in_b > b_in_a
    return a.area < b.area


OPP = {"left": "right", "right": "left", "top": "bottom", "bottom": "top"}


def solve(screens: list[dict], seen: dict[str, Seen], reference: str | None = None) -> Result:
    """The layout from what was seen.

    ``screens``: every screen in the layout, ``{"key", "device", "x", "y",
    "width", "height"}``. The reference screen stays where it is; the others
    are placed from it, each beside or over a screen already placed, in that
    screen's pixels. Screens not seen move with their own computer's screens
    that were (or stay put), and nothing ends up overlapping that didn't
    overlap in the photo.
    """
    info = {s["key"]: s for s in screens}
    ref, placed = measure(screens, seen, reference)
    keys = sorted(placed, key=lambda k: (k != ref, -len(placed[k].markers), k))
    rel = {}
    for i, a in enumerate(keys):
        for b in keys[i + 1:]:
            r = relation(placed[a], placed[b])
            if r is not None:
                rel[(a, b)] = r
                rel[(b, a)] = ("overlap",) if r[0] == "overlap" else ("side", OPP[r[1]])

    pos: dict[str, tuple[float, float]] = {ref: (float(info[ref]["x"]), float(info[ref]["y"]))}

    def place(p: str, n: str) -> tuple[float, float]:
        """Where ``n`` goes, from ``p`` already placed."""
        P, N = placed[p], placed[n]
        px, py = pos[p]
        pw, ph = float(info[p]["width"]), float(info[p]["height"])
        nw, nh = float(info[n]["width"]), float(info[n]["height"])
        r = rel.get((p, n))
        if r is not None and r[0] == "side":
            side = r[1]
            if side == "right":
                return px + pw, py + (N.y0 - P.y0) * P.k
            if side == "left":
                return px - nw, py + (N.y0 - P.y0) * P.k
            if side == "bottom":
                return px + (N.x0 - P.x0) * P.k, py + ph
            return px + (N.x0 - P.x0) * P.k, py - nh
        if r is not None and not in_front(P, N):
            # n is in front of p: where it covers p, in p's pixels.
            return px + (N.x0 - P.x0) * P.k, py + (N.y0 - P.y0) * P.k
        if r is not None:
            # n is behind p: p sits on n at (P - N) in n's pixels.
            return px - (P.x0 - N.x0) * N.k, py - (P.y0 - N.y0) * N.k
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
                    P, N = placed[p], placed[n]
                    d = math.hypot((P.x0 + P.x1 - N.x0 - N.x1) / 2, (P.y0 + P.y1 - N.y0 - N.y1) / 2)
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

    fronts, links, fits = [], [], []
    for i, a in enumerate(keys):
        for b in keys[i + 1:]:
            r = rel.get((a, b))
            if r is None:
                continue
            A, B = placed[a], placed[b]
            if r[0] == "overlap":
                behind, front = (a, b) if in_front(B, A) else (b, a)
                fronts.append((behind, front))
                if info[a]["device"] != info[b]["device"]:
                    Bh, Fr = placed[behind], placed[front]
                    fits.append({"behind": behind, "front": front, "x": (Fr.x0 - Bh.x0) * Bh.k,
                                 "y": (Fr.y0 - Bh.y0) * Bh.k, "scale": Bh.k / Fr.k})
                continue
            if info[a]["device"] == info[b]["device"]:
                continue  # its own OS moves the pointer between them
            side = r[1]
            if side in ("left", "right"):
                lo, hi = _span(A.y0, A.y1, B.y0, B.y1)
                a_span = ((lo - A.y0) * A.k, (hi - A.y0) * A.k)
                b_span = ((lo - B.y0) * B.k, (hi - B.y0) * B.k)
                a_len, b_len = float(info[a]["height"]), float(info[b]["height"])
            else:
                lo, hi = _span(A.x0, A.x1, B.x0, B.x1)
                a_span = ((lo - A.x0) * A.k, (hi - A.x0) * A.k)
                b_span = ((lo - B.x0) * B.k, (hi - B.x0) * B.k)
                a_len, b_len = float(info[a]["width"]), float(info[b]["width"])
            a_span = tuple(min(max(v, 0.0), a_len) for v in a_span)
            b_span = tuple(min(max(v, 0.0), b_len) for v in b_span)
            if a_span[1] - a_span[0] < 1 or b_span[1] - b_span[0] < 1:
                continue
            links.append({"a": a, "a_side": side, "a_span": a_span, "b": b, "b_side": OPP[side], "b_span": b_span})
    return Result(final, fronts, links, fits, found=keys, missing=missing, reference=ref)


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


def line_up(photo: bytes, screens: list[dict], planned: dict[str, dict], reference: str | None = None) -> Result:
    """All of it: the photo's bytes and the screens (see :func:`solve`) to the result."""
    try:
        import cv2  # noqa: F401
    except ImportError as exc:
        raise LineupError("this copy of Curslink has no camera support (install curslink[gaze])") from exc
    seen = find_screens(load_photo(photo), planned)
    if not seen:
        raise LineupError("Curslink couldn't find the patterns in that photo. Get every screen in the picture, "
                          "close enough that the patterns are sharp, and try again")
    return solve(screens, seen, reference)


# -- the host's end: asking phones, taking their photos -------------------------

class PhotoInbox:
    """The host service's part of a line-up: puts the patterns up on the
    paired computers, asks the phones for a photo and gathers it.

    ``send(device_id, t, **data)`` sends a frame; ``emit(event, **data)``
    tells the app (see curslink.events): ``lineup_camera`` (which phones
    were asked), ``lineup_photo`` (``{device, id, path}`` or ``{device, id,
    cancelled}`` or ``{device, id, error}``) and ``lineup_request``.
    """

    def __init__(self, server, emit, folder=None):
        self.server = server
        self.emit = emit
        self.folder = folder
        self.parts: dict[tuple[str, str], tuple[int, list[bytes]]] = {}

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
        for device_id in list(self.server.connections):
            if not self.server.supports(device_id, "lineup"):
                continue
            entry = devices.get(device_id)
            if data.get("hide") or not isinstance(entry, dict) or not isinstance(entry.get("screens"), list):
                self._send(device_id, "lineup", hide=True)
            else:
                seconds = entry.get("seconds", 300)
                self._send(device_id, "lineup", screens=entry["screens"], seconds=seconds)

    def camera(self, payload: str) -> None:
        """``lineup_camera {"id": ..., "cancel": false}``: ask every connected phone."""
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
                self._send(device_id, "lineup_camera", id=lineup_id)
                asked.append({"id": device_id, "name": conn.name})
        if data.get("cancel"):
            self.parts = {k: v for k, v in self.parts.items() if k[1] != lineup_id}
        else:
            self.emit("lineup_camera", id=lineup_id, devices=asked)

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
            size = data.get("size")
            if not isinstance(size, int) or isinstance(size, bool) or not 0 < size <= LINEUP_PHOTO_MAX:
                self.emit("lineup_photo", device=device_id, id=lineup_id, error="the photo is too big")
                return
            self.parts[key] = (size, [])
            return
        if key not in self.parts:
            return
        size, chunks = self.parts[key]
        try:
            chunks.append(base64.b64decode(data.get("data", ""), validate=True))
        except ValueError:
            self.parts.pop(key, None)
            self.emit("lineup_photo", device=device_id, id=lineup_id, error="the photo arrived garbled")
            return
        got = sum(len(c) for c in chunks)
        if got > size:
            self.parts.pop(key, None)
            self.emit("lineup_photo", device=device_id, id=lineup_id, error="the photo arrived garbled")
            return
        if not data.get("end"):
            return
        del self.parts[key]
        fd, path = tempfile.mkstemp(prefix="curslink-lineup-", suffix=".jpg", dir=self.folder)
        with os.fdopen(fd, "wb") as f:
            for c in chunks:
                f.write(c)
        self.emit("lineup_photo", device=device_id, id=lineup_id, path=path)
