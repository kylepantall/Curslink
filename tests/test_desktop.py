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


async def test_look_commands_keep_their_json():
    seen = []
    done = asyncio.Event()

    def handle(command):
        seen.append(command)
        if command == "stop":
            done.set()

    events.read_commands(asyncio.get_running_loop(), handle,
                         io.StringIO('LOOK {"device": "Kyles_PC", "screen": "\\\\.\\DISPLAY1"}\n'))
    await asyncio.wait_for(done.wait(), 2)
    assert seen[0] == 'look {"device": "Kyles_PC", "screen": "\\\\.\\DISPLAY1"}'


def test_look_command_goes_only_to_agents_that_understand_it():
    from curslink.app import look_command

    class Server:
        def __init__(self):
            self.connections = {"pc": object(), "old": object(), "tab": object()}
            self.sent = []

        def supports(self, device_id, feature):
            return device_id != "old" and feature == "look"

        def send(self, device_id, t, **data):
            self.sent.append((device_id, t, data))

    server = Server()
    look_command(server, '{"device": "pc", "screen": "main", "step": "Step 1"}')
    assert ("pc", "look", {"screen": "main", "step": "Step 1"}) in server.sent
    assert ("tab", "look", {"hide": True}) in server.sent
    assert not [s for s in server.sent if s[0] == "old"]
    server.sent.clear()
    look_command(server, '{"hide": true}')
    assert sorted(s[0] for s in server.sent) == ["pc", "tab"]
    look_command(server, "not json")  # ignored


async def test_loop_bridge_adds_up_motion_and_keeps_clicks_in_order():
    from curslink.app import LoopBridge

    calls = []

    class Router:
        def on_motion(self, dx, dy, ts=None):
            calls.append(("motion", dx, dy))

        def on_button(self, button, pressed):
            calls.append(("button", button, pressed))

    bridge = LoopBridge(asyncio.get_running_loop(), Router())
    for _ in range(10):
        bridge.on_motion(1, 2)
    bridge.on_button("left", True)
    bridge.on_motion(5, 0)
    await asyncio.sleep(0.05)
    assert calls == [("motion", 10, 20), ("button", "left", True), ("motion", 5, 0)]


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


def test_macos_capture_reads_deltas_and_survives_a_disabled_tap(monkeypatch):
    import types

    from curslink.inputs import pynput_backend as pb

    calls = []
    fields = {pb._MAC_DELTA_X: 3, pb._MAC_DELTA_Y: -2, pb._MAC_SOURCE_PID: 0}
    quartz = types.SimpleNamespace(
        CGEventGetIntegerValueField=lambda event, field: event[field],
        CGEventTapEnable=lambda tap, on: calls.append(("enable", tap, on)),
    )
    monkeypatch.setitem(sys.modules, "Quartz", quartz)

    class Handler:
        def on_motion(self, dx, dy):
            calls.append(("motion", dx, dy))

    monkeypatch.setattr(pb, "_mac_warp", lambda x, y: calls.append(("warp", x, y)))
    capture = pb.PynputCapture(park_at=(7, 8))
    capture._mac, capture._handler = True, Handler()
    assert capture._mac_mouse_tap(5, fields) is fields  # not grabbed: the Mac gets it
    capture._grabbed = True
    assert capture._mac_mouse_tap(5, fields) is None
    assert capture._mac_mouse_tap(6, {**fields, pb._MAC_SOURCE_PID: 99}) is None  # ours: no motion
    assert capture._mac_key_tap(10, "key") is None
    assert calls == [("motion", 3, -2), ("warp", 7, 8)]  # held at the park point after every report
    del calls[1]

    class Base:
        def _create_event_tap(self):
            return "tap"

        def _handler(self, proxy, event_type, event, refcon):
            calls.append(("pynput", event_type))
            return event

    listener = pb._mac_listener(Base)()
    listener._create_event_tap()
    assert listener._handler(None, 0xFFFFFFFE, None, None) is None
    assert listener._handler(None, 5, "ev", None) == "ev"
    assert calls[1:] == [("enable", "tap", True), ("pynput", 5)]


def test_hiding_from_the_dock_is_harmless_off_macos():
    from curslink import macdock

    macdock.hide()
    macdock.hide(macdock.ACCESSORY)


