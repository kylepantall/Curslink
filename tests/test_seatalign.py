"""Line up from your seat (curslink.seatalign): an overlap joined by eye, a corner at a time."""

import math

import pytest

from curslink import seatalign
from curslink.layout import Fit
from curslink.seatalign import Align

FRONT, BEHIND = (1512.0, 982.0), (3440.0, 1440.0)


def _laptop(x=400.0, y=1000.0, scale=0.9):
    """A laptop before the lower left of a wide monitor: only its top strip is seen against it."""
    return Align.from_fit(x, y, scale, None, FRONT, BEHIND)


def test_the_spots_are_the_ends_of_the_edge_the_screens_share():
    a = _laptop()
    assert len(a.spots) == 3 and a.spots[2] == (FRONT[0] / 2, 0.0)
    (u0, v0), (u1, v1) = a.spots[:2]
    # the top corners: the rest of the laptop is below the monitor
    assert (u0, v0) == (0.0, 0.0) and (u1, v1) == (FRONT[0], 0.0)
    assert a.seen(0) == pytest.approx((400.0, 1000.0))
    rings = a.rings()
    assert [r[3] for r in rings] == [1, 0, 0] and rings[0][2] >= seatalign.RING_MIN


def test_a_screen_hanging_off_a_corner_is_joined_where_it_crosses_the_rim():
    # the laptop's top right corner is off the monitor's right side
    a = Align.from_fit(2600.0, 900.0, 0.9, None, FRONT, BEHIND)
    assert len(a.spots) >= 2
    for i in range(len(a.spots)):
        x, y = a.seen(i)
        assert 0 < x < BEHIND[0] and 0 < y < BEHIND[1]


def test_screens_that_hardly_meet_have_one_spot_or_none():
    assert Align.from_fit(5000.0, 3000.0, 1.0, None, FRONT, BEHIND).spots == []
    a = Align.from_fit(3440.0 - 150, 1440.0 - 150, 1.0, None, FRONT, BEHIND)
    assert len(a.spots) <= 1
    a.nudge(3, 0)  # still moves as a whole
    assert not a.next()


def test_the_first_spot_moves_everything_and_the_second_turns_about_the_first():
    a = _laptop()
    a.nudge(5, -3)
    assert a.seen(0) == pytest.approx((405.0, 997.0)) and a.seen(1) == pytest.approx((405.0 + 1512 * 0.9, 997.0))
    assert a.next()
    was = a.seen(1)
    a.nudge(-4, 6)
    assert a.seen(0) == pytest.approx((405.0, 997.0))  # stays put
    assert a.seen(1) == pytest.approx((was[0] - 4, was[1] + 6))
    # smaller and turned, but still the laptop's shape
    q = a.quad
    top = math.hypot(q[2] - q[0], q[3] - q[1])
    side = math.hypot(q[6] - q[0], q[7] - q[1])
    assert top / side == pytest.approx(FRONT[0] / FRONT[1])
    cmd = a.command()
    assert len(cmd["quad"]) == 8 and cmd["scale"] == pytest.approx(top / FRONT[0], rel=1e-4)
    # the third, halfway: only along the line, the two ends staying put
    assert a.next() and a.step == 2
    ends, mid = (a.seen(0), a.seen(1)), a.seen(2)
    a.nudge(7, 0)
    assert a.seen(0) == pytest.approx(ends[0]) and a.seen(1) == pytest.approx(ends[1])
    ox, oy = ends[1][0] - ends[0][0], ends[1][1] - ends[0][1]
    moved = (a.seen(2)[0] - mid[0], a.seen(2)[1] - mid[1])
    assert moved[0] * oy - moved[1] * ox == pytest.approx(0, abs=1e-6)  # along the line
    assert math.hypot(*moved) == pytest.approx(7 * ox / math.hypot(ox, oy), rel=1e-6)
    assert not a.next() and a.back() and a.back() and a.step == 0


def test_a_mirror_dragged_by_hand_takes_the_corners_along():
    a = _laptop()
    x, y, k = seatalign.plain(a.quad, FRONT)
    assert not a.follow(x, y, k)
    assert a.follow(x + 10, y - 4, k)
    assert a.seen(0) == pytest.approx((410.0, 996.0))


# -- against a desk in 3D ---------------------------------------------------------------
np = pytest.importorskip("numpy")
pytest.importorskip("cv2")

from test_seat import _desk, _world  # noqa: E402

from curslink import seat  # noqa: E402


