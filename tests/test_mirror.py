"""Mirror mode: screens in front show what they cover of the screen in use."""

from __future__ import annotations

import base64
import json
import os
import sys

import pytest

from conftest import FakeCapture, FakeOverlay, FakeSink
from curslink.layout import Device, Layout, Screen
from curslink.mirror import (IN_FLIGHT, MirrorHost, MirrorPeer, MirrorSource, Stream, device_states, helper_frame,
                             streams_for)
from curslink.protocol import ProtocolError, decode, encode
from curslink.router import Router


def desk(mirror=True, pointer=True, lap_z=1, phone=False):
    """A PC monitor with a laptop in front of its bottom-right corner (and
    maybe a phone leaning on the laptop's top-left)."""
    devices = [
        Device("pc", "WKS82261", (Screen("main", "pc", 0, 0, 1000, 600),)),
        Device("lap", "MacBook", (Screen("main", "lap", 800, 400, 500, 300, os_x=0, os_y=0, z=lap_z),)),
    ]
    if phone:
        devices.append(Device("phone", "S25", (Screen("main", "phone", 760, 380, 100, 200, z=2),)))
    return Layout.from_devices(devices, allow_overlap=True, pointer_through=mirror and pointer, mirror=mirror)


class Transport:
    def __init__(self, connected=(), mirror=()):
        self.connected = set(connected)
        self.mirror = set(mirror)
        self.sent = []

    def is_connected(self, device_id):
        return device_id in self.connected

    def supports(self, device_id, feature):
        return feature == "mirror" and device_id in self.connected and device_id in self.mirror

    def send(self, device_id, t, **data):
        self.sent.append((device_id, t, data))

    def of(self, t, device=None):
        return [d for (dev, tt, d) in self.sent if tt == t and (device is None or dev == device)]


# -- the layout ---------------------------------------------------------------------


def test_covering_lists_every_screen_in_front_with_the_part_it_covers():
    lay = desk(phone=True)
    pc = lay.screen("pc", "main")
    assert [(f.device_id, r) for f, r in lay.covering(pc)] == [("lap", (800, 400, 200, 200)),
                                                               ("phone", (760, 380, 100, 200))]
    lap = lay.screen("lap", "main")
    assert [(f.device_id, r) for f, r in lay.covering(lap)] == [("phone", (800, 400, 60, 180))]
    assert lay.covering(lay.screen("phone", "main")) == []


def test_pointer_through_lets_the_device_behind_keep_the_covered_part():
    lay = desk()
    assert lay.screen_at(900, 500, "pc").device_id == "pc"
    assert lay.screen_at(900, 500).device_id == "lap"  # still the front one for everyone else
    assert lay.covered_by("pc", 900, 500) is None
    off = desk(mirror=False)
    assert off.screen_at(900, 500, "pc") is None


def test_mirror_without_its_pointer_keeps_todays_pointer():
    lay = desk(pointer=False)
    assert lay.mirror and not lay.pointer_through
    assert lay.screen_at(900, 500, "pc") is None


def test_same_device_overlap_prefers_the_screen_the_pointer_is_on():
    lay = Layout.from_devices([Device("pc", "PC", (
        Screen("big", "pc", 0, 0, 1000, 600), Screen("lid", "pc", 800, 400, 500, 300, os_x=1000, z=1)))],
        allow_overlap=True, pointer_through=True, mirror=True)
    big, lid = lay.screen("pc", "big"), lay.screen("pc", "lid")
    assert lay.screen_at(900, 500, "pc") == lid
    assert lay.screen_at(900, 500, "pc", prefer=big) == big
    assert [s.id for s in streams_for(lay, big)] == ["pc:big>pc:lid"]


# -- the router -----------------------------------------------------------------------


def test_local_pointer_stays_on_the_monitor_under_the_laptop():
    lay = desk()
    transport = Transport({"lap"}, {"lap"})
    router = Router(lay, "pc", FakeCapture(), FakeSink(), transport, FakeOverlay())
    moved = []
    router.on_moved = lambda: moved.append((router.screen.device_id, router.x, router.y))
    router.on_pointer(700, 300)
    router.on_pointer(900, 500)  # the real cursor under the laptop: it stays on the PC
    assert router.is_local and (router.x, router.y) == (900, 500)
    assert moved[-1] == ("pc", 900, 500)


def test_remote_pointer_goes_into_the_covered_part_and_leaves_past_it():
    # The laptop is behind this time (the PC's screen in front of its corner).
    lay = desk(lap_z=-1)
    transport = Transport({"lap"}, {"lap"})
    router = Router(lay, "pc", FakeCapture(), FakeSink(), transport, FakeOverlay())
    now = [10.0]
    router.clock = lambda: now[0]
    router.switch_to(lay.screen("lap", "main"), 1100, 650, "gaze")
    now[0] += 5
    router.on_motion(-250, -200)  # into the corner the PC covers: still the laptop
    assert router.active_device == "lap" and (router.x, router.y) == (850, 450)
    assert transport.of("move", "lap")[-1] == {"x": 50, "y": 50}
    router.on_motion(-100, 0)  # past the laptop's left edge, onto the PC
    assert router.is_local


# -- planning -----------------------------------------------------------------------


def test_streams_follow_the_screen_in_use_and_the_devices_that_take_part():
    lay = desk(phone=True)
    pc, lap = lay.screen("pc", "main"), lay.screen("lap", "main")
    assert [s.id for s in streams_for(lay, pc)] == ["pc:main>lap:main", "pc:main>phone:main"]
    assert [s.id for s in streams_for(lay, pc, lambda d: d != "phone")] == ["pc:main>lap:main"]
    assert [s.id for s in streams_for(lay, lap)] == ["lap:main>phone:main"]
    assert streams_for(desk(mirror=False), pc) == []


def test_device_states_say_what_to_capture_and_show_in_each_ones_own_pixels():
    lay = desk()
    stream, = streams_for(lay, lay.screen("pc", "main"))
    states = device_states([stream], {"pc": "WKS82261"}, 15)
    assert states["pc"] == {"captures": [{"id": stream.id, "screen": "main", "os": [800, 400, 200, 200],
                                          "screen_os": [0, 0, 1000, 600], "fps": 15}], "shows": []}
    show, = states["lap"]["shows"]
    assert show["os"] == [0, 0, 200, 200] and show["label"] == "WKS82261"
    assert show["screen_os"] == [0, 0, 500, 300]
    assert set(show["edges"]) == {"right", "bottom"}  # where it ends inside the laptop


def test_stream_pointer_is_a_fraction_of_the_mirror():
    s = Stream(Screen("a", "pc", 0, 0, 10, 10), Screen("b", "lap", 0, 0, 10, 10), (100, 200, 200, 100))
    assert s.pointer(150, 250) == (0.25, 0.5)
    assert s.pointer(99, 250) is None and s.pointer(300, 250) is None


# -- flow ---------------------------------------------------------------------------------


def test_source_keeps_two_frames_in_flight_and_sends_the_newest_waiting_one_on_an_answer():
    sent = []
    src = MirrorSource(lambda t, **d: sent.append(d["n"]))
    src.reset(["a"])
    for n in range(5):
        src.offer({"id": "a", "n": n})
    assert sent == list(range(IN_FLIGHT))
    src.ack("a")
    assert sent == [0, 1, 4]  # 2 and 3 were overtaken
    src.ack("a")
    src.ack("a")
    src.offer({"id": "a", "n": 5})
    assert sent[-1] == 5
    src.offer({"id": "gone", "n": 6})
    assert 6 not in sent


def make_host(lay, transport, local_lines=None):
    router = Router(lay, "pc", FakeCapture(), FakeSink(), transport, FakeOverlay())
    host = None
    local = MirrorPeer(local_lines.append if local_lines is not None else (lambda line: None),
                       lambda t, **d: host.local_send(t, **d))
    host = MirrorHost(lay, router, transport, local, fps=12, clock=lambda: 0.0)
    router.on_moved = host.update
    host.refresh()
    return host, router, local


def test_host_captures_its_own_corner_for_the_laptop_and_passes_frames_and_answers():
    lines = []
    transport = Transport({"lap"}, {"lap"})
    host, router, local = make_host(desk(), transport, lines)
    # This computer was told to capture; the laptop to show.
    state = json.loads(lines[-1].partition(" ")[2])
    assert [c["os"] for c in state["captures"]] == [[800, 400, 200, 200]] and state["captures"][0]["fps"] == 12
    show, = transport.of("mirror", "lap")[-1]["shows"]
    sid = show["id"]
    # The helper captured a frame: it goes to the laptop.
    assert local.helper_line('mirror_frame {"id": "%s", "jpg": "AAAA", "w": 200, "h": 200}' % sid)
    assert transport.of("mirror_frame", "lap") == [{"id": sid, "jpg": "AAAA", "w": 200, "h": 200}]
    for _ in range(3):
        local.helper_line('mirror_frame {"id": "%s", "jpg": "BBBB", "w": 200, "h": 200}' % sid)
    assert len(transport.of("mirror_frame", "lap")) == IN_FLIGHT
    host.on_message("lap", "mirror_ack", {"id": sid})
    assert len(transport.of("mirror_frame", "lap")) == IN_FLIGHT + 1
    # This computer casts its real cursor in the picture: none is drawn on top.
    assert state["captures"][0]["cursor"] is True
    router.on_pointer(900, 450)
    router.on_pointer(500, 300)
    assert transport.of("mirror_pointer", "lap") == []


def test_using_the_laptop_takes_the_mirror_down_and_coming_back_puts_it_up():
    lines = []
    transport = Transport({"lap"}, {"lap"})
    host, router, _local = make_host(desk(), transport, lines)
    router.switch_to(router.layout.screen("lap", "main"), 1200, 600, "edge")
    assert transport.of("mirror", "lap")[-1] == {"captures": [], "shows": []}
    assert json.loads(lines[-1].partition(" ")[2]) == {"captures": [], "shows": []}
    router.go_home("hotkey")
    assert transport.of("mirror", "lap")[-1]["shows"]


def test_agent_behind_the_host_captures_and_the_host_shows():
    lines = []
    lay = desk(lap_z=-1)  # the PC (host) in front of the laptop's corner
    transport = Transport({"lap"}, {"lap"})
    host, router, _local = make_host(lay, transport, lines)
    assert not transport.of("mirror", "lap")  # the host's own screen is in use: nothing behind it
    router.switch_to(lay.screen("lap", "main"), 1200, 600, "gaze")
    capture, = transport.of("mirror", "lap")[-1]["captures"]
    assert capture["os"] == [0, 0, 200, 200]
    state = json.loads(lines[-1].partition(" ")[2])
    assert state["shows"][0]["os"] == [800, 400, 200, 200]
    sid = capture["id"]
    host.on_message("lap", "mirror_frame", {"id": sid, "jpg": "QUJD", "w": 2, "h": 2})
    assert lines[-1].startswith("mirror_image ")
    assert transport.of("mirror_ack", "lap") == [{"id": sid}]  # shown here: the laptop may send another


def test_two_agents_overlapping_go_through_the_host_and_old_ones_are_left_out():
    lay = Layout.from_devices([
        Device("pc", "PC", (Screen("main", "pc", 0, 0, 1000, 600),)),
        Device("mon", "Monitor PC", (Screen("main", "mon", 1000, 0, 1000, 600),)),
        Device("lap", "MacBook", (Screen("main", "lap", 1800, 400, 500, 300, z=1),)),
    ], allow_overlap=True, pointer_through=True, mirror=True)
    transport = Transport({"mon", "lap"}, {"mon", "lap"})
    host, router, _local = make_host(lay, transport, [])
    router.switch_to(lay.screen("mon", "main"), 1500, 300, "gaze")
    sid = transport.of("mirror", "mon")[-1]["captures"][0]["id"]
    host.on_message("mon", "mirror_frame", {"id": sid, "jpg": "QUJD", "w": 2, "h": 2})
    assert transport.of("mirror_frame", "lap")[-1]["id"] == sid
    host.on_message("lap", "mirror_ack", {"id": sid})
    assert transport.of("mirror_ack", "mon") == [{"id": sid}]
    # An agent without the feature (an older version) is left out.
    transport.mirror.discard("lap")
    host.refresh()
    assert transport.of("mirror", "mon")[-1] == {"captures": [], "shows": []}