def test_mac_grab_detaches_the_cursor_and_warp_never_reattaches_it(monkeypatch):
    """0.4.10 re-associated the cursor after each warp; the next report's
    delta then held the warp's jump and bounced input straight back home."""
    import curslink.inputs.pynput_backend as pb

    calls = []
    monkeypatch.setattr(pb, "_mac_attach_cursor", lambda attached: calls.append(("attach", attached)))
    monkeypatch.setattr(pb, "_mac_warp", lambda x, y: calls.append(("warp", x, y)))
    monkeypatch.setattr(pb, "_mac_cursor_visible", lambda visible: calls.append(("visible", visible)))
    capture = pb.PynputCapture(park_at=(7, 8))
    capture._mac = True
    capture.set_grab(True)
    capture.set_grab(False)
    assert calls == [("attach", False), ("warp", 7, 8), ("visible", False), ("attach", True), ("visible", True)]
    import inspect
    assert "CGAssociateMouseAndMouseCursorPosition" not in inspect.getsource(pb._mac_warp)


def test_mac_drops_the_parking_jump_right_after_grabbing_but_not_later(monkeypatch):
    import curslink.inputs.pynput_backend as pb

    now = [100.0]
    monkeypatch.setattr(pb.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(pb, "_mac_delta", lambda event: event)
    monkeypatch.setattr(pb, "_mac_attach_cursor", lambda attached: None)
    monkeypatch.setattr(pb, "_mac_warp", lambda x, y: None)
    monkeypatch.setattr(pb, "_mac_cursor_visible", lambda visible: None)
    capture = pb.PynputCapture(park_at=(1000, 500))
    capture._mac = True
    capture.set_grab(True)
    assert capture._mac_motion((-1700, 0)) == (0, 0)   # the parking jump
    assert capture._mac_motion((6, 2)) == (6, 2)        # real motion straight away
    now[0] += 1.0
    assert capture._mac_motion((-300, 0)) == (-300, 0)  # later, a fast flick is a flick


def test_mac_input_coming_home_lands_on_the_edge_it_crossed(monkeypatch):
    import curslink.inputs.pynput_backend as pb

    calls = []
    monkeypatch.setattr(pb, "_mac_warp", lambda x, y: calls.append(("warp", x, y)))
    monkeypatch.setattr(pb, "_mac_attach_cursor", lambda attached: calls.append(("attach", attached)))
    monkeypatch.setattr(pb, "_mac_cursor_visible", lambda visible: None)
    monkeypatch.setattr(pb, "_mac_injected", lambda event: False)
    monkeypatch.setattr(pb, "_mac_delta", lambda event: (40, 0))
    monkeypatch.setattr(pb.sys, "platform", "darwin")

    sink = pb.PynputSink.__new__(pb.PynputSink)  # no pynput controllers needed
    sink.move_to(3455, 610)
    assert calls == [("warp", 3455, 610), ("attach", True)]  # a warp, not a posted event

    capture = pb.PynputCapture(park_at=(756, 491))
    capture._mac = True

    class Handler:
        def on_motion(self, dx, dy):
            capture.set_grab(False)  # this report sends input home

    capture._handler = Handler()
    capture.set_grab(True)
    del calls[:]
    capture._mac_mouse_tap(5, object())
    assert ("warp", 756, 491) not in calls  # not dragged back to the park point


def test_sign_in_on_windows_opens_the_windowed_build(monkeypatch, tmp_path):
    # Windows names are case-insensitive: the console curslink.exe must not
    # be mistaken for the app, or a console flashes up at sign-in.
    (tmp_path / "curslink.exe").write_text("")
    (tmp_path / "curslinkw.exe").write_text("")
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "platform", "win32")
    for running in ("curslink.exe", "curslinkw.exe"):
        monkeypatch.setattr(sys, "executable", str(tmp_path / running))
        assert autostart.launch_command() == [str(tmp_path / "curslinkw.exe"), "gui", "--minimized"]


def test_old_sign_in_entries_still_open_the_app(monkeypatch):
    seen = {}
    monkeypatch.setattr(cli, "wants_gui", lambda argv: False)
    import curslink.gui.app as gui_app
    monkeypatch.setattr(gui_app, "main", lambda argv: seen.setdefault("argv", argv) and 0)
    cli.main(["--minimized"])
    assert "--minimized" in seen["argv"]


