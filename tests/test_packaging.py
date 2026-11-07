import sys

from curslink import cli, overlay


def test_overlay_helper_uses_python_m_from_source(monkeypatch):
    monkeypatch.delattr(sys, "frozen", raising=False)
    assert overlay.helper_command() == [sys.executable, "-m", "curslink.overlay"]


def test_overlay_helper_reruns_packaged_executable(monkeypatch):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    assert overlay.helper_command() == [sys.executable, "_overlay-helper"]


def test_hidden_overlay_helper_command(monkeypatch):
    calls = []
    monkeypatch.setattr(overlay, "run_helper", lambda: calls.append(True))
    assert cli.main(["_overlay-helper"]) == 0
    assert calls == [True]


def test_switch_animation_positions_from_os_pixels():
    # Windows: a 150% screen right of a 100% one; Qt keeps each origin.
    screens = [(0, 0, 1920, 1080, 1.0), (1920, 0, 1707, 960, 1.5)]
    assert overlay.native_to_logical(screens, 100, 200, True) == (100, 200)
    assert overlay.native_to_logical(screens, 1920 + 300, 600, True) == (1920 + 200, 400)
    assert overlay.native_to_logical(screens, 5, 5, False) == (5, 5)  # macOS: points already


def test_an_effect_at_the_outer_edge_stays_on_its_screen():
    from curslink import overlay

    # An ultrawide left of the laptop: input coming home lands on its left edge.
    screens = [(0, 0, 1512, 982, 2.0), (-3440, 0, 3440, 1440, 1.0)]
    left, top, w, h, cx, cy = overlay.effect_box(screens, -3440, 700, 180)
    assert (left, top) == (-3440, 610) and (w, h) == (90, 180)  # cut, not hanging off every screen
    assert (left + cx, top + cy) == (-3440, 700)  # still drawn on the point
    assert overlay.effect_box(screens, 700, 500, 180) == (610, 410, 180, 180, 90, 90)  # mid screen: untouched