def test_agent_peer_shows_frames_answers_them_and_captures():
    helper, sent = [], []
    peer = MirrorPeer(helper.append, lambda t, **d: sent.append((t, d)))
    peer.handle("mirror", {"captures": [{"id": "c", "os": [0, 0, 10, 10]}], "shows": [{"id": "s", "os": [0, 0, 5, 5]}]})
    assert helper[-1].startswith("mirror {")
    peer.handle("mirror_frame", {"id": "s", "jpg": "QUJD", "w": 1, "h": 1})
    assert helper[-1].startswith("mirror_image ") and sent[-1] == ("mirror_ack", {"id": "s"})
    peer.handle("mirror_pointer", {"id": "s", "x": 0.5, "y": 0.5})
    assert helper[-1].startswith("mirror_pointer ")
    assert peer.helper_line('mirror_frame {"id": "c", "jpg": "QUJD", "w": 1, "h": 1}')
    assert sent[-1][0] == "mirror_frame"
    assert not peer.helper_line("drag {}")
    peer.stop()
    assert json.loads(helper[-1].partition(" ")[2]) == {"captures": [], "shows": []}


def test_helper_frame_parses_only_good_lines():
    assert helper_frame('mirror_frame {"id": "a", "jpg": "x"}') == {"id": "a", "jpg": "x"}
    assert helper_frame("mirror_frame nope") is None
    assert helper_frame('mirror_frame {"id": 1, "jpg": "x"}') is None
    assert helper_frame("pop 1 2") is None


# -- protocol and config ----------------------------------------------------------------


def test_mirror_frames_round_trip_and_are_checked():
    assert decode(encode("mirror", captures=[], shows=[])).data == {"captures": [], "shows": []}
    assert decode(encode("mirror_frame", id="a", jpg="QQ==", w=1, h=1)).get("jpg") == "QQ=="
    assert decode(encode("mirror_pointer", id="a", hide=True)).get("hide") is True
    for bad in (b'{"t":"mirror","captures":[]}', b'{"t":"mirror_frame","id":"a"}', b'{"t":"mirror_ack"}'):
        with pytest.raises(ProtocolError):
            decode(bad)


def test_config_reads_mirror_mode_only_with_overlap():
    from curslink.config import host_config

    def raw(**layout):
        return {"host": {"device_id": "pc"}, "secret": "s", "layout": layout,
                "devices": [{"id": "pc", "screens": [{"x": 0, "y": 0, "width": 100, "height": 100}]}]}

    cfg = host_config(raw(allow_overlap=True, mirror=True, mirror_fps=400))
    assert cfg.layout.mirror and cfg.layout.pointer_through and cfg.mirror_fps == 120
    assert host_config(raw(allow_overlap=True, mirror=True)).mirror_fps == 0  # auto
    cfg = host_config(raw(allow_overlap=True, mirror=True, mirror_pointer=False))
    assert cfg.layout.mirror and not cfg.layout.pointer_through
    assert not host_config(raw(mirror=True)).layout.mirror


# -- the helper (Qt, offscreen) --------------------------------------------------------------


@pytest.fixture
def qapp():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    pytest.importorskip("PySide6.QtWidgets")
    try:
        from PySide6.QtWidgets import QApplication
    except ImportError as exc:
        pytest.skip(f"Qt can't load: {exc}")
    return QApplication.instance() or QApplication(["curslink-test"])


def test_os_rect_to_logical_scales_physical_pixels_on_windows():
    from curslink.mirror.helper import os_to_logical

    screens = [(0, 0, 1920, 1080, 1.0), (1920, 0, 1280, 720, 2.0)]
    assert os_to_logical(screens, [100, 100, 200, 100], True) == (100, 100, 200, 100, 0)
    assert os_to_logical(screens, [1920 + 400, 200, 400, 200], True) == (1920 + 200, 100, 200, 100, 1)
    assert os_to_logical(screens, [1920 + 400, 200, 400, 200], False) == (2320, 200, 400, 200, 1)
    assert os_to_logical(screens, [9000, 9000, 10, 10], True) is None


def test_big_frames_are_squeezed_into_one_protocol_frame(qapp):
    from PySide6.QtGui import QColor, QImage

    from curslink.mirror.helper import MAX_B64, fit_jpeg

    img = QImage(2560, 1440, QImage.Format.Format_RGB32)
    for y in range(0, 1440, 3):  # noise-ish stripes compress badly
        for x in range(0, 2560, 97):
            img.setPixelColor(x, y, QColor((x * 7) % 255, (y * 13) % 255, (x * y) % 255))
    raw, quality, scale = fit_jpeg(img, 72, 1.0)
    assert len(base64.b64encode(raw)) <= MAX_B64 and scale <= 1600 / 2560
    frame = encode("mirror_frame", id="pc:main>lap:main", jpg=base64.b64encode(raw).decode(), w=2560, h=1440)
    assert decode(frame).get("w") == 2560


def test_helper_captures_and_shows_a_mirror(qapp):
    from PySide6.QtCore import QBuffer, QByteArray, QIODevice
    from PySide6.QtGui import QColor, QImage

    from curslink.mirror.helper import MirrorHelper

    out = []
    helper = MirrorHelper(out.append)
    state = {"captures": [{"id": "c", "screen": "main", "os": [10, 10, 120, 80], "fps": 30}],
             "shows": [{"id": "s", "screen": "main", "os": [200, 150, 160, 100], "label": "PC", "edges": ["left"]}]}
    assert helper.handle("mirror " + json.dumps(state))
    helper.captures["c"].grab()
    # Offscreen Qt may capture nothing; when it does, it's one frame line.
    for line in out:
        frame = json.loads(line.partition(" ")[2])
        assert line.startswith("mirror_frame ") and frame["id"] == "c" and frame["jpg"]
    window = helper.windows["s"]
    assert not window.isVisible()  # nothing until the first frame
    img = QImage(16, 10, QImage.Format.Format_RGB32)
    img.fill(QColor("tomato"))
    data = QByteArray()
    buf = QBuffer(data)
    buf.open(QIODevice.OpenModeFlag.WriteOnly)
    img.save(buf, "JPG", 80)
    jpg = base64.b64encode(bytes(data.data())).decode()
    helper.handle("mirror_image " + json.dumps({"id": "s", "jpg": jpg, "w": 16, "h": 10}))
    helper.handle('mirror_pointer {"id": "s", "x": 0.5, "y": 0.5}')
    qapp.processEvents()
    assert window.isVisible() and window.pointer == (0.5, 0.5)
    assert (window.width(), window.height()) == (160, 100)
    window.grab()  # paints without trouble
    helper.handle('mirror {"captures": [], "shows": []}')
    assert not helper.captures and not helper.windows


def test_windows_captures_copy_the_screen_themselves_at_most_win_shot_fps(qapp, monkeypatch):
    """Qt's grab copies with CAPTUREBLT, which hid the Windows cursor on
    every copy and stalled the screen: Windows goes through win_grab, in
    physical pixels, never more than WIN_SHOT_FPS a second."""
    from PySide6.QtGui import QColor, QGuiApplication, QImage

    from curslink.mirror import helper

    calls = []

    def fake_grab(x, y, w, h):
        calls.append((x, y, w, h))
        img = QImage(round(w), round(h), QImage.Format.Format_RGB32)
        img.fill(QColor("teal"))
        return img

    def no_qt_grab(*_a):
        raise AssertionError("Qt's grab copies with CAPTUREBLT")

    import types

    # As on Windows: past the macOS capture paths too, on a Mac runner.
    monkeypatch.setattr(helper, "sys", types.SimpleNamespace(platform="win32"))
    monkeypatch.setattr(helper, "win_copies", lambda: True)
    monkeypatch.setattr(helper, "win_grab", fake_grab)
    screen = QGuiApplication.primaryScreen()
    monkeypatch.setattr(type(screen), "grabWindow", no_qt_grab)
    out = []
    cap = helper.Capture({"id": "c", "screen": "main", "os": [10, 10, 120, 80], "fps": 120}, out.append)
    cap.timer.stop()
    cap.grab()
    g, k = screen.geometry(), screen.devicePixelRatio() or 1.0
    assert len(calls) == 1 and out
    x, y, w, h = calls[0]
    assert (round(w), round(h)) == (round(120 / k * k), round(80 / k * k))
    assert (round(x), round(y)) == (round(g.x() + (10 - g.x())), round(g.y() + (10 - g.y())))
    cap.grab()  # straight after: too soon for another copy
    assert len(calls) == 1
    cap.shot_at -= 1 / helper.WIN_SHOT_FPS
    cap.grab()
    assert len(calls) == 2
    cap.stop()


def test_no_drawn_pointer_for_a_phone_whose_capture_shows_its_own():
    lay = Layout.from_devices([
        Device("pc", "PC", (Screen("main", "pc", 0, 0, 1000, 600, z=1),)),
        Device("phone", "S25", (Screen("main", "phone", 900, 300, 400, 800),)),
    ], allow_overlap=True, pointer_through=True, mirror=True)

    class PhoneTransport(Transport):
        def supports(self, device_id, feature):
            return device_id == "phone" and feature in ("mirror", "mirror_cursor")

    transport = PhoneTransport({"phone"})
    host, router, _local = make_host(lay, transport, [])
    router.switch_to(lay.screen("phone", "main"), 1100, 700, "gaze")
    assert transport.of("mirror", "phone")[-1]["captures"]
    router.on_motion(-150, -200)  # into the corner the PC covers
    assert router.active_device == "phone" and (router.x, router.y) == (950, 500)
    assert not [d for d in transport.of("mirror_pointer") if "x" in d]


def test_quartz_images_become_qt_images(qapp):
    from PySide6.QtGui import QColor

    from curslink.mirror.helper import cgimage_to_qimage

    class FakeQuartz:
        """A 2x2 BGRA (32-bit little, premultiplied first) image, like macOS hands back."""
        def __init__(self, info=0x2002, alpha=2, bpp=32):
            self.info, self.alpha, self.bpp = info, alpha, bpp

        def CGImageGetWidth(self, _): return 2  # noqa: E704,N802
        def CGImageGetHeight(self, _): return 2  # noqa: E704,N802
        def CGImageGetBitsPerPixel(self, _): return self.bpp  # noqa: E704,N802
        def CGImageGetBytesPerRow(self, _): return 8  # noqa: E704,N802
        def CGImageGetBitmapInfo(self, _): return self.info  # noqa: E704,N802
        def CGImageGetAlphaInfo(self, _): return self.alpha  # noqa: E704,N802
        def CGImageGetDataProvider(self, _): return None  # noqa: E704,N802
        def CGDataProviderCopyData(self, _): return bytes([0, 0, 255, 255]) * 4  # noqa: E704,N802  # red

    img = cgimage_to_qimage(FakeQuartz(), object())
    assert (img.width(), img.height()) == (2, 2) and img.pixelColor(1, 1) == QColor(255, 0, 0)
    rgba = cgimage_to_qimage(FakeQuartz(info=1, alpha=1), object())  # RGBA, alpha last
    assert rgba.pixelColor(0, 0) == QColor(0, 0, 255)
    assert cgimage_to_qimage(FakeQuartz(bpp=24), object()) is None


def test_a_capture_says_why_it_got_nothing(qapp, caplog):
    from curslink.mirror.helper import Capture

    out = []
    cap = Capture({"id": "c", "screen": "main", "os": [90000, 90000, 10, 10], "fps": 1}, out.append)
    with caplog.at_level("WARNING", logger="curslink.mirror.helper"):
        cap.grab()
        cap.grab()
    cap.stop()
    assert not out
    said = [r.getMessage() for r in caplog.records if "couldn't capture" in r.getMessage()]
    assert len(said) == 1 and "no screen of this computer" in said[0]


def test_macos_asks_for_screen_recording_only_with_mirror_mode(monkeypatch):
    from curslink import macperms

    monkeypatch.setattr(macperms, "MAC", True)
    assert macperms.SCREEN_RECORDING not in macperms.needed("host")
    assert macperms.SCREEN_RECORDING in macperms.needed("host", mirror=True)
    assert macperms.mirror_on({"layout": {"allow_overlap": True, "mirror": True}})
    assert not macperms.mirror_on({"layout": {"allow_overlap": False, "mirror": True}})
    assert not macperms.mirror_on({"layout": {"allow_overlap": True}})
    assert macperms.PERMISSIONS[macperms.SCREEN_RECORDING].title == "Screen Recording"


# -- lining up an overlap (Fit) ------------------------------------------------------------


def _fit_layout(fits=()):
    from curslink.layout import Fit  # noqa: F401

    return Layout.from_devices([
        Device("mac", "Mac", (Screen("screen2", "mac", 0, 0, 3440, 1440),)),
        Device("pc", "PC", (Screen("main", "pc", 1000, 1200, 2880, 1536, z=1),)),
    ], allow_overlap=True, pointer_through=True, mirror=True, fits=fits)


