"""App mirror: drag an app's window onto another device and use it there."""

from __future__ import annotations

import base64
import json
import os
import sys

import pytest

from conftest import FakeCapture, FakeOverlay, FakeSink
from curslink.appmirror import GRIP, LAND_PX, AppMirrorDevice, AppMirrorHost, CastWatch, WindowDragWatch, is_app
from curslink.layout import Device, Layout, Screen
from curslink.mirror import MirrorHost, MirrorPeer
from curslink.protocol import ProtocolError, decode, encode
from curslink.router import Router


def desk(phone=False):
    """The PC (host) on the left, a Mac to its right (and maybe a phone right of that)."""
    devices = [Device("pc", "WKS82261", (Screen("main", "pc", 0, 0, 1920, 1080),)),
               Device("mac", "MacBook", (Screen("main", "mac", 1920, 0, 1440, 900),))]
    if phone:
        devices.append(Device("phone", "S25", (Screen("main", "phone", 3360, 0, 400, 860),)))
    return Layout.from_devices(devices)


class Conn:
    def __init__(self, platform):
        self.platform = platform


class Transport:
    def __init__(self, devices=("mac",), app=("mac",), platforms=None, native=None):
        self.connected = set(devices)
        self.app = set(app)
        self.native = set(app if native is None else native)
        self.sent = []
        platforms = platforms or {}
        self.connections = {d: Conn(platforms.get(d, "Darwin 24.1")) for d in devices}

    def is_connected(self, device_id):
        return device_id in self.connected

    def supports(self, device_id, feature):
        if device_id not in self.connected:
            return False
        if feature == "app_mirror":
            return device_id in self.app
        if feature == "app_native":
            return device_id in self.native
        return feature in ("mirror", "mirror_cursor", "mirror_tiles")

    def send(self, device_id, t, **data):
        self.sent.append((device_id, t, data))

    def of(self, t, device=None):
        return [d for (dev, tt, d) in self.sent if tt == t and (device is None or dev == device)]


class Ops:
    """Window operations, without a desktop."""

    def __init__(self):
        self.raised, self.moved, self.hidden, self.shown = [], [], [], []

    def supported(self):
        return True

    def window_at_cursor(self, skip=None):
        return None

    def window_rect(self, win):
        return None

    def raise_window(self, win):
        self.raised.append(win)

    def move_window(self, win, rect):
        self.moved.append((win, rect))
        return True

    def hide_window(self, win, home, in_use):
        self.hidden.append((win["wid"], list(home), in_use))
        return True

    def show_window(self, win, home):
        self.shown.append((win["wid"], home))

    def window_alive(self, win):
        return True

    def parks(self):
        return False


def now(fn, done):
    done(fn())


SAFARI = {"wid": 7, "pid": 41, "app": "Safari", "title": "Apple", "os": [140, 60, 800, 600],
          "start": [100, 50, 800, 600], "at": [0.5, 0.02]}


class Clock:
    t = 0.0

    def __call__(self):
        return self.t


def make(lay=None, transport=None):
    lay = lay or desk()
    transport = transport or Transport()
    lines = []
    router = Router(lay, "pc", FakeCapture(), FakeSink(), transport, FakeOverlay())
    clock = Clock()
    host = apps = None
    local = MirrorPeer(lines.append, lambda t, **d: host.local_send(t, **d))
    host = MirrorHost(lay, router, transport, local, fps=30, clock=clock)
    ops = Ops()
    device = AppMirrorDevice(lambda t, **d: apps.on_message("pc", t, d), now, lambda d, f: None, ops)
    local.on_captures = device.on_captures
    released = []
    apps = AppMirrorHost(lay, router, transport, device, changed=host.refresh,
                         release_local=lambda: released.append(True), clock=clock, local_peer=local)
    # The PC is a PC wherever the tests run (on a Mac runner this computer
    # would have a menu bar, and its full screen mirrors stop below it).
    platform = apps.platform
    apps.platform = lambda d: "windows" if d == "pc" else platform(d)
    host.apps = apps
    router.portal = apps
    router.on_any_button = apps.on_button
    router.listeners.append(apps.on_switch)
    router.on_moved = lambda: (apps.moved(), host.update())
    host.refresh()
    return lay, transport, router, host, apps, lines, clock, ops, released


def last_state(lines):
    return json.loads([ln for ln in lines if ln.startswith("mirror ")][-1].partition(" ")[2])


def drag_safari_to_the_pc():
    world = make()
    lay, transport, router, host, apps, lines, clock, ops, released = world
    router.switch_to(lay.screen("mac", "main"), 2400, 300, "edge")
    router.on_button("left", True)
    apps.on_message("mac", "app_drag", {"window": SAFARI})
    assert apps.window_drag("mac")
    # Over the edge to the PC with the button held: cast, not a file drag.
    router.switch_to(lay.screen("pc", "main"), 1900, 300, "edge")
    return world


def test_dragging_a_window_over_the_edge_casts_it_and_puts_the_real_one_back():
    lay, transport, router, host, apps, lines, clock, ops, released = drag_safari_to_the_pc()
    sid, = apps.sessions
    assert is_app(sid)
    # The Mac's window goes back where it was before the drag, and it's captured.
    restore, = transport.of("app_do", "mac")
    assert restore["do"] == "restore" and restore["os"] == [100, 50, 800, 600] and restore["window"]["wid"] == 7
    capture, = transport.of("mirror", "mac")[-1]["captures"]
    assert capture["id"] == sid and capture["window"]["wid"] == 7 and capture["os"] == [100, 50, 800, 600]
    # No pointer in the picture (the mirror draws it); the Mac hides its own
    # window; no bigger than shown (in the pixels of the screen showing it:
    # this computer's, a Retina Mac's until it says, else its OS pixels).
    assert "cursor" not in capture and capture["pointer"] is True and capture["tiles"] is True
    px = 2 if apps.platform("pc") == "mac" else 1
    assert capture["hide"] is True and capture["use"] is False and capture["out"] == [800 * px, 600 * px]
    # Not a file: the Mac got its button up, no drag_out.
    assert not transport.of("drag_out") and {"button": "left", "pressed": False} in transport.of("button", "mac")
    # This computer shows it, under the pointer where it was grabbed, the
    # app's shape and no frame of its own (round Mac corners).
    show, = last_state(lines)["shows"]
    assert show["app"] is True and show["label"] == "Safari · MacBook"
    x, y, w, h = show["os"]
    assert (w, h) == (800, 600) and show["ratio"] == pytest.approx(800 / 600, abs=1e-4)
    assert (x, y) == (1920 - 800, 300 - 12) and show["carried"] is True and show["corner"] == 12


def test_the_mirror_follows_the_pointer_until_the_button_comes_up():
    lay, transport, router, host, apps, lines, clock, ops, released = drag_safari_to_the_pc()
    clock.t = 1.0
    router.on_pointer(900, 400)
    show, = last_state(lines)["shows"]
    assert show["os"][:2] == [900 - 400, 400 - 12]
    clock.t = 2.0
    router.on_pointer(900, 1000)  # never off the screen
    show, = last_state(lines)["shows"]
    assert show["os"][:2] == [500, 1080 - 600]
    router.on_button("left", False)
    show, = last_state(lines)["shows"]
    assert show["carried"] is False and apps.carrying is None


def put_down():
    world = drag_safari_to_the_pc()
    lay, transport, router, host, apps, lines, clock, ops, released = world
    clock.t = 1.0
    router.on_pointer(900, 400)  # the mirror is at (500, 388), 800 x 600
    router.on_button("left", False)
    return world


def test_the_pointer_goes_through_the_mirror_to_the_app_and_comes_back_out():
    lay, transport, router, host, apps, lines, clock, ops, released = put_down()
    sid, = apps.sessions
    # Into the mirror: input is on the Mac, at the same spot in Safari's window.
    router.on_pointer(900, 688)
    assert router.active_device == "mac"
    enter = transport.of("enter", "mac")[-1]
    assert (enter["x"], enter["y"]) == (100 + 400, 50 + 300) and enter["effect"] == "none"
    assert transport.of("app_do", "mac")[-1] == {"id": sid, "do": "raise", "window": apps.sessions[sid].window}
    assert last_state(lines)["shows"][0]["hover"] is True
    # Keys go to the Mac now.
    router.on_key("a", True)
    assert transport.of("key", "mac")[-1] == {"key": "a", "pressed": True}
    # Moving in it moves on the Mac (same size here: one for one).
    router.on_motion(10, 0)
    assert transport.of("move", "mac")[-1] == {"x": 510, "y": 350}
    # Out past its left side: back on the PC, just outside the mirror, in its
    # ring (where it's resized from, and its corners' handles show)...
    router.on_motion(-500, 0)
    assert router.active_device == "pc"
    assert router.x == 500 - LAND_PX and router.y == pytest.approx(688)
    # ...and on, past it (this computer's own mouse, here).
    router.on_pointer(480, 688)
    assert router.active_device == "pc" and router.x == 480 and apps.through is None


