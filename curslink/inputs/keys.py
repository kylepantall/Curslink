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
