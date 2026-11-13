"""An agent's monitors reach the host's layout (curslink.displays, the
screens frame, ConfigFile.sync_screens)."""

import asyncio

import pytest

from conftest import FakeSink
from curslink.config import ConfigError, parse_layout
from curslink.configedit import ConfigFile, new_host_config, screens_note, sync_agent_screens
from curslink.displays import name_screens, parse_xrandr
from curslink.net.client import AgentClient, AgentSession
from curslink.net.server import HostServer
from curslink.protocol import Message, ProtocolError, decode, encode
from curslink.security import client_context, server_context

SECRET = "correct horse battery staple"


# ------------------------------------------------------------- listing them
def test_the_primary_monitor_is_main_and_the_rest_are_numbered():
    found = [
        {"x": 1920, "y": 0, "width": 2560, "height": 1440, "number": 1},
        {"x": 0, "y": 0, "width": 1920, "height": 1080, "number": 2, "primary": True},
        {"x": -1080, "y": -200, "width": 1080, "height": 1920, "number": 3},
    ]
    named = name_screens(found)
    assert [s["id"] for s in named] == ["main", "screen3", "screen1"]
    assert named[0] == {"id": "main", "name": "main", "x": 0, "y": 0, "width": 1920, "height": 1080}
    assert all(s["name"] == s["id"] for s in named)  # what older hosts use as the id


def test_without_a_primary_flag_the_one_at_the_origin_is_main():
    named = name_screens([{"x": 1512, "y": -300, "width": 2560, "height": 1440},
                          {"x": 0, "y": 0, "width": 1512, "height": 982}])
    assert [(s["id"], s["x"]) for s in named] == [("main", 0), ("screen2", 1512)]


def test_nonsense_monitors_are_left_out():
    assert name_screens([{"x": 0, "y": 0, "width": 0, "height": 10}, {"x": "a"}]) == []


def test_xrandr_output_is_read():
    text = """Screen 0: minimum 320 x 200, current 4480 x 1440, maximum 16384 x 16384
eDP-1 connected primary 1920x1080+0+360 (normal left inverted right x axis y axis) 344mm x 194mm
   1920x1080     60.00*+
HDMI-1 connected 2560x1440+1920+0 (normal left inverted right x axis y axis) 597mm x 336mm
DP-1 disconnected (normal left inverted right x axis y axis)
"""
    found = parse_xrandr(text)
    assert found == [{"x": 0, "y": 360, "width": 1920, "height": 1080, "primary": True},
                     {"x": 1920, "y": 0, "width": 2560, "height": 1440, "primary": False}]


# ------------------------------------------------------------- the frame
def test_screens_frames_validate():
    msg = decode(encode("screens", screens=[{"id": "main", "x": 0, "y": 0, "width": 10, "height": 10}]))
    assert msg.t == "screens" and msg.get("screens")[0]["id"] == "main"
    for bad in ({}, {"screens": "main"}, {"screens": [1]}):
        with pytest.raises(ProtocolError):
            decode(encode("screens", **bad))


async def wait_for(cond, timeout=3.0):
    for _ in range(int(timeout / 0.01)):
        if cond():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("condition not met")


async def test_an_agent_reports_its_monitors_and_a_new_one_when_plugged_in(monkeypatch):
    screens = [{"id": "main", "name": "main", "x": 0, "y": 0, "width": 1512, "height": 982}]
    monkeypatch.setattr("curslink.net.client.detect_screens", lambda: list(screens))
    monkeypatch.setattr("curslink.net.client.SCREENS_EVERY_S", 0.05)
    server = HostServer({"laptop"}, server_context(SECRET), host="127.0.0.1", port=0)
    changed = []
    server.on_screens = lambda conn: changed.append(list(conn.screens))
    await server.start()
    client = AgentClient("laptop", "Laptop", "127.0.0.1", server.port, client_context(SECRET),
                         AgentSession(FakeSink(), lambda e, x, y: None))
    task = asyncio.create_task(client.run_once())
    await wait_for(lambda: server.is_connected("laptop"))
    conn = server.connections["laptop"]
    assert conn.screens == screens
    await asyncio.sleep(0.15)
    assert changed == []  # nothing new: nothing sent
    screens.append({"id": "screen2", "name": "screen2", "x": 1512, "y": -300, "width": 2560, "height": 1440})
    await wait_for(lambda: changed)
    assert changed[-1] == screens and conn.screens == screens
    await server.close()
    await asyncio.wait_for(task, 3)


