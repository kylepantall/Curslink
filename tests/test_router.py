import pytest

from conftest import FakeCapture, FakeOverlay, FakeSink, FakeTransport
from curslink.gaze.zones import Target
from curslink.router import Router, RouterSettings


@pytest.fixture
def parts(layout):
    capture, sink, transport, overlay = FakeCapture(), FakeSink(), FakeTransport({"laptop"}), FakeOverlay()
    router = Router(layout, "desktop", capture, sink, transport, overlay, RouterSettings(gaze_switching=True))
    return router, capture, sink, transport, overlay


def test_starts_local(parts):
    router, capture, *_ = parts
    assert router.is_local and not capture.grabbed


def test_edge_push_switches_to_connected_device(parts):
    router, capture, sink, transport, overlay = parts
    router.on_pointer(1000, 500)
    assert router.is_local
    router.on_pointer(3839, 500)
    assert router.active_device == "laptop"
    assert capture.grabbed
    (dev, t, data), = transport.of_type("enter")
    assert dev == "laptop" and data["effect"] == "pop"
    assert data["x"] >= 0 and data["y"] == 300  # laptop OS coords


def test_edge_push_ignored_when_device_offline(layout):
    router = Router(layout, "desktop", FakeCapture(), FakeSink(), FakeTransport(), FakeOverlay())
    router.on_pointer(3839, 500)
    assert router.is_local


def test_remote_motion_moves_and_clamps(parts):
    router, capture, sink, transport, _ = parts
    router.on_pointer(3839, 500)
    transport.sent.clear()
    router.on_motion(10, 0)
    (_, _, data), = transport.of_type("move")
    assert data == {"x": router.x - 3840, "y": 300}
    # far down: clamp to the bottom edge instead of escaping the screen
    router.on_motion(0, 5000)
    assert transport.sent[-1][2]["y"] == 799


def test_motion_back_over_edge_returns_home(parts):
    router, capture, sink, transport, overlay = parts
    router.on_pointer(3839, 500)
    router.on_motion(-50, 0)
    assert router.is_local
    assert not capture.grabbed
    assert transport.of_type("leave")
    assert sink.calls[-1][0] == "move"
    x, y = sink.calls[-1][1:]
    assert 3780 < x < 3800 and y == 500  # the whole delta carries over
    assert overlay.shown and overlay.shown[-1][0] == "pop"


def test_input_forwarded_only_when_remote(parts):
    router, _, _, transport, _ = parts
    router.on_key("a", True)
    router.on_button("left", True)
    router.on_button("left", False)
    assert transport.sent == []
    router.on_pointer(3839, 500)
    router.on_key("a", True)
    router.on_scroll(0, -1)
    assert ("laptop", "key", {"key": "a", "pressed": True}) in transport.sent
    assert ("laptop", "scroll", {"dx": 0, "dy": -1}) in transport.sent


def test_held_keys_released_on_switch_away(parts):
    router, _, _, transport, _ = parts
    router.on_pointer(3839, 500)
    router.on_key("shift", True)
    router.on_button("left", True)
    router.go_home("test")
    sent = transport.sent
    assert ("laptop", "key", {"key": "shift", "pressed": False}) in sent
    assert ("laptop", "button", {"button": "left", "pressed": False}) in sent
    assert sent[-1][1] == "leave"


def test_local_held_keys_released_when_grabbing(parts):
    router, _, sink, _, _ = parts
    router.on_key("ctrl_l", True)
    router.on_pointer(3839, 500)
    assert ("key", "ctrl_l", False) in sink.calls


def test_return_hotkey(parts):
    router, *_ = parts
    router.on_pointer(3839, 500)
    for k in ("ctrl_l", "alt_l", "home"):
        router.on_key(k, True)
    assert router.is_local


def test_gaze_switches_device_and_remembers_position(parts):
    router, capture, _, transport, _ = parts
    router.on_gaze(Target("laptop", "main"))
    assert router.active_device == "laptop"
    enter = transport.of_type("enter")[-1][2]
    assert (enter["x"], enter["y"]) == (640, 400)  # centre of laptop
    router.on_motion(100, 50)
    router.on_gaze(Target("desktop", "left"))
    assert router.is_local
    router.on_gaze(Target("laptop", "main"))
    enter = transport.of_type("enter")[-1][2]
    assert (enter["x"], enter["y"]) == (740, 450)  # where we left it


