"""Experimental head tracking: overlaps worked out again for where the head is."""

from __future__ import annotations

import json
from dataclasses import replace

import pytest

from curslink.headfollow import GRID, HeadFollow, mediapipe_head, screen_axes, signature
from curslink.layout import Device, Fit, Layout, Screen


def seen(eye, point):
    """Where ``point`` (x, y, how far before the screen) is seen on the screen from ``eye`` (the same)."""
    (ex, ey, ez), (px, py, d) = eye, point
    return ((px * ez - ex * d) / (ez - d), (py * ez - ey * d) / (ez - d))


# A monitor 0.23 mm to the pixel; a laptop 250 mm (1087 px) before its lower
# left, turned in (its right side further back) and tipped back (its top
# further back); the eye 650 mm (2826 px) from the monitor.
MM = 0.23
EYE = (1700.0, 500.0, 2826.0)
SEEN = ((300.0, 1150.0), (1700.0, 1140.0), (1740.0, 1900.0), (310.0, 1930.0))  # its corners, as seen on the monitor
DEPTH = (1000.0, 760.0, 900.0, 1140.0)
# ...so where they are in the room: (across, down, how far before the monitor)
CORNERS = tuple(((x * (EYE[2] - d) + EYE[0] * d) / EYE[2], (y * (EYE[2] - d) + EYE[1] * d) / EYE[2], d)
                for (x, y), d in zip(SEEN, DEPTH))


def fit_from(eye=EYE):
    quad = tuple(v for c in CORNERS for v in seen(eye, c))
    return Fit(("mac", "screen2"), ("pc", "main"), quad[0], quad[1], 0.5, quad=quad, eye=eye,
               depth=tuple(c[2] for c in CORNERS), mm=MM)


def layout(fits=None):
    return Layout.from_devices([
        Device("mac", "Mac", (Screen("screen2", "mac", 0, 0, 3440, 1440),)),
        Device("pc", "PC", (Screen("main", "pc", 1000, 1200, 2880, 1536, z=1),)),
    ], allow_overlap=True, pointer_through=True, mirror=True, fits=fits if fits is not None else [fit_from()])


def test_a_fit_seen_from_elsewhere_is_where_its_corners_are_really_seen():
    f = fit_from()
    assert f.follows
    for eye in ((1830.0, 500.0, 2826.0), (1600.0, 420.0, 3100.0), (1700.0, 560.0, 2500.0)):
        assert f.seen_from(eye).quad == pytest.approx(fit_from(eye).quad, abs=1e-6)
    # 30 mm to the right: the laptop's corners slide left across the monitor, the nearer ones more
    # (3 or 4 mm for every centimetre: a quarter to a third of the head's move).
    moved = f.seen_from((EYE[0] + 30 / MM, EYE[1], EYE[2]))
    slides = [(moved.quad[2 * i] - f.quad[2 * i]) * MM for i in range(4)]
    assert all(-21 < s < -10 for s in slides) and slides[3] < slides[1]
    assert moved.x == pytest.approx(f.x + sum(slides) / 4 / MM)
    # From nowhere sensible, or a fit that can't tell: left as it is.
    assert f.seen_from((0.0, 0.0, 900.0)) is f
    plain = replace(f, eye=None)
    assert not plain.follows and plain.seen_from((0.0, 0.0, 3000.0)) is plain


def test_mediapipe_says_where_the_head_is_in_its_own_way():
    # 4 cm to the camera's right (your left), 2 cm up, 55 cm before it.
    m = [[1, 0, 0, 4.0], [0, 1, 0, 2.0], [0, 0, 1, -55.0], [0, 0, 0, 1]]
    assert mediapipe_head(m) == (-40.0, -20.0, 550.0)