def _join_by_eye(d, fit, true_eye, bend=0.0, model=True):
    """Someone at ``true_eye`` nudging until both rings close: what they end with."""
    pair = ("host/main", "mac/main")
    truth = seat._leans(d, [pair], true_eye)[pair]
    front = (d["screens"]["mac/main"]["width"], d["screens"]["mac/main"]["height"])
    behind = (d["screens"]["host/main"]["width"], d["screens"]["host/main"]["height"])
    real = Fit(("host", "main"), ("mac", "main"), *seatalign.plain(truth["quad"], front), quad=tuple(truth["quad"]))
    a = Align.from_fit(fit["x"], fit["y"], fit["scale"], fit.get("quad"), front, behind)
    assert len(a.spots) == 3
    for step in range(3):
        want = real.to_behind(front, *a.spots[step])
        for _ in range(400):
            x, y = a.seen(step)
            dx, dy = round(want[0] - x), round(want[1] - y)
            if not dx and not dy:
                break
            a.nudge(dx, dy)
        if step == 1 and model:
            # the desk says how it's drawn out from there: the third spot should need next to nothing
            assert a.adopt(seat.joined_shape(d, *pair, a.joins(2)))
            x, y = a.seen(2)
            want = real.to_behind(front, *a.spots[2])
            assert math.hypot(want[0] - x, want[1] - y) < (4 if not bend else 12)
        a.next()
    return a, real, front


@pytest.mark.parametrize("bend,model", [(0.0, True), (0.45, True), (0.0, False)])
def test_joined_from_further_back_the_edge_is_right_and_the_eyes_are_found(bend, model):
    desk, _layout, _solution, result = _desk(bend=bend)
    d = result.desk
    fit = dict(result.fits[0]) if isinstance(result.fits[0], dict) else vars(result.fits[0])
    true_eye = list(_world(desk, (60, 10, -950)))  # sat back 35 cm, a little right and lower
    a, real, front = _join_by_eye(d, fit, true_eye, bend, model)
    # along the edge that's seen, the picture is where it's truly seen (it began centimetres off)
    began = Fit(("", ""), ("", ""), fit["x"], fit["y"], fit["scale"], quad=tuple(fit["quad"]))
    off_before = off_after = 0.0
    for t in (0.0, 0.25, 0.5, 0.75, 1.0):
        u = a.spots[0][0] + t * (a.spots[1][0] - a.spots[0][0])
        v = a.spots[0][1] + t * (a.spots[1][1] - a.spots[0][1])
        want = real.to_behind(front, u, v)
        got, was = a.fit.to_behind(front, u, v), began.to_behind(front, u, v)
        off_after = max(off_after, math.hypot(got[0] - want[0], got[1] - want[1]))
        off_before = max(off_before, math.hypot(was[0] - want[0], was[1] - want[1]))
    assert off_before > 60 and off_after < (3.0 if not bend else 8.0)
    # ...and the joined spots say where the eyes are
    eye = seatalign.eye_from(d, "host/main", "mac/main", a.joins(2))
    assert eye is not None
    mm = d["mm"]
    err = [abs(eye[i] - true_eye[i]) * mm for i in range(3)]
    assert err[0] < 15 and err[1] < 15  # across and up: to a centimetre or so
    assert math.dist(eye, true_eye) < math.dist(d["eye"], true_eye) / 2  # and much nearer than the scan had them


class _Cfg:
    def __init__(self, fits):
        self._fits, self.saved = [dict(f) for f in fits], 0

    def fits(self):
        return self._fits

    def set_fit(self, behind, front, x, y, scale, quad=None, eye=None, depth=None, mm=None, **_kw):
        self._fits = [f for f in self._fits if (f["behind"], f["front"]) != (behind, front)]
        new = {"behind": behind, "front": front, "x": x, "y": y, "scale": scale}
        for k, v in (("quad", quad), ("eye", eye), ("depth", depth), ("mm", mm)):
            if v is not None:
                new[k] = v
        self._fits.append(new)

    def save(self):
        self.saved += 1


def test_saving_keeps_the_hand_fit_and_aims_the_desk_from_the_eyes(tmp_path):
    desk, _layout, _solution, result = _desk()
    d = result.desk
    fit = dict(result.fits[0]) if isinstance(result.fits[0], dict) else vars(result.fits[0])
    true_eye = list(_world(desk, (60, 10, -950)))
    a, _real, _front = _join_by_eye(d, fit, true_eye)
    cfg = _Cfg([fit])
    path = tmp_path / "seat.json"
    out = seat.apply_joined(cfg, "host/main", "mac/main", a.command(), a.joins(), desk=d, path=path)
    assert out["eye"] is not None and out["moved_mm"] > 200 and cfg.saved == 1
    kept = cfg.fits()[0]
    assert kept["quad"] == pytest.approx(a.command()["quad"])
    assert len(kept["eye"]) == 3 and len(kept["depth"]) == 4  # head tracking can follow it
    again = seat.load(path)
    assert again["how"] == "joined" and again["eye"] == pytest.approx(out["eye"])


def test_saving_without_a_desk_just_keeps_the_fit():
    a = _laptop()
    a.nudge(3, 2)
    cfg = _Cfg([])
    out = seat.apply_joined(cfg, "host/main", "mac/main", a.command(), a.joins(), desk=None)
    assert out["eye"] is None and cfg.fits()[0]["x"] == pytest.approx(403.0) and cfg.saved == 1
