import math

import pytest

from curslink.gaze.headpose import AngleSmoother, OneEuroFilter, rotation_to_euler
from curslink.gaze.zones import Calibration, GazeClassifier, GazeSettings, Target

LEFT, MID, RIGHT = Target("desktop", "left"), Target("desktop", "right"), Target("laptop", "main")


def rot(yaw, pitch, roll):
    y, p, r = map(math.radians, (yaw, pitch, roll))
    rz = [[math.cos(r), -math.sin(r), 0], [math.sin(r), math.cos(r), 0], [0, 0, 1]]
    ry = [[math.cos(y), 0, math.sin(y)], [0, 1, 0], [-math.sin(y), 0, math.cos(y)]]
    rx = [[1, 0, 0], [0, math.cos(p), -math.sin(p)], [0, math.sin(p), math.cos(p)]]
    mul = lambda a, b: [[sum(a[i][k] * b[k][j] for k in range(3)) for j in range(3)] for i in range(3)]
    return mul(rz, mul(ry, rx))


@pytest.mark.parametrize("angles", [(0, 0, 0), (25, -10, 3), (-40, 15, -8), (10, 30, 0)])
def test_rotation_to_euler_roundtrip(angles):
    got = rotation_to_euler(rot(*angles))
    assert got == pytest.approx(angles, abs=1e-6)


def test_one_euro_smooths_jitter_but_follows_steps():
    f = OneEuroFilter(min_cutoff=1.0, beta=0.05)
    out = [f(i / 30, 10 + (0.5 if i % 2 else -0.5)) for i in range(60)]
    assert max(out[30:]) - min(out[30:]) < 0.3  # jitter of 1.0 heavily damped
    for i in range(60, 75):
        v = f(i / 30, 40)
    assert v > 35  # a real 30 degree head turn gets through within 0.5 s


def test_smoother_resets_after_face_lost():
    s = AngleSmoother(reset_after_s=0.5)
    s(0.0, 0, 0)
    s(0.03, 0, 0)
    assert s(2.0, 30, 5) == (30, 5)


def make_cal():
    cal = Calibration()
    for target, (yaw, pitch) in {LEFT: (-20, 0), MID: (0, 0), RIGHT: (30, 5)}.items():
        for d in (-0.5, 0, 0.5):
            cal.add(target, yaw + d, pitch - d)
    return cal


def feed(clf, yaw, pitch, start, seconds, fps=30):
    result = None
    for i in range(int(seconds * fps)):
        r = clf.update(start + i / fps, yaw, pitch)
        result = r or result
    return result


def test_calibration_roundtrip(tmp_path):
    cal = make_cal()
    path = tmp_path / "cal.json"
    cal.save(path)
    assert Calibration.load(path).centroids() == cal.centroids()


def test_switch_requires_dwell():
    clf = GazeClassifier(make_cal(), GazeSettings(dwell_s=0.3))
    clf.force(MID)
    assert feed(clf, 29, 4, 0.0, 0.2) is None  # quick glance
    assert feed(clf, 0, 0, 0.2, 0.2) is None
    assert feed(clf, 29, 4, 1.0, 0.5) == RIGHT
    assert clf.current == RIGHT


def test_looking_away_keeps_current_target():
    clf = GazeClassifier(make_cal(), GazeSettings(dwell_s=0.0, max_distance_deg=10))
    clf.force(RIGHT)
    assert feed(clf, 5, -40, 0, 1.0) is None  # looking down at the desk
    assert clf.current == RIGHT


def test_hysteresis_near_the_boundary():
    clf = GazeClassifier(make_cal(), GazeSettings(dwell_s=0.0, switch_margin_deg=3))
    clf.force(MID)
    # just past the midpoint between MID (0) and RIGHT (30): not enough margin
    assert feed(clf, 16, 2, 0, 0.5) is None
    assert feed(clf, 20, 3, 1, 0.5) == RIGHT


def test_empty_calibration_rejected():
    with pytest.raises(ValueError):
        GazeClassifier(Calibration())
