"""Where you sit (curslink.seat): the eye point a desk's overlaps are aimed at."""

import math

import pytest

np = pytest.importorskip("numpy")

from lineup_desk import Desk, true_solution  # noqa: E402

from curslink import lineup, seat  # noqa: E402

NAMES = {"host/main": "Main monitor", "mac/main": "the MacBook"}


def _desk(bend=0.0, scan_eye=(20, -40, -600), laptop=(60, 120, -250)):
    """A wide monitor with a laptop in front of it, lined up from ``scan_eye`` (mm)."""
    desk = Desk([
        ("host/main", "host", 3440, 1440, 800, (0, 0, 0), 0, 0, bend),
        ("mac/main", "mac", 1512, 982, 304, laptop, 10, 15),
    ])
    layout = desk.layout()
    for s in layout:
        s["width_mm"], s["height_mm"] = desk.sizes()[s["key"]]
    solution = true_solution(desk, "host/main", scan_eye)
    return desk, layout, solution, lineup.arrange(layout, solution)


def _world(desk, mm):
    """A point of the test desk (mm) in the solved desk's world."""
    ref = desk.screens["host/main"]
    return ref["r"].T @ (np.asarray(mm, float) - ref["t"]) / ref["mm"]


def test_a_line_up_keeps_the_desk_it_solved():
    desk, _layout, solution, result = _desk()
    d = result.desk
    assert seat.valid(d) and d["how"] == "scan" and d["reference"] == "host/main"
    assert d["mm"] == pytest.approx(800 / 3440)
    assert d["eye"] == pytest.approx(list(solution.eye)) and d["scan_eye"] == d["eye"]
    assert set(d["screens"]) == {"host/main", "mac/main"}
    # plain Python agrees with the solver's own geometry
    for key, sp in solution.screens.items():
        s = d["screens"][key]
        for u, v in ((0, 0), (sp.width / 3, sp.height), (sp.width, sp.height / 2)):
            assert seat.at(s, u, v) == pytest.approx(list(sp.at([(u, v)])[0]), abs=1e-6)
        p = [100.0, -50.0, -900.0]
        assert seat.world(s, seat.local(s, p)) == pytest.approx(p, abs=1e-6)
    # the eye is 600 mm before the monitor, 127 mm below its top edge (which is 167 mm above its middle)
    assert seat.distance(d, "host/main") * d["mm"] == pytest.approx(600, abs=0.5)
    assert seat.eye_above(d, "host/main") * d["mm"] == pytest.approx(-(800 * 1440 / 3440 / 2 - 40), abs=0.5)


def test_a_curved_screen_is_measured_to_its_glass():
    desk, _layout, solution, result = _desk(bend=0.45)
    d = result.desk
    s = d["screens"]["host/main"]
    sp = solution.screens["host/main"]
    for u in (0.0, 400.0, 1720.0, 3440.0):
        assert seat.at(s, u, 300.0) == pytest.approx(list(sp.at([(u, 300.0)])[0]), abs=1e-6)
    assert seat.chord(s) == pytest.approx(sp.chord)
    # the middle of the glass is behind the plane of its corners, so further off than that plane
    flat = seat.local(s, d["eye"])[2] * s["scale"] * d["mm"]
    assert seat.distance(d, "host/main") * d["mm"] > flat + 20


def test_saved_and_loaded(tmp_path):
    _desk_, _layout, _solution, result = _desk()
    path = tmp_path / "pairing" / "seat.json"
    assert seat.load(path) is None
    assert seat.save(result.desk, path)
    assert seat.load(path) == result.desk
    path.write_text("{not json")
    assert seat.load(path) is None
    path.write_text('{"screens": {}, "eye": [0, 0, 0], "reference": "x"}')
    assert seat.load(path) is None  # (its reference isn't one of its screens)


def test_one_photo_keeps_no_desk():
    cv2 = pytest.importorskip("cv2")
    from lineup_desk import DESK, photo

    layout = DESK.layout()
    planned = lineup.plan([{"key": s["key"], "width": s["width"], "height": s["height"]} for s in layout])
    jpg, _cam, _pose = photo(DESK, planned)
    assert cv2 is not None and lineup.line_up(jpg, layout, planned).desk is None