def test_going_in_and_out_of_the_mirror_keeps_mirror_mode_on_the_screen_showing_it():
    lay, transport, router, host, apps, lines, clock, ops, released = put_down()
    planned = []
    real = host._plan
    host._plan = lambda screen: (planned.append(screen), real(screen))
    pc = lay.screen("pc", "main")
    router.on_pointer(900, 688)  # into Safari: input is on the Mac...
    assert router.active_device == "mac" and apps.in_use() == pc
    router.on_motion(10, 0)
    router.on_motion(-500, 0)  # ...and out again
    assert router.active_device == "pc" and apps.in_use() is None
    # Mirror mode never thought the Mac's screen was the one in use.
    assert planned and set(planned) == {pc}


def test_looking_at_the_screen_with_the_mirror_doesnt_take_the_pointer_out():
    from curslink.gaze.zones import Target

    lay, transport, router, host, apps, lines, clock, ops, released = put_down()
    router.on_pointer(900, 688)
    clock.t = 10.0
    router._moved_at = -100
    router.on_gaze(Target("pc", "main"))
    assert router.active_device == "mac"


def test_the_mirrors_own_edge_is_for_resizing_it_not_the_app():
    lay, transport, router, host, apps, lines, clock, ops, released = put_down()
    router.on_pointer(500 + GRIP - 2, 700)
    assert router.active_device == "pc"


def test_dragging_the_apps_title_bar_in_the_mirror_moves_the_mirror():
    lay, transport, router, host, apps, lines, clock, ops, released = put_down()
    sid, = apps.sessions
    router.on_pointer(900, 400)  # Safari's title bar, in the mirror
    assert router.active_device == "mac"
    router.on_button("left", True)
    router.on_motion(8, 0)
    # The Mac says its window started moving: it's put back, and the pointer
    # carries the mirror on the PC instead.
    apps.on_message("mac", "app_drag", {"window": dict(SAFARI, os=[108, 50, 800, 600])})
    assert router.active_device == "pc" and apps.carrying[0] == sid
    assert transport.of("app_do", "mac")[-1]["do"] == "restore"
    clock.t = 2.0
    router.on_pointer(1000, 600)
    show, = last_state(lines)["shows"]
    assert show["carried"] is True
    router.on_button("left", False)
    assert apps.carrying is None


def test_carrying_the_mirror_back_to_its_computer_ends_it():
    lay, transport, router, host, apps, lines, clock, ops, released = drag_safari_to_the_pc()
    router.switch_to(lay.screen("mac", "main"), 1930, 300, "edge")
    assert not apps.sessions
    assert transport.of("app_do", "mac")[-1]["do"] == "stop"
    assert last_state(lines)["shows"] == []


def test_frames_go_from_the_app_to_its_mirror_and_answers_back():
    lay, transport, router, host, apps, lines, clock, ops, released = drag_safari_to_the_pc()
    sid, = apps.sessions
    host.on_message("mac", "mirror_frame", {"id": sid, "jpg": "QUJD", "w": 800, "h": 600, "tile": [0, 0, 256, 256]})
    assert lines[-1].startswith("mirror_image ") and json.loads(lines[-1].partition(" ")[2])["tile"] == [0, 0, 256, 256]
    assert transport.of("mirror_ack", "mac") == [{"id": sid}]
    # Frames for an app that isn't cast (any more) are answered and dropped.
    host.on_message("mac", "mirror_frame", {"id": "app:mac:9:9", "jpg": "QUJD", "w": 1, "h": 1})
    assert transport.of("mirror_ack", "mac")[-1] == {"id": "app:mac:9:9"}


def test_the_app_closing_or_the_mirrors_x_ends_it():
    lay, transport, router, host, apps, lines, clock, ops, released = put_down()
    sid, = apps.sessions
    apps.on_message("mac", "app_window", {"id": sid, "gone": True})
    assert not apps.sessions
    lay, transport, router, host, apps, lines, clock, ops, released = put_down()
    sid, = apps.sessions
    # The mirror shows on this computer: its helper says it was closed.
    assert apps.local.helper_line('app_view {"id": "%s", "closed": true}' % sid)
    assert not apps.sessions


def test_the_app_resized_on_its_computer_keeps_the_mirrors_scale():
    lay, transport, router, host, apps, lines, clock, ops, released = put_down()
    sid, = apps.sessions
    apps.on_message("mac", "app_window", {"id": sid, "os": [100, 50, 1000, 500]})
    show, = last_state(lines)["shows"]
    assert show["os"][2:] == [1000, 500] and show["ratio"] == 2.0
    capture, = transport.of("mirror", "mac")[-1]["captures"]
    assert capture["os"] == [100, 50, 1000, 500]


def test_a_window_parked_out_of_sight_isnt_taken_for_moved():
    lay, transport, router, host, apps, lines, clock, ops, released = put_down()
    sid, = apps.sessions
    before = apps.sessions[sid].rect
    # An older Mac reports its hidden window in the far corner: it hasn't
    # moved, or the pointer going into the mirror would land in that corner.
    apps.on_message("mac", "app_window", {"id": sid, "os": [1439, 899, 800, 600]})
    assert apps.sessions[sid].rect == before


def test_a_parked_window_is_reported_at_home_on_its_way_there_too():
    class Parks(Ops):
        def parks(self):
            return True

        def window_rect(self, win):
            return [1439, 899, 800, 600]  # still in the corner

    ops = Parks()
    sent = []
    device = AppMirrorDevice(lambda t, **d: sent.append((t, d)), now, lambda d, f: None, ops)
    win = dict(SAFARI, os=[100, 50, 800, 600])
    capture = {"id": "app:mac:7:1", "window": win, "os": [100, 50, 800, 600], "hide": True}
    for use in (False, True):  # hidden, then in use (being moved home)
        device.on_captures([dict(capture, use=use)])
        assert device._rect(win) == [100, 50, 800, 600]


def test_resizing_the_mirror_where_its_shown_resizes_the_app():
    lay, transport, router, host, apps, lines, clock, ops, released = put_down()
    sid, = apps.sessions
    # Made wider and shorter where it's shown: any shape.
    apps.local.helper_line('app_view {"id": "%s", "os": [0, 0, 1000, 400]}' % sid)
    assert apps.sessions[sid].view == (0, 0, 1000, 400)
    # The app itself is asked to be that size (it lays itself out again).
    resize = transport.of("app_do", "mac")[-1]
    assert resize["do"] == "resize" and resize["size"] == [1000, 400]
    # The Mac says it is (or the nearest it'll go): the mirror follows it.
    apps.on_message("mac", "app_window", {"id": sid, "os": [100, 50, 1000, 420]})
    assert apps.sessions[sid].view[2:] == (1000, 420)
    router.on_pointer(500, 210)  # the middle of the mirror
    enter = transport.of("enter", "mac")[-1]
    assert (enter["x"], enter["y"]) == (600, 260)
    router.on_motion(10, 10)  # one for one
    assert transport.of("move", "mac")[-1] == {"x": 610, "y": 270}


