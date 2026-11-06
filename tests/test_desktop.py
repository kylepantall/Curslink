"""The pieces behind the desktop app that don't need a display."""

from __future__ import annotations

import asyncio
import io
import json
import plistlib
import sys

import pytest

from curslink import autostart, cli, events
from curslink.config import agent_config, host_config, load_raw
from curslink.configedit import ConfigFile, new_agent_config, new_host_config, role_of, slug, snap, touching_edges


# --------------------------------------------------------------------- events

def test_events_are_prefixed_json_lines():
    out = io.StringIO()
    ch = events.EventChannel()
    ch.emit("switch", to="laptop")  # disabled: nothing
    ch.enable(out)
    ch.emit("switch", **{"from": "desktop", "to": "laptop"})
    line = out.getvalue()
    assert line.startswith(events.PREFIX) and line.endswith("\n")
    assert events.parse(line) == {"event": "switch", "from": "desktop", "to": "laptop"}


@pytest.mark.parametrize("line", ["", "hello", "@curslink not json", '@curslink ["x"]', '@curslink {"x": 1}'])
def test_parse_ignores_anything_else(line):
    assert events.parse(line) is None


async def test_commands_end_with_stop_when_stdin_closes():
    seen = []
    done = asyncio.Event()

    def handle(command):
        seen.append(command)
        if command == "stop":
            done.set()

    events.read_commands(asyncio.get_running_loop(), handle, io.StringIO("home\n  Gaze   Pause \n\n"))
    await asyncio.wait_for(done.wait(), 2)
    assert seen == ["home", "gaze pause", "stop"]


def test_host_commands_drive_the_router(layout):
    from curslink.app import host_command
    from curslink.router import Router
    from tests.conftest import FakeCapture, FakeSink

    class Transport:
        def is_connected(self, device_id):
            return True

        def send(self, *a, **k):
            pass

    router = Router(layout, "desktop", FakeCapture(), FakeSink(), Transport())
    told = []
    router.on_gaze_paused = told.append
    stop = asyncio.Event()
    host_command(router, stop, "gaze pause")
    host_command(router, stop, "gaze pause")  # no change, no event
    host_command(router, stop, "gaze toggle")
    assert told == [True, False]
    router.switch_to(layout.screen("laptop", "main"), 10, 10, "edge")
    host_command(router, stop, "home")
    assert router.active_device == "desktop"
    host_command(router, stop, "stop")
    assert stop.is_set()


# ------------------------------------------------------------------ autostart

def test_autostart_linux(tmp_path):
    a = autostart.Autostart(platform="linux", home=tmp_path, config_home=tmp_path / "cfg")
    assert not a.is_enabled()
    a.set_enabled(True, ["/opt/curslink/curslink", "gui", "--minimized"])
    assert a.is_enabled()
    text = a.path.read_text()
    assert "Exec=/opt/curslink/curslink gui --minimized" in text
    assert a.command() == ["/opt/curslink/curslink", "gui", "--minimized"]
    a.path.write_text(text.replace("enabled=true", "enabled=false"))
    assert not a.is_enabled()
    a.set_enabled(False)
    assert not a.path.exists()


def test_autostart_macos(tmp_path):
    a = autostart.Autostart(platform="darwin", home=tmp_path)
    a.set_enabled(True, ["/usr/local/lib/curslink/curslink", "gui", "--minimized"])
    data = plistlib.loads(a.path.read_bytes())
    assert data["RunAtLoad"] is True and data["Label"] == autostart.APP_ID
    assert a.command() == ["/usr/local/lib/curslink/curslink", "gui", "--minimized"]
    a.set_enabled(False)
    assert not a.is_enabled()


def test_autostart_follows_the_executable(tmp_path, monkeypatch):
    a = autostart.Autostart(platform="darwin", home=tmp_path)
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    new = ["/Applications/Curslink.app/Contents/MacOS/curslink", "gui", "--minimized"]
    monkeypatch.setattr(autostart, "launch_command", lambda minimized=True: new)
    assert not a.refresh()  # off: stays off
    assert not a.is_enabled()
    a.set_enabled(True, ["/usr/local/lib/curslink/curslink", "gui", "--minimized"])
    assert a.refresh()
    assert a.command() == new
    assert not a.refresh()  # already right
    a.set_enabled(False)
    old = tmp_path / "Library" / "LaunchAgents" / f"{autostart.OLD_APP_IDS[0]}.plist"
    old.write_bytes(plistlib.dumps({"Label": autostart.OLD_APP_IDS[0], "ProgramArguments": ["x"]}))
    assert a.refresh()  # renamed: the old agent goes, the new one runs this copy
    assert not old.exists() and a.command() == new


class FakeRegistry:
    HKEY_CURRENT_USER = "HKCU"
    REG_SZ = 1

    def __init__(self):
        self.values = {}

    class _Key:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def OpenKey(self, root, path):  # noqa: N802
        return self._Key()

    CreateKey = OpenKey

    def QueryValueEx(self, key, name):  # noqa: N802
        if name not in self.values:
            raise FileNotFoundError(name)
        return self.values[name], self.REG_SZ

    def SetValueEx(self, key, name, _reserved, kind, value):  # noqa: N802
        self.values[name] = value

    def DeleteValue(self, key, name):  # noqa: N802
        if name not in self.values:
            raise FileNotFoundError(name)
        del self.values[name]


