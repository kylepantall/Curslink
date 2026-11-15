"""A desk in 3D for the line-up tests: screens standing where real ones do
(turned in, nearer, lower), and a phone camera moved about in front of them.

Lengths are in mm. A screen is ``(key, device, pixels w, h, width_mm, where
its middle is (x, y, z), yaw, pitch in degrees[, bend])``: x to the right, y
down, z away from the person; yaw turns it about the vertical (positive: it
turns to face right, its left edge coming nearer), pitch tips its top back.
``bend``: the angle a curved monitor's width curves through, in radians
(``width_mm`` is along the glass, the middle is that of its four corners).
"""

import numpy as np


def rot_y(deg):
    a = np.radians(deg)
    return np.array([[np.cos(a), 0, np.sin(a)], [0, 1, 0], [-np.sin(a), 0, np.cos(a)]])


def rot_x(deg):
    a = np.radians(deg)
    return np.array([[1, 0, 0], [0, np.cos(a), -np.sin(a)], [0, np.sin(a), np.cos(a)]])


class Desk:
    def __init__(self, screens):
        self.screens = {}
        for key, device, w, h, width_mm, mid, yaw, pitch, *more in screens:
            bend = float(more[0]) if more else 0.0
            pitch_mm = width_mm / w
            r = rot_y(-yaw) @ rot_x(-pitch)
            chord = w * float(np.sinc(bend / 2 / np.pi))
            top_left = np.array(mid, float) - r @ np.array([chord / 2 * pitch_mm, h / 2 * pitch_mm, 0])
            self.screens[key] = {"key": key, "device": device, "w": w, "h": h, "mm": pitch_mm, "r": r, "t": top_left,
                                 "bend": bend}

    def keys(self):
        return list(self.screens)

    def point(self, key, uv):
        s = self.screens[key]
        from curslink.lineup_scene import bent

        uv = np.atleast_2d(np.asarray(uv, float))
        x, z = bent(uv[:, 0] / s["w"], s["bend"])
        return (np.column_stack([x * s["w"], uv[:, 1], z * s["w"]]) * s["mm"]) @ s["r"].T + s["t"]

    def sizes(self):
        return {k: (s["w"] * s["mm"], s["h"] * s["mm"]) for k, s in self.screens.items()}

    def dims(self):
        return {k: (s["w"], s["h"]) for k, s in self.screens.items()}

    def layout(self):
        return [{"key": k, "device": s["device"], "width": s["w"], "height": s["h"], "x": i * 6000, "y": 0}
                for i, (k, s) in enumerate(self.screens.items())]


class Camera:
    def __init__(self, size=(1600, 1200), focal=1180.0, centre=None, k1=0.0, k2=0.0):
        self.size = size
        self.f = focal
        self.cx, self.cy = centre or (size[0] / 2, size[1] / 2)
        self.k1, self.k2 = k1, k2

    def look(self, at_from, target, roll=0.0):
        """World -> camera (R, t) standing at ``at_from`` looking at ``target``."""
        z = np.array(target, float) - np.array(at_from, float)
        z /= np.linalg.norm(z)
        x = np.cross(np.array([0, 1.0, 0]), z)
        x /= np.linalg.norm(x)
        y = np.cross(z, x)
        r = np.array([x, y, z])
        a = np.radians(roll)
        r = np.array([[np.cos(a), -np.sin(a), 0], [np.sin(a), np.cos(a), 0], [0, 0, 1]]) @ r
        return r, -r @ np.array(at_from, float)

    def project(self, pose, pts):
        r, t = pose
        xc = np.asarray(pts, float) @ r.T + t
        x, y = xc[:, 0] / xc[:, 2], xc[:, 1] / xc[:, 2]
        r2 = x * x + y * y
        d = 1 + self.k1 * r2 + self.k2 * r2 * r2
        return np.column_stack([self.f * d * x + self.cx, self.f * d * y + self.cy]), xc[:, 2]


def figure_of_eight(n, middle, reach=(260, 140, 120), aim=(0, 0, 0), wobble=40, seed=1):
    """``n`` places along a figure of 8 about ``middle``, each with where the phone points."""
    rng = np.random.default_rng(seed)
    out = []
    for i in range(n):
        a = 2 * np.pi * i / n
        at = np.array(middle, float) + np.array([reach[0] * np.sin(a), reach[1] * np.sin(2 * a), reach[2] * np.cos(a)])
        target = np.array(aim, float) + rng.normal(0, wobble, 3) * np.array([1, 1, 0])
        out.append((at, target, float(rng.normal(0, 3))))
    return out