def test_an_app_is_shown_at_its_own_size_in_the_scaling_of_the_screen_its_on():
    lay, transport, router, host, apps, lines, clock, ops, released = make()
    apps.platform = lambda device_id: "windows"  # two PCs, here
    # A window on a 150% screen: 1200 x 900 pixels, 800 x 600 of its own.
    router.switch_to(lay.screen("mac", "main"), 2400, 300, "edge")
    router.on_button("left", True)
    apps.on_message("mac", "app_drag", {"window": dict(SAFARI, os=[100, 50, 1200, 900], start=[100, 50, 1200, 900], scale=1.5)})
    router.switch_to(lay.screen("pc", "main"), 1900, 300, "edge")
    sid, = apps.sessions
    assert apps.sessions[sid].view[2:] == (800, 600)  # this screen's at 100% (until it says)
    # It says it's at 125%: 1000 x 750 of its pixels.
    router.on_button("left", False)
    apps.local.helper_line('app_view {"id": "%s", "dpr": 1.25}' % sid)
    assert apps.sessions[sid].view[2:] == (1000, 750)
    capture, = transport.of("mirror", "mac")[-1]["captures"]
    assert capture["out"] == [1000, 750]  # sent at the size shown, not the window's 1200 x 900


def test_the_app_in_use_streams_first_and_the_mirror_draws_its_pointer():
    lay, transport, router, host, apps, lines, clock, ops, released = put_down()
    sid, = apps.sessions
    capture, = transport.of("mirror", "mac")[-1]["captures"]
    assert capture["use"] is False and capture["fps"] == 30
    router.on_pointer(900, 688)  # into Safari
    capture, = transport.of("mirror", "mac")[-1]["captures"]
    assert capture["use"] is True and capture["fps"] == 60
    # Where it is in Safari goes to the mirror as it moves, as fractions.
    clock.t = 5.0
    router.on_motion(80, 60)
    pointer = json.loads([ln for ln in lines if ln.startswith("mirror_pointer ")][-1].partition(" ")[2])
    assert pointer == {"id": sid, "x": 0.6, "y": 0.6}
    router.on_motion(-900, 0)  # out
    pointer = json.loads([ln for ln in lines if ln.startswith("mirror_pointer ")][-1].partition(" ")[2])
    assert pointer == {"id": sid, "hide": True}


def test_the_app_is_hidden_on_its_own_computer_while_its_cast():
    lay, transport, router, host, apps, lines, clock, ops, released = make()
    router.on_button("left", True)
    apps.on_drag("pc", {"window": dict(SAFARI, app="Notepad", wid=99)})
    router.on_pointer(1919, 300)  # over the edge to the Mac: cast
    router.on_button("left", False)
    assert ops.hidden[-1] == (99, [100, 50, 800, 600], False)
    sid, = apps.sessions
    apps.stop(sid)
    assert ops.shown == [(99, [100, 50, 800, 600])]


def test_this_computers_own_window_cast_to_the_mac():
    lay, transport, router, host, apps, lines, clock, ops, released = make()
    router.on_button("left", True)
    apps.on_drag("pc", {"window": dict(SAFARI, app="Notepad", wid=99)})
    router.on_pointer(1919, 300)  # over the edge to the Mac
    assert router.active_device == "mac"
    sid, = apps.sessions
    assert released == [True]  # this computer's own drag was ended (Escape, let go)
    assert ops.moved and ops.moved[-1][1] == [100, 50, 800, 600]  # and the window put back
    show, = transport.of("mirror", "mac")[-1]["shows"]
    assert show["app"] and show["screen"] == "main" and "corner" in show
    capture, = last_state(lines)["captures"]
    assert capture["window"]["wid"] == 99


def test_older_agents_and_phones_without_app_mirror_never_get_one():
    lay = desk()
    transport = Transport(app=())
    lay, transport, router, host, apps, lines, clock, ops, released = make(lay, transport)
    router.on_button("left", True)
    apps.on_drag("pc", {"window": SAFARI})
    router.switch_to(lay.screen("mac", "main"), 1930, 300, "edge")
    assert not apps.sessions


def test_a_phones_app_is_cast_by_dragging_across_from_it():
    lay = desk(phone=True)
    transport = Transport(devices=("mac", "phone"), platforms={"phone": "Android 15"})
    lay, transport, router, host, apps, lines, clock, ops, released = make(lay, transport)
    router.switch_to(lay.screen("phone", "main"), 3500, 400, "edge")
    router.on_button("left", True)
    assert apps.window_drag("phone")
    router.switch_to(lay.screen("mac", "main"), 3300, 400, "edge")
    sid, = apps.sessions
    capture, = transport.of("mirror", "phone")[-1]["captures"]
    assert capture["os"] == [0, 0, 400, 860] and "window" not in capture
    show, = transport.of("mirror", "mac")[-1]["shows"]
    assert show["app"] and show["ratio"] == pytest.approx(400 / 860, abs=1e-4) and show["corner"] == 0
    assert not transport.of("app_do", "phone")


# -- the device side --------------------------------------------------------------------


def test_window_drag_watch_notices_a_window_moved_by_its_title_bar():
    calls = []
    rects = iter([[100, 50, 800, 600], [103, 50, 800, 600], [130, 60, 800, 600]])
    reports = []
    watch = WindowDragWatch(reports.append, now, lambda d, f: calls.append(f),
                            probe=lambda: dict(SAFARI, os=[100, 50, 800, 600]), rect=lambda w: next(rects))
    watch.on_button("left", True)
    for _ in range(3):
        calls.pop(0)()
    assert len(reports) == 1 and reports[0]["start"] == [100, 50, 800, 600] and reports[0]["os"] == [130, 60, 800, 600]
    watch.on_button("left", False)
    assert reports[-1] is None


def test_a_window_resized_is_not_dragged():
    calls, reports = [], []
    rects = iter([[100, 50, 900, 600], [100, 50, 1000, 600]])
    watch = WindowDragWatch(reports.append, now, lambda d, f: calls.append(f),
                            probe=lambda: dict(SAFARI, os=[100, 50, 800, 600]), rect=lambda w: next(rects))
    watch.on_button("left", True)
    calls.pop(0)()
    calls.pop(0)()
    watch.on_button("left", False)
    assert reports == []


def test_cast_watch_reports_moves_and_a_closed_window():
    calls, reports = [], []
    rects = {"a": [1, 2, 3, 4]}
    watch = CastWatch(lambda i, r: reports.append((i, r)), now, lambda d, f: calls.append(f),
                      rect=lambda w: rects.get(w["k"]))
    watch.set({"a": {"k": "a", "os": [1, 2, 3, 4]}})
    calls.pop(0)()
    assert reports == []
    rects["a"] = [5, 2, 3, 4]
    calls.pop(0)()
    assert reports == [("a", [5, 2, 3, 4])]
    rects.clear()
    calls.pop(0)()
    assert reports[-1] == ("a", None)


def test_device_does_what_the_host_asks():
    sent, ops = [], Ops()
    dev = AppMirrorDevice(lambda t, **d: sent.append((t, d)), now, lambda d, f: None, ops)
    win = {"wid": 7, "pid": 41}
    dev.handle({"id": "app:x", "do": "restore", "os": [1, 2, 3, 4], "window": win})
    dev.handle({"id": "app:x", "do": "raise", "window": win})
    assert ops.moved == [(win, [1, 2, 3, 4])] and ops.raised == [win]
    assert not dev.helper_line("mirror_frame {}")


def test_app_frames_are_checked():
    assert decode(encode("app_do", id="app:x", do="raise")).get("do") == "raise"
    assert decode(encode("app_drag", window=SAFARI)).get("window")["wid"] == 7
    assert decode(encode("app_view", id="app:x", os=[1, 2, 3, 4]))
    # Resizing an app from its mirror: an agent used to drop the link on it.
    assert decode(encode("app_do", id="app:x", do="resize", size=[800, 600])).get("size") == [800, 600]
    assert decode(encode("app_icon", id="app:x", png="iVBO")).get("png") == "iVBO"
    for bad in (dict(t="app_do", id="app:x", do="explode"), dict(t="app_view", id="app:x", os=[1, 2]),
                dict(t="app_drag"), dict(t="app_window", id=""), dict(t="app_icon", id="app:x")):
        with pytest.raises(ProtocolError):
            decode(json.dumps(bad).encode())


def test_config_has_app_mirror_on_unless_turned_off():
    from curslink.config import agent_config, host_config

    raw = {"host": {"device_id": "pc"}, "secret": "s",
           "devices": [{"id": "pc", "screens": [{"x": 0, "y": 0, "width": 100, "height": 100}]}]}
    assert host_config(raw).app_mirror is True
    assert host_config(dict(raw, app_mirror={"enabled": False})).app_mirror is False
    assert agent_config({"agent": {"device_id": "a", "host": "h", "app_mirror": False}, "secret": "s"}).app_mirror is False