def test_a_laptops_camera_looks_out_of_its_leaning_screen():
    across, down, out = screen_axes(fit_from())
    # Turned in (its right side further from you) and tipped back (its top further).
    assert across[0] > 0.95 and across[2] < -0.1
    assert down[1] > 0.9 and down[2] > 0.1
    assert out[2] > 0.85 and out[1] < 0  # (out of it is toward you and a little up)
    assert screen_axes(replace(fit_from(), depth=None)) == ((1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0))


class Clock:
    def __init__(self):
        self.t = 100.0

    def __call__(self):
        return self.t


def settle(follow, clock, head=(10.0, -40.0, 600.0), seconds=2.0):
    for _ in range(int(seconds * 30)):
        clock.t += 1 / 30
        follow.sample(clock.t, head)


def test_the_overlap_follows_the_head_from_where_it_first_sat(tmp_path):
    lay = layout()
    base = lay.fits[0]
    changed = []
    clock = Clock()
    home = tmp_path / "head-home.json"
    follow = HeadFollow(lay, "mac", lambda: changed.append(1), home_path=home, clock=clock)
    assert lay.part_grid == GRID and follow.home is None
    settle(follow, clock)
    assert follow.home == pytest.approx((10.0, -40.0, 600.0)) and not changed and lay.fits[0] is base
    assert json.loads(home.read_text())["fits"] == signature([base])
    # The head goes 30 mm to the right (the camera here is the monitor's: straight out of it).
    settle(follow, clock, (40.0, -40.0, 600.0))
    assert changed and follow.offset[0] == pytest.approx(30.0, abs=1.0)
    want = base.seen_from((EYE[0] + 30 / MM, EYE[1], EYE[2]))
    assert lay.fits[0].quad == pytest.approx(want.quad, abs=2.0)
    assert lay.fits[0].quad[0] < base.quad[0] - 40 and base.quad[:2] == pytest.approx(SEEN[0])
    # Not more often than 20 times a second, however fast the camera is.
    n = len(changed)
    for i in range(20):
        clock.t += 0.001
        follow.sample(clock.t, (40.0 + 3 * i, -40.0, 600.0))
    assert len(changed) <= n + 1
    # While the overlap's being adjusted by hand nothing moves.
    follow.busy = lambda: True
    n, held = len(changed), lay.fits
    settle(follow, clock, (-60.0, -40.0, 600.0))
    assert len(changed) == n and lay.fits is held
    follow.busy = lambda: False
    # No face for a while: back to the overlap as it was lined up.
    clock.t += 5.0
    follow.tick()
    assert lay.fits[0].quad == pytest.approx(base.quad)
    # Stopped: as lined up, and captured as it is again.
    settle(follow, clock, (70.0, -40.0, 600.0))
    assert lay.fits[0].quad != pytest.approx(base.quad)
    follow.stop()
    assert lay.fits[0] == base and lay.part_grid == 1


def test_home_is_kept_between_runs_until_the_overlap_is_lined_up_afresh(tmp_path):
    home = tmp_path / "head-home.json"
    clock = Clock()
    first = HeadFollow(layout(), "mac", lambda: None, home_path=home, clock=clock)
    settle(first, clock)
    again = HeadFollow(layout(), "mac", lambda: None, home_path=home, clock=clock)
    assert again.home == pytest.approx((10.0, -40.0, 600.0))
    # "Sit here": where the head is now, once it's still.
    again.rehome()
    assert again.home is None
    settle(again, clock, (55.0, -30.0, 640.0))
    assert again.home == pytest.approx((55.0, -30.0, 640.0))
    # A head that's moving about isn't a home yet.
    again.rehome()
    for i in range(60):
        clock.t += 1 / 30
        again.sample(clock.t, (55.0 + 4 * i, -30.0, 640.0))
    assert again.home is None
    # A new line-up (the laptop was moved): the old home goes with the old one.
    moved = replace(fit_from(), quad=tuple(v + 80 for v in fit_from().quad))
    assert HeadFollow(layout([moved]), "mac", lambda: None, home_path=home, clock=clock).home is None


