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