@pytest.mark.parametrize("bend", [0.0, 0.45])
def test_overlaps_are_aimed_again_from_where_you_sit(bend):
    """Sitting 35 cm further back than the phone was held, the laptop is seen
    over a part of the monitor centimetres off: the fit follows, to a few
    pixels of what the desk says is seen from there, a curved monitor too."""
    from curslink.lineup_scene import project_from

    desk, _layout, solution, result = _desk(bend)
    real = (-30.0, -60.0, -950.0)
    eye = _world(desk, real)
    fit = result.fits[0]
    new_desk, (aimed,) = seat.seated(result.desk, result.fits, eye, "phone")
    truth = lineup._Desk(true_solution(desk, "host/main", real)).lean("host/main", "mac/main")
    was = np.abs(np.array(fit["quad"]) - np.array(truth["quad"])).max()
    now = np.abs(np.array(aimed["quad"]) - np.array(truth["quad"])).max()
    assert was > 300 and now < 2
    assert aimed["eye"] == pytest.approx(truth["eye"], abs=1) and aimed["depth"] == pytest.approx(truth["depth"], abs=1)
    # the plain rectangle went with it
    assert aimed["x"] + 1512 * aimed["scale"] / 2 == pytest.approx(sum(aimed["quad"][0::2]) / 4, abs=40)
    assert aimed["scale"] < fit["scale"]  # (from further back the laptop covers less of the monitor)
    assert new_desk["how"] == "phone" and new_desk["eye"] == pytest.approx(list(eye))
    assert new_desk["scan_eye"] == result.desk["scan_eye"] and 100 < new_desk["moved_mm"] < 200
    assert result.fits[0] is fit and "quad" in fit  # (the fits given are left alone)
    # what the pattern step (or a hand) put right in a fit stays put right
    nudged = {**fit, "quad": [v + (7.0 if i % 2 == 0 else -4.0) for i, v in enumerate(fit["quad"])],
              "x": fit["x"] + 7.0, "y": fit["y"] - 4.0}
    _d, (again,) = seat.seated(result.desk, [nudged], eye, "phone")
    assert np.array(again["quad"]) - np.array(aimed["quad"]) == pytest.approx([7.0, -4.0] * 4, abs=1e-6)
    # and back again
    _d, (back,) = seat.seated(new_desk, [aimed], result.desk["scan_eye"], "scan")
    assert back["quad"] == pytest.approx(fit["quad"], abs=0.5) and back["scale"] == pytest.approx(fit["scale"], rel=1e-3)
    if not bend:
        assert project_from(eye, solution.screens["host/main"], solution.screens["mac/main"].corners()).ravel() == \
            pytest.approx(aimed["quad"], abs=2)


def test_fits_that_cant_be_aimed_are_left():
    _desk_, _layout, _solution, result = _desk()
    plain = {k: v for k, v in result.fits[0].items() if k not in ("quad", "eye", "depth")}
    other = {**result.fits[0], "front": "gone/main"}
    out, most = seat.reaim(result.desk, [plain, other], [0.0, 0.0, -4000.0])
    assert out[0] is plain and out[1] is other and most == 0.0


def test_without_numpy_a_fit_is_aimed_by_its_own_corners(monkeypatch):
    desk, _layout, _solution, result = _desk()

    def no_numpy(*_a, **_k):
        raise ImportError("no numpy here")

    monkeypatch.setattr(seat, "_leans", no_numpy)
    eye = _world(desk, (-30.0, -60.0, -950.0))
    precise = None
    with monkeypatch.context() as m:
        m.undo()
        precise = seat.reaim(result.desk, result.fits, eye)[0][0]
    (plain,), most = seat.reaim(result.desk, result.fits, eye)
    assert most > 0 and plain["quad"] == pytest.approx(precise["quad"], abs=2)  # (a flat monitor: the same)
    assert plain["eye"] == pytest.approx(precise["eye"], abs=1)


class _Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


