"""Keep my screen on (curslink.keepawake)."""

from __future__ import annotations

from curslink import keepawake
from curslink.config import agent_config, host_config
from curslink.keepawake import Backend, ScreenKeeper


class FakeBackend(Backend):
    ok = True
    name = "fake"

    def __init__(self):
        self.calls: list[str] = []

    def hold(self) -> bool:
        self.calls.append("hold")
        return True

    def release(self) -> None:
        self.calls.append("release")


def test_holds_only_while_linked_and_enabled():
    fake = FakeBackend()
    keeper = ScreenKeeper(True, fake)
    assert fake.calls == []
    keeper.linked(True)
    keeper.linked(True)
    assert fake.calls == ["hold"] and keeper.held
    keeper.linked(False)
    assert fake.calls == ["hold", "release"] and not keeper.held
    keeper.close()
    assert fake.calls == ["hold", "release"]


def test_off_never_holds():
    fake = FakeBackend()
    keeper = ScreenKeeper(False, fake)
    keeper.linked(True)
    assert fake.calls == []


def test_switched_on_and_off_while_linked():
    fake = FakeBackend()
    keeper = ScreenKeeper(False, fake)
    keeper.linked(True)
    assert keeper.command("keep_screen_on on")
    assert fake.calls == ["hold"]
    assert keeper.command("keep_screen_on off")
    assert fake.calls == ["hold", "release"]
    assert not keeper.command("netcheck")
    keeper.linked(False)
    assert keeper.command("keep_screen_on on")
    assert fake.calls == ["hold", "release"]  # not linked: nothing to hold
    keeper.linked(True)
    assert fake.calls == ["hold", "release", "hold"]


def test_failed_hold_is_tried_again():
    class Flaky(FakeBackend):
        def hold(self) -> bool:
            self.calls.append("hold")
            return len(self.calls) > 1

    fake = Flaky()
    keeper = ScreenKeeper(True, fake)
    keeper.linked(True)
    assert not keeper.held
    keeper.linked(True)
    assert keeper.held


def test_backends_per_platform():
    none = lambda _name: None  # noqa: E731
    mac = keepawake.backend("darwin", none, pid=42)
    assert mac.argv[-4:] == ["-d", "-i", "-w", "42"]
    assert keepawake.backend("win32", none).name == "SetThreadExecutionState"
    tools = {"systemd-inhibit": "/usr/bin/systemd-inhibit", "xdg-screensaver": "/usr/bin/xdg-screensaver"}
    linux = keepawake.backend("linux", tools.get, pid=7)
    assert linux.name == "systemd-inhibit" and "--what=idle" in linux.argv and "kill -0 7" in linux.argv[-1]
    assert linux.poke == ["/usr/bin/xdg-screensaver", "reset"]
    tools["gnome-session-inhibit"] = "/usr/bin/gnome-session-inhibit"
    assert keepawake.backend("linux", tools.get).name == "gnome-session-inhibit"
    assert not keepawake.backend("linux", none).ok


def test_linux_process_backend_runs_and_stops(tmp_path):
    impl = keepawake._Process("test", ["sleep", "30"])
    assert impl.hold()
    proc = impl.proc
    assert proc is not None and proc.poll() is None
    impl.release()
    assert proc.poll() is not None


def test_config_reads_keep_screen_on():
    host = {"host": {"device_id": "a"}, "devices": [{"id": "a", "name": "A", "screens": [
        {"id": "main", "x": 0, "y": 0, "width": 100, "height": 100}]}]}
    assert host_config(host).keep_screen_on is False
    host["host"]["keep_screen_on"] = True
    assert host_config(host).keep_screen_on is True
    agent = {"agent": {"device_id": "b", "host": "1.2.3.4", "keep_screen_on": True}}
    assert agent_config(agent).keep_screen_on is True


def test_helper_that_ends_is_started_again():
    impl = keepawake._Process("test", ["sleep", "30"])
    assert impl.hold()
    first = impl.proc
    first.kill()
    first.wait()
    impl._poke()
    assert impl.proc is not None and impl.proc is not first and impl.proc.poll() is None
    impl.release()


def test_mac_also_declares_user_activity():
    mac = keepawake.backend("darwin", lambda _n: "/usr/bin/caffeinate", pid=1)
    assert mac.poke == ["/usr/bin/caffeinate", "-u", "-t", "2"]


def test_windows_pokes_while_held(monkeypatch):
    calls = []
    win = keepawake._Windows()
    monkeypatch.setattr(win, "_set", lambda flags: calls.append(flags) or True)
    monkeypatch.setattr(keepawake, "_zero_mouse_move", lambda: calls.append("move"))
    started = []
    monkeypatch.setattr(win._poker, "start", lambda: started.append(True))
    assert win.hold()
    assert started == [True]
    win._poke()
    assert calls[-1] == "move" and calls[-2] == keepawake.ES_DISPLAY_REQUIRED | keepawake.ES_SYSTEM_REQUIRED
    win.release()
    assert calls[-1] == keepawake.ES_CONTINUOUS
