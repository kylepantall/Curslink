"""Auto line-up (curslink.lineup, curslink.lineup_scene): a desk in 3D with
every screen showing its patterns, seen by a phone camera moved about in
front of it, read back into a layout."""

import numpy as np
import pytest
from lineup_desk import DESK as DESK3D
from lineup_desk import Camera, Desk, figure_of_eight, flat_desk, photo, scan, true_solution

from curslink import lineup, lineup_scene

# A desk all in one plane, in mm: (key, device, layout px w, h, real x, y, w, h[, z]).
DESK = [
    ("host/main", "host", 1920, 1080, 0, 0, 600, 340),
    ("host/screen2", "host", 2560, 1440, 620, 0, 600, 340),
    ("pc/main", "pc", 1920, 1080, -540, 30, 520, 290),
    # A laptop standing in front of the main monitor's bottom middle.
    ("mac/main", "mac", 1440, 900, 250, 220, 300, 190, -8),
]


def screens(desk=DESK):
    # Where they are before the line-up: anywhere (a row, far apart).
    return [{"key": k, "device": d, "width": w, "height": h, "x": i * 5000, "y": 0}
            for i, (k, d, w, h, *_rest) in enumerate(desk)]


def plan_for(desk):
    return lineup.plan([{"key": k, "width": w, "height": h} for k, _d, w, h, *_r in desk])


# -- the sums alone (no OpenCV) --------------------------------------------------

def sightings(desk: Desk, cam: Camera, pose, scene, noise=0.3, rng=None, spoil=None):
    """What a perfect reader would give for this view, plus ``noise`` px."""
    rng = rng or np.random.default_rng(0)
    out = []
    for k, s in desk.screens.items():
        on, at = [], []
        for i, (x, y, w, h) in enumerate(lineup.marker_boxes(s["w"], s["h"])):
            uv = np.array([(x, y), (x + w, y), (x + w, y + h), (x, y + h)]) * [s["w"], s["h"]]
            xy, z = cam.project(pose, desk.point(k, uv))
            if (z <= 0).any() or (xy < 0).any() or (xy[:, 0] > cam.size[0]).any() or (xy[:, 1] > cam.size[1]).any():
                continue
            xy = xy + rng.normal(0, noise, xy.shape)
            if spoil == (k, i):
                xy = xy + 40  # a pattern read in the wrong place
            on.append(uv)
            at.append(xy)
        s = lineup_scene.sight(k, on, at, scene.tolerance)
        if s is not None:
            out.append(s)
    return out


def solved(desk=DESK3D, n=14, sizes=None, focal=None, noise=0.3, seed=0, spoil=None):
    rng = np.random.default_rng(seed)
    cam = Camera(focal=1180.0, k1=0.03, centre=(810, 590))
    scene = lineup_scene.Scene(desk.dims(), "host/main", sizes, cam.size, focal)
    for i, (at, target, roll) in enumerate(figure_of_eight(n, (-150, 60, -1050), aim=(-200, 80, 0), seed=seed)):
        scene.add(sightings(desk, cam, cam.look(at, target, roll), scene, noise, rng,
                            spoil if spoil and i % 2 else None))
    return scene, scene.solve()


def corner_errors(desk, sol):
    """How far each solved screen's corners are from the truth, in reference pixels."""
    ref = desk.screens[sol.reference]
    out = {}
    for k, sp in sol.screens.items():
        true = (desk.point(k, [(0, 0), (sp.width, 0), (sp.width, sp.height), (0, sp.height)]) - ref["t"]) @ ref["r"]
        out[k] = float(np.linalg.norm(sp.corners() - true / ref["mm"], axis=1).max())
    return out