# Kyle's sort of desk: a monitor in the middle, another to its left turned in,
# a laptop in front of the middle one, lower and nearer.
DESK = Desk([
    ("host/main", "host", 2560, 1440, 597, (0, 0, 0), 0, 0),
    ("pc/main", "pc", 1920, 1080, 527, (-552, 20, -124), 28, 0),
    ("mac/main", "mac", 1512, 982, 304, (120, 262, -260), 0, 18),
])


# -- drawn as a camera would see it (needs OpenCV) ------------------------------

def draw_screen(w: int, h: int, markers: list, cells: int = 6, lines=None):
    """A screen showing its patterns (and the check's ``lines``: runs of
    points as shares of its size), as curslink.gui.lineup draws them."""
    import cv2

    img = np.full((h, w), 45, np.uint8)  # whatever's on the screen behind the patterns
    for bits, fx, fy, fw, _fh in markers:
        x, y, side = fx * w, fy * h, fw * w
        cell = side / cells
        cv2.rectangle(img, (round(x - cell), round(y - cell)), (round(x + side + cell), round(y + side + cell)), 255, -1)
        cv2.rectangle(img, (round(x), round(y)), (round(x + side), round(y + side)), 0, -1)
        for r in range(4):
            for c in range(4):
                if bits >> (15 - (r * 4 + c)) & 1:
                    x0, y0 = x + (c + 1) * cell, y + (r + 1) * cell
                    cv2.rectangle(img, (round(x0), round(y0)), (round(x0 + cell) - 1, round(y0 + cell) - 1), 255, -1)
    for run in lines or []:
        pts = np.array([[(x * w * 16, y * h * 16) for x, y in run]]).round().astype(np.int32)
        cv2.polylines(img, pts, False, 255, max(2, round(0.008 * min(w, h))), cv2.LINE_AA, shift=4)
    return img