def _pose(at, towards=(0.0, 0.0, 1.0)):
    """World -> camera for a camera at ``at`` looking along ``towards``."""
    z = np.asarray(towards, float) / np.linalg.norm(towards)
    x = np.cross([0.0, 1.0, 0.0], z)
    x /= np.linalg.norm(x)
    r = np.array([x, np.cross(z, x), z])
    return r, -r @ np.asarray(at, float)


def test_the_phone_is_found_once_its_held_still_somewhere_new():
    desk, _layout, solution, result = _desk()
    mm = seat.mm_of(result.desk)
    clock = _Clock()
    phone = seat.PhoneSeat(solution, mm, clock=clock)
    start, lens = _world(desk, (20, -40, -600)), _world(desk, (-30, -60, -930))
    rng = np.random.default_rng(3)
    path = [start] * 4 + [start + (lens - start) * k / 5 for k in range(1, 5)] + [lens] * 8
    found = None
    for n, at in enumerate(path):
        clock.t = 0.3 * n
        if phone.sight(_pose(at + rng.normal(0, 2 / mm, 3))):
            found = n
            break
        assert phone.sight(None) is False  # (a frame it couldn't be placed in changes nothing)
    # not while it was still where the pattern step left it, nor on the way: once it had been still a second
    assert found is not None and 10 <= found <= 13 and phone.moved and phone.done
    eye = desk.screens["host/main"]["r"] @ (phone.eye * desk.screens["host/main"]["mm"]) + desk.screens["host/main"]["t"]
    # the eyes are a couple of centimetres behind the lens
    assert eye == pytest.approx([-30, -60, -930 - seat.LENS_TO_EYE_MM], abs=4)
    assert phone.sight(_pose(start)) is True and phone.eye is not None  # (found: nothing more changes it)


def test_a_phone_already_at_the_eyes_is_taken_after_a_wait_and_one_never_seen_is_given_up_on():
    desk, _layout, solution, result = _desk()
    clock = _Clock()
    phone = seat.PhoneSeat(solution, seat.mm_of(result.desk), clock=clock)
    at = _world(desk, (20, -40, -600))
    took = None
    for n in range(40):
        clock.t = 0.3 * n
        if phone.sight(_pose(at)):
            took = clock.t
            break
    assert took is not None and seat.WAIT_S <= took < seat.WAIT_S + 1 and not phone.moved
    clock = _Clock()
    lost = seat.PhoneSeat(solution, None, clock=clock)  # (no screen says how big it is: a guess at the scale)
    assert 0.2 < lost.mm < 0.3
    for n in range(200):
        clock.t = 0.3 * n
        assert not lost.sight(None)
    assert lost.gave_up and lost.done and lost.eye is None
    # moving about the whole time isn't a seat either
    clock = _Clock()
    restless = seat.PhoneSeat(solution, seat.mm_of(result.desk), clock=clock)
    for n in range(60):
        clock.t = 0.3 * n
        assert not restless.sight(_pose(at + np.array([400.0 * math.sin(n), 0.0, 0.0])))


