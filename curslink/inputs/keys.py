"""Platform-neutral key names.

On the wire a key is one of:

* a single character (``"a"``, ``"7"``, ``"/"``) for printable keys,
* a pynput ``Key`` name for special keys (``"shift"``, ``"ctrl_l"``,
  ``"enter"``, ``"f5"``, ``"cmd"``), or
* ``"vk:<number>"`` for anything else (platform specific, best effort).
"""

from __future__ import annotations

MODIFIER_FAMILIES: dict[str, frozenset[str]] = {
    "ctrl": frozenset({"ctrl", "ctrl_l", "ctrl_r"}),
    "alt": frozenset({"alt", "alt_l", "alt_r", "alt_gr"}),
    "shift": frozenset({"shift", "shift_l", "shift_r"}),
    "cmd": frozenset({"cmd", "cmd_l", "cmd_r"}),
}
_FAMILY_OF = {name: fam for fam, names in MODIFIER_FAMILIES.items() for name in names}


def family(name: str) -> str:
    """``ctrl_l`` -> ``ctrl``; non-modifiers map to themselves (chars lower-cased)."""
    if len(name) == 1:
        return name.lower()
    return _FAMILY_OF.get(name, name)


# Macs put the everyday shortcuts (copy, paste, tabs) on Cmd, PCs on Ctrl, and
# the key beside them is the other one. Swapping the two between a Mac and a
# PC keeps the shortcut under the same finger: Cmd+C on a Mac keyboard is
# Ctrl+C on Windows, and Ctrl (with Win) on a PC keyboard drives the Mac's
# Cmd (and Ctrl). Option and Alt are already the same key on the wire.
_CMD_CTRL_SWAP = {
    "cmd": "ctrl", "cmd_l": "ctrl_l", "cmd_r": "ctrl_r",
    "ctrl": "cmd", "ctrl_l": "cmd_l", "ctrl_r": "cmd_r",
}


def os_family(platform: str) -> str:
    """``"mac"``, ``"pc"`` (Windows, Linux) or ``""`` for unknown.

    Takes ``sys.platform`` (``darwin``, ``win32``) or ``platform.system()``
    style text (``"Darwin 24.1"``, ``"Windows 11"``) as agents send in hello.
    """
    p = (platform or "").strip().lower()
    if p.startswith(("darwin", "mac", "ios")):
        return "mac"
    if p.startswith(("win", "linux", "cygwin", "freebsd", "openbsd", "netbsd")):
        return "pc"
    return ""


def swap_for(local_platform: str, remote_platform: str) -> bool:
    """Should Cmd and Ctrl trade places when typing from one onto the other?"""
    a, b = os_family(local_platform), os_family(remote_platform)
    return bool(a and b and a != b)


def swap_cmd_ctrl(key: str) -> str:
    return _CMD_CTRL_SWAP.get(key, key)


OS_KINDS = ("mac", "windows", "linux", "android")
OS_LABELS = {"mac": "macOS", "windows": "Windows", "linux": "Linux", "android": "Android"}


def os_kind(platform: str) -> str:
    """``"mac"``, ``"windows"``, ``"linux"``, ``"android"`` or ``""`` (unknown).

    Takes the same text as :func:`os_family`; Android agents send ``"Android 14"``.
    """
    p = (platform or "").strip().lower()
    if p.startswith(("darwin", "mac", "ios")):
        return "mac"
    if p.startswith(("win", "cygwin")):
        return "windows"
    if p.startswith("android"):
        return "android"
    if p.startswith(("linux", "freebsd", "openbsd", "netbsd")):
        return "linux"
    return ""


def pair_key(src: str, dst: str) -> str:
    """How a pair is written in ``[keymap]``: ``"mac>windows"``."""
    return f"{src}>{dst}"


def preset_map(src: str, dst: str) -> dict[str, str]:
    """The built-in mapping for typing on a ``src`` keyboard into a ``dst``
    computer: Cmd and Ctrl trade places between a Mac and Windows or Linux.
    Android takes Cmd and Ctrl both as Ctrl already, so it needs nothing."""
    pc = ("windows", "linux")
    if (src == "mac" and dst in pc) or (src in pc and dst == "mac"):
        return {"cmd": "ctrl", "ctrl": "cmd"}
    return {}


_SIDES = ("", "_l", "_r")


