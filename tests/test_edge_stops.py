"""The pointer stopping at an edge of this computer: reports past the edge
count as pushes, and the log says why when it doesn't cross (Kyle, 0.4.76:
"Windows feels like it's forcefully stopping the cursor leaving the monitor")."""
import logging

import pytest

from conftest import FakeCapture, FakeOverlay, FakeSink, FakeTransport
from curslink.inputs import pynput_backend
from curslink.router import STOP_REPORTS, Router


@pytest.fixture
def router(layout):
    r = Router(layout, "desktop", FakeCapture(), FakeSink(), FakeTransport({"laptop"}), FakeOverlay())
    now = [100.0]
    r.clock = lambda: now[0]
    r.now = now
    return r


def test_a_report_past_the_edge_crosses(router):
    # Windows' hook says where a quick move was headed: past the desktop.
    router.on_pointer(3000, 500)
    router.on_pointer(3871, 500)
    assert router.active_device == "laptop"


def test_a_report_past_an_edge_that_leads_nowhere_stays_on_the_edge(router):
    router.on_pointer(3000, 50)
    router.on_pointer(3900, 40)
    assert router.is_local and (router.x, router.y) == (3839, 40)


def test_reports_past_the_outside_of_two_screens_land_on_the_nearest(router):
    router.on_pointer(-30, 500)
    assert router.is_local and router.screen.id == "left" and (router.x, router.y) == (0, 500)


def test_pushing_where_nothing_is_beyond_says_which_part_leads_on(router, caplog):
    caplog.set_level(logging.INFO, "curslink.router")
    for _ in range(STOP_REPORTS + 3):
        router.on_pointer(3839, 1050)
    assert router.is_local
    lines = [r.getMessage() for r in caplog.records if r.getMessage().startswith("edge:")]
    assert len(lines) == 1
    assert "right edge of desktop/right at (3839, 1050)" in lines[0]
    assert "laptop/main from 18% to 92%" in lines[0]


def test_a_few_reports_on_the_edge_say_nothing(router, caplog):
    caplog.set_level(logging.INFO, "curslink.router")
    for _ in range(STOP_REPORTS - 1):
        router.on_pointer(3839, 1050)
    assert not [r for r in caplog.records if r.getMessage().startswith("edge:")]


def test_pushing_towards_a_device_that_isnt_connected_says_so(layout, caplog):
    caplog.set_level(logging.INFO, "curslink.router")
    r = Router(layout, "desktop", FakeCapture(), FakeSink(), FakeTransport(), FakeOverlay())
    for _ in range(STOP_REPORTS):
        r.on_pointer(3839, 500)
    assert any("laptop isn't connected" in x.getMessage() for x in caplog.records)


def test_said_once_in_a_while(router, caplog):
    caplog.set_level(logging.INFO, "curslink.router")
    for _ in range(3):
        for _ in range(STOP_REPORTS):
            router.on_pointer(3839, 1050)
        router.on_pointer(3000, 500)
        router.now[0] += 1
    assert len([r for r in caplog.records if r.getMessage().startswith("edge:")]) == 1


def test_an_app_holding_the_cursor_in_a_box_is_named(router, caplog):
    caplog.set_level(logging.INFO, "curslink.router")
    router.cursor_clip = lambda: (100, 100, 800, 600)
    for _ in range(STOP_REPORTS):
        router.on_pointer(899, 400)
    (line,) = [r.getMessage() for r in caplog.records if r.getMessage().startswith("edge:")]
    assert "inside 800x600 at 100,100 (ClipCursor)" in line


def test_the_pointer_resting_free_mid_screen_says_nothing(router, caplog):
    caplog.set_level(logging.INFO, "curslink.router")
    for _ in range(STOP_REPORTS * 2):
        router.on_pointer(899, 400)
    assert not caplog.records


def test_coming_home_by_hotkey_onto_an_edge_explains_the_wait(router, caplog):
    caplog.set_level(logging.INFO, "curslink.router")
    router.on_pointer(3839, 500)
    assert router.active_device == "laptop"
    router.switch_to(router.layout.screen("desktop", "right"), 3839, 500, "hotkey")
    for _ in range(STOP_REPORTS):
        router.on_pointer(3839, 500)
    assert router.is_local
    assert any("come off this edge first (it came back here by hotkey)" in r.getMessage() for r in caplog.records)


class _Capture:
    _ungrabbed_at = 0.0
    _hook_heard_at = 0.0
    _deaf_said: dict = {}


def test_the_cursor_moving_with_the_hook_deaf_names_the_admin_app(caplog):
    caplog.set_level(logging.WARNING, "curslink.inputs.pynput_backend")
    cap = _Capture()
    cap._deaf_said = {}
    pos = iter([(10 * i, 0) for i in range(20)])
    now = [50.0]
    deaf = pynput_backend._DeafHook(cap, clock=lambda: now[0], cursor=lambda: next(pos),
                                    foreground=lambda: "Taskmgr.exe (runs as administrator)")
    for _ in range(10):
        deaf.check()
        now[0] += 0.25
    (line,) = [r.getMessage() for r in caplog.records]
    assert "Taskmgr.exe (runs as administrator)" in line and "apps that run as administrator" in line


def test_the_hook_hearing_the_mouse_is_not_deaf(caplog):
    caplog.set_level(logging.WARNING, "curslink.inputs.pynput_backend")
    cap = _Capture()
    cap._deaf_said = {}
    pos = iter([(10 * i, 0) for i in range(20)])
    now = [50.0]
    deaf = pynput_backend._DeafHook(cap, clock=lambda: now[0], cursor=lambda: next(pos), foreground=lambda: "x")
    for _ in range(10):
        cap._hook_heard_at = now[0]
        deaf.check()
        now[0] += 0.25
    assert not caplog.records
