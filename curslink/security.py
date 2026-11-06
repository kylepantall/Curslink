"""Encrypted, mutually authenticated transport using TLS with a pre-shared key.

Every device in a Curslink setup is configured with the same pairing secret.
Both sides stretch it with scrypt into a 256-bit key and use it for TLS-PSK
with ephemeral ECDHE, which gives us:

* encryption of every keystroke and pointer event on the LAN,
* mutual authentication (a peer without the secret cannot complete the
  handshake, in either direction),
* forward secrecy (ECDHE), without needing certificates.

TLS-PSK support in the standard library requires Python 3.13+.
"""

from __future__ import annotations

import hashlib
import ssl

_SALT = b"curslink/v1/psk"
PSK_IDENTITY = "curslink-v1"
# ECDHE-PSK suites only: never fall back to plain PSK (no forward secrecy).
_CIPHERS = "ECDHE-PSK-CHACHA20-POLY1305:ECDHE-PSK-AES256-CBC-SHA384:ECDHE-PSK-AES128-CBC-SHA256"

MIN_SECRET_LENGTH = 8


class WeakSecretError(ValueError):
    pass


def derive_key(secret: str) -> bytes:
    if len(secret) < MIN_SECRET_LENGTH:
        raise WeakSecretError(
            f"pairing secret must be at least {MIN_SECRET_LENGTH} characters"
        )
    return hashlib.scrypt(secret.encode(), salt=_SALT, n=2**14, r=8, p=1, dklen=32)


def _base_context(protocol: int) -> ssl.SSLContext:
    if not hasattr(ssl.SSLContext, "set_psk_client_callback"):
        raise RuntimeError("Curslink needs Python 3.13+ for TLS-PSK support")
    ctx = ssl.SSLContext(protocol)
    # The stdlib exposes PSK only for TLS 1.2 suites today.
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.maximum_version = ssl.TLSVersion.TLSv1_2
    ctx.set_ciphers(_CIPHERS)
    return ctx


def server_context(secret: str) -> ssl.SSLContext:
    key = derive_key(secret)
    ctx = _base_context(ssl.PROTOCOL_TLS_SERVER)

    def lookup(identity: str | None) -> bytes:
        # Returning an empty key makes the handshake fail for unknown identities.
        return key if identity == PSK_IDENTITY else b""

    ctx.set_psk_server_callback(lookup, identity_hint=PSK_IDENTITY)
    return ctx


def client_context(secret: str) -> ssl.SSLContext:
    key = derive_key(secret)
    ctx = _base_context(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE  # authentication comes from the PSK
    ctx.set_psk_client_callback(lambda hint: (PSK_IDENTITY, key))
    return ctx
