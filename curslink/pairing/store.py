"""The pairing store: one random 32-byte link key per paired device.

Pairing two devices (see :mod:`curslink.pairing.handshake`) gives both of
them the same link key. It keys TLS-PSK for every later connection between
them, whether that runs directly on the LAN or through a relay, so no shared
secret has to be typed into a config file.

The store is a small JSON file readable only by the current user::

    {"v": 1, "peers": {"laptop": {"key": "<base64>", "name": "Work laptop",
                                   "role": "agent", "paired_at": 1762424400}}}
"""

from __future__ import annotations

import base64
import contextlib
import json
import os
import sys
import tempfile
import time
from dataclasses import dataclass, replace
from pathlib import Path

ROLES = ("host", "agent")
KEY_BYTES = 32


def default_dir() -> Path:
    """Where Curslink keeps per-user state (``CURSLINK_HOME`` overrides it)."""
    override = os.environ.get("CURSLINK_HOME")
    if override:
        return Path(override)
    if sys.platform == "win32":
        return Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming") / "Curslink"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "Curslink"
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "curslink"


def default_path() -> Path:
    return default_dir() / "pairings.json"


@dataclass(frozen=True)
class Peer:
    device_id: str
    name: str
    role: str
    key: bytes
    paired_at: float = 0.0
    # Where it was last reached (an IP or name), so a paired agent can find
    # its host even when mDNS doesn't make it across the network.
    address: str = ""


def _bare(address: str) -> str:
    """Drop an IPv6 scope ("fe80::1%en0") and surrounding space."""
    return (address or "").strip().split("%")[0]


class PairingStore:
    """Paired devices, re-read from disk whenever another process changes it.

    ``curslink pair`` and a running host or agent are separate processes that
    share one file, so lookups check the file's mtime and reload if needed.
    """

    def __init__(self, path: str | Path | None = None):
        self.path = Path(path) if path else default_path()
        self._peers: dict[str, Peer] = {}
        self._stamp: tuple[int, int] | None = None
        self._reload()

    # ------------------------------------------------------------- reading
    def _reload(self) -> None:
        try:
            st = self.path.stat()
        except FileNotFoundError:
            self._peers, self._stamp = {}, None
            return
        stamp = (st.st_mtime_ns, st.st_size)
        if stamp == self._stamp:
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"cannot read pairing store {self.path}: {exc}") from exc
        peers = {}
        for device_id, rec in (raw.get("peers") or {}).items():
            try:
                key = base64.b64decode(rec["key"], validate=True)
            except (KeyError, ValueError, TypeError):
                continue
            if len(key) != KEY_BYTES:
                continue
            peers[device_id] = Peer(
                device_id=device_id,
                name=str(rec.get("name", device_id)),
                role=str(rec.get("role", "")),
                key=key,
                paired_at=float(rec.get("paired_at", 0)),
                address=str(rec.get("address", "") or ""),
            )
        self._peers, self._stamp = peers, stamp

    def get(self, device_id: str) -> Peer | None:
        self._reload()
        return self._peers.get(device_id)

    def key(self, device_id: str) -> bytes | None:
        peer = self.get(device_id)
        return peer.key if peer else None

    def peers(self, role: str | None = None) -> list[Peer]:
        self._reload()
        return [p for p in self._peers.values() if role is None or p.role == role]

    # ------------------------------------------------------------- writing
    def add(self, device_id: str, name: str, role: str, key: bytes, address: str = "") -> Peer:
        if role not in ROLES:
            raise ValueError(f"role must be one of {ROLES}")
        if len(key) != KEY_BYTES:
            raise ValueError("link keys are 32 bytes")
        self._reload()
        peer = Peer(device_id, name, role, key, time.time(), _bare(address))
        self._peers[device_id] = peer
        self._save()
        return peer

    def set_address(self, device_id: str, address: str) -> None:
        """Remember where ``device_id`` was last reached (saved only if it changed)."""
        address = _bare(address)
        self._reload()
        peer = self._peers.get(device_id)
        if peer is None or not address or peer.address == address:
            return
        self._peers[device_id] = replace(peer, address=address)
        self._save()

    def remove(self, device_id: str) -> bool:
        self._reload()
        if self._peers.pop(device_id, None) is None:
            return False
        self._save()
        return True

    def _save(self) -> None:
        data = {
            "v": 1,
            "peers": {
                p.device_id: {
                    "key": base64.b64encode(p.key).decode(),
                    "name": p.name,
                    "role": p.role,
                    "paired_at": int(p.paired_at),
                    **({"address": p.address} if p.address else {}),
                }
                for p in self._peers.values()
            },
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # Write to a private temp file and rename, so readers never see half a file.
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=".pairings-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(data, fh, indent=2)
            os.chmod(tmp, 0o600)
            os.replace(tmp, self.path)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(tmp)
            raise
        self._stamp = None
        self._reload()

