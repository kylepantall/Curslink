"""App mirror: drag an app's window onto another device and use it there."""

from __future__ import annotations

import base64
import json
import os

import pytest

from conftest import FakeCapture, FakeOverlay, FakeSink
from curslink.appmirror import GRIP, AppMirrorDevice, AppMirrorHost, CastWatch, WindowDragWatch, is_app
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
    def __init__(self, devices=("mac",), app=("mac",), platforms=None):
        self.connected = set(devices)
        self.app = set(app)
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
                         release_local=lambda: released.append(True), clock=clock)
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
    assert (x, y) == (1920 - 800, 300 - 12) and show["carried"] is True and show["corner"] == 10


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
    # Out past its left side: back on the PC, on the mirror's own edge
    # (where it's resized from)...
    router.on_motion(-500, 0)
    assert router.active_device == "pc"
    assert 500 <= router.x < 500 + GRIP and router.y == pytest.approx(688)
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
    for bad in (dict(t="app_do", id="app:x", do="explode"), dict(t="app_view", id="app:x", os=[1, 2]),
                dict(t="app_drag"), dict(t="app_window", id="")):
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

    # Its bottom-right corner, dragged out: bigger, the same shape.
    window.mousePressEvent(Ev((398, 298), (498, 398)))
    window.mouseMoveEvent(Ev((498, 298), (598, 398)))
    window.mouseReleaseEvent(Ev((498, 298), (598, 398)))
    view = json.loads([ln for ln in out if ln.startswith("app_view ")][-1].partition(" ")[2])
    assert view["id"] == show["id"] and view["os"][2:] == [500, 300]  # any shape: the app follows
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
    monkeypatch.setattr(apphelper, "bring_to_front", lambda w, above_all: fronts.append(above_all))
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
    # Never see-through, carried or not, nor while it first shows.
    assert not window.testAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
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

    # Its tag's box: the mirror fills its screen, and the app is asked to.
    box = tag.max_box().center()
    tag.mousePressEvent(Ev((box.x(), box.y())))
    full = window.screen().availableGeometry()
    assert window.geometry() == full and window.maxed is not None
    view = json.loads([ln for ln in out if ln.startswith("app_view ")][-1].partition(" ")[2])
    assert view["os"][2:] == [full.width(), full.height()]
    # Again: back where it was.
    tag.mousePressEvent(Ev((box.x(), box.y())))
    view = json.loads([ln for ln in out if ln.startswith("app_view ")][-1].partition(" ")[2])
    assert view["os"] == [100, 100, 400, 300] and window.maxed is None


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
