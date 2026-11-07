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