def test_a_line_up_saved_meanwhile_is_taken_as_the_new_overlap(tmp_path):
    lay = layout()
    clock = Clock()
    follow = HeadFollow(lay, "mac", lambda: None, home_path=tmp_path / "h.json", clock=clock)
    settle(follow, clock)
    settle(follow, clock, (40.0, -40.0, 600.0))
    new = replace(fit_from(), quad=tuple(v + 80 for v in fit_from().quad))
    lay.fits = (new,)  # (the fit command, from the Layout page)
    settle(follow, clock, (40.0, -40.0, 600.0))
    # From where the head is now: that's where it was lined up from.
    assert follow.base[(new.behind, new.front)] is new and lay.fits[0] is new
    assert follow.home == pytest.approx((40.0, -40.0, 600.0))


def test_overlaps_that_never_went_through_a_live_line_up_are_left_alone():
    plain = Fit(("mac", "screen2"), ("pc", "main"), 1200, 1100, 0.5)
    lay = layout([plain])
    clock = Clock()
    changed = []
    follow = HeadFollow(lay, "mac", lambda: changed.append(1), clock=clock)
    settle(follow, clock)
    settle(follow, clock, (90.0, 0.0, 600.0))
    assert follow.base == {} and lay.fits == (plain,) and not changed


def test_the_laptops_own_camera_goes_by_how_its_screen_leans():
    lay = layout()
    clock = Clock()
    follow = HeadFollow(lay, "pc", lambda: None, clock=clock)  # this computer is the one in front
    straight = HeadFollow(layout(), "mac", lambda: None, clock=clock)
    a = follow.fits_for((0.0, 0.0, 100.0))[(("mac", "screen2"), ("pc", "main"))]
    b = straight.fits_for((0.0, 0.0, 100.0))[(("mac", "screen2"), ("pc", "main"))]
    # Straight back from a screen that's turned in and tipped back isn't straight back from the monitor.
    assert a.quad != pytest.approx(b.quad, abs=0.5)
    assert follow.fits_for((0.0, 0.0, 0.0))[(("mac", "screen2"), ("pc", "main"))].quad == pytest.approx(lay.fits[0].quad)


# -- wired into the host ---------------------------------------------------------

class FakeMirror:
    tuning = None
    held = None

    def __init__(self):
        self.refreshed = 0

    def refresh(self):
        self.refreshed += 1


class FakeGaze:
    enabled = False
    camera = 0
    camera_name = None


class FakeConfig:
    device_id = "mac"
    gaze = FakeGaze()

    def __init__(self, on=True):
        self.follow_head = on
        self.layout = layout()


def test_the_host_follows_the_head_with_a_camera_of_its_own_when_gaze_is_off(monkeypatch, tmp_path):
    import asyncio

    from curslink import app
    from curslink.gaze import tracker as tracking

    started = []

    class FakeTracker:
        def __init__(self, camera, on_head, name=None):
            self.camera, self.on_head, self.stopped = camera, on_head, False
            started.append(self)

        def start(self):
            pass

        def stop(self):
            self.stopped = True

    # (A process of its own, not a thread of the host's: curslink.headcamera.)
    import curslink.headcamera as headcamera

    monkeypatch.setattr(headcamera, "HeadCameraProcess", FakeTracker)
    monkeypatch.setattr(app, "head_home_path", lambda: tmp_path / "head-home.json")

    async def go():
        loop = asyncio.get_running_loop()
        assert app.start_head_follow(FakeConfig(on=False), FakeMirror(), loop, own_camera=True) is None
        assert app.start_head_follow(FakeConfig(), None, loop, own_camera=True) is None  # no mirror mode
        cfg, mirror = FakeConfig(), FakeMirror()
        head = app.start_head_follow(cfg, mirror, loop, own_camera=True)
        assert len(started) == 1 and tracking.head_listener is not None
        assert started[0].on_head is tracking.head_listener
        # What a tracker's thread says goes to the loop, and on to the overlaps.
        t = 50.0
        for at in ((10.0, -40.0, 600.0), (45.0, -40.0, 600.0)):
            for _ in range(60):
                t += 1 / 30
                head.follow.clock = lambda t=t: t
                tracking.head_listener(t, at)
            await asyncio.sleep(0.01)
        assert mirror.refreshed and cfg.layout.fits[0].quad[0] < fit_from().quad[0] - 40
        # Gaze switching's own tracker feeds it instead when that's on.
        other = app.start_head_follow(FakeConfig(), FakeMirror(), loop, own_camera=False)
        assert len(started) == 1 and other.tracker is None
        other.stop()
        head.stop()
        assert started[0].stopped and tracking.head_listener is None
        assert cfg.layout.fits[0] == fit_from() and cfg.layout.part_grid == 1

    asyncio.run(go())