def render(desk: Desk, planned: dict, cam: Camera, pose, blur: float = 0.8, noise: float = 2.0,
           glare=None, motion: int = 0, seed: int = 0, shown: int = 0, lines=None):
    """The grey frame ``cam`` takes of ``desk`` from ``pose``, every screen
    in ``planned`` showing its patterns (its set number ``shown``, as they
    change through the scan). ``glare``: ``[(key, u, v, radius
    in screen widths)]`` reflections; ``motion``: pixels of smear."""
    import cv2

    rng = np.random.default_rng(seed)
    w, h = cam.size
    r, t = pose
    k = np.array([[cam.f, 0, cam.cx], [0, cam.f, cam.cy], [0, 0, 1.0]])
    out = np.full((h, w), 70, np.float32)
    order = sorted(desk.screens.values(), key=lambda s: -float((r @ desk.point(s["key"], [(s["w"] / 2, s["h"] / 2)])[0] + t)[2]))
    for s in order:
        if s["key"] not in planned:
            continue
        iw = 1100
        ih = round(iw * s["h"] / s["w"])
        sets = planned[s["key"]].get("sets") or [planned[s["key"]]["markers"]]
        img = draw_screen(iw, ih, sets[shown % len(sets)], lines=(lines or {}).get(s["key"])).astype(np.float32)
        for key, u, v, rad in glare or []:
            if key == s["key"]:
                yy, xx = np.mgrid[0:ih, 0:iw]
                img += 230 * np.exp(-(((xx - u * iw) ** 2 + (yy - v * ih) ** 2) / (2 * (rad * iw) ** 2)))
        step = s["mm"] * s["w"] / iw
        # A curved screen is drawn as upright strips, each as good as flat.
        strips = 24 if s["bend"] else 1
        for i in range(strips):
            a0, a1 = round(i * iw / strips), round((i + 1) * iw / strips)
            p0 = desk.point(s["key"], [(a0 * s["w"] / iw, 0)])[0]
            p1 = desk.point(s["key"], [(a1 * s["w"] / iw, 0)])[0]
            across = (p1 - p0) / (a1 - a0)
            # strip pixel (x, y) is at p0 + (x - a0) * across + y * down
            hm = k @ np.column_stack([r @ across, r @ s["r"][:, 1] * step, r @ (p0 - a0 * across) + t])
            part = np.zeros((ih, iw), np.float32)
            part[:, a0:a1] = 1
            warped = cv2.warpPerspective(img * part, hm, (w, h), flags=cv2.INTER_LINEAR)
            mask = cv2.warpPerspective(part, hm, (w, h), flags=cv2.INTER_LINEAR)
            out = out * (1 - mask) + warped
    if cam.k1 or cam.k2:
        # Where each pixel of the lens's picture comes from in the ideal one.
        yy, xx = np.mgrid[0:h, 0:w].astype(np.float64)
        xd, yd = (xx - cam.cx) / cam.f, (yy - cam.cy) / cam.f
        xu, yu = xd.copy(), yd.copy()
        for _ in range(6):
            r2 = xu * xu + yu * yu
            d = 1 + cam.k1 * r2 + cam.k2 * r2 * r2
            xu, yu = xd / d, yd / d
        out = cv2.remap(out, (xu * cam.f + cam.cx).astype(np.float32), (yu * cam.f + cam.cy).astype(np.float32),
                        cv2.INTER_LINEAR, borderValue=70)
    if motion > 1:
        kernel = np.zeros((motion, motion), np.float32)
        kernel[motion // 2, :] = 1 / motion
        out = cv2.filter2D(out, -1, kernel)
    if blur > 0:
        out = cv2.GaussianBlur(out, (0, 0), blur)
    out = out + rng.normal(0, noise, out.shape)
    return np.clip(out, 0, 255).astype(np.uint8)


def flat_desk(rows) -> Desk:
    """A desk from ``(key, device, pixels w, h, left, top, width_mm, height_mm[, z])``
    rows: every screen upright, facing the person."""
    out = []
    for key, device, w, h, x, y, rw, _rh, *z in rows:
        rh = rw * h / w
        out.append((key, device, w, h, rw, (x + rw / 2, y + rh / 2, z[0] if z else 0.0), 0, 0))
    return Desk(out)


def photo(desk, planned, size=(3000, 1500), offset=(120, 60), k1=0.0, seed=0):
    """One photo of the whole of ``desk`` (a Desk, or rows for :func:`flat_desk`)
    from in front of it, a little to one side: (JPEG bytes, the camera, its pose)."""
    import cv2

    if not isinstance(desk, Desk):
        desk = flat_desk(desk)
    pts = np.concatenate([desk.point(k, [(0, 0), (s["w"], s["h"])]) for k, s in desk.screens.items()])
    lo, hi = pts.min(0), pts.max(0)
    mid = (lo + hi) / 2
    cam = Camera(size=size, focal=0.75 * size[0], k1=k1)
    back = max(cam.f * (hi[0] - lo[0]) / (0.8 * size[0]), cam.f * (hi[1] - lo[1]) / (0.8 * size[1]))
    pose = cam.look((mid[0] + offset[0], mid[1] + offset[1], lo[2] - back), (mid[0], mid[1], mid[2]))
    ok, jpg = cv2.imencode(".jpg", render(desk, planned, cam, pose, seed=seed), [cv2.IMWRITE_JPEG_QUALITY, 88])
    assert ok
    return jpg.tobytes(), cam, pose


def scan(desk: Desk, planned, n=14, cam=None, middle=(-150, 60, -1050), aim=(-200, 80, 0), glare=True, seed=0):
    """Frames as a phone moved in a figure of 8 would send them: some with a
    reflection on a screen, some smeared by the movement."""
    cam = cam or Camera(focal=1180, k1=0.04, k2=-0.02, centre=(812, 588))
    for i, (at, target, roll) in enumerate(figure_of_eight(n, middle, aim=aim, seed=seed)):
        shine = None
        if glare and i % 3 == 0:
            shine = [(desk.keys()[0], 0.85, 0.8, 0.1)]
        elif glare and i % 3 == 1:
            shine = [(desk.keys()[1], 0.2, 0.3, 0.12)]
        yield render(desk, planned, cam, cam.look(at, target, roll), glare=shine,
                     motion=9 if i % 5 == 4 else 0, seed=seed + i)


def true_solution(desk: Desk, reference: str, eye_mm):
    """The desk as curslink.lineup_scene would have it if it measured perfectly."""
    from curslink.lineup_scene import ScreenPose, Solution

    ref = desk.screens[reference]
    screens = {}
    for k, s in desk.screens.items():
        screens[k] = ScreenPose(k, s["w"], s["h"], ref["r"].T @ s["r"], ref["r"].T @ (s["t"] - ref["t"]) / ref["mm"],
                                s["mm"] / ref["mm"], 9, bend=s["bend"])
    eye = ref["r"].T @ (np.asarray(eye_mm, float) - ref["t"]) / ref["mm"]
    return Solution(reference, screens, eye, 1000.0, (0.0, 0.0), (0.0, 0.0), 0.0, 9)