def test_only_app_mirror_on_an_agent_without_mirror_mode():
    lines = []
    peer = MirrorPeer(lines.append, lambda t, **d: None, only_apps=True)
    peer.handle("mirror", {"captures": [{"id": "a:b>c:d", "os": [0, 0, 1, 1]}, {"id": "app:x", "os": [0, 0, 1, 1]}],
                           "shows": [{"id": "a:b>c:d"}]})
    state = json.loads(lines[-1].partition(" ")[2])
    assert [c["id"] for c in state["captures"]] == ["app:x"] and state["shows"] == []


# -- the helper (Qt, offscreen) ---------------------------------------------------------------


@pytest.fixture
def qapp():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    pytest.importorskip("PySide6.QtWidgets")
    try:
        from PySide6.QtWidgets import QApplication
    except ImportError as exc:
        pytest.skip(f"Qt can't load: {exc}")
    return QApplication.instance() or QApplication(["curslink-test"])


def png(w, h, color="white"):
    from PySide6.QtCore import QBuffer, QByteArray, QIODevice
    from PySide6.QtGui import QColor, QImage

    img = QImage(w, h, QImage.Format.Format_ARGB32)
    img.fill(QColor(color))
    data = QByteArray()
    buf = QBuffer(data)
    buf.open(QIODevice.OpenModeFlag.WriteOnly)
    img.save(buf, "PNG")
    return base64.b64encode(bytes(data.data())).decode()


def jpeg(w, h, color="tomato"):
    from PySide6.QtCore import QBuffer, QByteArray, QIODevice
    from PySide6.QtGui import QColor, QImage

    img = QImage(w, h, QImage.Format.Format_RGB32)
    img.fill(QColor(color))
    data = QByteArray()
    buf = QBuffer(data)
    buf.open(QIODevice.OpenModeFlag.WriteOnly)
    img.save(buf, "JPG", 80)
    return base64.b64encode(bytes(data.data())).decode()


def test_helper_shows_an_app_in_a_window_of_its_own_and_resizes_it(qapp):
    from PySide6.QtCore import QPointF

    from curslink.appmirror import RING
    from curslink.appmirror.helper import AppWindow
    from curslink.mirror.helper import MirrorHelper

    out = []
    helper = MirrorHelper(out.append)
    show = {"id": "app:mac:7:1", "app": True, "screen": "main", "os": [100, 100, 400, 300], "ratio": 4 / 3,
            "label": "Safari · MacBook", "edges": [], "hover": True, "corner": 10, "raise": 0}
    helper.handle("mirror " + json.dumps({"captures": [], "shows": [show]}))
    window = helper.windows[show["id"]]
    assert isinstance(window, AppWindow) and not window.isVisible()
    helper.handle("mirror_image " + json.dumps({"id": show["id"], "jpg": jpeg(40, 30), "w": 40, "h": 30}))
    qapp.processEvents()
    assert window.isVisible() and window.tag is not None and window.tag.isVisible()
    window.grab()
    window.tag.grab()

    class Ev:
        def __init__(self, local, glob):
            self._l, self._g = QPointF(*local), QPointF(*glob)

        def position(self):
            return self._l

        def globalPosition(self):
            return self._g

    # Round it, outside the picture, its ring: the bottom-right corner,
    # dragged out, makes it bigger (any shape: the app follows).
    ring = window.ring
    assert ring is not None and ring.isVisible()
    assert ring.geometry() == window.geometry().adjusted(-RING, -RING, RING, RING)
    assert ring.sides(QPointF(RING + 404, RING + 304)) == "rb"
    assert ring.sides(QPointF(RING + 200, RING - 6)) == "t"
    assert ring.sides(QPointF(RING + 1, RING + 1)) == "lt"  # a rounded-off corner of the mirror
    ring.mousePressEvent(Ev((RING + 404, RING + 304), (504, 404)))
    ring.mouseMoveEvent(Ev((RING + 504, RING + 304), (604, 404)))
    assert window.geometry().width() == 500 and ring.geometry() == window.geometry().adjusted(-RING, -RING, RING, RING)
    ring.grab()  # paints, the corner's handle and all
    ring.mouseReleaseEvent(Ev((RING + 504, RING + 304), (604, 404)))
    view = json.loads([ln for ln in out if ln.startswith("app_view ")][-1].partition(" ")[2])
    assert view["id"] == show["id"] and view["os"][2:] == [500, 300]
    # The tag's x closes it.
    tag = window.tag
    box = tag.close_box().center()
    tag.mousePressEvent(Ev((box.x(), box.y()), (0, 0)))
    assert json.loads(out[-1].partition(" ")[2]) == {"id": show["id"], "closed": True}
    # The host's next state drops it, after Qt has deleted it: no error.
    from PySide6.QtCore import QCoreApplication, QEvent

    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    # A state that still lists it, and a picture for it, before the host has heard.
    helper.handle("mirror " + json.dumps({"captures": [], "shows": [show]}))
    helper.handle("mirror_image " + json.dumps({"id": show["id"], "jpg": jpeg(40, 30), "w": 40, "h": 30}))
    assert show["id"] not in helper.windows
    other = {**show, "id": "app:mac:8:2"}
    helper.handle("mirror " + json.dumps({"captures": [], "shows": [show, other]}))
    assert list(helper.windows) == [other["id"]]  # the rest of that state still applied
    helper.handle("mirror " + json.dumps({"captures": [], "shows": [other]}))
    assert list(helper.windows) == [other["id"]] and not helper.closed


def test_helper_brings_the_mirror_in_front_of_other_apps_and_over_them_while_carried(qapp, monkeypatch):
    from curslink.appmirror import helper as apphelper
    from curslink.mirror.helper import MirrorHelper

    fronts = []
    monkeypatch.setattr(apphelper, "bring_to_front",
                        lambda w, above_all, over_menus=False: fronts.append(above_all)
                        if isinstance(w, apphelper.AppWindow) else None)
    helper = MirrorHelper([].append)
    show = {"id": "app:mac:7:1", "app": True, "screen": "main", "os": [100, 100, 400, 300], "ratio": 4 / 3,
            "label": "Safari · MacBook", "edges": [], "hover": False, "corner": 10, "raise": 0, "carried": True}
    helper.handle("mirror " + json.dumps({"captures": [], "shows": [show]}))
    helper.handle("mirror_image " + json.dumps({"id": show["id"], "jpg": jpeg(40, 30), "w": 40, "h": 30}))
    assert fronts == [True]  # shown while carried: over every window
    helper.handle("mirror " + json.dumps({"captures": [], "shows": [dict(show, carried=False)]}))
    assert fronts == [True, False]  # put down: an ordinary window, at the front
    helper.handle("mirror " + json.dumps({"captures": [], "shows": [{**show, "carried": False, "raise": 1}]}))
    assert fronts == [True, False, False]  # pointed into: to the front again


def test_helper_captures_a_window(qapp, monkeypatch):
    from PySide6.QtGui import QColor, QImage

    from curslink.appmirror import helper as apphelper
    from curslink.appmirror import windows
    from curslink.mirror.helper import MirrorHelper

    out = []
    monkeypatch.setattr(apphelper.sys, "platform", "win32")
    img = QImage(300, 200, QImage.Format.Format_RGB32)
    img.fill(QColor("steelblue"))
    monkeypatch.setattr(apphelper, "win_print_window", lambda hwnd: (img, [10, 10, 300, 200]))
    monkeypatch.setattr(windows, "window_rect", lambda win: [10, 10, 300, 200])
    helper = MirrorHelper(out.append)
    capture = {"id": "app:pc:9:1", "screen": "main", "os": [10, 10, 300, 200], "fps": 30, "window": {"wid": 9},
               "tiles": True, "quality": "high", "key": 1}
    helper.handle("mirror " + json.dumps({"captures": [capture], "shows": []}))
    assert type(helper.captures[capture["id"]]).__name__ == "AppCapture"
    helper.captures[capture["id"]].grab()
    frames = [json.loads(ln.partition(" ")[2]) for ln in out if ln.startswith("mirror_frame ")]
    assert frames and all(f["id"] == capture["id"] and f["w"] == 300 and f["h"] == 200 for f in frames)
    helper.handle('mirror {"captures": [], "shows": []}')
    assert not helper.captures