def test_autostart_windows():
    reg = FakeRegistry()
    a = autostart.Autostart(platform="win32", registry=reg)
    cmd = [r"C:\Users\Kyle\AppData\Local\Programs\Curslink\curslinkw.exe", "--minimized"]
    a.set_enabled(True, cmd)
    assert a.is_enabled()
    assert a.command() == cmd
    a.set_enabled(False)
    a.set_enabled(False)  # already off: fine
    assert not a.is_enabled()


def test_launch_command_from_source(monkeypatch):
    monkeypatch.delattr(sys, "frozen", raising=False)
    cmd = autostart.launch_command()
    assert cmd[1:] == ["-m", "curslink", "gui", "--minimized"]


def test_launch_command_packaged(monkeypatch, tmp_path):
    exe = tmp_path / "curslink"
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(exe))
    monkeypatch.setattr(sys, "platform", "linux")
    assert autostart.launch_command(minimized=False) == [str(exe), "gui"]


# ----------------------------------------------------------------- config edits

SCREENS = [
    {"x": 0, "y": 0, "width": 2560, "height": 1440},
    {"x": -1920, "y": 200, "width": 1920, "height": 1080},
]


def test_new_host_config_loads(tmp_path):
    path = tmp_path / "curslink.toml"
    path.write_text(new_host_config("kyles-pc", "Kyle's PC", SCREENS))
    cfg = host_config(load_raw(path), base_dir=tmp_path)
    assert cfg.device_id == "kyles-pc" and cfg.name == "Kyle's PC"
    left, right = sorted(cfg.layout.screens("kyles-pc"), key=lambda s: s.x)
    # The layout starts at 0, 0; each screen keeps its OS position.
    assert (left.x, left.y, left.os_x, left.os_y) == (0, 200, -1920, 200)
    assert (right.x, right.os_x) == (1920, 0)


def test_new_agent_config_loads(tmp_path):
    path = tmp_path / "curslink.toml"
    path.write_text(new_agent_config("laptop", "Work laptop"))
    cfg = agent_config(load_raw(path), base_dir=tmp_path)
    assert cfg.device_id == "laptop" and cfg.name == "Work laptop"
    assert role_of(load_raw(path)) == "agent"


def test_edits_keep_comments(tmp_path):
    path = tmp_path / "curslink.toml"
    path.write_text(new_host_config("desk", "Desk", SCREENS[:1]).replace(
        "[host]", "# my own note\n[host]"))
    cfg = ConfigFile(path)
    cfg.set("gaze", "enabled", True)
    cfg.add_device("laptop", "Laptop")
    cfg.save()
    text = path.read_text()
    assert "# my own note" in text
    again = ConfigFile(path)
    assert again.get("gaze", "enabled") is True
    laptop = next(d for d in again.devices() if d["id"] == "laptop")
    assert laptop["screens"][0]["x"] == 2560  # placed to the right
    again.move_screen("laptop", "main", 2600, 100)
    again.resize_screen("laptop", "main", 1512, 982)
    again.save()
    cfg = host_config(load_raw(path), base_dir=tmp_path)
    s = cfg.layout.screen("laptop", "main")
    assert (s.x, s.y, s.width, s.height) == (2600, 100, 1512, 982)
    assert again.remove_device("laptop") and not again.has_device("laptop")


def test_slug():
    assert slug("Kyle's Work PC!") == "kyles-work-pc"
    assert slug("***") == "device"


def test_touching_edges_and_snap():
    devices = [
        {"id": "desk", "screens": [{"id": "a", "x": 0, "y": 0, "width": 1920, "height": 1080}]},
        {"id": "lap", "screens": [{"id": "m", "x": 1950, "y": 300, "width": 1512, "height": 982}]},
    ]
    edges = touching_edges(devices)
    assert ("desk/a", "right", "lap/m") in edges and ("lap/m", "left", "desk/a") in edges
    assert snap((1950, 300, 1512, 982), [(0, 0, 1920, 1080)]) == (1920, 300)
    assert snap((5000, 300, 10, 10), [(0, 0, 1920, 1080)]) == (5000, 300)


# ------------------------------------------------------------------ the CLI

def test_gui_opens_for_a_packaged_double_click(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(tmp_path / "curslinkw.exe"))
    assert cli.wants_gui([])
    assert not cli.wants_gui(["host"])
    monkeypatch.delattr(sys, "frozen")
    assert not cli.wants_gui([])


def test_host_and_agent_take_events_flag():
    args = cli.build_parser().parse_args(["host", "--events"])
    assert args.events
    args = cli.build_parser().parse_args(["gui", "--minimized"])
    assert args.minimized


def test_prefs_round_trip(tmp_path):
    pytest.importorskip("PySide6")
    from curslink.gui.prefs import Prefs

    path = tmp_path / "desktop.json"
    Prefs(start_minimized=False, config=str(tmp_path / "c.toml")).save(path)
    p = Prefs.load(path)
    assert p.start_minimized is False and p.config_path == tmp_path / "c.toml"
    path.write_text(json.dumps({"start_minimized": "yes", "bogus": 1}))
    assert Prefs.load(path).start_minimized is True  # bad values fall back


def test_config_edit_doesnt_recreate_a_deleted_config(tmp_path):
    path = tmp_path / "curslink.toml"
    cfg = ConfigFile(path)  # the file was deleted while the app ran
    cfg.set("host", "effect", "pop")
    cfg.save()
    assert not path.exists()
    path.write_text(new_host_config("pc", "PC", [{"id": "main", "x": 0, "y": 0, "width": 1920, "height": 1080}]))
    cfg = ConfigFile(path)
    cfg.set("host", "effect", "blink")
    cfg.save()
    assert load_raw(path)["host"]["effect"] == "blink"