# ------------------------------------------------------------- the layout
def host_with_agent(tmp_path, agent_screens=None):
    path = tmp_path / "curslink.toml"
    path.write_text(new_host_config("pc", "PC", [{"id": "main", "x": 0, "y": 0, "width": 2560, "height": 1440}]))
    cfg = ConfigFile(path)
    cfg.add_device("mac", "Mac", agent_screens)
    cfg.save()
    return cfg


def mac_screens(cfg):
    return {s["id"]: s for d in cfg.devices() if d["id"] == "mac" for s in d["screens"]}


def test_the_placeholder_becomes_every_monitor_the_agent_reported(tmp_path):
    cfg = host_with_agent(tmp_path)  # 1920x1080 "main" at x 2560: it reported nothing
    reported = [{"id": "main", "x": 0, "y": 0, "width": 1512, "height": 982},
                {"id": "screen2", "x": 1512, "y": -300, "width": 2560, "height": 1440}]
    done = sync_agent_screens(cfg, "mac", reported)
    assert done == {"added": ["screen2"], "removed": [], "changed": ["main"]}
    s = mac_screens(ConfigFile(cfg.path))
    assert (s["main"]["x"], s["main"]["y"], s["main"]["width"]) == (2560, 0, 1512)
    # Where it is in the Mac's own arrangement, beside main.
    assert (s["screen2"]["x"], s["screen2"]["y"]) == (2560 + 1512, -300)
    assert (s["screen2"]["os_x"], s["screen2"]["os_y"]) == (1512, -300)
    layout = parse_layout(ConfigFile(cfg.path).raw())
    assert {sc.id for sc in layout.screens("mac")} == {"main", "screen2"}
    assert "added screen2" in screens_note("Mac", done)
    assert sync_agent_screens(ConfigFile(cfg.path), "mac", reported) is None  # the same again: nothing


def test_screens_already_laid_out_stay_put_and_an_unplugged_one_goes(tmp_path):
    cfg = host_with_agent(tmp_path, [{"id": "main", "x": 0, "y": 0, "width": 1512, "height": 982},
                                     {"id": "screen2", "x": 1512, "y": 0, "width": 1920, "height": 1080}])
    cfg.move_screen("mac", "main", 2600, 500)  # arranged by hand
    cfg.save()
    done = sync_agent_screens(cfg, "mac", [{"id": "main", "x": 0, "y": 0, "width": 1512, "height": 982}])
    assert done == {"added": [], "removed": ["screen2"], "changed": []}
    s = mac_screens(ConfigFile(cfg.path))
    assert list(s) == ["main"] and (s["main"]["x"], s["main"]["y"]) == (2600, 500)


def test_screens_that_would_overlap_the_hosts_move_the_device_clear(tmp_path):
    cfg = host_with_agent(tmp_path, [{"id": "main", "x": 0, "y": 0, "width": 1512, "height": 982}])
    # The new monitor is left of the Mac's main, which is where the PC's screen is.
    done = sync_agent_screens(cfg, "mac", [{"id": "main", "x": 0, "y": 0, "width": 1512, "height": 982},
                                           {"id": "screen2", "x": -1920, "y": 0, "width": 1920, "height": 1080}])
    assert done["added"] == ["screen2"] and done["moved"]
    s = mac_screens(ConfigFile(cfg.path))
    assert min(sc["x"] for sc in s.values()) == 2560
    parse_layout(ConfigFile(cfg.path).raw())
    assert "right of your screens" in screens_note("Mac", done)


def test_an_unknown_device_is_left_alone(tmp_path):
    cfg = host_with_agent(tmp_path)
    assert sync_agent_screens(cfg, "phone", [{"id": "phone", "x": 0, "y": 0, "width": 400, "height": 800}]) is None


def test_a_phone_reporting_its_one_screen_by_name_changes_nothing(tmp_path):
    cfg = host_with_agent(tmp_path)
    cfg.add_device("pixel", "Pixel", [{"id": "phone", "x": 0, "y": 0, "width": 412, "height": 915}])
    cfg.save()
    assert sync_agent_screens(cfg, "pixel", [{"x": 0, "y": 0, "width": 412, "height": 915, "name": "phone"}]) is None