def test_helper_sends_the_app_at_the_size_shown_with_its_menus_on_it(qapp, monkeypatch):
    from PySide6.QtGui import QColor, QImage

    from curslink.appmirror import helper as apphelper
    from curslink.appmirror import windows
    from curslink.mirror.helper import MirrorHelper

    out = []
    monkeypatch.setattr(apphelper.sys, "platform", "win32")
    img = QImage(600, 400, QImage.Format.Format_RGB32)
    img.fill(QColor("steelblue"))
    monkeypatch.setattr(apphelper, "win_print_window", lambda hwnd: (img, [10, 10, 600, 400]))
    monkeypatch.setattr(windows, "window_rect", lambda win: [10, 10, 600, 400])
    # A context menu open on it, 100 x 100 at (310, 210): its own window.
    monkeypatch.setattr(windows, "popups", lambda win: [(77, [310, 210, 100, 100])])
    menu = QImage(100, 100, QImage.Format.Format_RGB32)
    menu.fill(QColor("white"))
    monkeypatch.setattr(apphelper, "popup_image", lambda wid, rect: menu)
    helper = MirrorHelper(out.append)
    capture = {"id": "app:pc:9:1", "screen": "main", "os": [10, 10, 600, 400], "fps": 30, "window": {"wid": 9},
               "key": 1, "use": True, "out": [300, 200]}
    helper.handle("mirror " + json.dumps({"captures": [capture], "shows": []}))
    got = helper.captures[capture["id"]].image()
    # Half size, as the mirror shows it, with the menu where it is on the window.
    assert (got.width(), got.height()) == (300, 200)
    assert QColor(got.pixel(175, 125)).name() == "#ffffff"
    assert QColor(got.pixel(50, 50)).name() == QColor("steelblue").name()
    helper.handle('mirror {"captures": [], "shows": []}')


def test_helper_mirror_says_its_screens_scaling_and_draws_the_pointer(qapp):
    from curslink.mirror.helper import MirrorHelper

    out = []
    helper = MirrorHelper(out.append)
    show = {"id": "app:mac:7:1", "app": True, "screen": "main", "os": [100, 100, 400, 300], "ratio": 4 / 3,
            "label": "Safari · MacBook", "edges": [], "hover": False, "corner": 10, "raise": 0}
    helper.handle("mirror " + json.dumps({"captures": [], "shows": [show]}))
    helper.handle("mirror_image " + json.dumps({"id": show["id"], "jpg": jpeg(40, 30), "w": 40, "h": 30}))
    said = [json.loads(ln.partition(" ")[2]) for ln in out if ln.startswith("app_view ")]
    assert said and said[0]["id"] == show["id"] and said[0]["dpr"] > 0
    helper.handle("mirror_pointer " + json.dumps({"id": show["id"], "x": 0.5, "y": 0.5}))
    window = helper.windows[show["id"]]
    assert window.pointer == (0.5, 0.5)
    window.grab()  # paints, pointer and all


def test_helper_mirror_is_solid_and_its_tag_fills_the_screen_with_it_and_back(qapp):
    from PySide6.QtCore import QPointF, Qt

    from curslink.mirror.helper import MirrorHelper

    out = []
    helper = MirrorHelper(out.append)
    show = {"id": "app:mac:7:1", "app": True, "screen": "main", "os": [100, 100, 400, 300], "ratio": 4 / 3,
            "label": "Safari · MacBook", "edges": [], "hover": True, "corner": 10, "raise": 0, "carried": True}
    helper.handle("mirror " + json.dumps({"captures": [], "shows": [show]}))
    helper.handle("mirror_image " + json.dumps({"id": show["id"], "jpg": jpeg(40, 30), "w": 40, "h": 30}))
    window = helper.windows[show["id"]]
    # Never see-through, carried or not, nor while it first shows (on a Mac
    # it's clear only round its rounded corners: a mask doesn't cut a solid one).
    assert window.testAttribute(Qt.WidgetAttribute.WA_TranslucentBackground) == (sys.platform == "darwin")
    assert window.windowOpacity() == 1.0
    window.tick()
    assert window.windowOpacity() == 1.0
    helper.handle("mirror " + json.dumps({"captures": [], "shows": [{**show, "carried": False}]}))
    tag = window.tag
    assert tag is not None and tag.isVisible()

    class Ev:
        def __init__(self, local):
            self._l = QPointF(*local)

        def position(self):
            return self._l

    # Its tag's box: the host is asked to make it fill its screen (it sizes
    # the app for that screen's shape)...
    box = tag.max_box().center()
    tag.mousePressEvent(Ev((box.x(), box.y())))
    view = json.loads([ln for ln in out if ln.startswith("app_view ")][-1].partition(" ")[2])
    assert view == {"id": show["id"], "full": True}
    # ...and says so: all of the screen, no rounded corners, no ring.
    whole = window.screen().geometry()
    helper.handle("mirror " + json.dumps({"captures": [], "shows": [{
        **show, "carried": False, "full": True, "corner": 0,
        "os": [whole.x(), whole.y(), whole.width(), whole.height()]}]}))
    assert window.geometry() == whole and window.maxed is not None and not window.ring.isVisible()
    helper.handle("mirror " + json.dumps({"captures": [], "shows": [{**show, "carried": False}]}))
    assert window.geometry().width() == 400 and window.maxed is None and window.ring.isVisible()


def test_a_mirror_against_its_screens_edge_keeps_the_pointer_in_the_app_there():
    lay, transport, router, host, apps, lines, clock, ops, released = drag_safari_to_the_pc()
    clock.t = 1.0
    router.on_pointer(900, 0)  # carried to the top of the PC's screen
    router.on_button("left", False)
    sid, = apps.sessions
    assert apps.sessions[sid].view[1] == 0
    router.on_pointer(900, 300)  # into Safari
    assert router.active_device == "mac"
    clock.t = 2.0
    # Up past its top (the tabs, the title bar): it stays in Safari, at its top.
    router.on_motion(0, -400)
    assert router.active_device == "mac" and apps.through == sid
    assert transport.of("move", "mac")[-1]["y"] == 50
    # Its left side has room beside it: out that way, as ever.
    router.on_motion(-500, 0)
    assert router.active_device == "pc"


# ------------------------------------------------- feeling like the app's own window
def test_an_older_agent_never_gets_a_resize_its_mirror_scales_instead():
    world = make(transport=Transport(native=()))
    lay, transport, router, host, apps, lines, clock, ops, released = world
    router.switch_to(lay.screen("mac", "main"), 2400, 300, "edge")
    router.on_button("left", True)
    apps.on_message("mac", "app_drag", {"window": SAFARI})
    router.switch_to(lay.screen("pc", "main"), 1900, 300, "edge")
    clock.t = 1.0
    router.on_pointer(900, 400)
    router.on_button("left", False)
    sid, = apps.sessions
    apps.local.helper_line('app_view {"id": "%s", "os": [0, 0, 1000, 750]}' % sid)
    assert not [d for d in transport.of("app_do", "mac") if d["do"] == "resize"]
    assert apps.sessions[sid].k == pytest.approx(1000 / 800)


def test_a_mirror_made_bigger_than_the_apps_own_screen_keeps_the_app_on_it():
    lay, transport, router, host, apps, lines, clock, ops, released = put_down()
    sid, = apps.sessions
    # The Mac's screen is 1440 x 900 (and its menu bar): the app can't be
    # 1800 x 1000 there, so it's as big as fits (the same shape), the
    # picture scaled up.
    apps.local.helper_line('app_view {"id": "%s", "os": [0, 0, 1800, 1000]}' % sid)
    resize = transport.of("app_do", "mac")[-1]
    assert resize["do"] == "resize" and resize["size"] == [1440, 800]
    s = apps.sessions[sid]
    assert s.k == pytest.approx(1800 / 1440)
    # The Mac says it is: the mirror stays the size it was made.
    apps.on_message("mac", "app_window", {"id": sid, "os": [1940, 25] + resize["size"]})
    assert apps.sessions[sid].view[2:] == pytest.approx((1800, 1000), abs=2)