def test_hint_command_marks_overlaps_on_the_agents_listed():
    from curslink.app import hint_command

    class Server:
        def __init__(self):
            self.connections = {"lap": object(), "old": object(), "tab": object()}
            self.sent = []

        def supports(self, device_id, feature):
            return device_id != "old" and feature == "hint"

        def send(self, device_id, t, **data):
            self.sent.append((device_id, t, data))

    server = Server()
    screens = [{"screen": "main", "size": [1920, 1080], "regions": [{"rect": [0, 0, 100, 100], "label": "x"}]}]
    hint_command(server, '{"devices": {"lap": {"screens": %s}}}' % __import__("json").dumps(screens))
    assert ("lap", "hint", {"screens": screens}) in server.sent
    assert ("tab", "hint", {"hide": True}) in server.sent
    assert not [s for s in server.sent if s[0] == "old"]
    server.sent.clear()
    hint_command(server, "not json")
    assert server.sent == []


def test_hint_frames_validate():
    import json

    import pytest

    from curslink.protocol import AGENT_FEATURES, ProtocolError, decode, encode

    assert "hint" in AGENT_FEATURES
    assert decode(encode("hint", hide=True)).t == "hint"
    assert decode(encode("hint", screens=[])).t == "hint"
    with pytest.raises(ProtocolError):
        decode(json.dumps({"t": "hint", "screens": "main"}).encode() + b"\n")


def test_mac_back_forward_and_smooth_scroll_reach_the_other_computer(monkeypatch):
    import curslink.inputs.pynput_backend as pb

    monkeypatch.setattr(pb, "_mac_attach_cursor", lambda attached: None)
    monkeypatch.setattr(pb, "_mac_warp", lambda x, y: None)
    monkeypatch.setattr(pb, "_mac_cursor_visible", lambda visible: None)
    monkeypatch.setattr(pb, "_mac_injected", lambda event: False)
    monkeypatch.setattr(pb, "_mac_field", lambda event, field: event["button"])
    monkeypatch.setattr(pb, "_mac_scroll", lambda event: event["scroll"])
    got = []

    class Handler:
        def on_button(self, name, pressed):
            got.append(("button", name, pressed))

        def on_scroll(self, dx, dy):
            got.append(("scroll", dx, dy))

    capture = pb.PynputCapture(park_at=(756, 491))
    capture._mac = True
    capture._handler = Handler()
    capture.set_grab(True)
    capture._on_click(0, 0, type("B", (), {"name": "middle"})(), True)  # pynput's "middle" for any of them
    assert got == []
    assert capture._mac_mouse_tap(pb._MAC_OTHER_DOWN, {"button": 3}) is None
    assert capture._mac_mouse_tap(pb._MAC_OTHER_UP, {"button": 3}) is None
    capture._mac_mouse_tap(pb._MAC_OTHER_DOWN, {"button": 4})
    capture._mac_mouse_tap(pb._MAC_OTHER_DOWN, {"button": 2})
    capture._on_scroll(0, 0, 0, 0)                                    # pynput's whole lines: ignored
    capture._mac_mouse_tap(pb._MAC_SCROLL, {"scroll": (0.0, 0.25)})  # a smooth wheel's quarter line
    assert got == [("button", "x1", True), ("button", "x1", False), ("button", "x2", True),
                   ("button", "middle", True), ("scroll", 0.0, 0.25)]


def test_windows_adds_up_fractional_scrolls_into_whole_notches(monkeypatch):
    import curslink.inputs.pynput_backend as pb

    sent = []
    sink = pb.PynputSink.__new__(pb.PynputSink)
    sink._mouse = type("M", (), {"scroll": lambda self, dx, dy: sent.append((dx, dy))})()
    sink._scroll_carry = (0.0, 0.0)
    from curslink.tally import Tally

    sink.notches = Tally("notches")
    monkeypatch.setattr(pb.sys, "platform", "win32")
    monkeypatch.setattr(pb, "_win_wheel", lambda x, y: False)  # not on Windows: pynput's own wheel
    for _ in range(9):
        sink.scroll(0.0, 0.25)
    assert sent == [(0, 1), (0, 1)]              # 2.25 lines: two notches, a quarter waiting
    sink.scroll(0.0, -3.5)
    assert sent[-1] == (0, -3)                   # down, carry included (0.25 - 3.5 = -3.25)