def expand_map(mapping: dict[str, str]) -> dict[str, str]:
    """A mapping written by family (``cmd = "ctrl"``) as exact key names.

    A modifier family maps each side to the same side (``cmd_l`` ->
    ``ctrl_l``) unless that side is written out itself; ``alt`` covers
    ``alt_gr`` too. A letter covers its capital (``z = "y"`` also turns
    ``Z`` into ``Y``)."""
    out: dict[str, str] = {}
    for src, dst in mapping.items():
        src, dst = str(src).strip(), str(dst).strip()
        if not src or not dst:
            continue
        if src in MODIFIER_FAMILIES and dst in MODIFIER_FAMILIES:
            for side in _SIDES:
                out.setdefault(src + side, dst + side)
            if src == "alt":
                out.setdefault("alt_gr", dst + "_r")
        elif src in MODIFIER_FAMILIES:
            for name in MODIFIER_FAMILIES[src]:
                out.setdefault(name, dst)
        elif len(src) == 1 and src.isalpha():
            out.setdefault(src.lower(), dst)
            out.setdefault(src.upper(), dst.upper() if len(dst) == 1 else dst)
        else:
            out.setdefault(src, dst)
    # Exact names written in the table win over what a family filled in.
    for src, dst in mapping.items():
        src, dst = str(src).strip(), str(dst).strip()
        if src and dst and src not in MODIFIER_FAMILIES and len(src) > 1:
            out[src] = dst
    return {k: v for k, v in out.items() if k != v}


class KeyMaps:
    """The host's keyboard mappings, per pair of computers.

    ``custom`` holds the ``[keymap]`` tables: ``"mac>windows"`` for a pair of
    systems, ``"device:<id>"`` for one device (which wins). A pair without a
    table of its own gets :func:`preset_map` when ``presets`` is on (the old
    ``[host].swap_cmd_ctrl``); a table, even an empty one, replaces it."""

    def __init__(self, custom: dict[str, dict[str, str]] | None = None, presets: bool = True):
        self.custom = {k: dict(v) for k, v in (custom or {}).items()}
        self.presets = presets
        self._cache: dict[tuple[str, str, str], dict[str, str]] = {}

    def table(self, src: str, dst: str, device_id: str = "") -> dict[str, str]:
        """The mapping (as written, by family) for typing from ``src`` into
        ``dst``, or into ``device_id``."""
        if device_id and f"device:{device_id}" in self.custom:
            return self.custom[f"device:{device_id}"]
        if pair_key(src, dst) in self.custom:
            return self.custom[pair_key(src, dst)]
        return preset_map(src, dst) if self.presets else {}

    def mapping(self, local_platform: str, remote_platform: str, device_id: str = "") -> dict[str, str]:
        src, dst = os_kind(local_platform), os_kind(remote_platform)
        k = (src, dst, device_id)
        if k not in self._cache:
            self._cache[k] = expand_map(self.table(src, dst, device_id)) if (src and dst) or device_id else {}
        return self._cache[k]

    def key(self, local_platform: str, remote_platform: str, key: str, device_id: str = "") -> str:
        return self.mapping(local_platform, remote_platform, device_id).get(key, key)


# Keys people remap, with names to show for them (``cmd`` is the Windows or
# Super key on a PC). Anything else can be typed in as its key name.
KEY_LABELS = {
    "ctrl": "Ctrl / Control", "cmd": "Cmd / Windows / Super", "alt": "Alt / Option", "shift": "Shift",
    "alt_gr": "AltGr", "caps_lock": "Caps Lock", "esc": "Esc", "tab": "Tab", "enter": "Enter",
    "backspace": "Backspace", "delete": "Delete", "insert": "Insert", "home": "Home", "end": "End",
    "page_up": "Page Up", "page_down": "Page Down", "up": "Up", "down": "Down", "left": "Left",
    "right": "Right", "space": "Space", "menu": "Menu", "print_screen": "Print Screen",
    **{f"f{n}": f"F{n}" for n in range(1, 13)},
}


def key_label(name: str, kind: str = "") -> str:
    """A key name as people know it; ``kind`` (an :func:`os_kind`) picks the
    word for Cmd, Ctrl and Alt on that system."""
    fam = family(name)
    side = {"_l": " (left)", "_r": " (right)"}.get(name[-2:], "") if name != fam else ""
    words = {
        "mac": {"cmd": "Cmd", "ctrl": "Control", "alt": "Option"},
        "windows": {"cmd": "Windows key", "ctrl": "Ctrl", "alt": "Alt"},
        "linux": {"cmd": "Super", "ctrl": "Ctrl", "alt": "Alt"},
        "android": {"cmd": "Meta", "ctrl": "Ctrl", "alt": "Alt"},
    }.get(kind, {})
    if name == "alt_gr":
        return "AltGr"
    if fam in words:
        return words[fam] + side
    if len(name) == 1:
        return name.upper() if name.isalpha() else name
    if name in KEY_LABELS:
        return KEY_LABELS[name]
    return KEY_LABELS[fam] + side if fam in KEY_LABELS else name


