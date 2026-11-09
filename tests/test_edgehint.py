"""The edge indicator: which edge is near, where it leads, and what each device is told."""

import pytest

from conftest import FakeCapture, FakeOverlay, FakeSink, FakeTransport
from curslink import protocol
from curslink.edgehint import RANGE_PX, EdgeIndicator, command_line, device_kind, find_hint
from curslink.layout import Device, EdgeEnd, EdgeLink, Layout, Screen
from curslink.net.client import AgentSession
from curslink.router import Router

MAC = Device("mac", "Kyle's Mac", (Screen("main", "mac", 0, 0, 2560, 1440),
                                   Screen("screen2", "mac", 2560, 0, 2560, 1440, os_x=2560)))
LAP = Device("lap", "WKS82261", (Screen("main", "lap", 5120, 600, 1920, 1080),))
LINK = EdgeLink(EdgeEnd("mac", "screen2", "right", 643, 1389), EdgeEnd("lap", "main", "left", 0, 1080))


def everyone(_device):
    return True


def test_no_hint_away_from_edges(layout):
    assert find_hint(layout, "desktop", 1000, 500, everyone) is None


def test_no_hint_between_two_screens_of_the_same_device(layout):
    # desktop/left's right edge leads to desktop/right: nothing to show.
    assert find_hint(layout, "desktop", 1915, 500, everyone) is None


def test_hint_near_an_edge_to_another_device(layout):
    hint = find_hint(layout, "desktop", 3840 - 40, 500, everyone)
    assert hint is not None
    assert (hint.here.id, hint.side) == ("right", "right")
    assert (hint.target.device_id, hint.target_side) == ("laptop", "left")
    # The laptop sits from y 200 to 1000: that's the stretch that leads across.
    assert (hint.a, hint.b) == (200, 1000)
    assert hint.at == 500
    assert (hint.ta, hint.tb) == (0, 800)
    assert hint.tat == pytest.approx(300)
    assert 0 < hint.near < 1


def test_hint_strength_grows_towards_the_edge(layout):
    far = find_hint(layout, "desktop", 3839 - RANGE_PX + 5, 500, everyone)
    close = find_hint(layout, "desktop", 3839, 500, everyone)
    assert far.near < 0.1
    assert close.near == 1.0
    assert find_hint(layout, "desktop", 3839 - RANGE_PX - 5, 500, everyone) is None


def test_no_hint_to_a_device_that_isnt_connected(layout):
    assert find_hint(layout, "desktop", 3839, 500, lambda d: d != "laptop") is None


def test_no_hint_beside_the_device_where_nothing_lines_up(layout):
    # Below the laptop (y 1000+), the right edge leads nowhere.
    assert find_hint(layout, "desktop", 3839, 1050, everyone) is None


def test_hint_uses_the_calibrated_stretch():
    lay = Layout.from_devices([MAC, LAP], links=[LINK])
    hint = find_hint(lay, "mac", 5110, 1000, everyone)
    assert (hint.here.id, hint.side, hint.a, hint.b) == ("screen2", "right", 643, 1389)
    assert (hint.target.device_id, hint.target_side, hint.ta, hint.tb) == ("lap", "left", 0, 1080)
    # Off the calibrated stretch the edge leads nowhere.
    assert find_hint(lay, "mac", 5110, 300, everyone) is None


def test_device_kinds():
    assert device_kind("Darwin 24.1.0") == "mac"
    assert device_kind("darwin") == "mac"
    assert device_kind("Windows 11") == "windows"
    assert device_kind("win32") == "windows"
    assert device_kind("Android 15") == "phone"
    assert device_kind("Linux 6.8") == "linux"


class Sent:
    def __init__(self):
        self.items = []

    def __call__(self, device, data):
        self.items.append((device, data))

    def last(self, device):
        return next(d for dev, d in reversed(self.items) if dev == device)


def indicator(layout, sent):
    return EdgeIndicator(layout, everyone, sent, name=lambda d: {"laptop": "Laptop"}.get(d, d),
                         kind=lambda d: "windows")


def test_indicator_shows_both_ends(layout):
    sent = Sent()
    edges = indicator(layout, sent)
    edges.update("desktop", 3839, 500)
    here, there = sent.last("desktop"), sent.last("laptop")
    assert here["role"] == "from" and here["side"] == "right" and here["name"] == "Laptop"
    assert here["rect"] == [1920, 0, 1920, 1080]
    assert here["near"] == 1.0
    assert there["role"] == "to" and there["side"] == "left" and "name" not in there
    assert there["rect"] == [0, 0, 1280, 800] and there["at"] == 300