def test_without_a_fit_the_layout_overlap_is_mirrored_as_is():
    lay = _fit_layout()
    (front, behind, there), = lay.covered_parts(lay.screen("mac", "screen2"))
    assert front.device_id == "pc" and behind == there == (1000, 1200, 2440, 240)


def test_a_fit_says_what_is_really_covered_and_where_it_shows():
    from curslink.layout import Fit

    # The PC's top-left corner really sits at (1200, 1100) on the Mac, and
    # one PC pixel is 0.5 Mac pixels: it spans 1440 x 768 Mac pixels there.
    lay = _fit_layout([Fit(("mac", "screen2"), ("pc", "main"), 1200, 1100, 0.5)])
    mac = lay.screen("mac", "screen2")
    (front, behind, there), = lay.covered_parts(mac)
    assert behind == (1200, 1100, 1440, 340)              # down to the Mac's bottom edge
    assert there == (1000, 1200, 2880, 680)               # the PC's whole width, its top 680 px
    stream, = streams_for(lay, mac)
    states = device_states([stream], {"mac": "Mac"}, 15)
    assert states["mac"]["captures"][0]["os"] == [1200, 1100, 1440, 340]
    assert states["pc"]["shows"][0]["os"] == [0, 0, 2880, 680]
    assert states["pc"]["shows"][0]["edges"] == ["bottom"]
    assert stream.pointer(1200 + 720, 1100 + 170) == (0.5, 0.5)


def test_a_fit_that_misses_the_screen_behind_mirrors_nothing():
    from curslink.layout import Fit

    lay = _fit_layout([Fit(("mac", "screen2"), ("pc", "main"), 1200, 1500, 0.5)])
    assert lay.covered_parts(lay.screen("mac", "screen2")) == []


def test_fits_are_read_from_the_config_and_checked():
    from curslink.config import ConfigError, parse_fits

    devices = list(_fit_layout().devices.values())
    fits = parse_fits([{"behind": "mac/screen2", "front": "pc/main", "x": 1200, "y": 1100.5, "scale": 0.5},
                       {"behind": "gone/main", "front": "pc/main", "x": 0, "y": 0, "scale": 1}], devices)
    assert [(f.behind, f.front, f.x, f.y, f.scale) for f in fits] == [(("mac", "screen2"), ("pc", "main"), 1200, 1100.5, 0.5)]
    with pytest.raises(ConfigError):
        parse_fits([{"behind": "mac/screen2", "front": "pc/main", "x": 0, "y": 0, "scale": 0}], devices)


def test_a_corner_clicked_on_each_screen_gives_the_fit():
    pytest.importorskip("PySide6.QtCore")
    from curslink.gui.fitcalib import solve

    # Kyle's desk: the laptop (2880 x 1536) stands in front of the Mac's
    # bottom-left, its left side past the Mac's left edge. Truth: its
    # top-left corner at (-700, 1100) on the Mac, 0.5 Mac px per laptop px.
    k, x, y = 0.5, -700.0, 1100.0
    top_right_on_mac = (x + 2880 * k, y)                       # the laptop's top-right corner, on the Mac
    mac_bottom_left_on_laptop = ((0 - x) / k, (1440 - y) / k)  # the Mac's bottom-left corner, behind the laptop
    got = solve((2880, 0), top_right_on_mac, (0, 1440), mac_bottom_left_on_laptop)
    assert got == pytest.approx((x, y, k))
    # Corners too close together along both sides: no answer.
    assert solve((2880, 0), (100, 100), (0, 1440), (2870, 10)) is None


def test_the_corner_asked_for_is_one_on_that_screen():
    pytest.importorskip("PySide6.QtCore")
    from curslink.gui.fitcalib import guess_corner

    mac, laptop = (0, 0, 3440, 1440), (-1000, 1200, 2880, 1536)
    assert guess_corner(laptop, mac) == "top-right"     # the laptop's corner on the Mac
    assert guess_corner(mac, laptop) == "bottom-left"   # the Mac's corner behind the laptop


def test_lining_up_goes_by_a_click_on_each_computer(qapp):
    from curslink.gui.fitcalib import FitCalibration

    sent = []
    behind = {"device": "mac", "name": "Mac", "rect": [0, 0, 3440, 1440],
              "screen": {"id": "screen2", "os_x": 0, "os_y": 0, "width": 3440, "height": 1440}}
    front = {"device": "pc", "name": "Laptop", "rect": [-1000, 1200, 2880, 1536],
             "screen": {"id": "main", "os_x": 0, "os_y": 0, "width": 2880, "height": 1536}}
    cal = FitCalibration(behind, front, "host", sent.append)
    got = []
    cal.finished.connect(got.append)
    assert cal.start() and sent[0] == "pick on"
    hint = json.loads(sent[1].partition(" ")[2])
    mac, pc = hint["devices"]["mac"]["screens"][0]["fit"], hint["devices"]["pc"]["screens"][0]["fit"]
    assert (mac["role"], mac["corner"], "waiting" in mac) == ("behind", "top-right", False)
    assert (pc["role"], pc["corner"], "waiting" in pc) == ("front", "bottom-left", True)
    cal.on_event({"event": "picked", "key": "tab"})                      # another corner, and back round
    assert cal.corners[0] == "bottom-right"
    for _ in range(3):
        cal.on_event({"event": "picked", "key": "tab"})
    cal.on_event({"event": "picked", "device": "pc", "screen": "main", "x": 5, "y": 5})  # not its turn
    cal.on_event({"event": "picked", "device": "mac", "screen": "screen2", "x": 740, "y": 1100})
    assert cal.active == 1
    cal.on_event({"event": "picked", "device": "pc", "screen": "main", "x": 1400, "y": 680})
    assert got and got[0] == pytest.approx((-700, 1100, 0.5)) and sent[-2:] == ["pick off", 'hint {"devices":{}}']


def test_the_fit_layer_paints(qapp):
    from PySide6.QtCore import QPointF
    from PySide6.QtGui import QImage, QPainter

    from curslink.gui.fitcalib import FitState, paint_fit

    img = QImage(800, 600, QImage.Format.Format_ARGB32)
    for st in (FitState("behind", "Laptop", "top-right", pointer=QPointF(400, 300)),
               FitState("front", "Mac", "bottom-left", point=QPointF(10, 10), step=2),
               FitState("front", "Mac", "bottom-left", waiting="Next, on Laptop")):
        p = QPainter(img)
        paint_fit(p, img.rect().toRectF(), st)
        p.end()


def test_a_fit_hanging_off_the_side_mirrors_into_the_right_corner():
    from curslink.layout import Fit

    # The laptop's left part sticks out past the Mac's left edge: what it
    # covers is the Mac's bottom-left, shown in the laptop's top-right.
    lay = _fit_layout([Fit(("mac", "screen2"), ("pc", "main"), -700, 1100, 0.5)])
    (front, behind, there), = lay.covered_parts(lay.screen("mac", "screen2"))
    assert behind == (0, 1100, 740, 340)
    assert there == (front.x + 1400, front.y, 1480, 680)


def test_fine_tuning_changes_the_mirror_at_once_and_holds_it_up():
    lines = []
    transport = Transport({"lap"}, {"lap"})
    host, router, _local = make_host(desk(), transport, lines)
    # Using the laptop: nothing mirrored, until fine-tuning holds the PC's screen up.
    router.switch_to(router.layout.screen("lap", "main"), 1200, 600, "edge")
    assert transport.of("mirror", "lap")[-1]["shows"] == []
    host.fit({"behind": "pc/main", "front": "lap/main", "x": 850, "y": 450, "scale": 1, "hold": True})
    state = json.loads(lines[-1].partition(" ")[2])
    assert [c["os"] for c in state["captures"]] == [[850, 450, 150, 150]]
    # A nudge without "hold" keeps it held.
    host.fit({"behind": "pc/main", "front": "lap/main", "x": 860, "y": 450, "scale": 1})
    assert [c["os"] for c in json.loads(lines[-1].partition(" ")[2])["captures"]] == [[860, 450, 140, 150]]
    # Letting go goes back to the screen in use: the laptop.
    host.fit({"behind": "pc/main", "front": "lap/main", "x": 860, "y": 450, "scale": 1, "hold": False})
    assert transport.of("mirror", "lap")[-1]["shows"] == []
    assert router.layout.fit(router.layout.screen("pc", "main"), router.layout.screen("lap", "main")).x == 860


def test_bad_fit_commands_change_nothing():
    from curslink.mirror import apply_fit

    lay = desk()
    assert apply_fit(lay, {"behind": "pc/nope", "front": "lap/main", "x": 1, "y": 1, "scale": 1}) is None
    assert apply_fit(lay, {"behind": "pc/main", "front": "lap/main", "x": 1, "y": 1, "scale": 0}) is None
    assert apply_fit(lay, {"behind": "pc", "front": "lap/main", "x": 1, "y": 1, "scale": 1}) is None
    assert lay.fits == ()
    assert apply_fit(lay, {"behind": "pc/main", "front": "lap/main", "x": 1, "y": 2, "scale": 0.5}) == (1, 2, 0.5)
    # Without a calibrated fit it goes back to the layout's.
    assert apply_fit(lay, {"behind": "pc/main", "front": "lap/main", "reset": True}) == (800, 400, 1.0)
    assert lay.fits == ()


def test_fine_tuning_moves_and_grows_from_the_fit_in_use():
    from curslink.mirror import apply_fit

    lay = desk()
    # Picture right by 5 and down by 2: the laptop sits that much up-left on the PC.
    assert apply_fit(lay, {"behind": "pc/main", "front": "lap/main", "dx": 5, "dy": 2}) == (795, 398, 1.0)
    x, y, k = apply_fit(lay, {"behind": "pc/main", "front": "lap/main", "grow": 2})
    assert k == 0.5
    # The middle of the covered part (897.5, 499) stays put on the laptop.
    assert (round(x + k * (897.5 - 795)), round(y + k * (499 - 398))) == (898, 499)


def test_nudging_and_growing_keep_the_covered_middle_in_place():
    from curslink.mirror import grow, nudge

    assert nudge((100, 50, 2.0), 3, -1) == (97, 51, 2.0)
    behind, front = (1000, 600), (500, 300)
    x, y, k = 800, 400, 1.0  # covers 800..1000 x 400..600; middle (900, 500) is (100, 100) on the front
    x2, y2, k2 = grow((x, y, k), 2.0, behind, front)
    assert k2 == 0.5
    assert (x2 + k2 * 100, y2 + k2 * 100) == (900, 500)


def test_fine_tune_dialog_sends_nudges_and_saves_what_the_service_says(qapp):
    from PySide6.QtCore import QEvent, Qt
    from PySide6.QtGui import QKeyEvent

    from curslink.gui.fittune import FitTuneDialog

    sent, saved = [], []
    b = {"key": "pc/main", "name": "PC"}
    f = {"key": "lap/main", "name": "Laptop"}
    d = FitTuneDialog(b, f, None, sent.append)
    d.saved.connect(saved.append)
    last = lambda: json.loads(sent[-1][4:])  # noqa: E731
    assert last() == {"behind": "pc/main", "front": "lap/main", "hold": True, "tune": True}
    d.keyPressEvent(QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Right, Qt.KeyboardModifier.NoModifier))
    assert last()["dx"] == 1 and last()["dy"] == 0
    d.keyPressEvent(QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Down, Qt.KeyboardModifier.ShiftModifier))
    assert last()["dy"] == 10
    d.keyPressEvent(QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Plus, Qt.KeyboardModifier.NoModifier))
    assert last()["grow"] > 1
    d.on_event({"event": "fit", "behind": "pc/main", "front": "lap/main", "x": 799, "y": 390, "scale": 0.99})
    d.on_event({"event": "fit", "behind": "other/main", "front": "lap/main", "x": 1, "y": 1, "scale": 1})
    d.accept()
    assert last()["hold"] is False and saved == [(799, 390, 0.99)]
    # Cancel puts the calibrated fit back (or drops the one being tried).
    d2 = FitTuneDialog(b, f, (800, 400, 1.0), sent.append)
    d2.move_by(5, 5)
    d2.reject()
    assert last() == {"behind": "pc/main", "front": "lap/main", "x": 800, "y": 400, "scale": 1.0, "hold": False}
    d3 = FitTuneDialog(b, f, None, sent.append)
    d3.reject()
    assert last() == {"behind": "pc/main", "front": "lap/main", "reset": True, "hold": False}
    import shiboken6

    for dialog in (d, d2, d3):  # gone before the app is, or Qt crashes on the way out
        shiboken6.delete(dialog)


