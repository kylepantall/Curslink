"""Loading ``curslink.toml`` for the host and agents."""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from curslink.gaze.zones import GazeSettings
from curslink.layout import Device, Layout, Screen
from curslink.net.relay import (
    DEFAULT_RELAY_PORT,
    MIN_RELAY_KEY_LENGTH,
    RelayTarget,
    decode_pairing_code,
    parse_address,
    room_id,
)
from curslink.protocol import DEFAULT_PORT
from curslink.router import RouterSettings
from curslink.security import WeakSecretError, derive_key


class ConfigError(ValueError):
    pass


@dataclass
class GazeConfig:
    enabled: bool = False
    camera: int = 0
    calibration: Path = Path("calibration.json")
    preview: bool = False
    settings: GazeSettings = field(default_factory=GazeSettings)


@dataclass
class HostConfig:
    device_id: str
    secret: str
    layout: Layout
    listen: str = "0.0.0.0"
    port: int = DEFAULT_PORT
    lan: bool = True
    relay: RelayTarget | None = None
    router: RouterSettings = field(default_factory=RouterSettings)
    gaze: GazeConfig = field(default_factory=GazeConfig)


@dataclass
class AgentConfig:
    device_id: str
    secret: str
    host: str
    port: int = DEFAULT_PORT
    name: str = ""
    modifier_map: dict[str, str] = field(default_factory=dict)
    animations: bool = True
    relay: RelayTarget | None = None


@dataclass
class RelayServerConfig:
    key: str
    listen: str = "0.0.0.0"
    port: int = DEFAULT_RELAY_PORT
    max_connections: int = 256
    max_rooms: int = 64


def _pairing_code(raw: dict[str, Any]) -> dict[str, str] | None:
    code = os.environ.get("CURSLINK_PAIRING_CODE") or raw.get("relay", {}).get("code")
    if not code:
        return None
    try:
        return decode_pairing_code(code)
    except ValueError as exc:
        raise ConfigError(f"invalid pairing code: {exc}") from exc


def _secret(raw: dict[str, Any]) -> str:
    code = _pairing_code(raw)
    secret = os.environ.get("CURSLINK_SECRET") or raw.get("secret") or (code or {}).get("secret")
    if not isinstance(secret, str) or not secret:
        raise ConfigError(
            "set `secret` in the config file, the CURSLINK_SECRET env var, or a [relay].code pairing code"
        )
    return secret


def relay_target(raw: dict[str, Any], secret: str) -> RelayTarget | None:
    """The ``[relay]`` table a host or agent uses to meet over the internet.

    Either ``address`` + ``key``, or a single ``code`` from ``curslink invite``.
    """
    table = raw.get("relay", {})
    code = _pairing_code(raw) or {}
    address = table.get("address") or code.get("relay")
    key = os.environ.get("CURSLINK_RELAY_KEY") or table.get("key") or code.get("relay_key")
    if not address and not key:
        return None
    if not address or not key:
        raise ConfigError("[relay] needs both `address` and `key` (or a `code`)")
    try:
        parse_address(address)
    except ValueError as exc:
        raise ConfigError(str(exc)) from exc
    try:
        room = room_id(derive_key(secret))
    except WeakSecretError as exc:
        raise ConfigError(str(exc)) from exc
    return RelayTarget(address=address, key=key, room=room)


def relay_server_config(raw: dict[str, Any]) -> RelayServerConfig:
    table = raw.get("relay", {})
    key = os.environ.get("CURSLINK_RELAY_KEY") or table.get("key")
    if not isinstance(key, str) or len(key) < MIN_RELAY_KEY_LENGTH:
        raise ConfigError(
            f"[relay].key (or CURSLINK_RELAY_KEY) must be at least {MIN_RELAY_KEY_LENGTH} characters; "
            "`curslink relay --init` writes a config with a random one"
        )
    port = _int(table, "port", DEFAULT_RELAY_PORT)
    if not 0 < port < 65536:
        raise ConfigError("[relay].port must be 1-65535")
    return RelayServerConfig(
        key=key,
        listen=table.get("listen", "0.0.0.0"),
        port=port,
        max_connections=_int(table, "max_connections", 256),
        max_rooms=_int(table, "max_rooms", 64),
    )


def _int(table: dict[str, Any], key: str, default: int | None = None) -> int:
    value = table.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError(f"`{key}` must be an integer")
    return value