def test_many_views_place_every_screen_in_3d():
    scene, sol = solved()
    assert sol.frames >= 6
    # Turned in 28 degrees, a laptop 26 cm nearer: every corner within a few pixels
    # of the reference (1 px is under a quarter of a millimetre).
    assert max(corner_errors(DESK3D, sol).values()) < 4
    ref = DESK3D.screens["host/main"]
    for k, sp in sol.screens.items():
        assert sp.scale == pytest.approx(DESK3D.screens[k]["mm"] / ref["mm"], rel=0.003)
        assert sp.error < 4  # and it knows it's sure
    # The camera, too: its focal length and its lens.
    assert sol.focal == pytest.approx(1180, rel=0.01)
    assert sol.centre == pytest.approx((810, 590), abs=4)
    assert sol.distortion[0] == pytest.approx(0.03, abs=0.01)
    assert sol.rms < 0.7


def test_one_view_cant_tell_how_far_unless_sizes_are_known():
    _scene, blind = solved(n=1)
    _scene, sized = solved(n=1, sizes=DESK3D.sizes())
    assert max(corner_errors(DESK3D, blind).values()) > 300  # near and small, or far and big?
    assert max(corner_errors(DESK3D, sized).values()) < 80
    # And unsure is what it says it is.
    assert blind.screens["mac/main"].error > 2 * solved()[1].screens["mac/main"].error


def test_a_misread_pattern_is_left_out():
    scene, sol = solved(spoil=("pc/main", 2))
    assert max(corner_errors(DESK3D, sol).values()) < 4


def test_noisier_frames_are_less_sure():
    _s, clean = solved(noise=0.2)
    _s, rough = solved(noise=1.2)
    assert rough.screens["pc/main"].error > 2 * clean.screens["pc/main"].error
    assert max(corner_errors(DESK3D, rough).values()) < 15


def test_same_view_twice_is_not_a_new_view():
    cam = Camera()
    scene = lineup_scene.Scene(DESK3D.dims(), "host/main", None, cam.size)
    pose = cam.look((-150, 60, -1050), (-200, 80, 0))
    assert scene.add(sightings(DESK3D, cam, pose, scene))
    assert not scene.add(sightings(DESK3D, cam, pose, scene))
    assert scene.add(sightings(DESK3D, cam, cam.look((150, 60, -1050), (-200, 80, 0)), scene))


def test_homography_and_pose_round_trip():
    cam = Camera()
    pose = cam.look((200, -100, -900), (0, 0, 0), roll=5)
    uv = np.array([(0, 0), (500, 0), (500, 300), (0, 300), (250, 150), (100, 250)], float)
    xy, _z = cam.project(pose, np.column_stack([uv, np.zeros(len(uv))]))
    h = lineup_scene.fit_homography(uv, xy)
    assert np.abs(lineup_scene.apply_homography(h, uv) - xy).max() < 1e-6
    r, t = lineup_scene.pose_from_homography(h, cam.f, cam.cx, cam.cy)
    assert np.allclose(r, pose[0], atol=1e-6) and np.allclose(t, pose[1], atol=1e-4)
    assert lineup_scene.focal_from_homographies([h], cam.cx, cam.cy) == pytest.approx(cam.f, rel=1e-4)


# -- from the desk in 3D to the layout -------------------------------------------

def test_layout_is_the_desk_as_seen_from_where_the_person_sits():
    result = lineup.arrange(DESK3D.layout(), true_solution(DESK3D, "host/main", (-150, 60, -1050)))
    assert result.positions["host/main"] == (0, 0)
    # The PC's monitor is to the left, turned in; its near edge meets the main one's left edge.
    assert result.positions["pc/main"][0] == -1920
    link = next(li for li in result.links if li["b"] == "pc/main")
    assert (link["a_side"], link["b_side"]) == ("left", "right")
    assert link["b_span"] == pytest.approx((0, 1080), abs=3)
    # Where the edges meet, the PC's 296 mm tall panel covers 296 mm of the main one's 336.
    assert link["a_span"][1] - link["a_span"][0] == pytest.approx(1440 * 296 / 336, abs=6)
    assert result.stance["pc/main"]["yaw"] == pytest.approx(28, abs=0.01)
    # The laptop is below, nearer: from here its top edge runs along the main one's bottom.
    link = next(li for li in result.links if li["b"] == "mac/main")
    assert (link["a_side"], link["b_side"]) == ("bottom", "top")
    assert result.stance["mac/main"]["pitch"] == pytest.approx(18, abs=0.01)
    assert result.stance["mac/main"]["depth"] > 800
    # Seen from further right, the nearer laptop sits further left against the monitor.
    moved = lineup.arrange(DESK3D.layout(), true_solution(DESK3D, "host/main", (150, 60, -1050)))
    assert moved.positions["mac/main"][0] < result.positions["mac/main"][0] - 100
    assert abs(moved.positions["pc/main"][1] - result.positions["pc/main"][1]) < 12  # the edges that touch barely move