def test_real_sizes_give_the_mirror_its_size_without_lining_up():
    # Kyle's desk: a 34" 3440x1440 ultrawide behind a 15.6" 1920x1080 laptop.
    devices = [
        Device("mac", "Mac", (Screen("wide", "mac", 0, 0, 3440, 1440),)),
        Device("pc", "Laptop", (Screen("main", "pc", 0, 1000, 1920, 1080, z=1),)),
    ]
    lay = Layout.from_devices(devices, allow_overlap=True, mirror=True)
    wide, lap = lay.screen("mac", "wide"), lay.screen("pc", "main")
    assert lay.sized_fit(wide, lap) is None
    assert lay.set_physical("mac", [{"x": 0, "y": 0, "width": 3440, "height": 1440,
                                     "width_mm": 800, "height_mm": 335}])
    assert lay.set_physical("pc", [{"x": 0, "y": 0, "width": 1920, "height": 1080,
                                    "width_mm": 344, "height_mm": 194}])
    assert not lay.set_physical("pc", [{"x": 0, "y": 0, "width": 1920, "height": 1080,
                                        "width_mm": 344, "height_mm": 194}])
    f = lay.sized_fit(wide, lap)
    # One laptop pixel (0.18 mm) is about 0.77 of the ultrawide's (0.23 mm).
    assert f.scale == pytest.approx(0.77, abs=0.01)
    # Its middle stays where the layout has it.
    assert (f.x + f.scale * 960, f.y + f.scale * 540) == pytest.approx((960, 1540))
    (front, behind, there), = lay.covered_parts(wide)
    assert there[2] == 1920  # the whole width of the laptop shows the Mac
    assert behind[2] == pytest.approx(1920 * f.scale, abs=1)
    # A calibrated fit still wins.
    from curslink.layout import Fit

    lay.fits = (Fit(("mac", "wide"), ("pc", "main"), 10, 1100, 0.5),)
    assert lay.covered_parts(wide)[0][1][:2] == (10, 1100)


def test_screen_sizes_are_matched_and_checked():
    from curslink.screensize import match, plausible

    assert plausible(1920, 1080, 344, 194)
    assert not plausible(1920, 1080, 0, 0)
    assert not plausible(1920, 1080, 160, 90 * 3)  # aspect doesn't fit its pixels
    assert not plausible(1920, 1080, 16, 9)  # 120 px per mm: a made-up EDID
    sizes = [{"x": 0, "y": 0, "width": 1920, "height": 1080, "width_mm": 344, "height_mm": 194},
             {"x": 1920, "y": 0, "width": 2560, "height": 1440, "width_mm": 597, "height_mm": 336}]
    assert match(sizes, 1920, 0, 2560, 1440) == (597, 336)
    assert match(sizes, -500, 0, 2560, 1440) == (597, 336)  # moved, but the only one that size
    assert match(sizes + sizes[1:], -500, 0, 2560, 1440) is None
    assert match(sizes, 0, 0, 1280, 720) is None
    # One monitor, laid out in logical pixels but measured in physical ones.
    assert match(sizes[:1], 0, 0, 1280, 720) == (344, 194)
    assert match(sizes[:1], 0, 0, 1280, 1024) is None


async def test_a_mirroring_agent_says_how_big_its_screens_are_in_hello(monkeypatch):
    import asyncio

    import curslink.screensize
    from conftest import FakeSink as Sink
    from curslink.net.client import AgentClient, AgentSession
    from curslink.net.server import HostServer
    from curslink.security import client_context, server_context

    sizes = [{"x": 0, "y": 0, "width": 1920, "height": 1080, "width_mm": 344.0, "height_mm": 194.0}]
    monkeypatch.setattr(curslink.screensize, "physical_sizes", lambda: sizes)
    server = HostServer({"laptop"}, server_context("correct horse battery staple"), host="127.0.0.1", port=0)
    await server.start()
    session = AgentSession(Sink(), lambda e, x, y: None)
    session.mirror = object()
    client = AgentClient("laptop", "Laptop", "127.0.0.1", server.port, client_context("correct horse battery staple"), session)
    task = asyncio.create_task(client.run_once())
    try:
        for _ in range(500):
            if server.is_connected("laptop"):
                break
            await asyncio.sleep(0.01)
        assert server.connections["laptop"].screen_mm == sizes
    finally:
        await server.close()
        await asyncio.wait_for(task, 3)


def test_mirror_rects_go_by_fractions_of_the_whole_screen(qapp):
    # Kyle's laptop: the layout has it at 1920x1080, Qt sees it at another
    # size (Windows scaling), but the pointer goes by fractions: so must the capture.
    from PySide6.QtGui import QGuiApplication

    from curslink.mirror.helper import spec_to_logical

    g = QGuiApplication.primaryScreen().geometry()
    x, y, w, h, index = spec_to_logical({"screen": "main", "os": [960, 540, 480, 270],
                                          "screen_os": [0, 0, 1920, 1080]})
    assert (x, y, w, h) == pytest.approx((g.x() + g.width() / 2, g.y() + g.height() / 2, g.width() / 4,
                                          g.height() / 4))
    assert QGuiApplication.screens()[index] == QGuiApplication.primaryScreen()


def test_crossing_between_fitted_screens_lands_where_the_screens_really_meet():
    from curslink.layout import Fit

    lay = desk()
    # The laptop really stands at (850, 500) on the PC, at half size: its
    # pixels are half the PC's.
    lay.fits = (Fit(("pc", "main"), ("lap", "main"), 850, 500, 0.5),)
    pc, lap = lay.screen("pc", "main"), lay.screen("lap", "main")
    # Off the PC's bottom edge: onto the laptop just under the mirror, whose
    # bottom (the PC's edge) is 200 laptop pixels down, not where the layout puts it.
    c = lay.find_crossing("pc", 900, 599, 0, 2)
    assert c.screen == lap and (c.x, c.y) == pytest.approx((900, 606))
    # Off the laptop's top: onto the PC just above where the laptop really is.
    c = lay.find_crossing("lap", 900, 400, 0, -1)
    assert c.screen == pc and (c.x, c.y) == pytest.approx((900, 498.5))
    # Without mirror mode it's the layout's geometry, as before.
    lay.mirror = False
    assert lay.find_crossing("pc", 900, 599, 0, 2).y == 603


def test_edge_glow_and_label_between_mirrored_screens_follow_the_fit():
    from curslink.edgehint import find_hint
    from curslink.layout import Fit

    # Mirroring, the edge still shows where the pointer really crosses (the
    # bar and label are kept out of the mirror itself).
    lay = desk()
    lay.fits = (Fit(("pc", "main"), ("lap", "main"), 850, 500, 0.5),)
    hint = find_hint(lay, "pc", 900, 590, lambda device: True)
    assert hint.side == "bottom" and hint.target.device_id == "lap"
    # The bar on the PC spans where the laptop really is along its bottom edge...
    assert (hint.a, hint.b) == pytest.approx((850, 1000))
    # ...and the one on the laptop sits on the mirror's bottom edge, 200 px down.
    assert hint.target_side == "top" and hint.trect[1] == 600
    assert (hint.ta, hint.tb) == pytest.approx((0, 300))
    assert find_hint(desk(mirror=False), "pc", 790, 500, lambda device: True) is not None


def test_adjusting_a_mirror_on_itself_moves_and_zooms_the_fit():
    lines = []
    transport = Transport({"lap"}, {"lap"})
    host, router, local = make_host(desk(), transport, lines)
    assert router.layout.pointer_through
    host.fit({"behind": "pc/main", "front": "lap/main", "hold": True, "tune": True})
    show, = transport.of("mirror", "lap")[-1]["shows"]
    assert show["tune"] is True
    assert not router.layout.pointer_through  # the pointer can reach the mirror
    sid = show["id"]
    pc, lap = router.layout.screen("pc", "main"), router.layout.screen("lap", "main")
    # Dragged right by a tenth of its 200 px: the picture moves 20 px right.
    host.on_message("lap", "mirror_tune", {"id": sid, "mx": 0.1, "my": 0})
    f = router.layout.fit(pc, lap)
    assert (f.x, f.y, f.scale) == pytest.approx((780, 400, 1.0))
    # Zoomed 2x about its top-left corner: that corner stays where it is.
    host.on_message("lap", "mirror_tune", {"id": sid, "grow": 2, "ax": 0, "ay": 0})
    f = router.layout.fit(pc, lap)
    assert f.scale == pytest.approx(0.5)
    # The mirror's top-left (the laptop's own top-left now) still shows PC (780, 400).
    assert (f.x, f.y) == pytest.approx((780, 400))
    # From a mirror not being adjusted: nothing.
    host.on_message("lap", "mirror_tune", {"id": "other", "mx": 1})
    # Done: the pointer goes through again, and the mirror lets it.
    host.fit({"behind": "pc/main", "front": "lap/main", "dx": 0, "hold": False})
    assert router.layout.pointer_through and host.tuning is None


def test_the_peer_passes_a_mirrors_adjustments_to_the_host():
    sent = []
    peer = MirrorPeer(lambda line: None, lambda t, **d: sent.append((t, d)))
    peer.handle("mirror", {"captures": [], "shows": [{"id": "s", "screen": "main", "os": [0, 0, 10, 10]}]})
    assert peer.helper_line('mirror_tune {"id": "s", "mx": 0.5, "my": 0}')
    assert peer.helper_line('mirror_tune {"id": "gone", "mx": 0.5}')
    assert sent == [("mirror_tune", {"id": "s", "mx": 0.5, "my": 0})]


def test_a_mirror_being_adjusted_takes_drags_and_the_wheel(qapp):
    from PySide6.QtCore import QEvent, QPoint, QPointF, Qt
    from PySide6.QtGui import QMouseEvent, QWheelEvent

    from curslink.mirror.helper import MirrorHelper

    out = []
    helper = MirrorHelper(out.append)
    spec = {"id": "s", "screen": "main", "os": [200, 150, 200, 100], "label": "PC", "edges": []}
    helper.handle("mirror " + json.dumps({"captures": [], "shows": [spec]}))
    plain = helper.windows["s"]
    assert not plain.tune and plain.testAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
    helper.handle("mirror " + json.dumps({"captures": [], "shows": [{**spec, "tune": True}]}))
    window = helper.windows["s"]
    assert window is not plain and window.tune
    assert not window.testAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)

    def mouse(kind, x, y, button=Qt.MouseButton.LeftButton):
        local = QPointF(x, y)
        glob = QPointF(window.mapToGlobal(QPoint(x, y)))
        return QMouseEvent(kind, local, glob, button, button, Qt.KeyboardModifier.NoModifier)

    # Drag from the middle: it moves.
    window.mousePressEvent(mouse(QEvent.Type.MouseButtonPress, 100, 50))
    window.mouseMoveEvent(mouse(QEvent.Type.MouseMove, 120, 40))
    window.mouseReleaseEvent(mouse(QEvent.Type.MouseButtonRelease, 120, 40))
    tune = json.loads(out[-1].partition(" ")[2])
    assert out[-1].startswith("mirror_tune ") and tune == {"id": "s", "mx": 0.1, "my": -0.1}
    # Drag the right side out by 20 px: 10% bigger, about the left side.
    window.mousePressEvent(mouse(QEvent.Type.MouseButtonPress, 195, 50))
    window.mouseMoveEvent(mouse(QEvent.Type.MouseMove, 215, 50))
    window.mouseReleaseEvent(mouse(QEvent.Type.MouseButtonRelease, 215, 50))
    assert json.loads(out[-1].partition(" ")[2]) == {"id": "s", "grow": 1.1, "ax": 0.0, "ay": 0.5}
    # One wheel notch: 5% bigger about the pointer.
    window.wheelEvent(QWheelEvent(QPointF(50, 25), QPointF(window.mapToGlobal(QPoint(50, 25))), QPoint(0, 0),
                                  QPoint(0, 120), Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier,
                                  Qt.ScrollPhase.NoScrollPhase, False))
    assert json.loads(out[-1].partition(" ")[2]) == {"id": "s", "grow": 1.05, "ax": 0.25, "ay": 0.25}
    window.grab()  # paints the outline and hint without trouble
    helper.handle('mirror {"captures": [], "shows": []}')
    qapp.processEvents()


def test_adjusting_puts_guides_on_both_screens_that_stay_put_behind():
    transport = Transport({"lap"}, {"lap"})
    host, router, local = make_host(desk(), transport, [])
    host.fit({"behind": "pc/main", "front": "lap/main", "hold": True, "tune": True})
    show, = transport.of("mirror", "lap")[-1]["shows"]
    assert show["guides"] == show["src"]
    start = list(show["guides"])
    sid = show["id"]
    host.on_message("lap", "mirror_tune", {"id": sid, "mx": 0.1, "my": 0})
    show, = transport.of("mirror", "lap")[-1]["shows"]
    assert show["guides"] == start and show["src"] != start  # the lines behind don't follow the mirror
    host.fit({"behind": "pc/main", "front": "lap/main", "dx": 0, "hold": False})
    assert all("guides" not in s for s in transport.of("mirror", "lap")[-1]["shows"])