def parse_layout(raw: dict[str, Any]) -> Layout:
    devices = []
    for d in raw.get("devices", []):
        if "id" not in d:
            raise ConfigError("every [[devices]] entry needs an `id`")
        screens = []
        for i, s in enumerate(d.get("screens", [])):
            screens.append(
                Screen(
                    id=str(s.get("id", f"screen{i}")),
                    device_id=d["id"],
                    x=_int(s, "x"),
                    y=_int(s, "y"),
                    width=_int(s, "width"),
                    height=_int(s, "height"),
                    os_x=_int(s, "os_x", 0),
                    os_y=_int(s, "os_y", 0),
                )
            )
        if not screens:
            raise ConfigError(f"device {d['id']!r} needs at least one [[devices.screens]]")
        devices.append(Device(id=d["id"], name=d.get("name", d["id"]), screens=tuple(screens)))
    if not devices:
        raise ConfigError("no [[devices]] defined")
    try:
        return Layout.from_devices(devices)
    except ValueError as exc:
        raise ConfigError(str(exc)) from exc


def load_raw(path: str | Path) -> dict[str, Any]:
    try:
        with open(path, "rb") as fh:
            return tomllib.load(fh)
    except FileNotFoundError as exc:
        raise ConfigError(f"config file not found: {path}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"invalid TOML in {path}: {exc}") from exc


def host_config(raw: dict[str, Any], base_dir: Path = Path(".")) -> HostConfig:
    host = raw.get("host", {})
    layout = parse_layout(raw)
    device_id = host.get("device_id")
    if device_id not in layout.devices:
        raise ConfigError("[host].device_id must name one of the [[devices]]")
    router = RouterSettings(
        effect=host.get("effect", "pop"),
        edge_switching=host.get("edge_switching", True),
        gaze_switching=raw.get("gaze", {}).get("enabled", False),
        gap_tolerance_px=_int(host, "gap_tolerance_px", 64),
        gaze_within_device=raw.get("gaze", {}).get("within_device", False),
        return_hotkey=host.get("return_hotkey", "ctrl+alt+home"),
        gaze_toggle_hotkey=host.get("gaze_toggle_hotkey", "ctrl+alt+g"),
    )
    if router.effect not in ("pop", "blink", "none"):
        raise ConfigError("[host].effect must be pop, blink or none")
    g = raw.get("gaze", {})
    defaults = GazeSettings()
    gaze = GazeConfig(
        enabled=g.get("enabled", False),
        camera=_int(g, "camera", 0),
        calibration=base_dir / g.get("calibration", "calibration.json"),
        preview=g.get("preview", False),
        settings=GazeSettings(
            max_distance_deg=float(g.get("max_distance_deg", defaults.max_distance_deg)),
            switch_margin_deg=float(g.get("switch_margin_deg", defaults.switch_margin_deg)),
            dwell_s=float(g.get("dwell_ms", defaults.dwell_s * 1000)) / 1000,
            pitch_weight=float(g.get("pitch_weight", defaults.pitch_weight)),
        ),
    )
    secret = _secret(raw)
    relay = relay_target(raw, secret)
    lan = host.get("lan", True)
    if not lan and relay is None:
        raise ConfigError("[host].lan = false needs a [relay] to connect through")
    return HostConfig(
        device_id=device_id,
        secret=secret,
        layout=layout,
        listen=host.get("listen", "0.0.0.0"),
        port=_int(host, "port", DEFAULT_PORT),
        lan=lan,
        relay=relay,
        router=router,
        gaze=gaze,
    )


def agent_config(raw: dict[str, Any]) -> AgentConfig:
    agent = raw.get("agent", {})
    if not agent.get("device_id"):
        raise ConfigError("[agent].device_id is required")
    secret = _secret(raw)
    relay = relay_target(raw, secret)
    if not agent.get("host") and relay is None:
        raise ConfigError("set [agent].host (LAN), a [relay] (internet), or both")
    return AgentConfig(
        device_id=agent["device_id"],
        secret=secret,
        host=agent.get("host", ""),
        relay=relay,
        port=_int(agent, "port", DEFAULT_PORT),
        name=agent.get("name", agent["device_id"]),
        modifier_map=dict(agent.get("modifier_map", {})),
        animations=agent.get("animations", True),
    )
