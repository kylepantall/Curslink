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


def normalize_char(char: str | None, vk: int | None) -> str | None:
    """Undo control-character mangling (Ctrl+A arrives as ``"\\x01"`` on Windows)."""
    if char and len(char) == 1 and ord(char) >= 32 and char != "\x7f":
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