def test_guides_cross_every_side_of_the_covered_area():
    from PySide6.QtCore import QRectF

    from curslink.mirror.helper import guide_shapes

    c = QRectF(100, 50, 300, 200)
    shapes = guide_shapes(c)
    kinds = [k for k, _a, _b in shapes]
    assert kinds.count("h") == 3 and kinds.count("v") == 3 and kinds.count("d") == 2
    (_, centre, radius), = [s for s in shapes if s[0] == "o"]
    assert centre == c.center()
    assert 100 < radius.x() < (300 ** 2 + 200 ** 2) ** 0.5 / 2  # inside the corners, outside the short sides
    for kind, a, b in shapes:
        if kind != "o":
            assert min(a.x(), b.x()) < c.left() - 1000 or min(a.y(), b.y()) < c.top() - 1000  # carried on far past


def test_the_mirror_draws_the_guides_where_they_are_behind(qapp):
    from PySide6.QtCore import QRectF

    from curslink.mirror.helper import GuideOverlay, MirrorHelper

    helper = MirrorHelper(lambda line: None)
    spec = {"id": "s", "screen": "main", "os": [200, 150, 200, 100], "label": "PC", "edges": [], "tune": True,
            "src": [1000, 500, 400, 200], "guides": [1100, 500, 400, 200]}
    helper.handle("mirror " + json.dumps({"captures": [], "shows": [spec]}))
    window = helper.windows["s"]
    # The guides' area starts a quarter of the picture to the right, same size.
    assert window.guide_rect(QRectF(0, 0, 200, 100)) == QRectF(50, 0, 200, 100)
    window.grab()
    g = QGuiApplication_screen_os()
    capture_spec = {"id": "s", "screen": "main", "os": [10, 10, 50, 40], "screen_os": g, "fps": 1,
                    "tune": True, "guides": [20, 20, 50, 40]}
    overlay = GuideOverlay(capture_spec)
    assert overlay.ok and overlay.covered.width() > 0
    overlay.grab()
    overlay.deleteLater()
    helper.handle('mirror {"captures": [], "shows": []}')
    qapp.processEvents()


def QGuiApplication_screen_os():  # noqa: N802
    from PySide6.QtGui import QGuiApplication

    g = QGuiApplication.primaryScreen().geometry()
    return [g.x(), g.y(), g.width(), g.height()]


def test_the_helpers_adjustments_reach_the_mirror_peer():
    import asyncio

    from curslink.app import chain_helper_lines

    class Overlay:
        on_line = None

    got, other = [], []
    overlay = Overlay()
    overlay.on_line = other.append

    class Peer:
        def helper_line(self, line):
            got.append(line)

    loop = asyncio.new_event_loop()
    try:
        chain_helper_lines(overlay, loop, Peer())
        overlay.on_line('mirror_tune {"id": "s", "mx": 0.1, "my": 0}')
        overlay.on_line('mirror_frame {"id": "s", "jpg": "QUJD"}')
        overlay.on_line("drag_end {}")
        loop.run_until_complete(asyncio.sleep(0))
    finally:
        loop.close()
    assert [g.split()[0] for g in got] == ["mirror_tune", "mirror_frame"] and other == ["drag_end {}"]


def test_a_held_chat_goes_onto_the_screen_in_front_instead_of_behind_it():
    lay = desk()
    transport = Transport({"lap"}, {"lap"})
    router = Router(lay, "pc", FakeCapture(), FakeSink(), transport, FakeOverlay())
    dragging = {"on": True}
    router.carries_drag = lambda src, dst: dragging["on"] and (src, dst) == ("pc", "lap")
    router.on_pointer(700, 300)
    router.on_pointer(900, 500)  # holding an Isolink chat: it goes to the laptop
    assert router.active_device == "lap" and (router.x, router.y) == (900, 500)
    router.go_home("test")
    dragging["on"] = False
    router.on_pointer(700, 300)
    router.on_pointer(900, 500)  # nothing held: it stays on the PC, as mirror mode does
    assert router.is_local and (router.x, router.y) == (900, 500)


def test_the_glow_and_label_show_under_the_mirror_too_which_streams_them():
    """Kyle: draw them as the device behind would; the mirror shows that
    part of its screen, so they come through it as drawn there."""
    from curslink.edgehint import EdgeIndicator
    from curslink.layout import Fit

    lay = desk()
    lay.fits = (Fit(("pc", "main"), ("lap", "main"), 850, 500, 0.5),)
    sent = []
    edges = EdgeIndicator(lay, lambda d: True, lambda device, data: sent.append((device, data)),
                          name=lambda d: d, kind=lambda d: "mac")
    edges.update("pc", 900, 590)  # under the laptop, which shows this part of the PC
    assert sorted(d for d, data in sent if "role" in data) == ["lap", "pc"]
    assert dict(sent)["lap"]["role"] == "to" and dict(sent)["pc"]["role"] == "from"
    # Without mirroring, the pointer's own side shows as ever.
    sent2 = []
    EdgeIndicator(desk(mirror=False), lambda d: True, lambda device, data: sent2.append((device, data)),
                  name=lambda d: d, kind=lambda d: "mac").update("pc", 790, 500)
    assert any(data.get("role") == "from" for _d, data in sent2)


# -- the agent's idle cursor ---------------------------------------------------
def _parking():
    from curslink.mirror import CursorPark
    from curslink.net.client import AgentSession
    from curslink.protocol import Message

    sink = FakeSink()
    session = AgentSession(sink, lambda effect, x, y: None)
    timers = []
    park = CursorPark(session, lambda delay, fn: timers.append(fn))
    peer = MirrorPeer(lambda line: None, lambda t, **data: None, on_shows=park.shows)
    show = {"id": "s1", "screen": "main", "os": [900, 0, 1020, 400], "screen_os": [0, 0, 1920, 1080]}

    def fire():
        while timers:
            timers.pop(0)()
    return sink, session, peer, show, fire, Message


def test_an_agents_idle_cursor_moves_off_a_mirror_once_each_time_input_leaves():
    sink, session, peer, show, fire, Message = _parking()
    session.handle(Message("enter", {"x": 1200, "y": 2}))
    session.handle(Message("leave", {}))
    sink.calls.clear()
    peer.handle("mirror", {"captures": [], "shows": [show]})
    fire()
    assert sink.calls == [("move", 1919, 540)]  # the right edge's middle: a pixel of it shows, no hot corner
    peer.handle("mirror", {"captures": [], "shows": []})
    peer.handle("mirror", {"captures": [], "shows": [show]})
    fire()
    assert sink.calls == [("move", 1919, 540)]  # not again (you may be using this computer's own mouse)
    session.handle(Message("enter", {"x": 1200, "y": 2}))
    session.handle(Message("leave", {}))
    sink.calls.clear()
    peer.handle("mirror", {"captures": [], "shows": [show]})
    fire()
    assert sink.calls == [("move", 1919, 540)]


def test_the_cursor_stays_while_input_is_here_or_a_mirror_is_being_adjusted():
    sink, session, peer, show, fire, Message = _parking()
    session.handle(Message("enter", {"x": 1200, "y": 2}))
    sink.calls.clear()
    peer.handle("mirror", {"captures": [], "shows": [show]})
    fire()
    assert sink.calls == []
    session.handle(Message("leave", {}))
    peer.handle("mirror", {"captures": [], "shows": [dict(show, tune=True)]})
    fire()
    assert sink.calls == []


# -- the pointer's real look ----------------------------------------------------
def _png(qapp, w=16, h=24, color="black"):
    from PySide6.QtCore import QBuffer, QByteArray, QIODevice
    from PySide6.QtGui import QColor, QImage

    img = QImage(w, h, QImage.Format.Format_ARGB32)
    img.fill(QColor(color))
    data = QByteArray()
    buf = QBuffer(data)
    buf.open(QIODevice.OpenModeFlag.WriteOnly)
    img.save(buf, "PNG")
    return bytes(data.data())


def test_the_pointers_look_goes_with_a_frame_to_the_mirror(qapp):
    lines = []
    transport = Transport({"lap"}, {"lap"})
    host, router, local = make_host(desk(), transport, lines)
    sid = transport.of("mirror", "lap")[-1]["shows"][0]["id"]
    cursor = {"png": "QUJD", "w": 16, "h": 24, "hx": 8, "hy": 12, "cw": 200}
    local.helper_line("mirror_frame " + json.dumps({"id": sid, "jpg": "AAAA", "w": 200, "h": 200, "cursor": cursor}))
    assert transport.of("mirror_frame", "lap")[-1]["cursor"] == cursor
    # The agent showing it hands the look to its helper on its own line.
    helper = []
    peer = MirrorPeer(helper.append, lambda t, **d: None)
    peer.handle("mirror", {"captures": [], "shows": [{"id": sid, "os": [0, 0, 5, 5]}]})
    peer.handle("mirror_frame", transport.of("mirror_frame", "lap")[-1])
    shape, image = helper[-2:]
    assert shape.startswith("mirror_shape ") and json.loads(shape.partition(" ")[2]) == dict(cursor, id=sid)
    assert image.startswith("mirror_image ") and "cursor" not in image


def test_a_newer_waiting_frame_keeps_the_pointers_look():
    sent = []
    src = MirrorSource(lambda t, **d: sent.append(d))
    src.reset(["a"])
    for n in range(IN_FLIGHT):
        src.offer({"id": "a", "n": n})
    src.offer({"id": "a", "n": 10, "cursor": {"png": "x"}})
    src.offer({"id": "a", "n": 11})
    src.ack("a")
    assert sent[-1] == {"id": "a", "n": 11, "cursor": {"png": "x"}}


def test_the_mirror_draws_the_real_look_at_its_hot_spot(qapp):
    from curslink.mirror.helper import MirrorHelper

    helper = MirrorHelper(lambda line: None)
    helper.handle("mirror " + json.dumps({"captures": [], "shows": [{"id": "s", "screen": "main",
                                                                        "os": [200, 150, 160, 100]}]}))
    png = base64.b64encode(_png(qapp)).decode()
    helper.handle("mirror_shape " + json.dumps({"id": "s", "png": png, "w": 16, "h": 24, "hx": 8, "hy": 12,
                                                "cw": 80}))
    window = helper.windows["s"]
    pix, w, h, hx, hy, cw = window.shape
    assert (w, h, hx, hy, cw) == (16, 24, 8, 12, 80) and not pix.isNull()
    window.set_pointer((0.5, 0.5))
    window.pixmap = window.shape[0]
    window.grab()  # paints the look (twice the size: the mirror is 160 wide for 80 captured)
    # Junk leaves the arrow.
    helper.handle('mirror_shape {"id": "s", "png": "!!", "w": 1, "h": 1, "hx": 0, "hy": 0, "cw": 1}')
    assert window.shape is None
    helper.handle('mirror {"captures": [], "shows": []}')
    assert not helper.shapes


def _jpg(qapp, w, h, color="teal"):
    from PySide6.QtCore import QBuffer, QIODevice
    from PySide6.QtGui import QColor, QImage

    img = QImage(w, h, QImage.Format.Format_RGB32)
    img.fill(QColor(color))
    buf = QBuffer()
    buf.open(QIODevice.OpenModeFlag.WriteOnly)
    img.save(buf, "JPG")
    return bytes(buf.data())


def _still_capture(qapp, monkeypatch, spec, size=(120, 80)):
    from PySide6.QtGui import QColor, QImage

    from curslink.mirror.helper import Capture

    out = []
    cap = Capture(spec, out.append)
    cap.timer.stop()
    still = QImage(*size, QImage.Format.Format_RGB32)
    still.fill(QColor("teal"))
    real = cap.image

    def image():
        real()  # finds where it captures
        cap.has_cursor = False  # a still picture, without this computer's cursor in it
        return still

    monkeypatch.setattr(cap, "image", image)
    return cap, out


def _frame_image(frame):
    from PySide6.QtGui import QImage

    img = QImage()
    assert img.loadFromData(base64.b64decode(frame["jpg"]), "JPG")
    return img


