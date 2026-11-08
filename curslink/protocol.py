"""Wire protocol shared by the host and agents.

Frames are newline-delimited UTF-8 JSON objects carried over a TLS-PSK
connection (see :mod:`curslink.security`). Every frame has a ``t`` (type)
field. The format is intentionally tiny and language-neutral so that a
future Android agent (Kotlin) can speak it without any Python code.

Host -> agent
    welcome      {device_id, features, udp}
                                       features: optional list, e.g. ["facing"]
                                       udp (optional): {port, id, key} for moves
                                       by UDP, see curslink.net.udp; only to an
                                       agent whose hello.features has "udp"
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
    drag_out     {}                    input is leaving for the host with the left
                                       button held: is it a file? End the drag here
                                       and say (drag_out back)
    fetch        {id} | {id, cancel}   send the files of that drag_out now (it
                                       was dropped on the host), or forget them
    popup_in     {chat, open, grabX?, grabY?}
                                       an Isolink floating chat carried here:
                                       press the left button and open it held
                                       (answer popup_result); see curslink.popups
    popup_close  {chat}                it opened on another device: close it here
    popup_prepare {chat}               a chat is being dragged elsewhere and may
                                       come here: get it ready (only to an agent
                                       whose hello.features has "popup_prepare")
    popup_unprepare {chat}             it didn't come here after all
    mirror       {captures, shows}     mirror mode (see curslink.mirror): what this
                                       device should capture and show now (all of
                                       it, each time); only to an agent whose
                                       hello.features has "mirror"
    mirror_frame {id, jpg, w, h}       a captured frame for a mirror it shows
    mirror_pointer {id, x, y} | {id, hide}
                                       where the pointer is in a mirror it shows
                                       (0..1 across and down), or none
    mirror_ack   {id}                  a frame it captured was shown: send another
    edge         {role, rect, side, a, b, at, near, name?, kind?} | {hide} | {flash}
                                       the edge indicator: an edge of this device
                                       leads to another (role "from") or the
                                       pointer is heading here ("to"); see
                                       curslink.edgehint. Only to an agent whose
                                       hello.features has "edge"

Agent -> host
    hello        {v, device_id, name, platform, screens, auth, camera, ping_s}
                                       ping_s (optional): ping this agent only this
                                       often (s, up to 15); a phone saves battery
    pong         {n, ts}               ts: the ping's, echoed (times the round trip)
    facing       {facing}              the agent's own camera sees the user
                                       facing it (true) or not (false)
    file_result  {id, ok, ...}         dropped files saved (or why not)
    drag_out     {id, card} | {none}   what the drag leaving this device carries
    file_*                             its files, once the host fetches them
    isolink      {me, windows, dragging}
                                       what this device's Isolink says (only to a
                                       host whose welcome.features has "popups")
    popup_result {chat, ok}            whether a popup_in chat opened here
    mirror_frame {id, jpg, w, h}       a frame of a mirror it captures
    mirror_tune {id, mx, my} | {id, grow, ax, ay}
                                       the mirror being adjusted was dragged or zoomed
    mirror_ack   {id}                  a frame of a mirror it shows was shown

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
        "refused",  # instead of welcome: why the host won't take this agent ({"reason"})
        "isolink",
        "popup_in",
        "popup_close",
        "popup_result",
        "popup_prepare",
        "popup_unprepare",
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
        "hint",
        "mirror",
        "mirror_frame",
        "mirror_pointer",
        "mirror_ack",
        "mirror_tune",
        "edge",
        "bye",
        "drag",
        "drag_out",
        "fetch",
        "file_begin",
        "file_item",
        "file_data",
        "file_end",
        "file_cancel",
        "file_result",
        "file_ack",
        "pair_hello",
        "pair_key",
        "pair_reveal",
        "pair_done",
    }
)

BUTTONS = frozenset({"left", "right", "middle", "x1", "x2"})
EFFECTS = frozenset({"pop", "blink", "none"})
# What this host understands beyond the base protocol, sent in welcome.features.
HOST_FEATURES = ("facing", "popups", "mirror")
# What this agent understands beyond the base protocol, sent in hello.features.
# An agent without "look" would reject a look frame as unknown and drop the
# connection, so the host only sends one to agents that list it.
AGENT_FEATURES = ("look", "hint")
# Added to hello.features by an agent that draws the edge indicator (edge frames).
EDGE_FEATURE = "edge"
# Added to hello.features by an agent that takes dragged files (drag and
# file_* frames); it's optional ([agent.files] enabled = false leaves it out).
FILES_FEATURE = "files"
# An agent that can hand a file drag over to the host (drag_out / fetch, and
# file_* frames the other way).
FILES_OUT_FEATURE = "files_out"
# An agent that takes a dragged file's bytes while it's carried (drag {id,
# card}, then drag {drop, id} or drag {hide, id}) and drops it as a real file.
DROP_FEATURE = "drop"
# Added by an agent that reached the host directly and takes moves by UDP
# (see curslink.net.udp); [agent].udp = false leaves it out.
UDP_FEATURE = "udp"
# An agent that carries Isolink's floating chats (isolink, popup_in,
# popup_close, popup_result; see curslink.popups); [agent.isolink] enabled =
# false leaves it out.
POPUPS_FEATURE = "popups"
# An agent that gets a chat ready while it's dragged elsewhere
# (popup_prepare, popup_unprepare).
PREPARE_FEATURE = "popup_prepare"
# An agent that acknowledges the bytes of a file as they arrive (file_ack,
# when file_begin asks for it), so the host keeps only a little in flight
# while a drag is carried and the pointer and the drop never wait behind it.
FILE_ACK_FEATURE = "file_ack"
# An agent that takes part in mirror mode (mirror, mirror_frame,
# mirror_pointer, mirror_ack): it can capture part of its screens and show
# part of another's (see curslink.mirror).
MIRROR_FEATURE = "mirror"
# Listed with "mirror" by an agent whose captures already show its pointer
# (a phone, which draws its own): the host sends no mirror_pointer for them.
MIRROR_CURSOR_FEATURE = "mirror_cursor"
FILE_FRAMES = frozenset({"file_begin", "file_item", "file_data", "file_end", "file_cancel", "file_result",
                         "file_ack"})


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


def _is_number(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


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
    elif t == "hint":
        if msg.data.get("hide") is not True and not isinstance(msg.data.get("screens"), list):
            raise ProtocolError("hint needs screens, or hide")
    elif t == "mirror":
        if not isinstance(msg.data.get("captures"), list) or not isinstance(msg.data.get("shows"), list):
            raise ProtocolError("mirror needs captures and shows")
    elif t in ("mirror_frame", "mirror_pointer", "mirror_ack"):
        if not isinstance(msg.data.get("id"), str) or not msg.data["id"]:
            raise ProtocolError(f"{t}.id must be a non-empty string")
        if t == "mirror_frame" and not isinstance(msg.data.get("jpg"), str):
            raise ProtocolError("mirror_frame.jpg must be a string")
    elif t == "edge":
        d = msg.data
        if d.get("hide") is not True and d.get("flash") is not True:
            rect = d.get("rect")
            if d.get("role") not in ("from", "to") or d.get("side") not in ("left", "right", "top", "bottom"):
                raise ProtocolError("edge needs a role and a side, or hide or flash")
            if not (isinstance(rect, list) and len(rect) == 4 and all(_is_number(v) for v in rect)):
                raise ProtocolError("edge.rect must be [x, y, width, height]")
            for key in ("a", "b", "at", "near"):
                if not _is_number(d.get(key)):
                    raise ProtocolError(f"edge.{key} must be a number")
    elif t == "drag":
        if msg.data.get("hide") is not True and not isinstance(msg.data.get("card"), dict):
            raise ProtocolError("drag needs a card, or hide")
    elif t == "fetch":
        if not isinstance(msg.data.get("id"), str):
            raise ProtocolError("fetch.id must be a string")
    elif t in ("popup_in", "popup_close", "popup_result", "popup_prepare", "popup_unprepare"):
        if not isinstance(msg.data.get("chat"), str) or not msg.data["chat"]:
            raise ProtocolError(f"{t}.chat must be a non-empty string")
    elif t == "isolink":
        if msg.data.get("me") is not None and not isinstance(msg.data.get("me"), str):
            raise ProtocolError("isolink.me must be a string or null")
        if msg.data.get("dragging") is not None and not isinstance(msg.data.get("dragging"), dict):
            raise ProtocolError("isolink.dragging must be an object or null")
    elif t in FILE_FRAMES:
        if not isinstance(msg.data.get("id"), str):
            raise ProtocolError(f"{t}.id must be a string")
        if t == "file_data" and not isinstance(msg.data.get("data"), str):
            raise ProtocolError("file_data.data must be a string")
        if t == "file_ack" and (not isinstance(msg.data.get("n"), int) or isinstance(msg.data.get("n"), bool)):
            raise ProtocolError("file_ack.n must be an integer")
    elif t == "hello":
        if msg.data.get("v") != PROTOCOL_VERSION:
            raise ProtocolError(
                f"unsupported protocol version {msg.data.get('v')!r}, expected {PROTOCOL_VERSION}"
            )
        if not isinstance(msg.data.get("device_id"), str) or not msg.data["device_id"]:
            raise ProtocolError("hello.device_id must be a non-empty string")
