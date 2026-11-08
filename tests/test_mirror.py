"""Mirror mode: screens in front show what they cover of the screen in use."""

from __future__ import annotations

import base64
import json
import os

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
    assert states["pc"] == {"captures": [{"id": stream.id, "screen": "main", "os": [800, 400, 200, 200], "fps": 15}],
                            "shows": []}
    show, = states["lap"]["shows"]
    assert show["os"] == [0, 0, 200, 200] and show["label"] == "WKS82261"
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
    # The pointer under the laptop is drawn in its mirror.
    router.on_pointer(900, 450)
    assert transport.of("mirror_pointer", "lap")[-1] == {"id": sid, "x": 0.5, "y": 0.25}
    router.on_pointer(500, 300)
    assert transport.of("mirror_pointer", "lap")[-1] == {"id": sid, "hide": True}


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

    cfg = host_config(raw(allow_overlap=True, mirror=True, mirror_fps=40))
    assert cfg.layout.mirror and cfg.layout.pointer_through and cfg.mirror_fps == 30
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


def test_two_clicked_corners_give_the_fit():
    pytest.importorskip("PySide6.QtCore")
    from curslink.gui.fitcalib import solve

    assert solve([(2640, 1100), (1200, 1102)], "top", (2880, 1536)) == (1200, 1101, 0.5)
    x, y, k = solve([(1200, 900), (2640, 900)], "bottom", (2880, 1536))
    assert (x, k) == (1200, 0.5) and y == 900 - 768
    assert solve([(1200, 900), (1210, 900)], "top", (2880, 1536)) is None  # too close


def test_lining_up_on_another_computer_goes_by_its_clicks(qapp):
    from curslink.gui.fitcalib import FitCalibration

    sent = []
    behind = {"device": "mac", "name": "Mac", "screen": {"id": "screen2", "os_x": 0, "os_y": 0,
                                                          "width": 3440, "height": 1440}}
    cal = FitCalibration(behind, {"name": "PC", "width": 2880, "height": 1536}, "pc", sent.append)
    got = []
    cal.finished.connect(got.append)
    assert cal.start() and sent[0] == "pick on"
    hint = json.loads(sent[1].partition(" ")[2])
    assert hint["devices"]["mac"]["screens"][0]["fit"]["corners"] == "top"
    cal.on_event({"event": "picked", "key": "tab"})
    assert cal.corners == "bottom"
    cal.on_event({"event": "picked", "key": "tab"})
    cal.on_event({"event": "picked", "device": "mac", "screen": "screen2", "x": 1200, "y": 1100})
    cal.on_event({"event": "picked", "device": "other", "screen": "screen2", "x": 5, "y": 5})  # not that screen
    cal.on_event({"event": "picked", "device": "mac", "screen": "screen2", "x": 2640, "y": 1100})
    assert got == [(1200, 1100, 0.5)] and sent[-2:] == ["pick off", 'hint {"devices":{}}']


def test_the_fit_layer_paints(qapp):
    from PySide6.QtCore import QPointF
    from PySide6.QtGui import QImage, QPainter

    from curslink.gui.fitcalib import FitState, frame_for, paint_fit

    st = FitState("PC", 2880 / 1536, "top", [QPointF(100, 300)], QPointF(500, 302))
    frame = frame_for(st)
    assert round(frame.width()) == 400 and round(frame.height()) == round(400 / (2880 / 1536))
    img = QImage(800, 600, QImage.Format.Format_ARGB32)
    p = QPainter(img)
    paint_fit(p, img.rect().toRectF(), st)
    p.end()
