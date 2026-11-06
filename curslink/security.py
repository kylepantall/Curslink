"""Encrypted, mutually authenticated transport using TLS with a pre-shared key.

Two ways to key it:

* **Paired** (the default): pairing two devices (``curslink pair``) gives
  them a random 32-byte link key, kept in the pairing store. The agent's PSK
  identity is ``curslink-v2:<device_id>`` and the host looks that device's
  key up in its store.
* **Shared secret** (the original setup): every device has the same
  ``secret``; both sides stretch it with scrypt into a 256-bit key used
  with the identity ``curslink-v1``.

Either way we get, without certificates:

* encryption of every keystroke and pointer event on the LAN,
* mutual authentication (a peer without the key cannot complete the
  handshake, in either direction),
* forward secrecy (ECDHE).

The stdlib PSK callback can't tell which connection it is serving, so the
agent's ``hello`` also carries :func:`hello_auth`, a MAC over the TLS
channel binding and its device id, which ties the claimed device id to the
key that was actually used.

TLS-PSK support in the standard library requires Python 3.13+.
"""

from __future__ import annotations

import hashlib
import hmac
import ssl
from typing import Callable

_SALT = b"curslink/v1/psk"
PSK_IDENTITY = "curslink-v1"
PAIRED_IDENTITY_PREFIX = "curslink-v2:"
# ECDHE-PSK suites only: never fall back to plain PSK (no forward secrecy).
_CIPHERS = "ECDHE-PSK-CHACHA20-POLY1305:ECDHE-PSK-AES256-CBC-SHA384:ECDHE-PSK-AES128-CBC-SHA256"

MIN_SECRET_LENGTH = 8

KeyLookup = Callable[[str], bytes | None]


class WeakSecretError(ValueError):
    pass


def derive_key(secret: str) -> bytes:
    if len(secret) < MIN_SECRET_LENGTH:
        raise WeakSecretError(
            f"pairing secret must be at least {MIN_SECRET_LENGTH} characters"
        )
    return hashlib.scrypt(secret.encode(), salt=_SALT, n=2**14, r=8, p=1, dklen=32)


def paired_identity(device_id: str) -> str:
    return PAIRED_IDENTITY_PREFIX + device_id


def _base_context(protocol: int) -> ssl.SSLContext:
    if not hasattr(ssl.SSLContext, "set_psk_client_callback"):
        raise RuntimeError("Curslink needs Python 3.13+ for TLS-PSK support")
    ctx = ssl.SSLContext(protocol)
    # The stdlib exposes PSK only for TLS 1.2 suites today.
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.maximum_version = ssl.TLSVersion.TLSv1_2
    ctx.set_ciphers(_CIPHERS)
    return ctx


def server_context(secret: str | None = None, paired_key: KeyLookup | None = None) -> ssl.SSLContext:
    """Host side. ``paired_key(device_id)`` returns a paired device's link key."""
    legacy = derive_key(secret) if secret else None
    ctx = _base_context(ssl.PROTOCOL_TLS_SERVER)

    def lookup(identity: str | None) -> bytes:
        # Returning an empty key makes the handshake fail for unknown identities.
        if identity == PSK_IDENTITY and legacy:
            return legacy
        if identity and identity.startswith(PAIRED_IDENTITY_PREFIX) and paired_key:
            return paired_key(identity[len(PAIRED_IDENTITY_PREFIX):]) or b""
        return b""

    ctx.set_psk_server_callback(lookup, identity_hint=PSK_IDENTITY)
    return ctx


def client_context(
    secret: str | None = None, *, device_id: str | None = None, key: bytes | None = None
) -> ssl.SSLContext:
    """Agent side: a paired link ``key`` (with this ``device_id``), or the shared ``secret``."""
    if key is not None:
        if not device_id:
            raise ValueError("a paired key needs the device id it was paired as")
        identity, psk = paired_identity(device_id), key
    elif secret:
        identity, psk = PSK_IDENTITY, derive_key(secret)
    else:
        raise ValueError("no key: pair this device or set a shared secret")
    ctx = _base_context(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE  # authentication comes from the PSK
    ctx.set_psk_client_callback(lambda hint: (identity, psk))
    return ctx


def channel_binding(ssl_object) -> bytes:
    binding = ssl_object.get_channel_binding("tls-unique") if ssl_object else None
    if not binding:
        raise ValueError("no TLS channel binding available")
    return binding


def hello_auth(key: bytes, binding: bytes, device_id: str) -> str:
    return hmac.new(key, b"curslink hello\0" + binding + device_id.encode(), hashlib.sha256).hexdigest()


class HelloVerifier:
    """Checks that an agent's hello was sent with the key for the id it claims.

    Agents that predate pairing send no ``auth``; they are let in only on a
    shared secret and only under a device id that is not paired.
    """

    def __init__(self, secret: str | None = None, paired_key: KeyLookup | None = None):
        self._legacy = derive_key(secret) if secret else None
        self._paired_key = paired_key or (lambda d: None)

    def __call__(self, device_id: str, auth: object, binding: bytes | None) -> bool:
        paired = self._paired_key(device_id)
        if not isinstance(auth, str):
            return paired is None and self._legacy is not None
        if binding is None:
            return False
        for key in (paired, self._legacy if paired is None else None):
            if key and hmac.compare_digest(auth, hello_auth(key, binding, device_id)):
                return True
        return False