def test_a_capture_casts_the_real_cursor_into_the_picture(qapp, monkeypatch):
    from PySide6.QtGui import QCursor

    import curslink.mirror.cursorshape as cursorshape

    look = {"color": "red"}
    monkeypatch.setattr(cursorshape, "current",
                        lambda dpr=1.0: cursorshape.shape(_png(qapp, 8, 8, look["color"]), 8, 8, 0, 0))
    cap, out = _still_capture(qapp, monkeypatch, {"id": "c", "screen": "main", "os": [10, 10, 120, 80], "fps": 1,
                                                 "cursor": True})
    QCursor.setPos(50, 50)
    if (QCursor.pos().x(), QCursor.pos().y()) != (50, 50):
        pytest.skip("can't place the cursor here")
    out.clear()
    cap.last_hash = b""
    cap.grab()
    frame = json.loads(out[-1].partition(" ")[2])
    assert "cursor" not in frame  # it's in the picture, not sent beside it
    img = _frame_image(frame)
    k = img.width() / 120
    px = img.pixelColor(round(43 * k), round(43 * k))  # inside the look, past its hot spot at (40, 40)
    assert px.red() > 180 and px.green() < 90
    n = len(out)
    cap.grab()
    assert len(out) == n  # same picture, same cursor: nothing new
    look["color"] = "blue"
    cap.grab()
    assert len(out) == n + 1
    # Adjusting (the pointer's on the screen in front): no cursor cast.
    cap.spec = dict(cap.spec, tune=True)
    cap.last_hash = b""
    cap.grab()
    px = _frame_image(json.loads(out[-1].partition(" ")[2])).pixelColor(round(43 * k), round(43 * k))
    assert px.blue() < 150
    cap.stop()


def test_tiles_go_at_full_resolution_and_only_when_they_change(qapp, monkeypatch):
    from PySide6.QtGui import QColor, QPainter

    from curslink.mirror import helper as mh

    cap, out = _still_capture(qapp, monkeypatch, {"id": "c", "screen": "main", "os": [10, 10, 120, 80], "fps": 1,
                                                 "tiles": True}, size=(600, 300))
    out.clear()
    cap.grab()
    tiles = [json.loads(line.partition(" ")[2]) for line in out]
    assert {(f["w"], f["h"]) for f in tiles} == {(600, 300)}
    assert sorted(tuple(f["tile"]) for f in tiles) == [(x, y, min(mh.TILE, 600 - x), min(mh.TILE, 300 - y))
                                                      for x in range(0, 600, mh.TILE) for y in range(0, 300, mh.TILE)]
    assert _frame_image(tiles[0]).width() == mh.TILE  # never scaled down
    out.clear()
    cap.grab()
    assert out == []
    p = QPainter(cap.image())
    p.fillRect(300, 20, 4, 4, QColor("white"))
    p.end()
    cap.grab()
    # Only the changed 32 px square around those pixels, not the whole tile.
    assert [json.loads(line.partition(" ")[2])["tile"] for line in out] == [[288, 0, mh.BLOCK, mh.BLOCK]]
    cap.stop()


def test_a_mirror_puts_its_tiles_together_and_shows_once_whole(qapp):
    from curslink.mirror.helper import MirrorHelper

    helper = MirrorHelper(lambda line: None)
    helper.handle("mirror " + json.dumps({"captures": [], "shows": [{"id": "s", "screen": "main",
                                                                        "os": [200, 150, 160, 100]}]}))
    window = helper.windows["s"]
    jpg = base64.b64encode(_jpg(qapp, 300, 200)).decode()
    helper.handle("mirror_image " + json.dumps({"id": "s", "jpg": jpg, "w": 600, "h": 200, "tile": [0, 0, 300, 200]}))
    assert not window.isVisible()
    helper.handle("mirror_image " + json.dumps({"id": "s", "jpg": jpg, "w": 600, "h": 200, "tile": [300, 0, 300, 200]}))
    assert window.isVisible() and window.canvas.width() == 600 and window.pixmap is None
    window.close()


def test_tiles_wait_for_room_without_dropping_each_other():
    from curslink.mirror import TILES_IN_FLIGHT

    sent = []
    src = MirrorSource(lambda t, **d: sent.append(d))
    src.reset(["a"])
    for n in range(TILES_IN_FLIGHT + 3):
        src.offer({"id": "a", "tile": [n, 0, 1, 1], "n": n})
    src.offer({"id": "a", "tile": [TILES_IN_FLIGHT, 0, 1, 1], "n": 99})  # a newer copy of a waiting tile
    assert len(sent) == TILES_IN_FLIGHT
    for _ in range(3):
        src.ack("a")
    # It replaced the older copy and went last: where parts overlap, newest lands on top.
    assert [f["n"] for f in sent[TILES_IN_FLIGHT:]] == [TILES_IN_FLIGHT + 1, TILES_IN_FLIGHT + 2, 99]


def test_crossing_goes_by_where_the_line_up_put_the_screen_in_front():
    """Kyle: after lining up, the pointer only got into part of the laptop
    (where the layout had it, not where it really stands), and with no
    crossing there, no edge glow or landing effect either."""
    from curslink.layout import Fit

    lay = desk()
    lay.fits = (Fit(("pc", "main"), ("lap", "main"), 500, 450, 1.0),)  # really at 500..1000 x 450..750
    # Down off the PC's bottom edge where the laptop really is (the layout has it from 800 only).
    c = lay.find_crossing("pc", 600, 599, 0, 5)
    assert c is not None and c.screen.device_id == "lap"
    assert (round(c.x), round(c.y)) == (900, 556)
    # Right off the PC's edge where the layout has the laptop but it really isn't.
    assert lay.find_crossing("pc", 999, 420, 5, 0) is None
    # And back up off the laptop's top edge: onto the PC where that really is.
    c = lay.find_crossing("lap", 900, 400, 0, -5)
    assert c is not None and c.screen.device_id == "pc" and (round(c.x), round(c.y)) == (600, 443)


def test_a_mac_capture_that_has_the_cursor_in_it_casts_that_one(qapp, monkeypatch):
    """macOS can draw the cursor as a window of its own, which a grab takes
    in: drawing it in as well showed two."""
    from curslink.mirror import helper as mh

    class Q:
        kCGCursorWindowLevelKey = 19

        @staticmethod
        def CGWindowLevelForKey(key):  # noqa: N802
            return 2147483630

    infos = [{"kCGWindowNumber": 1, "kCGWindowLayer": 0, "kCGWindowOwnerName": "Finder"},
             {"kCGWindowNumber": 2, "kCGWindowLayer": 2147483630, "kCGWindowOwnerName": "WindowServer"},
             {"kCGWindowNumber": 3, "kCGWindowLayer": 25, "kCGWindowOwnerName": "WindowServer",
              "kCGWindowName": "Cursor"},
             {"kCGWindowNumber": 4, "kCGWindowLayer": 25, "kCGWindowOwnerName": "WindowServer",
              "kCGWindowName": "Menubar"}]
    assert mh.mac_cursor_windows(Q, infos) == [2, 3]
    import curslink.mirror.cursorshape as cursorshape

    calls = []
    monkeypatch.setattr(cursorshape, "current", lambda dpr=1.0: calls.append(dpr))
    cap, out = _still_capture(qapp, monkeypatch, {"id": "c", "screen": "main", "os": [10, 10, 120, 80], "fps": 1,
                                                 "cursor": True})
    real = cap.image

    def image():
        img = real()
        cap.has_cursor = True
        return img

    monkeypatch.setattr(cap, "image", image)
    cap.grab()
    assert calls == []  # not looked up, so not drawn in
    cap.stop()


def test_a_diagnostics_bundle_has_the_layout_but_no_secret(tmp_path):
    from curslink.diagnostics import layout_text

    cfg = tmp_path / "curslink.toml"
    cfg.write_text('secret = "hush"\n[host]\nport = 1\n[layout]\nmirror = true\n'
                   '[[layout.fit]]\nbehind = "pc/main"\nfront = "lap/main"\nx = 1.5\ny = 2\nscale = 0.5\n')
    text = layout_text(cfg)
    assert "hush" not in text and "port" not in text
    assert "mirror = true" in text and 'behind = "pc/main"' in text


def test_lossless_tiles_are_png_and_the_mirror_reads_them(qapp, monkeypatch):
    from curslink.mirror import device_states
    from curslink.mirror.helper import MirrorHelper, encode_tile
    from PySide6.QtGui import QColor, QImage

    part = QImage(64, 64, QImage.Format.Format_RGB32)
    part.fill(QColor(10, 200, 30))
    assert encode_tile(part, "lossless").startswith(b"\x89PNG")
    assert encode_tile(part, "high").startswith(b"\xff\xd8")
    cap, out = _still_capture(qapp, monkeypatch, {"id": "c", "screen": "main", "os": [10, 10, 120, 80], "fps": 1,
                                                 "tiles": True, "quality": "lossless"}, size=(100, 50))
    out.clear()
    cap.grab()
    frame = json.loads(out[0].partition(" ")[2])
    assert base64.b64decode(frame["jpg"]).startswith(b"\x89PNG")
    helper = MirrorHelper(lambda line: None)
    helper.handle("mirror " + json.dumps({"captures": [], "shows": [{"id": "c", "screen": "main",
                                                                        "os": [200, 150, 160, 100]}]}))
    helper.handle("mirror_image " + json.dumps(frame))
    window = helper.windows["c"]
    assert window.isVisible() and window.canvas.pixelColor(5, 5) == QColor("teal")
    window.close()
    cap.stop()
    # The host passes the setting on with tiles.
    s = Stream(Screen("main", "pc", 0, 0, 100, 100), Screen("main", "lap", 50, 50, 100, 100), (50, 50, 50, 50))
    states = device_states([s], {}, 30, tiles=lambda _s: True, quality="lossless")
    assert states["pc"]["captures"][0]["quality"] == "lossless"


def test_mirror_quality_must_be_one_of_the_choices(tmp_path):
    from curslink.config import ConfigError, _mirror_quality

    assert _mirror_quality({}) == "auto" and _mirror_quality({"mirror_quality": "auto"}) == "auto"
    assert _mirror_quality({"mirror_quality": "lossless"}) == "lossless"
    with pytest.raises(ConfigError):
        _mirror_quality({"mirror_quality": "ultra"})


def test_the_landing_effect_goes_by_the_hosts_screen_box():
    """Kyle: the landing animation played in the wrong place coming out of
    a mirrored part. The agent placed it by its own screen list, which with
    Windows scaling can be in other pixels than the point it got."""
    from curslink.net.client import AgentSession
    from curslink.protocol import Message

    shown = []
    session = AgentSession(FakeSink(), lambda *a: shown.append(a))
    session.handle(Message("enter", {"x": 1211, "y": 331, "effect": "pop", "box": [0, 0, 2560, 1600]}))
    session.handle(Message("enter", {"x": 5, "y": 6, "effect": "pop"}))  # an older host: no box
    assert shown == [("pop", 1211, 331, (0, 0, 2560, 1600)), ("pop", 5, 6)]
    transport = Transport({"lap"}, {"lap"})
    host, router, _local = make_host(desk(), transport)
    router.switch_to(router.layout.screen("lap", "main"), 1000, 500, "edge")
    enter = transport.of("enter", "lap")[-1]
    assert enter["box"] == [0, 0, 500, 300]


# -- joined screens -------------------------------------------------------------------


def wall(joined=None, gap=0):
    """Kyle's desk: A (a laptop) in front of B (the Mac's ultrawide), and C
    (another Mac monitor) to B's right, ``gap`` px apart."""
    devices = [
        Device("mac", "Mac", (Screen("b", "mac", 0, 0, 3440, 1440),
                              Screen("c", "mac", 3440 + gap, 0, 1920, 1080, os_x=3440))),
        Device("pc", "Laptop", (Screen("a", "pc", -200, 900, 1000, 600, z=1),)),
    ]
    return Layout.from_devices(devices, allow_overlap=True, pointer_through=True, mirror=True,
                               mirror_joined=joined)


def test_joined_screens_share_an_edge_and_skip_the_screen_in_front():
    lay = wall()
    b, c, a = lay.screen("mac", "b"), lay.screen("mac", "c"), lay.screen("pc", "a")
    assert lay.joined(b) == [c]
    assert lay.joined(c) == [b]  # A overlaps B: not joined to it, and C's row stops there
    assert not lay.touching(a, b)
    apart = wall(gap=40)
    assert apart.joined(apart.screen("mac", "b")) == [apart.screen("mac", "c")]
    assert apart.joined(apart.screen("mac", "b"), gap=10) == []


def test_joined_screens_carry_on_along_a_row():
    devices = [Device("mac", "Mac", (Screen("b", "mac", 0, 0, 1000, 600), Screen("c", "mac", 1000, 0, 800, 600),
                                     Screen("d", "mac", 1800, 100, 800, 600))),
               Device("pc", "Laptop", (Screen("a", "pc", 100, 300, 400, 300, z=1),))]
    lay = Layout.from_devices(devices, allow_overlap=True, mirror=True)
    b = lay.screen("mac", "b")
    assert [s.id for s in lay.joined(b)] == ["c", "d"]