def test_gaze_ignored_mid_drag_offline_or_paused(parts, layout):
    router, _, _, transport, _ = parts
    router.on_gaze(Target("tablet", "main"))  # offline
    assert router.is_local
    router.on_gaze(Target("laptop", "main"))
    router.on_button("left", True)
    router.on_gaze(Target("desktop", "left"))
    assert router.active_device == "laptop"  # drag in progress
    router.on_button("left", False)
    for k in ("ctrl", "alt", "g"):
        router.on_key(k, True)
    assert router.gaze_paused
    router.on_gaze(Target("desktop", "left"))
    assert router.active_device == "laptop"


def test_gaze_within_same_device_is_opt_in(parts):
    router, _, sink, _, _ = parts
    router.on_gaze(Target("desktop", "right"))
    assert router.screen.id == "left"
    router.settings.gaze_within_device = True
    router.on_gaze(Target("desktop", "right"))
    assert router.screen.id == "right"
    assert sink.calls[-1] == ("move", 2880, 540)


def test_gaze_back_home_lands_where_you_left_it_not_on_the_monitor_looked_at(parts):
    router, _, sink, _, _ = parts
    router.on_pointer(500, 400)                 # working on the left monitor
    router.on_gaze(Target("laptop", "main"))
    assert router.active_device == "laptop"
    router.on_gaze(Target("desktop", "right"))  # eyes land on the right monitor
    assert router.is_local
    assert router.screen.id == "left"
    assert sink.calls[-1] == ("move", 500, 400)


def test_gaze_to_another_device_ignores_which_of_its_monitors(parts):
    router, _, sink, _, _ = parts
    router.on_pointer(2500, 300)                # right monitor
    router.on_gaze(Target("laptop", "main"))
    router.on_gaze(Target("desktop", "left"))
    assert router.screen.id == "right"
    assert sink.calls[-1] == ("move", 2500, 300)


def test_disconnect_of_active_device_returns_home(parts):
    router, capture, _, transport, _ = parts
    router.on_pointer(3839, 500)
    transport.connected.discard("laptop")
    router.on_disconnect("laptop")
    assert router.is_local and not capture.grabbed


def test_switch_listeners_notified(parts):
    router, *_ = parts
    events = []
    router.listeners.append(events.append)
    router.on_pointer(3839, 500)
    assert events[-1].to_device == "laptop" and events[-1].reason == "edge"


def test_input_stays_home_while_it_cant_be_captured(parts):
    router, capture, _, transport, _ = parts
    router.blocked = lambda: "macOS hasn't allowed Accessibility"
    router.on_gaze(Target("laptop", "main"))
    router.on_pointer(3839, 500)
    assert router.is_local and not capture.grabbed and transport.sent == []
    router.blocked = lambda: ""
    router.on_gaze(Target("laptop", "main"))
    assert router.active_device == "laptop"


def test_coming_home_onto_the_edge_doesnt_bounce_straight_back(parts):
    router, _, _, transport, overlay = parts
    router.on_pointer(3839, 500)                 # pushed off the right edge
    assert router.active_device == "laptop"
    router.on_gaze(Target("desktop", "right"))   # look back: lands where it left, on the edge
    assert router.is_local
    router.on_pointer(3839, 500)                 # still resting on that edge
    assert router.is_local
    router.on_pointer(3700, 500)                 # moved away...
    router.on_pointer(3839, 500)                 # ...and pushed again
    assert router.active_device == "laptop"


def test_switching_to_where_input_already_is_does_nothing(parts):
    router, _, sink, transport, overlay = parts
    router.on_gaze(Target("laptop", "main"))
    sent = len(transport.sent)
    router.switch_to(router.screen, router.x, router.y, "gaze")
    assert len(transport.sent) == sent           # no second enter, no animation
    router.on_gaze(Target("desktop", "left"))
    shown = len(overlay.shown)
    router.go_home("app")
    assert len(overlay.shown) == shown