def test_a_laptop_in_front_of_a_monitor_overlaps_it():
    desk = Desk([
        ("host/main", "host", 2560, 1440, 597, (0, 0, 0), 0, 0),
        ("mac/main", "mac", 1512, 982, 304, (60, 150, -250), 0, 15),
    ])
    result = lineup.arrange(desk.layout(), true_solution(desk, "host/main", (0, 0, -900)))
    assert result.fronts == [("host/main", "mac/main")]
    fit = result.fits[0]
    assert fit["behind"] == "host/main" and fit["front"] == "mac/main"
    # Nearer, so it covers more of the monitor than its size alone would.
    flat = (304 / 1512) / (597 / 2560)
    assert fit["scale"] > 1.2 * flat
    x, y = result.positions["mac/main"]
    assert (x, y) == (round(fit["x"]), round(fit["y"]))


def test_relation_sides_and_gaps():
    a = lineup.Placed("a", 0, 0, 100, 60, 1)
    assert lineup.relation(a, lineup.Placed("b", 105, 10, 200, 70, 1)) == ("side", "right")
    assert lineup.relation(a, lineup.Placed("b", 10, -50, 90, -2, 1)) == ("side", "top")
    assert lineup.relation(a, lineup.Placed("b", 300, 0, 400, 60, 1)) is None
    assert lineup.relation(a, lineup.Placed("b", 50, 30, 120, 80, 1)) == ("overlap",)


# -- with the pictures (OpenCV) --------------------------------------------------

cv2 = pytest.importorskip("cv2")


def test_marker_bits_match_opencv():
    img = cv2.aruco.generateImageMarker(cv2.aruco.getPredefinedDictionary(cv2.aruco.DICT_4X4_250), 17, 6)
    bits = lineup.marker_bits(17)
    for r in range(4):
        for c in range(4):
            assert bool(bits >> (15 - (r * 4 + c)) & 1) == (img[1 + r, 1 + c] > 127)


def test_plan_gives_each_screen_its_own_markers():
    planned = lineup.plan([{"key": "a/main", "width": 1920, "height": 1080},
                           {"key": "b/main", "width": 1080, "height": 1920}])
    assert planned["a/main"]["slot"] == 0 and planned["b/main"]["slot"] == 1
    assert len(planned["a/main"]["markers"]) == lineup.PER_SCREEN
    for p in planned.values():
        for _bits, x, y, w, h in p["markers"]:
            assert 0 <= x and x + w <= 1 and 0 <= y and y + h <= 1
    # Square on the screen itself.
    _b, _x, _y, w, h = planned["a/main"]["markers"][0]
    assert w * 1920 == pytest.approx(h * 1080, rel=1e-3)


