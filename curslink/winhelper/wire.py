"""What the agent sends the helper over its pipe: one JSON array a line.

    ["hello", version]          the helper's first line, to whoever connects
    ["m", x, y]                 move the pointer
    ["b", button, pressed]
    ["s", dx, dy]
    ["k", key, pressed]

Kept this small on purpose: the helper in Program Files is only replaced
when the setting is switched on again, so it must keep understanding
newer agents.
"""

from __future__ import annotations

import json
from typing import Any

from curslink.protocol import BUTTONS


def encode(*fields: Any) -> bytes:
    return json.dumps(fields, separators=(",", ":")).encode() + b"\n"


def _num(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def decode(line: bytes) -> tuple | None:
    """A command, or None for anything malformed (ignored, not fatal)."""
    try:
        fields = json.loads(line)
    except (ValueError, UnicodeDecodeError):
        return None
    if not isinstance(fields, list) or not fields:
        return None
    op, args = fields[0], fields[1:]
    if op == "m" and len(args) == 2 and all(map(_num, args)):
        return ("m", int(args[0]), int(args[1]))
    if op == "s" and len(args) == 2 and all(map(_num, args)):
        return ("s", float(args[0]), float(args[1]))
    if op == "b" and len(args) == 2 and args[0] in BUTTONS and isinstance(args[1], bool):
        return ("b", args[0], args[1])
    if op == "k" and len(args) == 2 and isinstance(args[0], str) and args[0] and isinstance(args[1], bool):
        return ("k", args[0], args[1])
    if op == "hello" and len(args) == 1 and isinstance(args[0], int):
        return ("hello", args[0])
    return None


def apply(sink, command: tuple) -> None:
    op = command[0]
    if op == "m":
        sink.move_to(command[1], command[2])
    elif op == "b":
        sink.button(command[1], command[2])
    elif op == "s":
        sink.scroll(command[1], command[2])
    elif op == "k":
        sink.key(command[1], command[2])
