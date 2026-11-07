"""Calibrated edges: where two screens of different devices really join."""

import json

import pytest

from conftest import FakeCapture, FakeOverlay, FakeSink, FakeTransport
from curslink.config import ConfigError, parse_layout
from curslink.configedit import ConfigFile
from curslink.layout import Device, EdgeEnd, EdgeLink, Layout, Screen
from curslink.router import Router

MAC = Device("mac", "Kyle's Mac", (Screen("main", "mac", 0, 0, 2560, 1440),
                                   Screen("screen2", "mac", 2560, 0, 2560, 1440, os_x=2560)))
LAP = Device("lap", "WKS82261", (Screen("main", "lap", 5120, 600, 1920, 1080),))
LINK = EdgeLink(EdgeEnd("mac", "screen2", "right", 643, 1389), EdgeEnd("lap", "main", "left", 0, 1080))


def linked_layout():
    return Layout.from_devices([MAC, LAP], links=[LINK])


def test_crossing_comes_out_the_same_fraction_along():
    lay = linked_layout()
    mid = (643 + 1389) / 2
    c = lay.find_crossing("mac", 5119, mid, 1, 0)
    assert (c.screen.device_id, c.screen.id) == ("lap", "main")
    assert c.y == pytest.approx(600 + 540, abs=1)
    # and back again
    c = lay.find_crossing("lap", 5120, 600 + 540, -1, 0)
    assert (c.screen.device_id, c.screen.id) == ("mac", "screen2")
    assert c.y == pytest.approx(mid, abs=1)


def test_off_the_stretch_doesnt_cross_even_where_the_layout_touches():
    lay = linked_layout()
    assert Layout.from_devices([MAC, LAP]).find_crossing("mac", 5119, 1420, 1, 0) is not None
    assert lay.find_crossing("mac", 5119, 1420, 1, 0) is None
    assert lay.find_crossing("mac", 5119, 300, 1, 0) is None


def test_router_crosses_by_the_link():
    router = Router(linked_layout(), "mac", FakeCapture(), FakeSink(), FakeTransport({"lap"}), FakeOverlay())
    router.on_pointer(5000, 900)
    router.on_pointer(5119, 1016)
    assert router.active_device == "lap"
    assert router.y == pytest.approx(1140, abs=1)


def test_link_validation():
    bad = EdgeLink(EdgeEnd("mac", "screen2", "right", 0, 1), EdgeEnd("mac", "main", "left", 0, 1))
    with pytest.raises(ValueError):
        Layout.from_devices([MAC, LAP], links=[bad])
    with pytest.raises(ValueError):
        Layout.from_devices([MAC, LAP], links=[EdgeLink(EdgeEnd("mac", "nope", "right", 0, 1), LINK.b)])


RAW = {
    "devices": [
        {"id": "mac", "screens": [{"id": "screen2", "x": 0, "y": 0, "width": 2560, "height": 1440}]},
        {"id": "lap", "screens": [{"id": "main", "x": 2560, "y": 600, "width": 1920, "height": 1080}]},
    ],
}


def test_config_parses_links_and_skips_screens_that_are_gone():
    raw = {**RAW, "layout": {"links": [
        {"a": "mac/screen2", "a_side": "right", "a_span": [643, 1389],
         "b": "lap/main", "b_side": "left", "b_span": [0, 1080]},
        {"a": "mac/gone", "a_side": "right", "a_span": [0, 1], "b": "lap/main", "b_side": "left", "b_span": [0, 1]},
    ]}}
    lay = parse_layout(raw)
    assert lay.links == (EdgeLink(EdgeEnd("mac", "screen2", "right", 643, 1389), EdgeEnd("lap", "main", "left", 0, 1080)),)


@pytest.mark.parametrize("change", [{"a_side": "middle"}, {"a_span": [1]}, {"b": "lap"}, {"b_span": "all"}])
def test_config_rejects_bad_links(change):
    link = {"a": "mac/screen2", "a_side": "right", "a_span": [0, 10], "b": "lap/main", "b_side": "left",
            "b_span": [0, 10], **change}
    with pytest.raises(ConfigError):
        parse_layout({**RAW, "layout": {"links": [link]}})


def test_configedit_sets_replaces_and_removes_links(tmp_path):
    path = tmp_path / "curslink.toml"
    path.write_text('[[devices]]\nid = "mac"\n[[devices.screens]]\nid = "screen2"\nx = 0\ny = 0\nwidth = 2560\n'
                    'height = 1440\n\n[[devices]]\nid = "lap"\n[[devices.screens]]\nid = "main"\nx = 2560\ny = 600\n'
                    'width = 1920\nheight = 1080\n')
    cfg = ConfigFile(path)
    cfg.set_link("mac/screen2", "right", (643.4, 1389), "lap/main", "left", (0, 1080))
    cfg.set_link("lap/main", "left", (10, 1070), "mac/screen2", "right", (650, 1380))  # same pair: replaced
    cfg.save()
    cfg = ConfigFile(path)
    (link,) = cfg.links()
    assert link["a"] == "lap/main" and link["a_span"] == [10, 1070]
    assert len(parse_layout(cfg.raw()).links) == 1
    assert cfg.remove_link("mac/screen2", "lap/main")
    cfg.save()
    assert ConfigFile(path).links() == [] and "links" not in path.read_text()


def test_picking_catches_clicks_and_keys_on_the_other_computer():
    transport = FakeTransport({"lap"})
    router = Router(linked_layout(), "mac", FakeCapture(), FakeSink(), transport, FakeOverlay())
    router.switch_to(router.layout.screen("lap", "main"), 5130, 900, "test")
    picked = []
    router.picker = picked.append
    router.on_button("left", True)
    router.on_button("left", False)
    router.on_key("backspace", True)
    router.on_key("backspace", False)
    router.on_key("esc", True)
    router.on_key("esc", False)
    router.on_key("a", True)
    assert picked == [{"device": "lap", "screen": "main", "x": 10, "y": 300}, {"key": "backspace"}, {"key": "esc"}]
    assert not transport.of_type("button")
    assert [d["key"] for _, _, d in transport.of_type("key")] == ["a"]
    router.picker = None
    router.on_button("left", True)
    assert transport.of_type("button")


def test_hint_frame_carries_an_edge_through():
    from curslink.app import hint_command

    class Server:
        connections = {"lap": object()}
        sent = []

        def supports(self, device_id, feature):
            return True

        def send(self, device_id, t, **data):
            self.sent.append((device_id, t, data))

    server = Server()
    entry = {"screen": "main", "os": [0, 0, 1920, 1080], "size": [1920, 1080],
             "edge": {"side": "left", "other": "Kyle's Mac", "points": [0]}}
    hint_command(server, json.dumps({"devices": {"lap": {"screens": [entry]}}}))
    assert server.sent == [("lap", "hint", {"screens": [entry]})]