def test_the_phone_finds_itself_by_the_patterns_still_up():
    """The real thing, as frames: the camera sits back from where the scan
    was held, and the patterns the check leaves up place it to millimetres."""
    pytest.importorskip("cv2")
    from lineup_desk import Camera, render

    from curslink.lineup_check import Check

    desk, layout, solution, result = _desk(bend=0.45)
    planned = lineup.plan([{"key": s["key"], "width": s["width"], "height": s["height"]} for s in layout])
    showing = lineup.check_plan(planned)
    cam = Camera(focal=1180, k1=0.04, k2=-0.02, centre=(812, 588))
    locate = Check(solution, result, showing, [1180, 812, 588, 0.04, -0.02], cam.size).locate
    dims = {s["key"]: (s["width"], s["height"]) for s in layout}
    clock = _Clock()
    phone = seat.PhoneSeat(solution, seat.mm_of(result.desk), clock=clock)
    real = np.array((-30.0, -60.0, -950.0))
    lens, scan = real + (0, 0, seat.LENS_TO_EYE_MM), np.array((20.0, -40.0, -600.0))
    rng = np.random.default_rng(1)
    path = [scan] * 3 + [scan + (lens - scan) * k / 4 for k in range(1, 4)] + [lens] * 12
    for n, at in enumerate(path):
        clock.t = 0.3 * n
        frame = render(desk, showing, cam, cam.look(at + rng.normal(0, 3, 3), (0, 60, 0), 2.0), seed=n)
        sightings = lineup.read_frame(frame, showing, dims, 6.4, thorough=False, curved=0.03)
        if phone.sight(locate(sightings) if sightings else None):
            break
    assert phone.eye is not None and n < 14
    ref = desk.screens["host/main"]
    assert ref["r"] @ (phone.eye * ref["mm"]) + ref["t"] == pytest.approx(real, abs=10)
    new_desk, (aimed,) = seat.seated(result.desk, result.fits, phone.eye, "phone")
    truth = lineup._Desk(true_solution(desk, "host/main", real)).lean("host/main", "mac/main")
    assert np.abs(np.array(aimed["quad"]) - np.array(truth["quad"])).max() < 12
    assert seat.distance(new_desk, "host/main") * new_desk["mm"] == pytest.approx(950 + 45, abs=15)  # (to the glass)


def test_what_the_card_says():
    desk, _layout, _solution, result = _desk()
    scan = seat.summary(result.desk, result.fits, NAMES)
    assert scan["main"] == "host/main" and scan["distance"] == ("60 cm", "Your eyes to Main monitor")
    assert scan["eye"] == ("13 cm below the top edge", "Against the top edge of Main monitor")
    assert scan["moved"] is None
    new_desk, fits = seat.seated(result.desk, result.fits, _world(desk, (-30.0, -60.0, -950.0)), "phone")
    info = seat.summary(new_desk, fits, NAMES)
    assert info["distance"] == ("95 cm", "Your eyes to Main monitor: 35 cm further back than where the phone was "
                                         "held for the scan")
    assert info["eye"][0] == "11 cm below the top edge"
    assert info["hidden"][0] == "About a fifth" and "the MacBook hides from here: the bottom 12 cm" in info["hidden"][1]
    assert info["moved"][0] == "15 cm"
    # the monitor's top edge is well above the eyes: lower it
    assert len(info["tips"]) == 1 and "11 cm above your eyes" in info["tips"][0] and "Lowering it" in info["tips"][0]
    # no size known: nothing in centimetres to say
    assert seat.summary({**new_desk, "mm": None}, fits, NAMES) is None
    assert seat.summary(None, fits, NAMES) is None


def test_suggestions():
    desk, _layout, _solution, result = _desk()

    def tips(eye_mm, d=result.desk, fits=result.fits):
        new, aimed = seat.seated(d, fits, _world(desk, eye_mm), "phone")
        return seat.summary(new, aimed, NAMES)["tips"]

    # eyes a little above the top edge (167 mm above the monitor's middle), a metre back: nothing to change
    assert tips((0.0, -200.0, -1000.0)) == []
    # looking down on it from well above
    high, = tips((0.0, -400.0, -1000.0))
    assert "You look down on Main monitor" in high and "23 cm below your eyes" in high and "Raising it" in high
    # close up, an 80 cm monitor reaches further to each side than is easy to take in
    near = tips((0.0, -200.0, -450.0))
    wide = next(t for t in near if "further than is easy" in t)
    assert "Sitting about" in wide and "cm further back" in wide
    back = int(wide.split("Sitting about ")[1].split(" cm")[0])
    left, right = seat.spread({**result.desk, "eye": list(_world(desk, (0.0, -200.0, -450.0 - 10 * back)))}, "host/main")
    assert max(left, right) <= seat.SPREAD_DEG < max(seat.spread(
        {**result.desk, "eye": list(_world(desk, (0.0, -200.0, -450.0 - 10 * back + 20)))}, "host/main"))
    # a laptop standing high before the monitor hides a lot of it
    desk2, _l, _s, tall = _desk(laptop=(60, 60, -250))
    new, aimed = seat.seated(tall.desk, tall.fits, _world(desk2, (0.0, -200.0, -1000.0)), "phone")
    info = seat.summary(new, aimed, NAMES)
    hides = next(t for t in info["tips"] if "hides" in t)
    assert hides.startswith("The MacBook hides") and "Raising Main monitor by" in hides
    # off the centre line of a curved monitor
    desk3, _l, _s, curved = _desk(bend=0.45)
    new, aimed = seat.seated(curved.desk, curved.fits, _world(desk3, (150.0, -200.0, -1000.0)), "phone")
    off = next(t for t in seat.summary(new, aimed, NAMES)["tips"] if "centre line" in t)
    assert "15 cm to the right of the middle of Main monitor" in off
    new, aimed = seat.seated(curved.desk, curved.fits, _world(desk3, (10.0, -200.0, -1000.0)), "phone")
    assert not any("centre line" in t for t in seat.summary(new, aimed, NAMES)["tips"])