def test_live_scan_lines_up_an_angled_desk():
    """The whole of it: frames from a phone moved in a figure of 8 (lens
    distortion, reflections on the screens, some frames smeared), to the layout."""
    layout = DESK3D.layout()
    planned = lineup.plan([{"key": s["key"], "width": s["width"], "height": s["height"]} for s in layout])
    live = lineup.Live(layout, planned, "host/main")
    assert live.hint().startswith("Hold the phone up")
    hints = set()
    for frame in scan(DESK3D, planned, n=24):
        live.frame(frame)
        result = live.update()
        hints.add(live.hint({"host/main": 1, "pc/main": 2, "mac/main": 3}))
        assert 0 <= live.progress() <= 1
        if live.aligned:
            break
    assert live.aligned and live.hint() == "Lined up" and live.progress() == 1
    assert "Move the phone slowly in a figure of 8" in hints
    assert set(live.states().values()) == {"locked"}
    assert result.views >= lineup.MIN_VIEWS
    # The same layout as the desk itself gives, seen from where the scan began.
    first = figure_of_eight(24, (-150, 60, -1050), aim=(-200, 80, 0))[0][0]
    true = lineup.arrange(layout, true_solution(DESK3D, "host/main", first))
    for key, (x, y) in true.positions.items():
        assert result.positions[key] == pytest.approx((x, y), abs=6), key
    for mine, theirs in zip(sorted(result.links, key=lambda li: li["b"]), sorted(true.links, key=lambda li: li["b"])):
        assert (mine["a"], mine["a_side"], mine["b"], mine["b_side"]) == \
            (theirs["a"], theirs["a_side"], theirs["b"], theirs["b_side"])
        assert mine["a_span"] == pytest.approx(theirs["a_span"], abs=6)
        assert mine["b_span"] == pytest.approx(theirs["b_span"], abs=6)
    assert result.stance["pc/main"]["yaw"] == pytest.approx(28, abs=0.5)
    assert result.stance["mac/main"]["pitch"] == pytest.approx(18, abs=0.5)
    # The lens was worked out along the way.
    assert live.scene.intr[0] == pytest.approx(1180, rel=0.015)


def test_live_scan_says_which_screen_is_missing():
    layout = DESK3D.layout()
    planned = lineup.plan([{"key": s["key"], "width": s["width"], "height": s["height"]} for s in layout])
    live = lineup.Live(layout, planned, "host/main")
    numbers = {"host/main": 1, "pc/main": 2, "mac/main": 3}
    cam = Camera()
    # Close in on the main monitor and the laptop: the PC's is out of the picture.
    from lineup_desk import render

    live.frame(render(DESK3D, planned, cam, cam.look((300, 100, -700), (200, 100, 0))))
    live.update()
    assert live.states() == {"host/main": "placing", "pc/main": "unseen", "mac/main": "placing"}
    assert live.hint(numbers) == "Get screen 2 in the picture"
    assert not live.aligned and live.usable


def test_photo_of_the_desk_lines_up_the_layout():
    planned = plan_for(DESK)
    data, _cam, _pose = photo(DESK, planned)
    result = lineup.line_up(data, screens(), planned, reference="host/main")
    assert set(result.found) == {k for k, *_ in DESK}
    assert result.missing == [] and result.views == 1
    pos = result.positions
    assert pos["host/main"] == (0, 0)  # the reference stays put
    # Beside each other, top edges level.
    assert pos["host/screen2"][0] == 1920
    assert abs(pos["host/screen2"][1]) < 25
    assert pos["pc/main"][0] == -1920
    assert pos["pc/main"][1] == pytest.approx(30 * 3.2, abs=25)
    # The laptop is in front of the main monitor.
    assert ("host/main", "mac/main") in result.fronts
    fit = next(f for f in result.fits if f["front"] == "mac/main")
    assert fit["behind"] == "host/main"
    assert fit["x"] == pytest.approx(250 * 3.2, abs=30)
    assert fit["y"] == pytest.approx(220 * 3.2, abs=30)
    assert fit["scale"] == pytest.approx(3.2 / 4.8, rel=0.06)
    # The PC's edge meets the main monitor's left edge over the PC's whole height.
    link = next(li for li in result.links if {li["a"], li["b"]} == {"host/main", "pc/main"})
    main_end = (link["a_span"], link["a_side"]) if link["a"] == "host/main" else (link["b_span"], link["b_side"])
    pc_end = (link["b_span"], link["b_side"]) if link["a"] == "host/main" else (link["a_span"], link["a_side"])
    assert main_end[1] == "left" and pc_end[1] == "right"
    assert main_end[0][0] == pytest.approx(96, abs=25) and main_end[0][1] == pytest.approx(1032, abs=25)
    assert pc_end[0][0] == pytest.approx(0, abs=20) and pc_end[0][1] == pytest.approx(1080, abs=20)
    # The host's own two screens: its OS moves the pointer between them.
    assert not any({li["a"], li["b"]} == {"host/main", "host/screen2"} for li in result.links)


