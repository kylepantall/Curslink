"""Wire protocol shared by the host and agents.

Frames are newline-delimited UTF-8 JSON objects carried over a TLS-PSK
connection (see :mod:`curslink.security`). Every frame has a ``t`` (type)
field. The format is intentionally tiny and language-neutral so that a
future Android agent (Kotlin) can speak it without any Python code.

Host -> agent
    welcome      {device_id, features}  features: optional list, e.g. ["facing"]
    enter        {x, y, effect}        agent now owns the pointer; warp to x, y
    leave        {effect, x, y}        host took control back; x, y (optional):
                                       where the pointer left, for the animation
    move         {x, y, ts}            absolute position in the agent's OS coords;
                                       ts (optional): when the host captured it,
                                       host monotonic ms (see curslink.smoothing)
    button       {button, pressed}
    scroll       {dx, dy}
    key          {key, pressed}        see :mod:`curslink.inputs.keys`
    ping         {n, ts}               ts (optional): host monotonic ms
    drag         {card} | {hide: true} a file drag is being carried onto this
                                       device: show (or take down) the
                                       placeholder that follows the pointer
    file_*                             a dropped file, see curslink.files.transfer

Agent -> host
    hello        {v, device_id, name, platform, screens, auth, camera}
    pong         {n, ts}               ts: the ping's, echoed (times the round trip)
    facing       {facing}              the agent's own camera sees the user
                                       facing it (true) or not (false)
    file_result  {id, ok, ...}         dropped files saved (or why not)

``hello.camera`` is true when the agent runs its own camera to confirm
gaze switches (see :mod:`curslink.gaze.confirm`). An agent only sends
``facing`` to a host whose ``welcome.features`` lists ``"facing"``, so
older hosts never see a frame type they don't know.

``hello.auth`` proves which key the agent holds (see
:func:`curslink.security.hello_auth`); agents older than pairing omit it.

Pairing (its own plain TCP connection, see :mod:`curslink.pairing.handshake`)
    pair_hello   {v, device_id, name, role, commit}     initiator
    pair_key     {device_id, name, role, pub}           recipient
    pair_reveal  {pub}                                  initiator
    pair_done    {ok, mac | reason}                     recipient
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

PROTOCOL_VERSION = 1
DEFAULT_PORT = 24850
MAX_FRAME_BYTES = 64 * 1024

MESSAGE_TYPES = frozenset(
    {
        "hello",
        "welcome",
        "enter",
        "leave",
        "move",
        "button",
        "scroll",
        "key",
        "ping",
        "pong",
        "facing",
        "look",
        "bye",
        "drag",
        "file_begin",
        "file_item",
        "file_data",
        "file_end",
        "file_cancel",
        "file_result",
        "pair_hello",
        "pair_key",
        "pair_reveal",
        "pair_done",
    }
)

BUTTONS = frozenset({"left", "right", "middle", "x1", "x2"})
EFFECTS = frozenset({"pop", "blink", "none"})
# What this host understands beyond the base protocol, sent in welcome.features.
HOST_FEATURES = ("facing",)
# What this agent understands beyond the base protocol, sent in hello.features.
# An agent without "look" would reject a look frame as unknown and drop the
# connection, so the host only sends one to agents that list it.
AGENT_FEATURES = ("look",)
# Added to hello.features by an agent that takes dragged files (drag and
# file_* frames); it's optional ([agent.files] enabled = false leaves it out).
FILES_FEATURE = "files"
FILE_FRAMES = frozenset({"file_begin", "file_item", "file_data", "file_end", "file_cancel", "file_result"})


class ProtocolError(ValueError):
    """Raised when a frame is malformed or violates the protocol."""


@dataclass(frozen=True)
class Message:
    t: str
    data: dict[str, Any] = field(default_factory=dict)

    def get(self, key: str, default: Any = None) -> Any:
        return self.data.get(key, default)


def encode(t: str, **data: Any) -> bytes:
    if t not in MESSAGE_TYPES:
        raise ProtocolError(f"unknown message type {t!r}")
    payload = {"t": t, **data}
    raw = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode()
    if len(raw) >= MAX_FRAME_BYTES:
        raise ProtocolError("frame too large")
    return raw + b"\n"


def decode(line: bytes) -> Message:
    if len(line) > MAX_FRAME_BYTES:
        raise ProtocolError("frame too large")
    try:
        obj = json.loads(line)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ProtocolError(f"invalid JSON frame: {exc}") from exc
    if not isinstance(obj, dict):
        raise ProtocolError("frame must be a JSON object")
    t = obj.pop("t", None)
    if t not in MESSAGE_TYPES:
        raise ProtocolError(f"unknown message type {t!r}")
    msg = Message(t, obj)
    _validate(msg)
    return msg


def _num(msg: Message, key: str) -> None:
    value = msg.data.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ProtocolError(f"{msg.t}.{key} must be a number")


def _validate(msg: Message) -> None:
    t = msg.t
    if t in ("move", "enter"):
        _num(msg, "x")
        _num(msg, "y")
        if t == "enter" and msg.data.get("effect", "none") not in EFFECTS:
            raise ProtocolError("enter.effect is not a known effect")
    elif t == "scroll":
        _num(msg, "dx")
        _num(msg, "dy")
    elif t == "button":
        if msg.data.get("button") not in BUTTONS:
            raise ProtocolError("button.button is not a known button")
        if not isinstance(msg.data.get("pressed"), bool):
            raise ProtocolError("button.pressed must be a boolean")
    elif t == "key":
        if not isinstance(msg.data.get("key"), str) or not msg.data["key"]:
            raise ProtocolError("key.key must be a non-empty string")
        if not isinstance(msg.data.get("pressed"), bool):
            raise ProtocolError("key.pressed must be a boolean")
    elif t == "facing":
        if not isinstance(msg.data.get("facing"), bool):
            raise ProtocolError("facing.facing must be a boolean")
    elif t == "look":
        if msg.data.get("hide") is not True and not isinstance(msg.data.get("screen"), str):
            raise ProtocolError("look needs a screen, or hide")
    elif t == "drag":
        if msg.data.get("hide") is not True and not isinstance(msg.data.get("card"), dict):
            raise ProtocolError("drag needs a card, or hide")
    elif t in FILE_FRAMES:
        if not isinstance(msg.data.get("id"), str):
            raise ProtocolError(f"{t}.id must be a string")
        if t == "file_data" and not isinstance(msg.data.get("data"), str):
            raise ProtocolError("file_data.data must be a string")
    elif t == "hello":
        if msg.data.get("v") != PROTOCOL_VERSION:
            raise ProtocolError(
                f"unsupported protocol version {msg.data.get('v')!r}, expected {PROTOCOL_VERSION}"
            )
        if not isinstance(msg.data.get("device_id"), str) or not msg.data["device_id"]:
            raise ProtocolError("hello.device_id must be a non-empty string")