def test_hidden_share():
    _desk_, _layout, _solution, result = _desk()
    d = result.desk
    w, h = 3440.0, 1440.0
    whole = {"behind": "host/main", "front": "mac/main", "quad": [0, 0, w, 0, w, h, 0, h]}
    assert seat.hidden(d, whole) == {"share": pytest.approx(1.0), "height": pytest.approx(h)}
    corner = {"behind": "host/main", "front": "mac/main", "quad": [w - 500, h - 300, w + 900, h - 300, w + 900, h + 700,
                                                                  w - 500, h + 700]}
    assert seat.hidden(d, corner) == {"share": pytest.approx(500 * 300 / (w * h)), "height": pytest.approx(300)}
    off = {"behind": "host/main", "front": "mac/main", "quad": [w + 10, 0, w + 500, 0, w + 500, 300, w + 10, 300]}
    assert seat.hidden(d, off) == {"share": 0.0, "height": 0.0}
    side = {"behind": "host/main", "front": "mac/main", "x": -100.0, "y": 200.0, "scale": 1.0}  # (no quad: its rectangle)
    assert seat.hidden(d, side)["share"] == pytest.approx((1512 - 100) * 982 / (w * h)) and seat.hidden(d, side)["height"] == 0


def _looks(desk_dict, camera, eye, turn=1.0, keys=("host/main", "mac/main")):
    """The head's (yaw, pitch) looking at the camera and at each screen's
    middle from ``eye``, the head making ``turn`` of each turn from the camera."""
    def angles(target):
        d = np.asarray(target, float) - np.asarray(eye, float)
        d /= np.linalg.norm(d)
        return d

    def as_yaw_pitch(d):
        return math.degrees(math.atan2(d[0], d[2])), math.degrees(math.asin(max(-1.0, min(1.0, d[1]))))

    ahead = angles(camera["at"])
    out = {}
    for k in keys:
        d = angles(seat.centre(desk_dict["screens"][k]))
        axis = np.cross(ahead, d)
        a = math.asin(min(1.0, np.linalg.norm(axis))) * turn
        axis /= max(1e-12, np.linalg.norm(axis))
        # Rodrigues: ``ahead`` turned ``a`` towards ``d``
        v = ahead * math.cos(a) + np.cross(axis, ahead) * math.sin(a) + axis * (axis @ ahead) * (1 - math.cos(a))
        out[k] = as_yaw_pitch(v)
    return out, as_yaw_pitch(ahead)


