from curslink.inputs.keys import Hotkey, family, normalize_char


def test_normalize_control_chars():
    assert normalize_char("\x01", None) == "a"
    assert normalize_char("\x03", 0x43) == "c"
    assert normalize_char("A", 0x41) == "A"
    assert normalize_char(None, 0x37) == "7"
    assert normalize_char(None, 0xFF) == "vk:255"
    assert normalize_char(None, None) is None


def test_family():
    assert family("ctrl_l") == "ctrl"
    assert family("alt_gr") == "alt"
    assert family("G") == "g"
    assert family("home") == "home"


def test_hotkey_fires_once_per_press():
    hk = Hotkey("ctrl+alt+home")
    assert not hk.feed("ctrl_l", True)
    assert not hk.feed("alt_l", True)
    assert hk.feed("home", True)
    assert not hk.feed("home", True)  # key repeat
    hk.feed("home", False)
    assert hk.feed("home", True)


def test_hotkey_needs_all_parts():
    hk = Hotkey("ctrl+alt+g")
    assert not hk.feed("alt", True)
    assert not hk.feed("g", True)


def test_windows_absolute_move_covers_the_virtual_desktop():
    import ctypes

    from curslink.inputs.pynput_backend import _INPUT, win_absolute

    assert ctypes.sizeof(_INPUT) == (40 if ctypes.sizeof(ctypes.c_void_p) == 8 else 28)
    assert win_absolute(0, 0, 0, 0, 1920, 1080) == (0, 0)
    assert win_absolute(1919, 1079, 0, 0, 1920, 1080) == (65535, 65535)
    # a monitor left of the primary starts the virtual desktop at -1920
    assert win_absolute(-1920, 0, -1920, 0, 3840, 1080) == (0, 0)
    assert win_absolute(0, 540, -1920, 0, 3840, 1080)[0] == round(1920 * 65535 / 3839)
    assert win_absolute(99999, -5, 0, 0, 1920, 1080) == (65535, 0)  # clamped


def test_os_family_and_swap():
    from curslink.inputs.keys import os_family, swap_cmd_ctrl, swap_for

    assert os_family("darwin") == os_family("Darwin 24.1.0") == "mac"
    assert os_family("win32") == os_family("Windows 11") == os_family("Linux 6.8") == "pc"
    assert os_family("?") == ""
    assert swap_for("darwin", "Windows 10") and swap_for("win32", "Darwin 24.1")
    assert not swap_for("darwin", "Darwin 24.1") and not swap_for("win32", "Linux 6.8")
    assert not swap_for("darwin", "?")  # unknown: leave keys alone
    assert [swap_cmd_ctrl(k) for k in ("cmd", "cmd_r", "ctrl_l", "alt", "a")] == ["ctrl", "ctrl_r", "cmd_l", "alt", "a"]


def test_cmd_ctrl_swapper_follows_the_agents_platform():
    from types import SimpleNamespace

    from curslink.app import cmd_ctrl_swapper

    server = SimpleNamespace(connections={"pc": SimpleNamespace(platform="Windows 11"),
                                          "mac": SimpleNamespace(platform="Darwin 24.1")})
    key_for = cmd_ctrl_swapper(server, "darwin")
    assert key_for("pc", "cmd") == "ctrl" and key_for("pc", "ctrl") == "cmd"
    assert key_for("mac", "cmd") == "cmd"
    assert key_for("gone", "cmd") == "cmd"


def test_os_kind_tells_windows_linux_and_android_apart():
    from curslink.inputs.keys import os_kind

    assert os_kind("darwin") == os_kind("Darwin 24.1") == "mac"
    assert os_kind("win32") == os_kind("Windows 11") == "windows"
    assert os_kind("linux") == os_kind("Linux 6.8") == "linux"
    assert os_kind("Android 14") == "android"
    assert os_kind("?") == ""