def test_photo_with_monitor_sizes_gets_an_angled_desk_right():
    layout = DESK3D.layout()
    for s in layout:
        s["width_mm"], s["height_mm"] = DESK3D.sizes()[s["key"]]
    planned = lineup.plan([{"key": s["key"], "width": s["width"], "height": s["height"]} for s in layout])
    data, cam, pose = photo(DESK3D, planned, size=(2400, 1800), offset=(60, 40))
    result = lineup.line_up(data, layout, planned, reference="host/main")
    eye = -pose[0].T @ pose[1]
    true = lineup.arrange(layout, true_solution(DESK3D, "host/main", eye))
    for key, (x, y) in true.positions.items():
        assert result.positions[key] == pytest.approx((x, y), abs=12), key
    assert result.stance["pc/main"]["yaw"] == pytest.approx(28, abs=3)


def test_screens_out_of_the_photo_move_with_their_computer():
    desk = DESK[:2] + DESK[2:3]
    planned = plan_for(desk)
    # The host's second screen is in the layout but wasn't showing patterns (it's off).
    del planned["host/screen2"]
    data, _cam, _pose = photo(desk, planned)
    before = screens(desk)
    result = lineup.line_up(data, before, planned, reference="host/main")
    assert result.missing == ["host/screen2"]
    old_main, old_s2 = before[0], before[1]
    dx = result.positions["host/main"][0] - old_main["x"]
    assert result.positions["host/screen2"][0] == old_s2["x"] + dx


def test_no_patterns_says_so():
    blank = cv2.imencode(".png", np.full((400, 600), 128, np.uint8))[1].tobytes()
    with pytest.raises(lineup.LineupError, match="couldn't find"):
        lineup.line_up(blank, screens(), lineup.plan([{"key": "host/main", "width": 1920, "height": 1080}]))
    with pytest.raises(lineup.LineupError, match="isn't a photo"):
        lineup.line_up(b"not a picture", screens(), {})


def test_a_reflection_over_some_patterns_still_reads_the_screen():
    from lineup_desk import render

    desk = flat_desk(DESK[:1])
    planned = plan_for(DESK[:1])
    cam = Camera()
    pose = cam.look((40, 200, -700), (300, 170, 0))
    # A lamp reflected across the top right of the screen.
    frame = render(desk, planned, cam, pose, glare=[("host/main", 0.9, 0.1, 0.16)])
    seen = lineup.read_frame(frame, planned, {"host/main": (1920, 1080)}, 4.0, thorough=True)
    assert len(seen) == 1 and seen[0].markers >= 6 and seen[0].rms < 1.0
    # A window reflected over the whole right half: the left half's patterns are enough.
    frame = render(desk, planned, cam, pose, glare=[("host/main", 0.85, 0.5, 0.3), ("host/main", 0.6, 0.2, 0.2)])
    seen = lineup.read_frame(frame, planned, {"host/main": (1920, 1080)}, 4.0, thorough=True)
    assert len(seen) == 1 and 2 <= seen[0].markers < 8 and seen[0].rms < 1.0


# -- the host service's end ------------------------------------------------------

class FakeConn:
    def __init__(self, name, features, screen_mm=()):
        self.name, self.features, self.screen_mm = name, features, list(screen_mm)


class FakeServer:
    def __init__(self):
        mm = [{"x": 0, "y": 0, "width": 1512, "height": 982, "width_mm": 304.0, "height_mm": 197.0}]
        self.connections = {"phone": FakeConn("Kyle's phone", ["edge", "lineup_camera"]),
                            "mac": FakeConn("Mac", ["identify", "lineup"], mm)}
        self.sent = []

    def supports(self, device_id, feature):
        return feature in self.connections[device_id].features

    def send(self, device_id, t, **data):
        self.sent.append((device_id, t, data))


