"""Code-confirmed pairing: agree a link key with a device you can see.

The initiator (the device where you clicked "pair") and the recipient run
an ephemeral Diffie-Hellman exchange over any byte stream, plain TCP on the
LAN or a relay connection. Both derive a 6-digit code from the exchange.
The initiator shows its code and you type it into the recipient. If the
codes match, nobody sat in the middle, and both sides keep the same 32-byte
link key (see :mod:`curslink.pairing.store`).

    initiator                                   recipient
    pair_hello  {v, device_id, name, role, commit = H(A)}  ->
                <-  pair_key {device_id, name, role, pub = B}
    pair_reveal {pub = A}                                  ->
    shows code                                  you type the code
                <-  pair_done {ok, mac | reason}

Scanning a QR code replaces typing the code: the recipient shows a QR code
holding a one-time token (see :mod:`curslink.pairing.qr`), and the
initiator that scanned it sends ``pair_hello.qr = true`` and
``pair_reveal.proof = HMAC(token, transcript)``. Only a device that saw the
QR code can make that proof, and a man in the middle would have a different
transcript on each side, so the recipient takes the proof in place of a
typed code.

Either side may add ``screens`` (its OS screens, as in ``hello``) and
``platform`` to its first message, so a host that pairs with a phone can put
it in the layout at the right size before it ever connects.

The initiator commits to its public value before seeing the recipient's,
so a man in the middle can't search for keys that give matching codes: one
guess per attempt, a one in a million chance. The recipient refuses after
a wrong code, and the listener in :mod:`curslink.pairing.service` slows
down repeated attempts.

Only the standard library is used: DH on the 2048-bit MODP group from
RFC 3526 (group 14), SHA-256 and HKDF. A Kotlin agent can do the same with
``javax.crypto``.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import secrets
from dataclasses import dataclass, field
from typing import Awaitable, Callable

from curslink.net.framing import read_message
from curslink.pairing.store import KEY_BYTES, ROLES
from curslink.protocol import Message, ProtocolError, encode

PAIRING_VERSION = 1

# RFC 3526, 2048-bit MODP group (id 14). p is a safe prime, g = 2.
P = int(
    "FFFFFFFFFFFFFFFFC90FDAA22168C234C4C6628B80DC1CD129024E088A67CC74"
    "020BBEA63B139B22514A08798E3404DDEF9519B3CD3A431B302B0A6DF25F1437"
    "4FE1356D6D51C245E485B576625E7EC6F44C42E9A637ED6B0BFF5CB6F406B7ED"
    "EE386BFB5A899FA5AE9F24117C4B1FE649286651ECE45B3DC2007CB8A163BF05"
    "98DA48361C55D39A69163FA8FD24CF5F83655D23DCA3AD961C62F356208552BB"
    "9ED529077096966D670C354E4ABC9804F1746C08CA18217C32905E462E36CE3B"
    "E39E772C180E86039B2783A2EC07A28FB5C55DF06F4C52C9DE2BCBF695581718"
    "3995497CEA956AE515D2261898FA051015728E5A8AACAA68FFFFFFFFFFFFFFFF",
    16,
)
G = 2
Q = (P - 1) // 2
PUB_BYTES = 256
CODE_DIGITS = 6

STEP_TIMEOUT_S = 15.0
CODE_TIMEOUT_S = 120.0


class PairingError(Exception):
    """Pairing failed or was refused; ``str(exc)`` says why."""


@dataclass(frozen=True)
class DeviceInfo:
    device_id: str
    name: str
    role: str


@dataclass(frozen=True)
class PairResult:
    peer: DeviceInfo
    key: bytes
    # What the peer said about itself beyond its id: "screens" (a list, as in
    # hello) and "platform", when it sent them.
    extra: dict = field(default_factory=dict, compare=False)


# --------------------------------------------------------------- crypto bits
def _keypair() -> tuple[int, bytes]:
    x = secrets.randbits(320) | (1 << 319)
    return x, pow(G, x, P).to_bytes(PUB_BYTES, "big")


def _peer_value(raw: object) -> int:
    try:
        data = bytes.fromhex(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError) as exc:
        raise PairingError("malformed public value") from exc
    y = int.from_bytes(data, "big")
    # In the prime-order subgroup and not a trivial value.
    if len(data) != PUB_BYTES or not 1 < y < P - 1 or pow(y, Q, P) != 1:
        raise PairingError("invalid public value")
    return y


def _commit(pub: bytes) -> str:
    return hashlib.sha256(b"curslink pair commit\0" + pub).hexdigest()


def _hkdf(ikm: bytes, salt: bytes, info: bytes, length: int) -> bytes:
    prk = hmac.new(salt, ikm, hashlib.sha256).digest()
    out, block, i = b"", b"", 1
    while len(out) < length:
        block = hmac.new(prk, block + info + bytes([i]), hashlib.sha256).digest()
        out += block
        i += 1
    return out[:length]


def _transcript(initiator: DeviceInfo, commit: str, recipient: DeviceInfo, pub_r: bytes, pub_i: bytes) -> bytes:
    h = hashlib.sha256(b"curslink pair v1\0")
    for part in (
        initiator.device_id, initiator.name, initiator.role, commit,
        recipient.device_id, recipient.name, recipient.role,
    ):
        data = part.encode()
        h.update(len(data).to_bytes(4, "big") + data)
    h.update(pub_r + pub_i)
    return h.digest()


def _derive(x: int, peer: int, transcript: bytes) -> tuple[str, bytes, bytes]:
    shared = pow(peer, x, P).to_bytes(PUB_BYTES, "big")
    code_bytes = _hkdf(shared, transcript, b"curslink code", 8)
    code = f"{int.from_bytes(code_bytes, 'big') % 10**CODE_DIGITS:0{CODE_DIGITS}d}"
    key = _hkdf(shared, transcript, b"curslink link key", KEY_BYTES)
    mac = hmac.new(key, b"curslink pair confirm\0" + transcript, hashlib.sha256).hexdigest()
    return code, key, mac


def qr_proof(token: bytes, transcript: bytes) -> str:
    return hmac.new(token, b"curslink pair qr\0" + transcript, hashlib.sha256).hexdigest()


def _extra(msg: Message) -> dict:
    out = {}
    screens = msg.get("screens")
    if isinstance(screens, list):
        out["screens"] = [s for s in screens[:16] if isinstance(s, dict)]
    platform = msg.get("platform")
    if isinstance(platform, str) and platform:
        out["platform"] = platform[:64]
    return out


def format_code(code: str) -> str:
    return f"{code[:3]} {code[3:]}"


def normalise_code(text: str) -> str:
    return "".join(ch for ch in text if ch.isdigit())


# ------------------------------------------------------------------ framing
async def _read(reader: asyncio.StreamReader, expected: str, timeout: float) -> Message:
    try:
        msg = await asyncio.wait_for(read_message(reader), timeout)
    except asyncio.TimeoutError as exc:
        raise PairingError("the other device stopped responding") from exc
    except ProtocolError as exc:
        raise PairingError(f"bad message: {exc}") from exc
    if msg is None:
        raise PairingError("the other device closed the connection")
    if msg.t == "pair_done" and expected != "pair_done":
        raise PairingError(str(msg.get("reason") or "refused"))
    if msg.t != expected:
        raise PairingError(f"expected {expected}, got {msg.t}")
    return msg


def _info(msg: Message) -> DeviceInfo:
    device_id, name, role = msg.get("device_id"), msg.get("name"), msg.get("role")
    if not isinstance(device_id, str) or not device_id or len(device_id) > 128:
        raise PairingError("missing device id")
    if role not in ROLES:
        raise PairingError("unknown role")
    if not isinstance(name, str) or not name:
        name = device_id
    return DeviceInfo(device_id, name[:128], role)


def _check_roles(me: DeviceInfo, peer: DeviceInfo) -> None:
    if peer.device_id == me.device_id:
        raise PairingError(f"both devices are called {me.device_id!r}; give one a different device_id")
    if peer.role == me.role:
        raise PairingError(f"both devices are {me.role}s; pair a host with an agent")


# --------------------------------------------------------------- the sides
async def initiate(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    me: DeviceInfo,
    show_code: Callable[[str, DeviceInfo], None],
    code_timeout: float = CODE_TIMEOUT_S,
    *,
    token: bytes | None = None,
    about: dict | None = None,
) -> PairResult:
    """Ask the device on the other end of the stream to pair.

    ``show_code`` is called with the formatted code once it is known; show
    it to the user, who types it into the other device. With the ``token``
    from the other device's QR code there's no code to type: it's still
    shown, but the other device checks the token instead. ``about`` adds
    ``screens`` / ``platform`` to the request.
    """
    x, pub = _keypair()
    commit = _commit(pub)
    hello = dict(about or {})
    if token is not None:
        hello["qr"] = True
    writer.write(encode("pair_hello", **hello, v=PAIRING_VERSION, device_id=me.device_id,
                        name=me.name, role=me.role, commit=commit))
    await writer.drain()
    msg = await _read(reader, "pair_key", STEP_TIMEOUT_S)
    peer = _info(msg)
    _check_roles(me, peer)
    y = _peer_value(msg.get("pub"))
    transcript = _transcript(me, commit, peer, y.to_bytes(PUB_BYTES, "big"), pub)
    reveal = {"pub": pub.hex()}
    if token is not None:
        reveal["proof"] = qr_proof(token, transcript)
    writer.write(encode("pair_reveal", **reveal))
    await writer.drain()
    code, key, mac = _derive(x, y, transcript)
    show_code(format_code(code), peer)
    done = await _read(reader, "pair_done", code_timeout + STEP_TIMEOUT_S)
    if done.get("ok") is not True:
        raise PairingError(str(done.get("reason") or "refused"))
    if not isinstance(done.get("mac"), str) or not hmac.compare_digest(done.get("mac"), mac):
        raise PairingError("the other device's confirmation did not match")
    return PairResult(peer, key, _extra(msg))


async def respond(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    me: DeviceInfo,
    ask_code: Callable[[DeviceInfo], Awaitable[str | None]],
    code_timeout: float = CODE_TIMEOUT_S,
    *,
    token: bytes | None = None,
    about: dict | None = None,
) -> PairResult:
    """Answer a pairing request; ``ask_code`` asks the user for the code
    shown on the initiator, returning ``None`` if they decline.

    ``token`` is the one in the QR code this device is showing: a request
    that says it scanned it (``qr``) proves it with the token instead of a
    typed code. ``about`` adds ``screens`` / ``platform`` to our reply."""

    def refuse(reason: str) -> None:
        writer.write(encode("pair_done", ok=False, reason=reason))

    hello = await _read(reader, "pair_hello", STEP_TIMEOUT_S)
    if hello.get("v") != PAIRING_VERSION:
        refuse("unsupported pairing version")
        raise PairingError(f"unsupported pairing version {hello.get('v')!r}")
    peer = _info(hello)
    try:
        _check_roles(me, peer)
    except PairingError as exc:
        refuse(str(exc))
        raise
    commit = hello.get("commit")
    if not isinstance(commit, str) or len(commit) != 64:
        refuse("missing commitment")
        raise PairingError("missing commitment")
    scanned = hello.get("qr") is True
    if scanned and token is None:
        refuse("no QR code is showing here; scan it again")
        raise PairingError("a request for a QR code that isn't showing")
    x, pub = _keypair()
    writer.write(encode("pair_key", **dict(about or {}), device_id=me.device_id, name=me.name, role=me.role,
                        pub=pub.hex()))
    await writer.drain()
    reveal = await _read(reader, "pair_reveal", STEP_TIMEOUT_S)
    y = _peer_value(reveal.get("pub"))
    pub_i = y.to_bytes(PUB_BYTES, "big")
    if not hmac.compare_digest(_commit(pub_i), commit):
        refuse("commitment mismatch")
        raise PairingError("the initiator's key did not match its commitment")
    transcript = _transcript(peer, commit, me, pub, pub_i)
    code, key, mac = _derive(x, y, transcript)

    if scanned:
        proof = reveal.get("proof")
        if not isinstance(proof, str) or not hmac.compare_digest(proof, qr_proof(token, transcript)):
            refuse("that QR code has expired; scan the new one")
            raise PairingError("wrong QR proof")
        writer.write(encode("pair_done", ok=True, mac=mac))
        await writer.drain()
        return PairResult(peer, key, _extra(hello))

    try:
        entered = await asyncio.wait_for(ask_code(peer), code_timeout)
    except asyncio.TimeoutError:
        refuse("timed out waiting for the code")
        raise PairingError("timed out waiting for the code") from None
    if entered is None:
        refuse("declined")
        raise PairingError("declined")
    if not hmac.compare_digest(normalise_code(entered), code):
        refuse("wrong code")
        raise PairingError("wrong code")
    writer.write(encode("pair_done", ok=True, mac=mac))
    await writer.drain()
    return PairResult(peer, key, _extra(hello))