def test_the_apps_full_screen_button_fills_the_screen_its_mirror_is_on():
    lay, transport, router, host, apps, lines, clock, ops, released = put_down()
    sid, = apps.sessions
    before = apps.sessions[sid].view
    apps.on_message("mac", "app_window", {"id": sid, "full": True})
    s = apps.sessions[sid]
    assert s.view == (0, 0, 1920, 1080) and s.full is not None
    show, = last_state(lines)["shows"]
    assert show["full"] is True and show["corner"] == 0 and show["os"] == [0, 0, 1920, 1080]
    # The app takes the PC's screen's shape, as big as fits on the Mac.
    resize = transport.of("app_do", "mac")[-1]
    assert resize["do"] == "resize"
    w, h = resize["size"]
    assert w / h == pytest.approx(1920 / 1080, rel=0.01) and h <= 900 - 25
    # The Mac says it's that size: the mirror still fills the screen.
    apps.on_message("mac", "app_window", {"id": sid, "os": [1940, 25, w, h]})
    assert apps.sessions[sid].view == (0, 0, 1920, 1080)
    # Pressed again (or its tag's box): back as it was, the app its old size.
    apps.local.helper_line('app_view {"id": "%s", "full": true}' % sid)
    assert apps.sessions[sid].view == before and apps.sessions[sid].full is None
    assert transport.of("app_do", "mac")[-1]["size"] == [800, 600]


def test_out_of_a_full_screen_app_through_an_edge_that_leads_on():
    lay, transport, router, host, apps, lines, clock, ops, released = put_down()
    sid, = apps.sessions
    apps.on_message("mac", "app_window", {"id": sid, "full": True})
    s = apps.sessions[sid]
    apps.on_message("mac", "app_window", {"id": sid, "os": [1940, 25, 1555, 875]})
    router.on_pointer(900, 500)  # into the app, filling the PC's screen
    assert router.active_device == "mac" and apps.through == sid
    clock.t = 2.0
    # Up past its top: the PC's top edge leads nowhere, so it stays in the app.
    router.on_motion(0, -2000)
    assert router.active_device == "mac" and apps.through == sid
    # Left past its left side: nothing there either.
    router.on_motion(-3000, 0)
    assert router.active_device == "mac" and apps.through == sid
    # Right past its right side: the PC's right edge leads to the Mac.
    router.on_motion(4000, 0)
    assert apps.through is None and router.active_device == "mac" and router.screen == lay.screen("mac", "main")
    assert s.full is not None


def test_coming_out_of_an_app_lands_in_its_ring_and_a_press_there_resizes_it():
    lay, transport, router, host, apps, lines, clock, ops, released = put_down()
    sid, = apps.sessions
    router.on_pointer(900, 688)
    clock.t = 2.0
    router.on_motion(-500, 0)  # out by its left side
    assert router.active_device == "pc" and router.x == 500 - LAND_PX
    # Pressed there (on the ring), and dragged in: resizing it, not into the app.
    router.on_button("left", True)
    assert apps.resizing == sid
    router.on_pointer(560, 688)
    assert router.active_device == "pc" and apps.through is None
    router.on_button("left", False)
    assert apps.resizing is None
    router.on_pointer(570, 688)
    assert router.active_device == "mac"


def test_in_the_app_past_its_screens_edge_the_pointer_stays_in_the_app():
    lay, transport, router, host, apps, lines, clock, ops, released = put_down()
    sid, = apps.sessions
    s = apps.sessions[sid]
    # Safari's window reaches past the bottom of the Mac's screen.
    apps.on_message("mac", "app_window", {"id": sid, "os": [100, 500, 800, 600]})
    router.on_pointer(900, 688)
    assert router.active_device == "mac"
    clock.t = 2.0
    router.on_motion(0, 250)  # below the Mac's screen, still in Safari
    assert router.active_device == "mac" and apps.through == sid
    assert transport.of("move", "mac")[-1]["y"] > 900
    assert s.rect[1] + s.rect[3] > 900


def test_a_window_used_through_its_mirror_is_brought_all_onto_its_screen():
    sent, ops = [], Ops()
    ops.fit_on_screen = lambda r: [max(0, r[0]), max(25, r[1]), min(r[2], 1440), min(r[3], 875)]
    dev = AppMirrorDevice(lambda t, **d: sent.append((t, d)), now, lambda d, f: None, ops)
    win = {"wid": 7, "pid": 41}
    capture = {"id": "app:x", "window": win, "os": [-200, 600, 800, 600], "hide": True, "use": False}
    dev.on_captures([capture])
    assert ops.hidden[-1] == (7, [-200, 600, 800, 600], False)
    dev.on_captures([dict(capture, use=True)])
    # Used: all of it on its screen, so the pointer reaches all of it.
    assert ops.moved[-1][1] == [0, 600, 800, 600] and ops.hidden[-1] == (7, [0, 600, 800, 600], True)
    # Resized bigger than its screen: as big as fits.
    ops.window_rect = lambda w: [0, 600, 800, 600]
    dev.handle({"id": "app:x", "do": "resize", "size": [2000, 1200], "window": win})
    assert ops.moved[-1][1] == [0, 600, 1440, 875]


def test_the_full_screen_button_is_kept_from_the_app_and_fills_the_mirrors_screen():
    sent, ops = [], Ops()
    win = {"wid": 7, "pid": 41}
    ops.window_rect = lambda w: [100, 50, 800, 600]
    ops.cursor = lambda: (160, 62)
    hits = {"what": "full"}
    ops.title_hit = lambda w, x, y: hits["what"]
    dev = AppMirrorDevice(lambda t, **d: sent.append((t, d)), now, lambda d, f: None, ops)
    clock = Clock()
    dev.clock = clock
    dev.on_captures([{"id": "app:x", "window": win, "os": [100, 50, 800, 600], "hide": True, "use": True}])
    assert dev.intercept("left", True) and dev.intercept("left", False)
    assert sent[-1] == ("app_window", {"id": "app:x", "full": True})
    # A press anywhere else in it goes to the app.
    hits["what"] = None
    assert not dev.intercept("left", True) and not dev.intercept("left", False)
    # A double click on its title bar: the first press goes to the app, the second doesn't.
    hits["what"] = "title"
    n = len(sent)
    assert not dev.intercept("left", True) and not dev.intercept("left", False)
    clock.t = 0.2
    assert dev.intercept("left", True) and dev.intercept("left", False)
    assert len(sent) == n + 1 and sent[-1][1]["full"] is True
    # Slower than a double click: two clicks, both the app's.
    clock.t = 5.0
    assert not dev.intercept("left", True)
    clock.t = 6.0
    assert not dev.intercept("left", True)
    # A right press, or nothing cast: never kept.
    assert not dev.intercept("right", True)
    dev.on_captures([])
    assert not dev.intercept("left", True)


def test_an_app_gone_full_screen_where_it_runs_is_put_back_and_its_mirror_fills_instead():
    calls, reports, fulls = [], [], []
    went = {"a": False}
    watch = CastWatch(lambda i, r: reports.append((i, r)), now, lambda d, f: calls.append(f),
                      rect=lambda w: [1, 2, 3, 4], full=lambda w: went[w["k"]], on_full=fulls.append)
    watch.set({"a": {"k": "a", "os": [1, 2, 3, 4]}})
    went["a"] = True
    calls.pop(0)()
    assert fulls == ["a"] and reports == []


def test_a_mirror_nobody_is_near_shows_a_still():
    lay, transport, router, host, apps, lines, clock, ops, released = put_down()
    capture, = transport.of("mirror", "mac")[-1]["captures"]
    assert capture["fps"] == 30  # just put down, the pointer on it
    router.on_pointer(100, 100)  # far away
    capture, = transport.of("mirror", "mac")[-1]["captures"]
    assert capture["fps"] == 1
    router.on_pointer(480, 600)  # near it: live again
    capture, = transport.of("mirror", "mac")[-1]["captures"]
    assert capture["fps"] == 30


def test_the_apps_icon_goes_to_the_device_showing_its_mirror():
    lay, transport, router, host, apps, lines, clock, ops, released = put_down()
    sid, = apps.sessions
    apps.on_message("mac", "app_window", {"id": sid, "icon": "iVBORw0KGgo="})
    # The PC shows it: its own helper gets it.
    icon = json.loads([ln for ln in lines if ln.startswith("app_icon ")][-1].partition(" ")[2])
    assert icon == {"id": sid, "png": "iVBORw0KGgo="}
    # A device's helper says what icon its app has: on to the host.
    sent = []
    dev = AppMirrorDevice(lambda t, **d: sent.append((t, d)), now, lambda d, f: None, Ops())
    assert dev.helper_line('app_icon {"id": "app:x", "png": "iVBO"}')
    assert sent == [("app_window", {"id": "app:x", "icon": "iVBO"})]