def test_mirror_stays_on_while_on_a_joined_screen_only_when_set():
    off = wall()
    assert streams_for(off, off.screen("mac", "c")) == []
    on = wall({("mac", "b"): "all"})
    c = on.screen("mac", "c")
    stream, = streams_for(on, c)
    assert stream.id == "mac:b>pc:a"
    assert streams_for(on, c, joined=False) == []
    # On the laptop itself, its own desktop shows.
    assert streams_for(on, on.screen("pc", "a")) == []
    # Listed: only those screens count.
    assert streams_for(wall({("mac", "b"): frozenset({("mac", "c")})}), c)
    assert streams_for(wall({("mac", "b"): frozenset({("mac", "x")})}), c) == []


def test_host_keeps_the_mirror_up_moving_onto_a_joined_screen():
    lay = wall({("mac", "b"): "all"})
    transport = Transport({"pc"}, {"pc"})
    router = Router(lay, "mac", FakeCapture(), FakeSink(), transport, FakeOverlay())
    host = None
    lines = []
    local = MirrorPeer(lines.append, lambda t, **d: host.local_send(t, **d))
    host = MirrorHost(lay, router, transport, local, fps=12, clock=lambda: 0.0)
    router.on_moved = host.update
    router.on_pointer(1000, 300)
    host.refresh()
    assert transport.of("mirror", "pc")[-1]["shows"]
    router.on_pointer(4000, 300)  # onto C
    assert router.screen.id == "c"
    assert transport.of("mirror", "pc")[-1]["shows"]
    state = json.loads(lines[-1].partition(" ")[2])
    assert "cursor" not in state["captures"][0]  # the pointer isn't on B to cast


def test_parse_mirror_joined():
    from curslink.config import ConfigError, parse_layout

    raw = {"layout": {"allow_overlap": True, "mirror": True,
                      "mirror_joined": {"mac/b": "all", "mac/c": ["mac/b", "gone/x"], "gone/y": "all"}},
           "devices": [{"id": "mac", "screens": [{"id": "b", "x": 0, "y": 0, "width": 100, "height": 100},
                                                 {"id": "c", "x": 100, "y": 0, "width": 100, "height": 100}]}]}
    lay = parse_layout(raw)
    assert lay.mirror_joined == {("mac", "b"): "all", ("mac", "c"): frozenset({("mac", "b")})}
    raw["layout"]["mirror_joined"] = {"mac/b": 3}
    with pytest.raises(ConfigError):
        parse_layout(raw)


def test_one_click_places_the_screen_in_front_at_its_real_size():
    from curslink.mirror import apply_fit

    lay = desk()
    pc, lap = lay.screen("pc", "main"), lay.screen("lap", "main")
    # Without real sizes, 1:1: its top-left corner where it was clicked.
    assert apply_fit(lay, {"behind": "pc/main", "front": "lap/main", "place": [1500, 900], "corner": [0, 0]}) \
        == (1500, 900, 1.0)
    # With them, at its real size; the corner clicked stays where it was clicked.
    lay.physical[("pc", "main")] = (pc.width / 4.0, pc.height / 4.0)      # 4 px per mm
    lay.physical[("lap", "main")] = (lap.width / 8.0, lap.height / 8.0)   # 8 px per mm
    x, y, k = apply_fit(lay, {"behind": "pc/main", "front": "lap/main", "place": [1500, 900], "corner": [1, 1]})
    assert k == pytest.approx(0.5)
    assert (x + lap.width * k, y + lap.height * k) == pytest.approx((1500, 900))


def test_lining_up_in_mirror_mode_takes_one_click_behind(qapp):
    from curslink.gui.fitcalib import FitCalibration

    sent = []
    behind = {"device": "mac", "name": "Mac", "rect": [0, 0, 3440, 1440],
              "screen": {"id": "screen2", "os_x": 0, "os_y": 0, "width": 3440, "height": 1440}}
    front = {"device": "phone", "name": "Phone", "rect": [1500, 900, 384, 832],
             "screen": {"id": "phone", "os_x": 0, "os_y": 0, "width": 384, "height": 832}}
    cal = FitCalibration(behind, front, "host", sent.append, one_click=True)
    got = []
    cal.finished.connect(got.append)
    assert cal.start()
    hint = json.loads(sent[1].partition(" ")[2])
    assert list(hint["devices"]) == ["mac"]  # nothing asked of the phone
    corner = cal.corners[0]
    cal.on_event({"event": "picked", "device": "mac", "screen": "screen2", "x": 1600, "y": 950})
    from curslink.gui.fitcalib import corner_at

    assert got == [{"place": [1600.0, 950.0], "corner": list(corner_at(corner, 1.0, 1.0))}]


def test_placing_it_again_while_adjusting_moves_the_guides_there():
    transport = Transport({"lap"}, {"lap"})
    host, router, local = make_host(desk(), transport, [])
    host.fit({"behind": "pc/main", "front": "lap/main", "hold": True, "tune": True})
    old = transport.of("mirror", "lap")[-1]["shows"][0]["guides"]
    host.fit({"behind": "pc/main", "front": "lap/main", "place": [100, 100], "corner": [0, 0],
              "hold": True, "tune": True})
    show, = transport.of("mirror", "lap")[-1]["shows"]
    assert show["guides"] != old and show["guides"] == show["src"]


def test_fine_tuning_starts_from_the_layout_when_the_saved_line_up_is_far_off():
    pytest.importorskip("PySide6.QtCore")
    from curslink.gui.pages.layout import far_from_layout

    ultrawide, phone = (0, 0, 3440, 1440), (1500, 1000, 384, 832)
    assert far_from_layout((570.2, 1321.3, 0.3149), ultrawide, phone)     # Kyle's old one, on the left
    assert not far_from_layout((1520, 1010, 0.8), ultrawide, phone)       # roughly where it's laid out


class FakeStream:
    """A ScreenCaptureKit stream as the capture sees it."""

    def __init__(self):
        self.users = 0
        self.dead = False
        self.configured = []
        self.latest = None

    def acquire(self):
        self.users += 1

    def release(self):
        self.users -= 1

    def configure(self, *args):
        self.configured.append(args)

    def stalled(self):
        return self.dead

    def frame(self):
        return self.latest


def test_a_mac_capture_streams_and_sends_only_new_frames(qapp, monkeypatch):
    """macOS: one ScreenCaptureKit stream per screen, and nothing new from
    it means no work at all (DisplayLink froze under 30 screenshots a second)."""
    from PySide6.QtGui import QColor, QImage

    from curslink.mirror import helper, macstream

    stream = FakeStream()
    monkeypatch.setattr(macstream, "stream_for", lambda *a: stream)
    monkeypatch.setattr(helper.QGuiApplication, "platformName", staticmethod(lambda: "cocoa"))
    monkeypatch.setattr(helper, "mac_unseen_windows", lambda: {7})
    out = []
    cap = helper.Capture({"id": "c", "screen": "main", "os": [10, 10, 120, 80], "fps": 30, "cursor": True}, out.append)
    cap.timer.stop()
    assert cap.mac_stream_image(10, 10, 120, 80, 0) is None and cap.skip  # starting: wait quietly
    assert stream.users == 1
    (rect, scale, fps, cursor, unseen), = set(stream.configured)
    assert rect == (10, 10, 120, 80) and fps == 30 and cursor and unseen == frozenset({7})
    img = QImage(120, 80, QImage.Format.Format_ARGB32_Premultiplied)
    img.fill(QColor("tomato"))
    stream.latest = (1, img)
    cap.skip = False
    got = cap.mac_stream_image(10, 10, 120, 80, 0)
    assert got is img and cap.has_cursor  # the stream draws the real cursor in
    cap.last_sent = __import__("time").monotonic()
    cap.skip = False
    assert cap.mac_stream_image(10, 10, 120, 80, 0) is None and cap.skip  # same frame: nothing to do
    stream.dead = True
    cap.skip = False
    assert cap.mac_stream_image(10, 10, 120, 80, 0) is None and not cap.skip  # broken: screenshots instead
    cap.stop()
    assert stream.users == 0 and cap.stream is None


def test_a_stream_forgets_its_picture_when_the_area_moves_and_lingers_before_stopping(monkeypatch):
    from curslink.mirror import macstream

    s = macstream.MacStream(5, (0, 0, 1000, 800))
    applied = []
    monkeypatch.setattr(s, "_apply", lambda: applied.append(s.want))
    stopped = []
    monkeypatch.setattr(s, "stop", lambda: stopped.append(True))
    s.acquire()
    s.configure((0, 0, 100, 50), 2.0, 30, True, None)
    s.latest = (3, "img")
    s.configure((0, 0, 100, 50), 2.0, 30, True, None)  # the same: nothing to change
    assert len(applied) == 1 and s.frame() == (3, "img")
    s.configure((0, 0, 100, 50), 2.0, 30, True, frozenset({9}))  # a mirror came up: filter only
    assert len(applied) == 2 and s.frame() == (3, "img")
    s.configure((40, 0, 100, 50), 2.0, 30, True, frozenset({9}))  # moved: the old picture is wrong now
    assert len(applied) == 3 and s.frame() is None
    macstream._streams[5] = s
    try:
        s.release()
        macstream.reap(s.released_at + macstream.LINGER_S - 1)
        assert not stopped
        macstream.reap(s.released_at + macstream.LINGER_S + 1)
        assert stopped
    finally:
        macstream._streams.pop(5, None)


def test_a_stream_that_never_starts_gives_way_to_screenshots(monkeypatch):
    from curslink.mirror import macstream

    s = macstream.MacStream(6, (0, 0, 10, 10))
    monkeypatch.setattr(s, "stop", lambda: None)
    try:
        assert not s.stalled(s.made_at + 1)
        assert s.stalled(s.made_at + macstream.START_S + 1)
        assert 6 in macstream._broken
    finally:
        macstream._broken.discard(6)


@pytest.mark.skipif(__import__("sys").platform != "darwin", reason="macOS only")
def test_screencapturekit_bridge_has_what_the_stream_uses(qapp):
    """The pyobjc names the stream calls exist, and frames read back."""
    import CoreMedia
    import objc
    import Quartz
    import ScreenCaptureKit as sck

    from curslink.mirror import macstream

    cls = macstream._output_class(sck, objc)
    out = cls.alloc().init()
    assert out.respondsToSelector_(b"stream:didOutputSampleBuffer:ofType:")
    s = macstream.MacStream(1, (0, 0, 1000, 800))
    cfg = s._config(sck, CoreMedia, Quartz, ((10, 20, 100, 50), 2.0, 30, True, None))
    assert (cfg.width(), cfg.height()) == (200, 100) and cfg.showsCursor()
    assert sck.SCStreamFrameInfoStatus
    assert macstream.display_for(Quartz, 0, 0, 1, 1) is None or True  # callable without a screen
    err, pix = Quartz.CVPixelBufferCreate(None, 8, 4, macstream._KCV_BGRA, None, None)
    assert err == 0
    img = macstream.pixel_buffer_to_qimage(Quartz, pix)
    assert img is not None and (img.width(), img.height()) == (8, 4)


def test_mirror_fps_is_auto_by_default_and_goes_to_120():
    from curslink.config import ConfigError, _mirror_fps

    assert _mirror_fps({}) == 0 and _mirror_fps({"mirror_fps": "auto"}) == 0
    assert _mirror_fps({"mirror_fps": 120}) == 120 and _mirror_fps({"mirror_fps": 500}) == 120
    assert _mirror_fps({"mirror_fps": 0}) == 1
    with pytest.raises(ConfigError):
        _mirror_fps({"mirror_fps": "fast"})


def test_auto_frame_rate_follows_the_worse_link_and_a_set_one_stays():
    lay = desk()
    router = Router(lay, "pc", FakeCapture(), FakeSink(), Transport({"lap"}, mirror={"lap"}), FakeOverlay())

    class Server(Transport):
        levels = {}

        def link_level(self, device_id):
            return self.levels.get(device_id, "good")

    server = Server({"lap"}, mirror={"lap"})
    host = MirrorHost(lay, router, server, None, 0)
    (s,) = streams_for(lay, lay.screen("pc", "main"))
    assert host.stream_fps(s) == 120
    Server.levels = {"lap": "fair"}
    assert host.stream_fps(s) == 30
    Server.levels = {"lap": "poor"}
    assert host.stream_fps(s) == 15
    assert MirrorHost(lay, router, server, None, 60).stream_fps(s) == 60
    assert MirrorHost(lay, router, server, None, 999).stream_fps(s) == 120


