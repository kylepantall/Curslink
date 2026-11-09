"""Shortcuts typed on a Mac host reach the computer input is on (Alt+F4,
Alt+letter, Cmd+Tab and the rest of macOS's own shortcuts)."""

from types import SimpleNamespace

from curslink.inputs.keys import KeyMaps, mac_pc_name, normalize_char


def test_mac_top_row_keys_are_f_keys_to_a_pc():
    # The Spotlight / Launchpad key sits where F4 is: Option+F4 is Alt+F4.
    assert mac_pc_name("vk:177", 177, "", {"alt"}) == "f4"
    assert mac_pc_name("vk:131", 131, "", set()) == "f4"
    assert mac_pc_name("vk:160", 160, "", set()) == "f3"
    assert mac_pc_name("vk:63", 63, "", set()) == ""  # fn: nothing on a PC


def test_option_letters_are_alt_letters_to_a_pc():
    assert mac_pc_name("∂", 2, "d", {"alt"}) == "d"
    assert mac_pc_name("Î", 2, "D", {"alt", "shift"}) == "D"
    assert mac_pc_name("∂", 2, "d", {"alt", "cmd"}) == "d"


def test_characters_a_layout_puts_on_option_are_kept():
    assert mac_pc_name("#", 20, "3", {"alt"}) is None   # UK Option+3
    assert mac_pc_name("€", 19, "2", {"alt"}) is None   # UK Option+2
    assert mac_pc_name("@", 37, "l", {"alt"}) is None   # German Option+L
    assert mac_pc_name("d", 2, "d", {"alt"}) is None
    assert mac_pc_name("d", 2, "d", set()) is None


def test_function_keys_macos_gives_as_private_characters_are_not_typed():
    assert normalize_char("", 177) == "vk:177"
    assert normalize_char("a", 0) == "a"


def test_key_mapper_uses_pc_names_only_for_computers_that_arent_macs():
    from curslink.app import key_mapper

    server = SimpleNamespace(connections={"pc": SimpleNamespace(platform="Windows 11"),
                                          "mac": SimpleNamespace(platform="Darwin 24.1"),
                                          "phone": SimpleNamespace(platform="Android 15")})
    names = {"∂": "d", "vk:177": "f4", "vk:63": ""}
    key_for = key_mapper(server, KeyMaps(), "darwin", pc_names=names)
    assert key_for("pc", "∂") == "d" and key_for("pc", "vk:177") == "f4"
    assert key_for("pc", "vk:63") == ""
    assert key_for("pc", "cmd") == "ctrl"  # the Cmd <-> Ctrl preset still applies
    assert key_for("phone", "∂") == "d"
    assert key_for("mac", "∂") == "∂" and key_for("mac", "vk:177") == "vk:177"


def test_router_sends_nothing_for_a_key_the_computer_has_none_for(layout):
    from conftest import FakeCapture, FakeOverlay, FakeSink, FakeTransport
    from curslink.router import Router

    transport = FakeTransport({"laptop"})
    router = Router(layout, "desktop", FakeCapture(), FakeSink(), transport, FakeOverlay())
    router.key_for = lambda device, key: {"vk:63": ""}.get(key, key)
    router.on_pointer(3839, 500)
    router.on_key("vk:63", True)
    router.on_key("f4", True)
    router.go_home("test")
    keys = [(d["key"], d["pressed"]) for _, t, d in transport.sent if t == "key"]
    assert keys == [("f4", True), ("f4", False)]


def _mac_capture(bases):
    from curslink.inputs.pynput_backend import PynputCapture

    cap = PynputCapture(hide_cursor=False)
    cap._mac = True
    cap._mac_base = lambda vk: bases.get(vk, "")
    seen = []
    cap._handler = SimpleNamespace(on_key=lambda k, p: seen.append((k, p)))
    return cap, seen


class _Key(SimpleNamespace):
    pass


def test_mac_capture_notes_what_option_letters_mean_and_releases_them_as_pressed(monkeypatch):
    import curslink.inputs.pynput_backend as pb

    names = {"alt": "alt", "d": "∂", "d_up": "d", "f4": "vk:177"}
    monkeypatch.setattr(pb, "key_to_name", lambda key: names[key.n])
    cap, seen = _mac_capture({2: "d"})
    cap._on_key(_Key(n="alt", vk=None), True, False)
    cap._on_key(_Key(n="d", vk=2), True, False)
    cap._on_key(_Key(n="alt", vk=None), False, False)
    cap._on_key(_Key(n="d_up", vk=2), False, False)  # Option already up: macOS says "d"
    assert seen == [("alt", True), ("∂", True), ("alt", False), ("∂", False)]
    assert cap.pc_names == {"∂": "d"}
    cap._on_key(_Key(n="f4", vk=177), True, False)
    assert cap.pc_names["vk:177"] == "f4"


class _FakeCGS:
    def __init__(self, on):
        self.on = set(on)
        self.calls = 0

    def enabled(self, hotkey):
        return hotkey in self.on

    def set_enabled(self, hotkey, on):
        self.calls += 1
        (self.on.add if on else self.on.discard)(hotkey)
        return True


def test_macos_shortcuts_go_off_while_input_is_away_and_come_back(tmp_path):
    from curslink.inputs.macshortcuts import SystemShortcuts

    cgs = _FakeCGS({1, 2, 64, 79})
    state = tmp_path / "off.json"
    s = SystemShortcuts(cgs, state)
    s.set_off(True)
    assert s.wait(2.0)
    assert cgs.on == set() and state.exists()
    s.set_off(False)
    assert s.wait(2.0)
    assert cgs.on == {1, 2, 64, 79} and not state.exists()


def test_shortcuts_left_off_by_a_run_that_stopped_are_turned_back_on(tmp_path):
    from curslink.inputs.macshortcuts import SystemShortcuts

    cgs = _FakeCGS({1})
    state = tmp_path / "off.json"
    state.write_text("[2, 64]")
    SystemShortcuts(cgs, state).restore_left_over()
    assert cgs.on == {1, 2, 64} and not state.exists()


def test_shortcuts_the_user_turned_off_stay_off(tmp_path):
    from curslink.inputs.macshortcuts import SystemShortcuts

    cgs = _FakeCGS({1, 2})  # 64 (Spotlight) was off before Curslink started
    s = SystemShortcuts(cgs, tmp_path / "off.json")
    s.set_off(True)
    s.wait(2.0)
    s.set_off(False)
    s.wait(2.0)
    assert cgs.on == {1, 2}
