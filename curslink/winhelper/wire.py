"""What the agent sends the helper over its pipe: one JSON array a line.

    ["hello", version]          the helper's first line, to whoever connects
    ["m", x, y]                 move the pointer
    ["b", button, pressed]
    ["s", dx, dy]
    ["k", key, pressed]
    ["watch", mode]             (v2) the host asks the helper to listen to
                                the mouse and keyboard too: "off", "moves"
                                (report the pointer's moves) or "grab" (also
                                buttons, wheel and keys, and keep them from
                                this PC: input is on another computer)

What the helper sends back, on its second pipe (``events_pipe_name``), a
line per event its hooks saw that this PC's own hooks may not have: they
can't hear the mouse over an app that runs as administrator. ``g`` is 1 if
the helper was holding input back ("grab") when it saw the event:

    ["pm", x, y, g]             the pointer moved to (x, y), physical pixels
    ["pb", button, pressed, g]  left, right, middle, x1, x2
    ["ps", dx, dy, g]           wheel notches
    ["pk", vk, pressed, g]      a key, by Windows virtual-key code

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


WATCH_MODES = ("off", "moves", "grab")


def _flag(v: Any) -> bool:
    return v in (0, 1) and not isinstance(v, bool)


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
    if op == "watch" and len(args) == 1 and args[0] in WATCH_MODES:
        return ("watch", args[0])
    return None


def decode_event(line: bytes) -> tuple | None:
    """One of the helper's reports, or None for anything malformed."""
    try:
        fields = json.loads(line)
    except (ValueError, UnicodeDecodeError):
        return None
    if not isinstance(fields, list) or len(fields) != 4 or not _flag(fields[3]):
        return None
    op, a, b, g = fields
    if op == "pm" and _num(a) and _num(b):
        return ("pm", int(a), int(b), bool(g))
    if op == "ps" and _num(a) and _num(b):
        return ("ps", float(a), float(b), bool(g))
    if op == "pb" and a in BUTTONS and isinstance(b, bool):
        return ("pb", a, b, bool(g))
    if op == "pk" and isinstance(a, int) and not isinstance(a, bool) and 0 < a < 256 and isinstance(b, bool):
        return ("pk", a, b, bool(g))
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