def test_a_capture_never_runs_faster_than_its_screen_refreshes(qapp, monkeypatch):
    from curslink.mirror import helper

    cap = helper.Capture({"id": "c", "screen": "main", "os": [10, 10, 120, 80], "fps": 120}, lambda line: None)
    cap.timer.stop()
    screen = helper.QGuiApplication.screens()[0]
    monkeypatch.setattr(type(screen), "refreshRate", lambda self: 60.0)
    assert cap.fps() == 60
    monkeypatch.setattr(type(screen), "refreshRate", lambda self: 120.0)
    assert cap.fps() == 120
    cap.spec = dict(cap.spec, fps=24)
    assert cap.fps() == 24


def test_only_the_changed_squares_of_each_tile_are_found():
    import numpy as np

    from curslink.mirror.helper import BLOCK, TILE, changed_parts

    before = np.zeros((300, 600), dtype=np.uint32)
    assert changed_parts(before, before.copy()) == []  # still: nothing to send
    after = before.copy()
    after[5, 5] = 1  # one pixel in the first tile
    after[40:70, 260:300] = 7  # across squares in the second tile
    after[299, 599] = 3  # the last, partial, tile's corner
    assert changed_parts(before, after) == [
        (0, 0, BLOCK, BLOCK),
        (256, 32, 64, 64),
        (576, 288, 24, 12),
    ]
    assert TILE % BLOCK == 0


def test_overlapping_parts_keep_their_order_and_covered_ones_go():
    from curslink.mirror import IN_FLIGHT, TILES_IN_FLIGHT

    sent = []
    src = MirrorSource(lambda t, **d: sent.append(d))
    src.reset(["a"])
    for n in range(TILES_IN_FLIGHT):
        src.offer({"id": "a", "tile": [1000 + n, 0, 1, 1], "n": n})
    src.offer({"id": "a", "tile": [0, 0, 32, 32], "n": "small"})
    src.offer({"id": "a", "tile": [0, 0, 64, 32], "n": "wider"})  # covers the small one: it's out of date
    src.offer({"id": "a", "tile": [32, 0, 64, 64], "n": "overlap"})  # only overlaps: both go, in order
    for _ in range(4):
        src.ack("a")
    assert [f["n"] for f in sent[TILES_IN_FLIGHT:]] == ["wider", "overlap"]


def test_the_host_asks_for_the_whole_picture_when_a_mirror_is_new_in_front():
    lay = desk()
    transport = Transport({"lap"}, mirror={"lap"})
    router = Router(lay, "pc", FakeCapture(), FakeSink(), transport, FakeOverlay())
    host = MirrorHost(lay, router, transport, None, 30)
    host.local = object()  # this computer captures (never called here)
    host._to = lambda device, t, **data: transport.sent.append((device, t, data))
    host.refresh()
    key = lambda: [c["key"] for d, t, data in transport.sent if t == "mirror" and d == "pc"
                   for c in data["captures"]][-1]
    first = key()
    host.refresh()  # nothing new in front: the same key
    assert key() == first
    host.sent.pop("lap")  # the laptop went and came back: its mirror starts empty
    host.refresh()
    assert key() == first + 1


def test_a_new_key_sends_every_tile_again(qapp, monkeypatch):
    cap, out = _still_capture(qapp, monkeypatch, {"id": "c", "screen": "main", "os": [10, 10, 120, 80], "fps": 1,
                                                 "tiles": True, "key": 1}, size=(600, 300))
    cap.grab()
    out.clear()
    cap.grab()
    assert out == []
    cap.set(dict(cap.spec, key=2))
    cap.timer.stop()
    out.clear()
    cap.grab()
    assert len(out) == 6  # 3 x 2 tiles


def test_auto_quality_follows_the_worse_link():
    lay = desk()
    router = Router(lay, "pc", FakeCapture(), FakeSink(), Transport({"lap"}, mirror={"lap"}), FakeOverlay())

    class Server(Transport):
        levels = {}

        def link_level(self, device_id):
            return self.levels.get(device_id, "good")

    server = Server({"lap"}, mirror={"lap"})
    host = MirrorHost(lay, router, server, None, 0, quality="auto")
    (s,) = streams_for(lay, lay.screen("pc", "main"))
    assert host.stream_quality(s) == "lossless"
    Server.levels = {"lap": "fair"}
    assert host.stream_quality(s) == "high"
    Server.levels = {"lap": "poor"}
    assert host.stream_quality(s) == "standard"
    assert MirrorHost(lay, router, server, None, 0, quality="standard").stream_quality(s) == "standard"


def test_a_cursor_look_that_cant_be_read_is_cast_as_an_arrow(qapp, monkeypatch):
    """On Windows reading the look once failed every time (a 64-bit handle
    given to ctypes as a C int), and the capture then drew no cursor at all."""
    from PySide6.QtGui import QImage

    import curslink.mirror.cursorshape as cursorshape

    def broken(dpr):
        raise OverflowError("int too long to convert")

    monkeypatch.setattr(cursorshape.sys, "platform", "win32")
    monkeypatch.setattr(cursorshape, "_windows", broken)
    monkeypatch.setattr(cursorshape, "_failed", 0.0)
    look = cursorshape.current(1.0)
    assert look is not None and look == cursorshape.arrow()
    img = QImage()
    assert img.loadFromData(base64.b64decode(look["png"]), "PNG")
    assert (look["hx"], look["hy"]) == (1, 1) and look["w"] < 20 and look["h"] < 30
    assert img.pixelColor(round(img.width() * 0.2), round(img.height() * 0.4)).alpha() > 200  # filled in
    # Tried again later, not given up on for good.
    calls = []
    monkeypatch.setattr(cursorshape, "_windows", lambda dpr: calls.append(dpr) or {"key": "real"})
    assert cursorshape.current(1.0) == cursorshape.arrow() and calls == []
    monkeypatch.setattr(cursorshape, "_failed", cursorshape.time.monotonic() - cursorshape.RETRY_S - 1)
    assert cursorshape.current(1.0) == {"key": "real"} and calls == [1.0]
    # Hidden is still nothing drawn.
    monkeypatch.setattr(cursorshape, "_windows", lambda dpr: None)
    assert cursorshape.current(1.0) is None


@pytest.mark.skipif(sys.platform != "win32", reason="Windows only")
def test_the_windows_cursor_look_is_read_without_ctypes_errors(qapp, monkeypatch):
    import curslink.mirror.cursorshape as cursorshape

    monkeypatch.setattr(cursorshape, "_failed", 0.0)
    monkeypatch.setattr(cursorshape, "_win_last", None)
    look = cursorshape._windows(1.0)  # raises on a ctypes mistake; None if the runner hides the cursor
    assert look is None or (look["png"] and look["w"] > 0)
    cursorshape.current(1.0)
    assert cursorshape._failed == 0.0


def test_off_the_back_screen_where_only_real_sizes_disagree_the_pointer_still_crosses():
    """Kyle (0.4.71): PC laptop in front of a Mac's ultrawide, never lined
    up. By their real sizes the laptop covers less of the ultrawide than the
    layout draws, so going down off the ultrawide's bottom edge above the
    laid-out PC went nowhere, and the pointer had no way back to the PC."""
    devices = [
        Device("pc", "Kyles_PC", (Screen("main", "pc", -436, -719, 2880, 1620, z=1),)),
        Device("mac", "WKS82261", (Screen("screen2", "mac", 752, -1681, 3440, 1440, os_x=-3440, os_y=-461),
                                   Screen("main", "mac", 4192, -1222, 1512, 982))),
    ]
    lay = Layout.from_devices(devices, allow_overlap=True, pointer_through=True, mirror=True)
    lay.physical.update({("pc", "main"): (344, 194), ("mac", "screen2"): (787, 330), ("mac", "main"): (301, 196)})
    s2 = lay.screen("mac", "screen2")
    assert lay.sized_fit(s2, lay.screen("pc", "main")) is not None  # the real sizes make a fit
    c = lay.find_crossing("mac", 2100, -242, 0, 20)
    assert c is not None and c.screen.device_id == "pc" and c.screen.contains(c.x, c.y)
    # The router: down through the ultrawide, onto the PC.
    transport = Transport({"mac"}, {"mac"})
    router = Router(lay, "pc", FakeCapture(), FakeSink(), transport, FakeOverlay())
    now = [10.0]
    router.clock = lambda: now[0]
    router.switch_to(s2, 2340, -720, "edge")
    now[0] += 5
    for _ in range(60):
        router.on_motion(-10, 20)
        if router.is_local:
            break
    assert router.is_local
    # Lined up by hand, though, the line-up alone decides (see the test above).
    from curslink.layout import Fit

    lay.fits = (Fit(("mac", "screen2"), ("pc", "main"), 0, 1300, 0.5),)
    assert lay.find_crossing("mac", 2300, -242, 0, 20) is None  # laid out there, lined up elsewhere


def test_a_diagnostics_bundle_has_every_setting_and_screen_but_no_secret(tmp_path):
    """Kyle: the layout should be in the logs, not need a screenshot."""
    from curslink.diagnostics import REDACTED, config_text

    cfg = tmp_path / "curslink.toml"
    cfg.write_text('secret = "hush"\n[pairing]\nstore = "x"\n[host]\nport = 24850\nreturn_hotkey = "ctrl+alt+home"\n'
                   '[host.relay]\nkey = "shh"\nauth_token = "t0k"\n[layout]\nmirror = true\n'
                   '[[devices]]\nid = "mac"\nname = "WKS82261"\n'
                   '[[devices.screens]]\nid = "screen2"\nx = 752\ny = -1681\nwidth = 3440\nheight = 1440\n')
    (tmp_path / "curslink.host.toml").write_text('[layout]\nallow_overlap = true\nsecret = "also hush"\n')
    text = config_text(cfg)
    for secret in ("hush", "shh", "t0k"):
        assert secret not in text
    assert REDACTED in text
    assert "port = 24850" in text and 'return_hotkey = "ctrl+alt+home"' in text
    assert "x = 752" in text and "width = 3440" in text and "allow_overlap = true" in text
    assert "# curslink.host.toml" in text


# -- a held chat crossing next to the mirrored part (Kyle, 0.4.76) ------------------
def _kyles_desk():
    """Kyle's desk on 0.4.76: a PC host in front of the bottom-left corner
    of a Mac's ultrawide, lined up (a Fit), mirror mode on."""
    from curslink.layout import Fit

    devices = [
        Device("kyles-pc", "Kyles_PC", (Screen("main", "kyles-pc", -1127, -581, 2880, 1620, z=2),)),
        Device("wks82261", "WKS82261", (
            Screen("main", "wks82261", 4196, -1234, 1512, 982),
            Screen("screen2", "wks82261", 756, -1692, 3440, 1440, os_x=-3440, os_y=-458, z=1),
        )),
    ]
    fits = (Fit(("wks82261", "screen2"), ("kyles-pc", "main"), -532.6, 1151.8, 0.5227),)
    return Layout.from_devices(devices, allow_overlap=True, pointer_through=True, mirror=True, fits=fits)


def _chat_router():
    transport = Transport({"wks82261"}, {"wks82261"})
    router = Router(_kyles_desk(), "kyles-pc", FakeCapture(), FakeSink(), transport, FakeOverlay())
    router.carries_drag = lambda src, dst: True  # an Isolink chat is held all along
    return router


def test_a_chat_carried_up_off_the_pc_stays_on_the_mac_through_a_wobble():
    """0.4.76: up off the PC's top edge the pointer landed 2 px above the
    part of the Mac's screen that the PC covers; the next move a pixel or
    two down carried the chat straight back (three times in 13 s)."""
    router = _chat_router()
    router.on_pointer(2480, 10)
    router.on_pointer(2480, 0)
    assert router.active_device == "wks82261", router.active_device
    x, y = router.x, router.y
    behind = router.layout.covered_parts(router.screen)[0][1]
    assert behind[1] - 3 <= y < behind[1]  # just above it, as on Kyle's desk
    router.on_motion(-3, 4)   # the hand wobbles into the covered part
    router.on_motion(2, -1)
    assert router.active_device == "wks82261"
    router.on_motion(0, 30)   # but going on down into it carries it back
    assert router.active_device == "kyles-pc"


def test_a_chat_carried_off_the_pc_and_away_can_come_back_through_the_covered_part():
    router = _chat_router()
    router.on_pointer(2480, 10)
    router.on_pointer(2480, 0)
    assert router.active_device == "wks82261"
    router.on_motion(0, -60)  # well clear of the covered part: armed again
    router.on_motion(0, 62)   # back into it by a couple of pixels
    assert router.active_device == "kyles-pc"