def test_an_icon_and_the_pointers_look_reach_a_mirror_made_after_them(qapp):
    from curslink.mirror.helper import MirrorHelper

    helper = MirrorHelper([].append)
    show = {"id": "app:mac:7:1", "app": True, "screen": "main", "os": [100, 100, 400, 300], "ratio": 4 / 3,
            "label": "Safari · MacBook", "edges": [], "hover": False, "corner": 10, "raise": 0}
    helper.handle("app_icon " + json.dumps({"id": show["id"], "png": png(32, 32)}))
    helper.handle("mirror_shape " + json.dumps({"id": show["id"], "png": png(16, 24), "w": 16, "h": 24,
                                                "hx": 2, "hy": 3, "cw": 400}))
    helper.handle("mirror " + json.dumps({"captures": [], "shows": [show]}))
    window = helper.windows[show["id"]]
    assert window.icon_png and not window.windowIcon().isNull()
    assert window.shape is not None


def test_the_apps_cursor_is_a_little_window_of_its_own_where_the_host_says(qapp):
    from curslink.mirror.helper import MirrorHelper

    helper = MirrorHelper([].append)
    show = {"id": "app:mac:7:1", "app": True, "screen": "main", "os": [100, 100, 400, 300], "ratio": 4 / 3,
            "label": "Safari · MacBook", "edges": [], "hover": False, "corner": 10, "raise": 0}
    helper.handle("mirror " + json.dumps({"captures": [], "shows": [show]}))
    helper.handle("mirror_image " + json.dumps({"id": show["id"], "jpg": jpeg(40, 30), "w": 40, "h": 30}))
    window = helper.windows[show["id"]]
    helper.handle("mirror_shape " + json.dumps({"id": show["id"], "png": png(16, 24), "w": 16, "h": 24,
                                                "hx": 2, "hy": 3, "cw": 400}))
    helper.handle("mirror_pointer " + json.dumps({"id": show["id"], "x": 0.5, "y": 0.5}))
    sprite = window.sprite
    g = window.geometry()
    assert sprite.isVisible()
    assert (sprite.x() + 2, sprite.y() + 3) == (g.x() + 200, g.y() + 150)  # its hot spot on the spot
    sprite.grab()
    helper.handle("mirror_pointer " + json.dumps({"id": show["id"], "hide": True}))
    assert not sprite.isVisible()


def test_fit_on_screen_keeps_a_window_all_on_its_screen():
    from curslink.appmirror.windows import fit_on_screen

    screens = [{"x": 0, "y": 0, "width": 1440, "height": 900}, {"x": 1440, "y": 0, "width": 2560, "height": 1440}]
    assert fit_on_screen([100, 100, 800, 600], screens) == [100, 100, 800, 600]
    x, y, w, h = fit_on_screen([1000, 500, 800, 600], screens)  # mostly on the big one
    assert x >= 1440 and y + h <= 1440 and (w, h) == (800, 600)
    x, y, w, h = fit_on_screen([-300, 400, 3000, 2000], screens[:1])
    assert x >= 0 and x + w <= 1440 and y + h <= 900 and w == 1440


def test_a_capture_sends_its_apps_icon_once_small_enough_for_a_frame(qapp, monkeypatch):
    from curslink.appmirror import MAX_ICON
    from curslink.appmirror import helper as apphelper
    from curslink.mirror.helper import MirrorHelper

    out = []
    monkeypatch.setattr(apphelper, "process_path", lambda pid: "/Applications/Safari.app")
    sizes = []

    def icon(path, px):
        sizes.append(px)
        return "x" * (MAX_ICON + 1 if px > 64 else 100)

    monkeypatch.setattr(apphelper, "icon_png", icon)
    helper = MirrorHelper(out.append)
    capture = {"id": "app:mac:7:1", "screen": "main", "os": [10, 10, 300, 200], "fps": 30,
               "window": {"wid": 7, "pid": 41}, "key": 1}
    helper.handle("mirror " + json.dumps({"captures": [capture], "shows": []}))
    cap = helper.captures[capture["id"]]
    cap.send_icon()
    icons = [json.loads(ln.partition(" ")[2]) for ln in out if ln.startswith("app_icon ")]
    assert icons == [{"id": capture["id"], "png": "x" * 100}] and sizes == [128, 96, 64]
    helper.handle('mirror {"captures": [], "shows": []}')


def use_this_computers_own_app():
    """Notepad (on the PC, the host) cast to the Mac and put down there, the
    pointer then moved into its mirror: input is on the PC, in Notepad."""
    lay, transport, router, host, apps, lines, clock, ops, released = make()
    router.clock = clock
    router.capture.park_at = (960, 540)  # where the PC's cursor is held while input is away
    router.on_button("left", True)
    apps.on_drag("pc", {"window": dict(SAFARI, app="Notepad", wid=99)})
    router.on_pointer(1919, 300)  # over the edge to the Mac: cast
    router.on_button("left", False)
    sid, = apps.sessions
    s = apps.sessions[sid]
    vx, vy, vw, vh = s.view
    clock.t = 1.0
    router.on_motion(vx + vw / 2 - router.x, vy + vh / 2 - router.y)
    assert router.active_device == "pc" and apps.through == sid
    return lay, transport, router, apps, clock, s


def test_a_pc_cursor_report_from_before_it_was_put_in_the_app_is_dropped():
    # Kyle, 0.4.124: going into the mirror of one of the PC's own apps, the
    # first report was from the cursor still held in the middle of the screen:
    # out of the app at once, in again, out, every 10-30 ms.
    lay, transport, router, apps, clock, s = use_this_computers_own_app()
    landed = (router.x, router.y)
    clock.t = 1.05
    router.on_pointer(963, 541)  # the held cursor, moved a little: before the move to the app
    assert router.active_device == "pc" and apps.through == s.id
    assert (router.x, router.y) == landed
    router.on_pointer(landed[0] + 3, landed[1] + 2)  # the real cursor, where it was put
    assert (router.x, router.y) == (landed[0] + 3, landed[1] + 2) and apps.through == s.id
    clock.t = 1.1
    router.on_pointer(963, 541)  # now that is the pointer moving
    assert apps.through != s.id or (router.x, router.y) == (963, 541)


def test_pushing_on_the_pc_screens_edge_in_its_own_app_comes_out_of_the_app():
    # The window against the PC's left edge: pushing on it there is moving out
    # of the app that way (into its mirror's ring on the Mac), not wherever the
    # PC screen's edge leads, nor stuck.
    lay, transport, router, apps, clock, s = use_this_computers_own_app()
    s.rect = (0.0, s.rect[1], s.rect[2], s.rect[3])
    s.view = (2200, 288, 800, 600)  # the mirror clear of the Mac's edges
    clock.t = 2.0
    router.on_pointer(0, s.rect[1] + 100)
    assert router.active_device == "mac" and apps.through is None
    vx = s.view[0]
    assert router.x == vx - LAND_PX


def test_a_cast_window_minimised_where_it_runs_is_put_back_and_not_taken_for_closed():
    calls, reports, put_back = [], [], []
    rects = {"a": [1, 2, 3, 4]}
    clock = Clock()

    def unmin(w, r):
        if r is None or r[0] <= -30000:
            put_back.append(w["k"])
            return True
        return False

    watch = CastWatch(lambda i, r: reports.append((i, r)), now, lambda d, f: calls.append(f),
                      rect=lambda w: rects.get(w["k"]), unmin=unmin, clock=clock)
    watch.set({"a": {"k": "a", "os": [1, 2, 3, 4]}})
    calls.pop(0)()
    rects["a"] = [-32000, -32000, 160, 28]  # Windows: minimised
    calls.pop(0)()
    assert put_back == ["a"] and reports == []
    unmin_off = watch.unmin
    watch.unmin = lambda w, r: False
    rects.clear()  # still on its way back (macOS: not on screen yet)
    clock.t = 1.0
    calls.pop(0)()
    assert reports == []
    rects["a"] = [1, 2, 3, 4]
    calls.pop(0)()
    assert reports == []  # back where it was
    watch.unmin = unmin_off