def test_indicator_doesnt_repeat_itself(layout):
    sent = Sent()
    edges = indicator(layout, sent)
    edges.update("desktop", 3839, 500)
    n = len(sent.items)
    edges.update("desktop", 3839, 501)  # a pixel: nothing worth sending
    assert len(sent.items) == n
    edges.update("desktop", 3839, 520)
    assert len(sent.items) == n + 2


def test_indicator_hides_when_the_pointer_moves_away(layout):
    sent = Sent()
    edges = indicator(layout, sent)
    edges.update("desktop", 3839, 500)
    edges.update("desktop", 2500, 500)
    assert sent.last("desktop") == {"hide": True}
    assert sent.last("laptop") == {"hide": True}
    n = len(sent.items)
    edges.update("desktop", 2400, 500)
    assert len(sent.items) == n  # already hidden


def test_crossing_flashes_and_keeps_the_way_back_dark(layout):
    sent = Sent()
    edges = indicator(layout, sent)
    edges.update("desktop", 3839, 500)
    edges.crossed("laptop")
    assert sent.last("desktop") == {"flash": True} and sent.last("laptop") == {"flash": True}
    n = len(sent.items)
    # Just arrived on the laptop, on the edge leading back: stays dark...
    edges.update("laptop", 3842, 500)
    edges.update("laptop", 3860, 500)
    assert len(sent.items) == n
    # ...until the pointer has been well away from it.
    edges.update("laptop", 3840 + 200, 500)
    edges.update("laptop", 3845, 500)
    assert sent.last("laptop")["role"] == "from" and sent.last("desktop")["role"] == "to"


def test_disabled_indicator_shows_nothing(layout):
    sent = Sent()
    edges = indicator(layout, sent)
    edges.enabled = False
    edges.update("desktop", 3839, 500)
    assert sent.items == []


def test_router_reports_positions(layout):
    seen = []
    router = Router(layout, "desktop", FakeCapture(), FakeSink(), FakeTransport({"laptop"}), FakeOverlay())
    router.on_position = lambda device, x, y: seen.append((device, x, y))
    router.on_pointer(1000, 500)
    assert seen[-1] == ("desktop", 1000, 500)
    router.on_pointer(3839, 500)  # crosses to the laptop
    router.on_motion(30, 0)
    assert seen[-1][0] == "laptop"


def test_edge_frames_validate():
    good = protocol.encode("edge", role="from", rect=[0, 0, 10, 10], side="left", a=0, b=10, at=5, near=0.5,
                           name="x", kind="mac")
    assert protocol.decode(good).t == "edge"
    assert protocol.decode(protocol.encode("edge", hide=True)).data == {"hide": True}
    assert protocol.decode(protocol.encode("edge", flash=True)).data == {"flash": True}
    with pytest.raises(protocol.ProtocolError):
        protocol.decode(protocol.encode("edge", role="from", rect=[0, 0], side="left", a=0, b=1, at=0, near=1))
    with pytest.raises(protocol.ProtocolError):
        protocol.decode(protocol.encode("edge", role="diagonal", rect=[0, 0, 1, 1], side="left", a=0, b=1, at=0,
                                        near=1))


def test_agent_passes_edge_frames_on():
    got = []
    session = AgentSession(FakeSink(), lambda *a: None)
    session.handle(protocol.Message("edge", {"hide": True}))  # nobody listening: ignored
    session.on_edge = got.append
    session.handle(protocol.Message("edge", {"flash": True}))
    assert got == [{"flash": True}]


def test_command_line_is_one_line():
    assert command_line({"hide": True}) == 'edge {"hide":true}'


def test_length_fades_out_at_both_ends():
    pytest.importorskip("PySide6.QtWidgets")
    from curslink.edgeoverlay import length_stops

    for at in (0, 120, 500, 999, 1000):
        stops = length_stops(1000, at)
        assert stops[0] == (0.0, 0.0) and stops[-1] == (1.0, 0.0)
        assert [p for p, _ in stops] == sorted(p for p, _ in stops)
        assert max(a for _, a in stops) >= 0.35


def settle(ov) -> None:
    """Run the helper's fade to its end, as if time had passed."""
    for _ in range(20):
        ov._ticked -= 1
        ov._tick()