def test_follow_head_is_read_from_the_layout_table():
    from curslink.config import ConfigError, _follow_head

    assert _follow_head({}) is False and _follow_head({"follow_head": True}) is True
    with pytest.raises(ConfigError):
        _follow_head({"follow_head": "yes"})


def test_the_head_camera_is_a_process_of_its_own_that_the_host_only_reads_lines_from(monkeypatch):
    """On a Windows host, loading the camera's libraries on one of the
    host's own threads held the whole host up for minutes: they're loaded
    in a child, which prints where the head is."""
    import sys
    import time

    from curslink import headcamera

    assert headcamera.parse("h 12.5 -40.0 610.2\n") == (12.5, -40.0, 610.2)
    assert headcamera.parse("h 1 2") is None and headcamera.parse("x 1 2 3") is None
    assert headcamera.parse("h a b c") is None
    assert headcamera.command(1, "FHD cam")[-3:] == ["curslink.headcamera", "1", "FHD cam"]
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    assert headcamera.command(2)[1:] == ["_head-camera", "2"]
    monkeypatch.undo()
    # A child that says where the head is twice, then waits to be stopped.
    script = ("import sys\nprint('h 10.0 -20.0 600.0', flush=True)\nprint('nonsense', flush=True)\n"
              "print('h 11.0 -20.0 601.0', flush=True)\nprint('camera 0: open', file=sys.stderr, flush=True)\n"
              "sys.stdin.read()\n")
    monkeypatch.setattr(headcamera, "command", lambda camera, name=None: [sys.executable, "-c", script])
    seen = []
    proc = headcamera.HeadCameraProcess(0, lambda t, at: seen.append((t, at)))
    proc.start()
    deadline = time.monotonic() + 20
    while len(seen) < 2 and time.monotonic() < deadline:
        time.sleep(0.01)
    assert [at for _t, at in seen] == [(10.0, -20.0, 600.0), (11.0, -20.0, 601.0)]
    assert abs(seen[0][0] - time.monotonic()) < 20 and proc.alive()
    child = proc._proc
    proc.stop()
    assert child.poll() is not None and not proc.alive()  # closing its stdin was enough


def test_a_head_camera_that_dies_is_started_again_a_few_times(monkeypatch):
    import sys
    import time

    from curslink import headcamera

    starts = []

    def command(camera, name=None):
        starts.append(camera)
        return [sys.executable, "-c", "raise SystemExit(3)"]

    monkeypatch.setattr(headcamera, "command", command)
    monkeypatch.setattr(headcamera, "RESTART_AFTER_S", 0.01)
    proc = headcamera.HeadCameraProcess(4, lambda t, at: None)
    proc.start()
    deadline = time.monotonic() + 30
    while len(starts) < headcamera.RESTARTS + 1 and time.monotonic() < deadline:
        time.sleep(0.02)
    time.sleep(0.3)
    assert len(starts) == headcamera.RESTARTS + 1 and "exit code 3" in proc.error
    proc.stop()
