import pytest

from conftest import FakeCapture, FakeOverlay, FakeSink, FakeTransport
from curslink.config import ConfigError, host_config
from curslink.pointerfeel import (DeviceFeel, PointerFeel, PointerSettings, Profile, pointer_settings,
                                  profile_from_wire)
from curslink.router import Router, RouterSettings


def test_windows_curve_is_one_to_one_without_precision_at_the_default_speed():
    assert Profile("windows", 10, accel=False).gain(5000) == 1.0
    assert Profile("windows", 20, accel=False).gain(5000) == 3.5
    assert Profile("windows", 1, accel=False).gain(5000) == 0.03125


@pytest.mark.parametrize("profile", [Profile("windows", 10, True), Profile("mac", 0.6875),
                                     Profile("linux", 0.0, True), Profile("windows", 14, False)])
def test_each_curve_speeds_up_with_the_mouse_and_inverts(profile):
    slow, fast = profile.gain(200), profile.gain(20000)
    assert fast >= slow > 0
    for counts in (50, 800, 4000, 30000):
        px = profile.out_speed(counts)
        assert profile.counts_for(px) == pytest.approx(counts, rel=1e-3)


def test_faster_settings_move_further():
    assert Profile("mac", 3.0).gain(4000) > Profile("mac", 0.5).gain(4000)
    assert Profile("windows", 15, True).gain(4000) > Profile("windows", 10, True).gain(4000)
    assert Profile("mac", -1).gain(4000) == 1.0  # acceleration off


def test_profiles_from_agents_are_checked():
    assert profile_from_wire({"os": "windows", "speed": 12, "accel": False}) == Profile("windows", 12.0, False)
    for bad in (None, "x", {"os": "amiga", "speed": 1}, {"os": "mac", "speed": "fast"},
                {"os": "mac", "speed": True}, {"os": "mac", "speed": float("nan")}):
        assert profile_from_wire(bad) is None
    p = Profile("linux", -0.5, False)
    assert profile_from_wire(p.to_wire()) == p


def test_settings_per_device_over_the_defaults():
    s = pointer_settings({"pointer": {"speed": 1.5, "device": {"pc": {"feel": "native"}, "phone": {"speed": 0.5}}}})
    assert s.for_device("pc") == DeviceFeel("native", 1.5)
    assert s.for_device("phone") == DeviceFeel("here", 0.5)
    assert s.for_device("other") == DeviceFeel("here", 1.5)
    assert pointer_settings({}).for_device("x") == DeviceFeel()
    for bad in ({"feel": "fast"}, {"speed": 0}, {"speed": 9}, {"speed": "1"}, {"device": {"pc": 2}}, {"device": 3}):
        with pytest.raises(ConfigError):
            pointer_settings({"pointer": bad})


def test_speed_multiplies_and_same_as_here_is_untouched():
    feel = PointerFeel(PointerSettings(devices={"pc": DeviceFeel("here", 2.0)}))
    assert feel.scale("pc", 3, -4, 1.0) == (6, -8)
    assert feel.scale("other", 3, -4, 1.0) == (3, -4)


def test_native_uses_the_devices_own_curve():
    here = Profile("windows", 10, accel=False)            # 1 px per count
    there = {"pc": Profile("windows", 20, accel=False)}   # 3.5 px per count
    feel = PointerFeel(PointerSettings(devices={"pc": DeviceFeel("native", 1.0)}), here, there.get)
    dx, dy = feel.scale("pc", 10, 0, 1.0)
    assert dx == pytest.approx(35) and dy == 0
    # Its speed trims native too.
    feel.settings.devices["pc"] = DeviceFeel("native", 0.5)
    assert feel.scale("pc", 10, 0, 1.008)[0] == pytest.approx(17.5)


def test_native_from_a_mac_slows_a_careful_move_and_speeds_a_flick():
    here = Profile("windows", 10, accel=False)
    feel = PointerFeel(PointerSettings(DeviceFeel("native")), here, lambda d: Profile("mac", 0.6875))
    t = 1.0
    slow = []
    for _ in range(5):
        t += 0.008
        slow.append(feel.scale("mac", 1, 0, t)[0])
    t += 1.0
    fast = []
    for _ in range(5):
        t += 0.008
        fast.append(feel.scale("mac", 60, 0, t)[0] / 60)
    assert slow[-1] < fast[-1]


def test_native_without_a_profile_falls_back_to_the_speed():
    feel = PointerFeel(PointerSettings(DeviceFeel("native", 1.25)), Profile("mac", 1.0), lambda d: None)
    assert feel.scale("phone", 8, 0, 1.0) == (10, 0)


def test_router_applies_the_feel_to_remote_moves(layout):
    transport = FakeTransport({"laptop"})
    router = Router(layout, "desktop", FakeCapture(), FakeSink(), transport, FakeOverlay(), RouterSettings())
    ticks = iter(range(1000, 10**6))
    router.clock = lambda: float(next(ticks))
    router.feel = PointerFeel(PointerSettings(devices={"laptop": DeviceFeel("here", 2.0)}))
    router.on_pointer(3839, 500)
    assert router.active_device == "laptop"
    x = router.x
    transport.sent.clear()
    router.on_motion(10, 0)
    assert router.x == x + 20
    # Home is never scaled: the real cursor moves there.
    router.go_home("test")
    router.on_pointer(1000, 500)
    assert router.x == 1000


def test_host_config_reads_pointer():
    from pathlib import Path

    from curslink.config import load_raw

    raw = load_raw(Path(__file__).parent.parent / "examples" / "curslink.toml")
    assert host_config(raw).pointer.for_device("laptop") == DeviceFeel()
    raw["pointer"] = {"device": {"laptop": {"feel": "native", "speed": 1.25}}}
    assert host_config(raw).pointer.for_device("laptop") == DeviceFeel("native", 1.25)