def test_expand_map_covers_both_sides_and_capitals():
    from curslink.inputs.keys import expand_map

    m = expand_map({"cmd": "ctrl", "ctrl": "cmd", "z": "y", "caps_lock": "esc"})
    assert m["cmd"] == "ctrl" and m["cmd_l"] == "ctrl_l" and m["ctrl_r"] == "cmd_r"
    assert m["z"] == "y" and m["Z"] == "Y" and m["caps_lock"] == "esc"
    # an exact side written out wins over its family
    assert expand_map({"alt": "cmd", "alt_gr": "alt_gr"}).get("alt_gr") is None
    assert expand_map({"alt": "cmd"})["alt_l"] == "cmd_l"


def test_keymaps_per_pair_device_and_presets():
    from curslink.inputs.keys import KeyMaps

    maps = KeyMaps({"linux>windows": {"caps_lock": "ctrl"}, "mac>linux": {}, "device:pc2": {"cmd": "alt"}})
    assert maps.key("darwin", "Windows 11", "cmd_l") == "ctrl_l"        # preset
    assert maps.key("darwin", "Linux 6.8", "cmd") == "cmd"              # empty table: nothing changes
    assert maps.key("linux", "Windows 11", "caps_lock") == "ctrl"       # custom pair
    assert maps.key("linux", "Windows 11", "cmd") == "cmd"
    assert maps.key("darwin", "Windows 11", "cmd", "pc2") == "alt"      # one device wins
    assert maps.key("darwin", "Android 14", "cmd") == "cmd"
    assert KeyMaps(presets=False).key("darwin", "Windows 11", "cmd") == "cmd"


def test_learned_fix_and_guided_steps():
    from curslink.inputs.keys import guided_steps, learned_fix

    assert learned_fix("cmd_l", "ctrl") == ("cmd", "ctrl")
    assert learned_fix("ctrl_r", "ctrl") is None
    assert learned_fix("Z", "y") == ("z", "y")
    assert learned_fix("z", "z") is None
    assert learned_fix("caps_lock", "esc") == ("caps_lock", "esc")
    mac, win = guided_steps("mac"), guided_steps("windows")
    assert mac[0][0] == "cmd" and "Cmd" in mac[0][1]
    assert win[0][0] == "ctrl" and "Windows key" in win[1][1]
    assert ("z", "Type the letter Z") in win


def test_key_mapper_uses_the_pair_of_the_agent():
    from types import SimpleNamespace

    from curslink.app import key_mapper
    from curslink.inputs.keys import KeyMaps

    server = SimpleNamespace(connections={"win": SimpleNamespace(platform="Windows 11"),
                                          "mac": SimpleNamespace(platform="Darwin 24.1")})
    key_for = key_mapper(server, KeyMaps({"linux>windows": {"alt": "cmd"}}), "linux")
    assert key_for("win", "alt_l") == "cmd_l"
    assert key_for("mac", "ctrl") == "cmd"   # preset for linux -> mac
    assert key_for("gone", "alt") == "alt"


def test_agent_learn_mode_reports_keys_instead_of_typing():
    import asyncio

    from conftest import FakeSink
    from curslink.app import agent_command
    from curslink.net.client import AgentSession
    from curslink.protocol import Message

    sink = FakeSink()
    session = AgentSession(sink, lambda e, x, y: None)
    session.handle(Message("key", {"key": "shift", "pressed": True}))
    heard = []
    session.set_learning(lambda k, p: heard.append((k, p)))
    assert sink.calls[-1] == ("key", "shift", False)  # let go, not left stuck
    session.handle(Message("key", {"key": "cmd_l", "pressed": True}))
    session.handle(Message("key", {"key": "cmd_l", "pressed": False}))
    assert heard == [("cmd_l", True), ("cmd_l", False)]
    assert ("key", "cmd_l", True) not in sink.calls
    stop = asyncio.Event()
    agent_command(session, stop, "keys learn off")
    assert session.learn is None
    session.handle(Message("key", {"key": "a", "pressed": True}))
    assert sink.calls[-1] == ("key", "a", True)
    agent_command(session, stop, "stop")
    assert stop.is_set()
