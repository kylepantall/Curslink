"""Auto line-up (curslink.lineup): a desk drawn in perspective, with every
screen showing its patterns, read back into a layout."""

import pytest

from curslink import lineup

cv2 = pytest.importorskip("cv2")
np = pytest.importorskip("numpy")

# The desk, in mm: (key, device, layout px w, h, real x, y, w, h). Listed back to front.
DESK = [
    ("host/main", "host", 1920, 1080, 0, 0, 600, 340),
    ("host/screen2", "host", 2560, 1440, 620, 0, 600, 340),
    ("pc/main", "pc", 1920, 1080, -540, 30, 520, 290),
    # A laptop standing in front of the main monitor's bottom middle.
    ("mac/main", "mac", 1440, 900, 250, 220, 300, 190),
]


def screens(desk=DESK):
    # Where they are before the line-up: anywhere (a row, far apart).
    return [{"key": k, "device": d, "width": w, "height": h, "x": i * 5000, "y": 0}
            for i, (k, d, w, h, *_rest) in enumerate(desk)]


def draw_screen(w: int, h: int, markers: list) -> "np.ndarray":
    """A screen showing its patterns, as curslink.gui.lineup draws them."""
    img = np.full((h, w), 45, np.uint8)  # whatever's on the screen behind the patterns
    for bits, fx, fy, fw, fh in markers:
        x, y, side = fx * w, fy * h, fw * w
        cell = side / lineup.CELLS
        cv2.rectangle(img, (round(x - cell), round(y - cell)), (round(x + side + cell), round(y + side + cell)), 255, -1)
        cv2.rectangle(img, (round(x), round(y)), (round(x + side), round(y + side)), 0, -1)
        for r in range(4):
            for c in range(4):
                if bits >> (15 - (r * 4 + c)) & 1:
                    x0, y0 = x + (c + 1) * cell, y + (r + 1) * cell
                    cv2.rectangle(img, (round(x0), round(y0)), (round(x0 + cell) - 1, round(y0 + cell) - 1), 255, -1)
    return img


def photo(desk=DESK, slant=True, planned=None) -> tuple[bytes, dict]:
    """A photo of ``desk`` (as DESK), every screen showing its patterns."""
    if planned is None:
        planned = lineup.plan([{"key": k, "width": w, "height": h} for k, _d, w, h, *_r in desk])
    xs = [v for _k, _d, _w, _h, x, _y, rw, _rh in desk for v in (x, x + rw)]
    ys = [v for _k, _d, _w, _h, _x, y, _rw, rh in desk for v in (y, y + rh)]
    x0, x1, y0, y1 = min(xs) - 40, max(xs) + 40, min(ys) - 40, max(ys) + 40
    W, H = 3600, 1400
    desk_quad = np.float32([[x0, y0], [x1, y0], [x1, y1], [x0, y1]])
    # A phone held a little to one side and below: the desk in perspective.
    shot = np.float32([[150, 120], [3450, 40], [3380, 1360], [230, 1260]]) if slant else \
        np.float32([[0, 0], [W, 0], [W, H], [0, H]])
    g = cv2.getPerspectiveTransform(desk_quad, shot)
    out = np.full((H, W), 90, np.uint8)
    for key, _d, w, h, x, y, rw, rh in desk:
        scale = 900 / w  # drawn smaller than the real pixels; it's a photo anyway
        iw, ih = round(w * scale), round(h * scale)
        img = draw_screen(iw, ih, planned[key]["markers"])
        to_desk = np.array([[rw / iw, 0, x], [0, rh / ih, y], [0, 0, 1]], np.float64)
        m = g @ to_desk
        warped = cv2.warpPerspective(img, m, (W, H))
        mask = cv2.warpPerspective(np.full((ih, iw), 255, np.uint8), m, (W, H))
        out[mask > 0] = warped[mask > 0]
    out = cv2.GaussianBlur(out, (3, 3), 0)
    ok, jpg = cv2.imencode(".jpg", out, [cv2.IMWRITE_JPEG_QUALITY, 88])
    assert ok
    return jpg.tobytes(), planned


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


def test_photo_of_the_desk_lines_up_the_layout():
    data, planned = photo()
    result = lineup.line_up(data, screens(), planned, reference="host/main")
    assert set(result.found) == {k for k, *_ in DESK}
    assert result.missing == []
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
    assert main_end[0][0] == pytest.approx(96, abs=25) and main_end[0][1] == pytest.approx(1024, abs=25)
    assert pc_end[0][0] == pytest.approx(0, abs=20) and pc_end[0][1] == pytest.approx(1080, abs=20)
    # The host's own two screens: its OS moves the pointer between them.
    assert not any({li["a"], li["b"]} == {"host/main", "host/screen2"} for li in result.links)


def test_screens_out_of_the_photo_move_with_their_computer():
    desk = DESK[:2] + DESK[2:3]
    data, planned = photo(desk)
    # The host's second screen is in the layout but wasn't showing patterns (it's off).
    del planned["host/screen2"]
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


def test_relation_sides_and_gaps():
    a = lineup.Placed("a", 0, 0, 100, 60, 1, [])
    assert lineup.relation(a, lineup.Placed("b", 105, 10, 200, 70, 1, [])) == ("side", "right")
    assert lineup.relation(a, lineup.Placed("b", 10, -50, 90, -2, 1, [])) == ("side", "top")
    assert lineup.relation(a, lineup.Placed("b", 300, 0, 400, 60, 1, [])) is None
    assert lineup.relation(a, lineup.Placed("b", 50, 30, 120, 80, 1, [])) == ("overlap",)


class FakeConn:
    def __init__(self, name, features):
        self.name, self.features = name, features


class FakeServer:
    def __init__(self):
        self.connections = {"phone": FakeConn("Kyle's phone", ["edge", "lineup_camera"]),
                            "mac": FakeConn("Mac", ["identify", "lineup"])}
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
    assert event == "lineup_photo" and data["id"] == "L1" and data["device"] == "phone"
    with open(data["path"], "rb") as f:
        assert f.read() == photo
    inbox.handle("phone", "lineup_photo", {"id": "L2", "cancel": True})
    assert events[-1] == ("lineup_photo", {"device": "phone", "id": "L2", "cancelled": True})
    inbox.handle("phone", "lineup_request", {})
    assert events[-1] == ("lineup_request", {"device": "phone"})
    inbox.markers(json.dumps({"hide": True}))
    assert server.sent[-1] == ("mac", "lineup", {"hide": True})