def test_inbox_asks_phones_and_gathers_the_photo(tmp_path):
    import base64
    import json
    import os

    events = []
    server = FakeServer()
    inbox = lineup.PhotoInbox(server, lambda e, **d: events.append((e, d)), folder=str(tmp_path))
    inbox.markers(json.dumps({"devices": {"mac": {"screens": [{"screen": "main", "markers": []}]}}}))
    assert server.sent == [("mac", "lineup", {"screens": [{"screen": "main", "markers": []}], "seconds": 300})]
    # The Mac's monitor's real size goes to the app, for the sums.
    assert events[-1] == ("lineup_sizes", {"devices": {"mac": server.connections["mac"].screen_mm}})
    inbox.camera(json.dumps({"id": "L1"}))
    assert server.sent[-1] == ("phone", "lineup_camera", {"id": "L1"})
    assert events[-1] == ("lineup_camera", {"id": "L1", "devices": [{"id": "phone", "name": "Kyle's phone"}]})
    photo = os.urandom(100_000)
    inbox.handle("phone", "lineup_photo", {"id": "L1", "size": len(photo)})
    for i in range(0, len(photo), 30_000):
        chunk = photo[i:i + 30_000]
        inbox.handle("phone", "lineup_data", {"id": "L1", "data": base64.b64encode(chunk).decode(),
                                              "end": i + 30_000 >= len(photo)})
    event, data = events[-1]
    assert event == "lineup_photo" and data["id"] == "L1" and data["device"] == "phone" and "seq" not in data
    with open(data["path"], "rb") as f:
        assert f.read() == photo
    inbox.handle("phone", "lineup_photo", {"id": "L2", "cancel": True})
    assert events[-1] == ("lineup_photo", {"device": "phone", "id": "L2", "cancelled": True})
    inbox.handle("phone", "lineup_request", {})
    assert events[-1] == ("lineup_request", {"device": "phone"})
    inbox.markers(json.dumps({"hide": True}))
    assert server.sent[-1] == ("mac", "lineup", {"hide": True})


def test_inbox_live_frames_and_how_its_going(tmp_path):
    import base64
    import json

    events = []
    server = FakeServer()
    inbox = lineup.PhotoInbox(server, lambda e, **d: events.append((e, d)), folder=str(tmp_path))
    inbox.camera(json.dumps({"id": "L1", "live": True}))
    assert server.sent[-1] == ("phone", "lineup_camera", {"id": "L1", "live": True})
    for seq in (0, 1):
        inbox.handle("phone", "lineup_photo", {"id": "L1", "size": 3, "seq": seq, "focal": 1180.5})
        inbox.handle("phone", "lineup_data", {"id": "L1", "data": base64.b64encode(b"abc").decode(), "end": True})
        event, data = events[-1]
        assert event == "lineup_photo" and data["seq"] == seq and data["focal"] == 1180.5
    # The app's answer goes to that phone: the next frame, and what to show.
    inbox.live(json.dumps({"device": "phone", "id": "L1", "seq": 1, "progress": 0.42, "hint": "Move the phone",
                           "screens": [[1, "locked"], [2, "placing"]]}))
    assert server.sent[-1] == ("phone", "lineup_live", {"id": "L1", "seq": 1, "progress": 0.42,
                                                        "hint": "Move the phone",
                                                        "screens": [[1, "locked"], [2, "placing"]]})
    inbox.live(json.dumps({"device": "mac", "id": "L1", "progress": 1}))  # not a phone: nothing
    assert server.sent[-1][0] == "phone"
    inbox.live(json.dumps({"device": "phone", "id": "L1", "progress": 1, "hint": "Lined up", "done": True}))
    assert server.sent[-1][2]["done"] is True
    inbox.handle("phone", "lineup_photo", {"id": "L1", "done": True})
    assert events[-1] == ("lineup_photo", {"device": "phone", "id": "L1", "done": True})