def test_helper_draws_the_bar_and_label():
    QtWidgets = pytest.importorskip("PySide6.QtWidgets")
    from curslink.edgeoverlay import EdgeOverlay

    QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    ov = EdgeOverlay(lambda x, y: (x, y))
    ov.handle({"role": "from", "rect": [0, 0, 1000, 800], "side": "right", "a": 100, "b": 700, "at": 400,
               "near": 1, "name": "WKS82261", "kind": "windows"})
    settle(ov)
    assert ov.bar.isVisible() and ov.label.isVisible()
    g = ov.bar.geometry()
    assert (g.right() + 1, g.top(), g.height()) == (1000, 100, 600)
    lg = ov.label.geometry()
    assert lg.right() < 1000 and lg.bottom() < 400  # beside the edge, above the pointer
    image = ov.bar.grab().toImage()
    edge_px, end_px = image.pixelColor(g.width() - 1, 300), image.pixelColor(g.width() - 1, 1)
    assert edge_px.alpha() > 150 and end_px.alpha() < 40  # bright by the pointer, faded at the end
    # Heading here: the bar only.
    ov.handle({"role": "to", "rect": [0, 0, 1000, 800], "side": "left", "a": 0, "b": 800, "at": 400, "near": 1})
    settle(ov)
    assert ov.bar.isVisible() and not ov.label.isVisible()
    ov.handle({"flash": True})
    ov.flash_at -= 10  # long past
    ov._tick()
    assert not ov.bar.isVisible()


# A laptop standing in front of the lower right of a monitor (the overlap layout).
MON = Device("pc", "Kyles_PC", (Screen("main", "pc", 0, 0, 2560, 1440),))
MACBOOK = Device("mac", "MacBook", (Screen("main", "mac", 1800, 900, 1512, 982, z=1),))


def overlap_layout(pointer_through=False):
    return Layout.from_devices([MON, MACBOOK], allow_overlap=True, pointer_through=pointer_through)


def test_boundary_of_a_screen_in_front_glows_where_it_is():
    lay = overlap_layout()
    # On the monitor, just left of where the laptop covers it.
    hint = find_hint(lay, "pc", 1780, 1200, everyone)
    assert hint is not None and hint.target.device_id == "mac"
    assert hint.side == "right" and hint.rect == (0, 0, 1800, 1440)  # the bar sits at x 1800, not the screen's edge
    assert (hint.a, hint.b, hint.at) == (900, 1440, 1200)
    assert hint.target_side == "left" and hint.trect == (1800, 900, 1512, 982)
    assert hint.tat == 300


def test_leaving_a_screen_in_front_glows_inside_the_screen_behind():
    lay = overlap_layout()
    hint = find_hint(lay, "mac", 1805, 1200, everyone)
    assert hint is not None and hint.target.device_id == "pc" and hint.side == "left"
    # The monitor's bar is at the laptop's left edge (x 1800), not on the monitor's far right.
    assert hint.target_side == "right" and hint.trect == (0, 0, 1800, 1440)
    sent = Sent()
    edges = EdgeIndicator(lay, everyone, sent, name=lambda d: d, kind=lambda d: "mac")
    edges.update("mac", 1805, 1200)
    assert sent.last("pc")["rect"] == [0, 0, 1800, 1440] and sent.last("pc")["side"] == "right"
    assert sent.last("mac")["rect"] == [0, 0, 1512, 982] and sent.last("mac")["side"] == "left"


def test_mirror_mode_has_no_boundary_inside_the_screen_behind():
    lay = overlap_layout(pointer_through=True)
    assert find_hint(lay, "pc", 1780, 1200, everyone) is None


def test_chevron_points_towards_the_edge():
    pytest.importorskip("PySide6.QtWidgets")
    from curslink.edgeoverlay import chevron_points

    for side, (dx, dy) in {"right": (1, 0), "left": (-1, 0), "top": (0, -1), "bottom": (0, 1)}.items():
        first, tip, last = chevron_points(side, 100, 100)
        arms = ((first[0] + last[0]) / 2, (first[1] + last[1]) / 2)
        # The tip sits beyond the middle of the arms, in the edge's direction.
        assert (tip[0] - arms[0]) * dx + (tip[1] - arms[1]) * dy > 0
        assert (tip[0] - arms[0]) * dy == 0 and (tip[1] - arms[1]) * dx == 0