def test_the_device_puts_back_a_minimised_cast_window():
    ops = Ops()
    ops.undo_minimise = lambda w: True
    dev = AppMirrorDevice(lambda t, **d: None, now, lambda d, f: None, ops)
    assert dev._undo_min({"wid": 1}, [-32000, -32000, 160, 28])
    ops.window_rect = lambda w: [10, 10, 100, 100]
    assert not dev._undo_min({"wid": 1}, [10, 10, 100, 100])
    # A parked Mac window is reported at home: where it really is says.
    ops.window_rect = lambda w: None
    assert dev._undo_min({"wid": 1}, [10, 10, 100, 100])


def test_ring_look_while_the_pointer_is_in_the_app_shows_no_handle(qapp):
    # The ring's timer asks where the pointer is 30 times a second; while it's
    # in the app (the mirror has a pointer) and nothing is being dragged, that
    # used to raise TypeError ('NoneType' object is not subscriptable) on
    # every tick, flooding the helper's stderr (0.4.124/0.4.125 on a Mac).
    from curslink.mirror.helper import MirrorHelper

    helper = MirrorHelper(lambda line: None)
    show = {"id": "app:pc:7:1", "app": True, "screen": "main", "os": [100, 100, 400, 300], "ratio": 4 / 3,
            "label": "Notepad · PC", "edges": [], "hover": True, "corner": 10, "raise": 0}
    helper.handle("mirror " + json.dumps({"captures": [], "shows": [show]}))
    window = helper.windows[show["id"]]
    ring = window.ring
    assert ring is not None
    window.pointer = (0.5, 0.5)
    ring.drag = None
    ring.near = "rb"
    ring.look()
    assert ring.near is None


def test_full_screen_leaves_the_menu_bar_dock_and_taskbar_and_lets_the_pointer_to_them():
    # Kyle, 0.4.125: a full screen mirror covered the Mac's menu bar and Dock.
    lay, transport, router, host, apps, lines, clock, ops, released = put_down()
    sid, = apps.sessions
    # The helper showing it says what part of its screen apps fill (no taskbar).
    apps.local.helper_line('app_view {"id": "%s", "work": [0, 0, 1920, 1040]}' % sid)
    apps.on_message("mac", "app_window", {"id": sid, "full": True})
    s = apps.sessions[sid]
    assert s.view == (0, 0, 1920, 1040)
    show, = last_state(lines)["shows"]
    assert show["os"] == [0, 0, 1920, 1040]
    # In the app, down past its bottom: onto the taskbar, not held in the app.
    router.on_pointer(900, 500)
    assert router.active_device == "mac" and apps.through == sid
    clock.t = 3.0
    router.on_motion(0, 2000)
    assert router.active_device == "pc" and apps.through is None and router.y >= 1040


def test_a_screen_said_after_it_filled_it_fits_the_mirror_to_its_work_area():
    lay, transport, router, host, apps, lines, clock, ops, released = put_down()
    sid, = apps.sessions
    apps.on_message("mac", "app_window", {"id": sid, "full": True})
    assert apps.sessions[sid].view == (0, 0, 1920, 1080)
    apps.local.helper_line('app_view {"id": "%s", "work": [0, 0, 1920, 1040]}' % sid)
    assert apps.sessions[sid].view == (0, 0, 1920, 1040)


def test_the_apps_minimise_button_minimises_its_mirror_and_its_taskbar_button_brings_it_back():
    # Kyle, 0.4.125: no way to put a mirror away but keep it on its screen.
    lay, transport, router, host, apps, lines, clock, ops, released = put_down()
    sid, = apps.sessions
    s = apps.sessions[sid]
    router.on_pointer(900, 688)  # into Safari, on its minimise button
    assert router.active_device == "mac"
    apps.on_message("mac", "app_window", {"id": sid, "min": True})
    assert s.min and apps.through is None
    # The pointer stays where it was on the PC's screen; the mirror is put away.
    assert router.active_device == "pc" and (router.x, router.y) == pytest.approx((900, 688), abs=2)
    show, = last_state(lines)["shows"]
    assert show["min"] is True
    clock.t = 2.0
    router.on_pointer(902, 690)  # where the mirror was: nothing to go into
    assert router.active_device == "pc"
    # Its taskbar button pressed (or the Dock icon): back, and usable again.
    apps.local.helper_line('app_view {"id": "%s", "min": false}' % sid)
    assert not s.min and last_state(lines)["shows"][0]["min"] is False
    router.on_pointer(900, 688)
    assert router.active_device == "mac"


def test_the_minimise_button_is_kept_from_the_app_and_minimises_the_mirror():
    sent, ops = [], Ops()
    win = {"wid": 7, "pid": 41}
    ops.window_rect = lambda w: [100, 50, 800, 600]
    ops.cursor = lambda: (160, 62)
    ops.title_hit = lambda w, x, y: "min"
    dev = AppMirrorDevice(lambda t, **d: sent.append((t, d)), now, lambda d, f: None, ops)
    dev.on_captures([{"id": "app:x", "window": win, "os": [100, 50, 800, 600], "hide": True, "use": True}])
    assert dev.intercept("left", True) and dev.intercept("left", False)
    assert sent[-1] == ("app_window", {"id": "app:x", "min": True})


def test_a_pc_apps_mirror_on_a_mac_has_the_macs_round_corners():
    # Kyle, 0.4.125: "app frames aren't rounded": a PC app's 8 px corners,
    # shown at its size on a Mac, were all but square.
    lay = desk()
    transport = Transport(platforms={"mac": "Darwin 24.1"})
    lay, transport, router, host, apps, lines, clock, ops, released = make(lay, transport)
    import curslink.appmirror as am

    s = apps.start("mac", dict(SAFARI), lay.screen("pc", "main"))
    s.window["scale"] = 2.0
    apps.platform = lambda d: "windows" if d == "mac" else "mac"
    s.view = (s.view[0], s.view[1], 400, 300)
    assert apps._corner(s) == am.CORNERS["mac"]


def test_this_computers_cursor_is_hidden_while_its_app_is_used_through_the_mirror():
    lay, transport, router, apps, clock, s = use_this_computers_own_app()
    hidden = []
    router.capture.set_cursor_hidden = hidden.append
    s.view = (2200, 288, 800, 600)  # the mirror clear of the Mac's edges
    clock.t = 2.0
    # Out of the app onto the Mac (the mirror's ring), and back in.
    vx, vy, vw, vh = s.view
    router.on_pointer(s.rect[0] - 5, s.rect[1] + 100)
    assert router.active_device == "mac"
    router.on_motion(vx + 50 - router.x, vy + 100 - router.y)
    assert router.active_device == "pc" and apps.through == s.id
    assert hidden[-1] is True
    # Into a mirrored app from the Mac: the Mac's cursor isn't parked away (it flashed).
    assert "park" not in transport.of("leave", "mac")[-1]


def test_helper_mirror_minimised_stays_put_away_and_says_its_screens_work_area(qapp):
    from curslink.mirror.helper import MirrorHelper

    out = []
    helper = MirrorHelper(out.append)
    show = {"id": "app:mac:7:1", "app": True, "screen": "main", "os": [100, 100, 400, 300], "ratio": 4 / 3,
            "label": "Safari · MacBook", "edges": [], "hover": False, "corner": 10, "raise": 0, "carried": False}
    helper.handle("mirror " + json.dumps({"captures": [], "shows": [show]}))
    frame = "mirror_image " + json.dumps({"id": show["id"], "jpg": jpeg(40, 30), "w": 40, "h": 30})
    helper.handle(frame)
    window = helper.windows[show["id"]]
    assert window.isVisible()
    said = [json.loads(ln.partition(" ")[2]) for ln in out if ln.startswith("app_view ")]
    assert any(isinstance(v.get("work"), list) and len(v["work"]) == 4 for v in said)
    # Minimised (its minimise button, the host says): put away, and a new
    # picture doesn't bring it back.
    helper.handle("mirror " + json.dumps({"captures": [], "shows": [{**show, "min": True}]}))
    assert not window.isVisible() or window.isMinimized()
    helper.handle(frame)
    assert not window.isVisible() or window.isMinimized()
    # Back (its taskbar button, the Dock icon).
    helper.handle("mirror " + json.dumps({"captures": [], "shows": [{**show, "min": False}]}))
    assert window.isVisible() and not window.isMinimized()