def test_the_computers_camera_and_where_you_look_find_the_seat():
    """No phone: where the head is before the laptop's camera (the size of
    the face says how far, a little wrong), and how far it turns from the
    camera to each screen."""
    desk, _layout, _solution, result = _desk()
    d = result.desk
    mm = d["mm"]
    camera = seat.assumed_camera(d, "mac/main")
    assert camera["how"] == "assumed" and camera["screen"] == "mac/main"
    # at the middle of the laptop's top edge, a touch above it, looking out of it
    top = seat.at(d["screens"]["mac/main"], 756, 0)
    assert math.dist(camera["at"], top) * mm == pytest.approx(8, abs=0.1)
    eye = _world(desk, (-30.0, -60.0, -950.0))
    head = [sum(camera["r"][a][i] * (eye[i] - camera["at"][i]) for i in range(3)) * mm for a in range(3)]
    assert head[2] > 500  # (in front of the camera)
    assert seat.from_camera(d, camera, head) == pytest.approx(list(eye), abs=1e-6)
    for turn in (1.0, 0.8):
        for wrong in (1.0, 0.9, 1.12):  # the face's size puts the head this much too near or far
            looks, at_camera = _looks(d, camera, eye, turn)
            found = seat.from_looks(d, camera, [h * wrong for h in head], looks, at_camera)
            assert found["pairs"] == 3 and found["by_face"] == pytest.approx(np.linalg.norm(head) * wrong, rel=1e-6)
            off = math.dist(found["eye"], eye) * mm
            # rough, as promised: within 10 cm, and no worse than the face alone would be
            assert off < 100 and off <= abs(wrong - 1) * np.linalg.norm(head) + 60
    # with nothing looked at, it's where the face says
    alone = seat.from_looks(d, camera, head, {})
    assert alone["pairs"] == 0 and alone["eye"] == pytest.approx(list(eye), abs=1e-6)
    assert seat.from_looks({**d, "mm": None}, camera, head, {}) is None
    assert seat.from_looks(d, camera, [0.0, 0.0, 50.0], {}) is None  # (a face 5 cm from the lens isn't one)


def test_a_camera_is_placed_by_an_eye_point_thats_known():
    desk, _layout, _solution, result = _desk()
    d, _fits = seat.seated(result.desk, result.fits, _world(desk, (-30.0, -60.0, -950.0)), "phone")
    # a camera perched on the monitor, 30 mm above its top edge
    real = seat.assumed_camera(d, "host/main")
    real["at"] = seat.world(d["screens"]["host/main"], (1720.0, -30.0 / d["mm"], 10.0 / d["mm"]))
    head = [sum(real["r"][a][i] * (d["eye"][i] - real["at"][i]) for i in range(3)) * d["mm"] for a in range(3)]
    found = seat.locate_camera(d, head, "host/main")
    assert found["how"] == "found" and found["at"] == pytest.approx(real["at"], abs=1e-6)
    assert seat.from_camera(d, found, head) == pytest.approx(d["eye"], abs=1e-6)
    assert seat.locate_camera(d, head, "nowhere/main")["screen"] == "host/main"  # (the main one, then)


def test_the_saved_layout_is_aimed_and_saved(tmp_path):
    from curslink.configedit import ConfigFile, new_host_config

    desk, _layout, _solution, result = _desk()
    path = tmp_path / "curslink.toml"
    path.write_text(new_host_config("host", "Host", [{"id": "main", "x": 0, "y": 0, "width": 3440, "height": 1440}]))
    cfg = ConfigFile(path)
    fit = result.fits[0]
    cfg.set_fit(fit["behind"], fit["front"], fit["x"], fit["y"], fit["scale"], quad=fit["quad"], eye=fit["eye"],
                depth=fit["depth"], mm=fit["mm"], bezel=[20, 30, 20, 0])
    cfg.set_fit("host/main", "other/main", 10.0, 20.0, 1.0)
    cfg.save()
    assert [f["front"] for f in seat.config_fits(cfg)] == ["mac/main", "other/main"]
    eye = _world(desk, (-30.0, -60.0, -950.0))
    new = seat.apply(cfg, result.desk, eye, "camera", tmp_path / "seat.json")
    assert new["how"] == "camera" and seat.load(tmp_path / "seat.json") == new
    saved = {t["front"]: t for t in ConfigFile(path).fits()}
    want = seat.seated(result.desk, result.fits, eye, "camera")[1][0]
    assert [float(v) for v in saved["mac/main"]["quad"]] == pytest.approx(want["quad"], abs=0.2)
    assert [float(v) for v in saved["mac/main"]["eye"]] == pytest.approx(want["eye"], abs=0.2)
    assert [float(v) for v in saved["mac/main"]["bezel"]] == [20, 30, 20, 0]  # (kept)
    assert float(saved["other/main"]["x"]) == 10.0 and "quad" not in saved["other/main"]