def guided_steps(kind: str) -> list[tuple[str, str]]:
    """What the "Fix a key" check asks for on a ``kind``
    computer: (key name expected, what to ask). Modifiers first, then the
    letters that move between QWERTY, QWERTZ and AZERTY."""
    mods = [("ctrl", "ctrl"), ("cmd", "cmd"), ("alt", "alt")] if kind != "mac" else \
        [("cmd", "cmd"), ("ctrl", "ctrl"), ("alt", "alt")]
    steps = [(k, f"Press the key you use as {key_label(k, kind)} on this computer") for k, _ in mods]
    steps.append(("shift", "Press Shift"))
    steps += [(c, f"Type the letter {c.upper()}") for c in "qwzyam"]
    return steps


def learned_fix(sent: str, wanted: str) -> tuple[str, str] | None:
    """The fix for a key that arrived as ``sent`` when ``wanted`` was meant,
    written by family so both sides of a modifier follow; ``None`` when they
    already agree."""
    a, b = family(sent), family(wanted)
    if a == b:
        return None
    if a in MODIFIER_FAMILIES and b in MODIFIER_FAMILIES:
        return a, b
    return (a if len(sent) == 1 else sent), (b if len(wanted) == 1 else wanted)


def normalize_char(char: str | None, vk: int | None) -> str | None:
    """Undo control-character mangling (Ctrl+A arrives as ``"\\x01"`` on Windows)."""
    if char and len(char) == 1 and ord(char) >= 32 and char != "\x7f" and not is_private_use(char):
        return char
    if vk is not None and (0x41 <= vk <= 0x5A or 0x30 <= vk <= 0x39):
        return chr(vk).lower()
    if char and len(char) == 1 and 1 <= ord(char) <= 26:
        return chr(ord(char) + 96)
    if vk is not None:
        return f"vk:{vk}"
    return None


class Hotkey:
    """A combination like ``ctrl+alt+home`` that fires once when completed."""

    def __init__(self, spec: str):
        parts = [p.strip().lower() for p in spec.split("+") if p.strip()]
        if not parts:
            raise ValueError("empty hotkey")
        self.spec = spec
        self.parts = frozenset(family(p) for p in parts)
        self._held: set[str] = set()
        self._fired = False

    def feed(self, key: str, pressed: bool) -> bool:
        fam = family(key)
        if pressed:
            self._held.add(fam)
            if self.parts <= self._held and fam in self.parts and not self._fired:
                self._fired = True
                return True
        else:
            self._held.discard(fam)
            if fam in self.parts:
                self._fired = False
        return False


# -- Mac keyboards typing on a PC ---------------------------------------------
# The top row of a Mac keyboard has keys a PC has no key for (Mission Control,
# Spotlight, Launchpad, Dictation, Do Not Disturb), in the places of F3 to F6.
# A PC gets the F-key in that spot, so Option+F4 is Alt+F4 there. By keycode.
MAC_TOP_ROW = {160: "f3", 131: "f4", 177: "f4", 176: "f5", 178: "f6"}
# Keys a PC has nothing for at all: fn and the globe key.
MAC_ONLY_KEYS = frozenset({63, 179})


def is_private_use(char: str) -> bool:
    """macOS hands function keys it has no name for over as characters in
    the Unicode private use area (U+F700 and on): never something to type."""
    return len(char) == 1 and 0xE000 <= ord(char) <= 0xF8FF


def mac_pc_name(name: str, vk: int | None, base: str | None, held: set[str]) -> str | None:
    """What a key typed on a Mac keyboard means to a PC, if that isn't
    ``name`` itself; ``""`` for a key a PC has nothing for.

    ``base`` is what the key types with no modifiers (or only Shift), and
    ``held`` the modifier families held down. Option changes what letters
    type (Option+D is "∂"), but on a PC that's Alt+D, a shortcut. Characters
    a layout puts on Option on purpose (the UK "#" and "€", the German "@")
    are kept; only letters that came out as something else go back to the
    letter. With Cmd or Ctrl held it's a shortcut whatever the key."""
    if vk is not None and vk in MAC_TOP_ROW:
        return MAC_TOP_ROW[vk]
    if vk is not None and vk in MAC_ONLY_KEYS:
        return ""
    if not base or len(base) != 1 or not base.isprintable() or len(name) != 1 or name == base:
        return None
    if held & {"cmd", "ctrl"}:
        return base
    if "alt" in held and base.isascii() and base.isalpha() and not name.isascii():
        return base
    return None