def test_windows_wheel_refusals_are_logged_like_moves(caplog, monkeypatch):
    import logging

    import curslink.inputs.pynput_backend as pb

    f = pb._WinInjectFailures(clock=lambda: 0.0)
    monkeypatch.setattr(pb, "_win_foreground", lambda: "Taskmgr.exe (administrator)")
    with caplog.at_level(logging.WARNING, logger="curslink.input"):
        f.failed(5, False, what="scroll")
    assert "refused an injected pointer scroll (error 5, 1 so far)" in caplog.text
    assert "Taskmgr.exe" in caplog.text


def test_scroll_tally_logs_the_first_then_every_so_often(caplog):
    import logging

    from curslink.tally import Tally

    now = [0.0]
    t = Tally("scroll", every_s=2.0, clock=lambda: now[0])
    with caplog.at_level(logging.INFO, logger="curslink.input"):
        t.add(0, 1)
        now[0] = 0.5
        t.add(0, 1)
        now[0] = 2.6
        t.add(0, 2)
    lines = [r.getMessage() for r in caplog.records]
    assert len(lines) == 2 and "1 in" in lines[0] and "2 in" in lines[1] and "dy +3.00" in lines[1]


def test_mac_cursor_shown_again_if_a_hung_app_kept_it_hidden(monkeypatch):
    """Coming home while the app in front isn't responding can leave the
    cursor hidden; it's checked and shown again."""
    import curslink.inputs.pynput_backend as pb

    class CG:
        shows = 0
        visible = False

        def CGDisplayShowCursor(self, d):
            CG.shows += 1

    class IsVisible:
        restype = None

        def __call__(self):
            return CG.visible

    CG.CGCursorIsVisible = IsVisible()

    cg = CG()
    monkeypatch.setattr(pb, "_mac_lib", lambda path: cg)
    monkeypatch.setattr(pb, "_mac_sets_cursor_in_background", lambda a, b: None)
    nudges = []
    monkeypatch.setattr(pb, "_mac_nudge_cursor", lambda: nudges.append(1))
    monkeypatch.setattr(pb, "_MAC_SHOW_CHECKS_S", ())
    monkeypatch.setattr(pb, "_mac_hidden", False)
    pb._mac_check_shown(pb._mac_show_gen, 1)
    assert CG.shows == 1 and nudges == [1]
    CG.visible = True
    pb._mac_check_shown(pb._mac_show_gen, 1)
    assert CG.shows == 1                       # showing: left alone
    CG.visible = False
    pb._mac_check_shown(pb._mac_show_gen - 1, 1)
    assert CG.shows == 1                       # stale check (hidden again since): ignored


def test_windows_refused_moves_are_logged_now_and_then(monkeypatch, caplog):
    import logging

    import curslink.inputs.pynput_backend as pb

    monkeypatch.setattr(pb, "_win_foreground", lambda: "Game.exe (not responding, runs as administrator)")
    t = [0.0]
    f = pb._WinInjectFailures(clock=lambda: t[0])
    with caplog.at_level(logging.INFO, logger="curslink.inputs.pynput_backend"):
        for _ in range(50):
            f.failed(5, False)
            t[0] += 0.01
        t[0] += 5.0
        f.failed(5, True)
        f.ok()
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 2 and "Game.exe" in warnings[0] and "error 5" in warnings[0]
    assert "51 moves were refused" in caplog.records[-1].getMessage()


def test_mac_wheel_mouse_scrolls_whole_lines_and_trackpad_fractions():
    from curslink.inputs.pynput_backend import mac_scroll_lines

    # A slow notch on a wheel mouse: macOS says 0.1 lines fixed-point, 1 whole line.
    assert mac_scroll_lines(False, (0, -1), (0.0, -0.1)) == (0.0, -1.0)
    # A trackpad's fractions are the real amount.
    assert mac_scroll_lines(True, (0, 1), (0.0, 0.25)) == (0.0, 0.25)