def test_indicator_goes_dark_while_setting_up(layout):
    sent = Sent()
    edges = indicator(layout, sent)
    edges.update("desktop", 3839, 500)
    assert sent.last("desktop")["role"] == "from"
    edges.pause(True)
    assert sent.last("desktop") == {"hide": True} and sent.last("laptop") == {"hide": True}
    edges.update("desktop", 3839, 500)
    assert sent.last("desktop") == {"hide": True}
    edges.pause(False)
    edges.update("desktop", 3839, 500)
    assert sent.last("desktop")["role"] == "from"


def test_helper_places_the_bar_by_fractions_of_the_screen():
    QtWidgets = pytest.importorskip("PySide6.QtWidgets")
    from PySide6.QtGui import QGuiApplication

    from curslink.edgeoverlay import EdgeOverlay

    QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    g = QGuiApplication.primaryScreen().geometry()
    # Windows scaling: the layout has the screen bigger than Qt does (here 1.5x),
    # and a plain pixel conversion would put the bar a third short.
    ow, oh = round(g.width() * 1.5), round(g.height() * 1.5)
    ov = EdgeOverlay(lambda x, y: (x, y))
    ov.handle({"role": "to", "rect": [0, oh // 2, ow, oh - oh // 2], "side": "top", "a": 0, "b": ow,
               "at": ow // 2, "near": 1, "screen": "main", "screen_os": [0, 0, ow, oh]})
    bar = ov.bar.geometry()
    assert abs(bar.top() - (g.y() + g.height() / 2)) <= 2  # halfway down, as in the layout
    assert abs(bar.width() - g.width()) <= 2


def test_glow_is_one_smooth_falloff_and_fades_with_the_mirror():
    pytest.importorskip("PySide6.QtWidgets")
    from curslink import edgeoverlay
    from curslink.mirror import helper

    stops = edgeoverlay.glow_stops(64, dark=True)
    alphas = [a for _, a in stops]
    assert alphas == sorted(alphas, reverse=True) and alphas[-1] == 0.0  # brightest on the edge, gone at the far side
    assert len(set(round(a, 3) for a in alphas)) > 20  # a curve, not a few solid bands
    assert edgeoverlay.FADE_S == helper.FADE_S and edgeoverlay.FLASH_S == helper.FADE_S


def test_bar_renders_at_the_screens_resolution():
    QtWidgets = pytest.importorskip("PySide6.QtWidgets")
    from curslink.edgeoverlay import Bar

    QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    bar = Bar()
    bar.side, bar.at = "top", 200
    bar.resize(400, 64)
    img = bar.render(1.0, dpr=2.0)
    assert (img.width(), img.height()) == (800, 128)
    column = [img.pixelColor(400, y).alpha() for y in range(0, 128)]
    assert column[0] > 150 and column[-1] < 5
    assert len(set(column)) > 40  # changes smoothly, pixel by pixel


def test_crossing_flare_fades_like_the_mirror():
    QtWidgets = pytest.importorskip("PySide6.QtWidgets")
    from curslink import edgeoverlay
    from curslink.edgeoverlay import EdgeOverlay

    QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    ov = EdgeOverlay(lambda x, y: (x, y))
    ov.handle({"role": "to", "rect": [0, 0, 1000, 800], "side": "left", "a": 0, "b": 800, "at": 400, "near": 1})
    settle(ov)
    ov.handle({"flash": True})
    # Halfway through the mirror's fade, the flare is halfway down: a straight line, same length.
    ov.flash_at -= edgeoverlay.FADE_S / 2
    ov._tick()
    assert ov.level == pytest.approx(0.5, abs=0.05)
    ov.flash_at -= edgeoverlay.FADE_S
    ov._tick()
    assert ov.level == 0.0 and not ov.bar.isVisible()


def test_landing_animation_is_placed_by_fractions_of_the_screen():
    QtWidgets = pytest.importorskip("PySide6.QtWidgets")
    from PySide6.QtGui import QGuiApplication

    from curslink.overlay import make_effect_widget

    QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    g = QGuiApplication.primaryScreen().geometry()
    # Windows scaling: this process has the screen 1.5x bigger than Qt does.
    ow, oh = round(g.width() * 1.5), round(g.height() * 1.5)
    w = make_effect_widget("pop", ow // 2, oh // 2, (0, 0, ow, oh))
    try:
        c = w.geometry().center()
        assert abs(c.x() - (g.x() + g.width() / 2)) <= 3 and abs(c.y() - (g.y() + g.height() / 2)) <= 3
    finally:
        w.timer.stop()
        w.close()
