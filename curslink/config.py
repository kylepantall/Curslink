"""Loading ``curslink.toml`` for the host and agents."""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from curslink.gaze.confirm import ConfirmSettings, FacingSettings
from curslink.gaze.zones import GazeSettings
from curslink.layout import Device, Layout, Screen
from curslink.pairing.service import DEFAULT_PAIRING_PORT
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
    camera_name: str = ""            # follow this camera if its index changes
    calibration: Path = Path("calibration.json")
    preview: bool = False
    settings: GazeSettings = field(default_factory=GazeSettings)
    # Agents with their own camera must agree before gaze switches to them.
    confirm: ConfirmSettings = field(default_factory=ConfirmSettings)
    # Used instead of the calibration when there isn't one: this camera
    # then only tells whether you face this computer.
    facing: FacingSettings = field(default_factory=FacingSettings)


@dataclass
class AgentGazeConfig:
    """``[agent.gaze]``: this agent's own camera tells the host when you face it."""

    enabled: bool = False
    camera: int = 0
    camera_name: str = ""            # follow this camera if its index changes
    preview: bool = False
    settings: FacingSettings = field(default_factory=FacingSettings)


@dataclass
class PairingConfig:
    enabled: bool = True             # listen for pairing requests and advertise on the LAN
    port: int = DEFAULT_PAIRING_PORT
    store: Path | None = None        # None = the per-user default (see pairing.store)


@dataclass
class HostConfig:
    device_id: str
    secret: str | None
    layout: Layout
    listen: str = "0.0.0.0"
    port: int = DEFAULT_PORT
    lan: bool = True
    # Send pointer moves by UDP to agents that take them (see curslink.net.udp).
    udp: bool = True
    relay: RelayTarget | None = None
    router: RouterSettings = field(default_factory=RouterSettings)
    gaze: GazeConfig = field(default_factory=GazeConfig)
    pairing: PairingConfig = field(default_factory=PairingConfig)
    # Drag files over an edge (or look at a device while dragging) to send them.
    files: bool = True

    @property
    def name(self) -> str:
        return self.layout.devices[self.device_id].name


@dataclass
class AgentFilesConfig:
    """``[agent.files]``: files dragged here from the host."""

    enabled: bool = True
    folder: Path | None = None       # None = Downloads


@dataclass
class AgentConfig:
    device_id: str
    secret: str | None
    host: str = ""                   # empty = find the paired host on the LAN
    port: int = DEFAULT_PORT
    name: str = ""
    host_id: str = ""                # which paired host to use if there are several
    modifier_map: dict[str, str] = field(default_factory=dict)
    animations: bool = True
    # Play the host's moves back evenly (see curslink.smoothing).
    smooth_pointer: bool = True
    # Take pointer moves by UDP when the host is reached directly (see curslink.net.udp).
    udp: bool = True
    relay: RelayTarget | None = None
    pairing: PairingConfig = field(default_factory=PairingConfig)
    gaze: AgentGazeConfig = field(default_factory=AgentGazeConfig)
    files: AgentFilesConfig = field(default_factory=AgentFilesConfig)


def agent_files_config(raw: Any) -> AgentFilesConfig:
    if not isinstance(raw, dict):
        raise ConfigError("[agent.files] must be a table")
    folder = str(raw.get("folder", "") or "").strip()
    return AgentFilesConfig(
        enabled=bool(raw.get("enabled", True)),
        folder=Path(folder).expanduser() if folder else None,
    )


def _secret(raw: dict[str, Any]) -> str | None:
    """The optional shared secret. Without one, devices must be paired."""
    secret = os.environ.get("CURSLINK_SECRET") or raw.get("secret") or (_pairing_code(raw) or {}).get("secret")
    if secret is None or secret == "":
        return None
    if not isinstance(secret, str):
        raise ConfigError("`secret` must be a string")
    return secret


def pairing_config(raw: dict[str, Any], base_dir: Path = Path(".")) -> PairingConfig:
    p = raw.get("pairing", {})
    store = p.get("store")
    if store is not None and not isinstance(store, str):
        raise ConfigError("[pairing].store must be a path")
    return PairingConfig(
        enabled=bool(p.get("enabled", True)),
        port=_int(p, "port", DEFAULT_PAIRING_PORT),
        store=(base_dir / Path(store).expanduser()) if store else None,
    )


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


def relay_target(raw: dict[str, Any], secret: str | None) -> RelayTarget | None:
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
    # Without a shared secret the room comes from each pair's link key at
    # runtime (see relay.paired_room_id), so it is left empty here.
    room = ""
    if secret:
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
                    z=_int(s, "z", 0),
                )
            )
        if not screens:
            raise ConfigError(f"device {d['id']!r} needs at least one [[devices.screens]]")
        devices.append(Device(id=d["id"], name=d.get("name", d["id"]), screens=tuple(screens)))
    if not devices:
        raise ConfigError("no [[devices]] defined")
    allow_overlap = raw.get("layout", {}).get("allow_overlap", False)
    if not isinstance(allow_overlap, bool):
        raise ConfigError("[layout].allow_overlap must be true or false")
    try:
        return Layout.from_devices(devices, allow_overlap)
    except ValueError as exc:
        raise ConfigError(str(exc)) from exc


def jump_screens(g: dict[str, Any], layout: Layout) -> frozenset[tuple[str, str]]:
    """``[gaze].jump_screens``: the monitors (``"device/screen"``) where
    looking puts the pointer on that very monitor. Monitors no longer in the
    layout are skipped, so removing a device doesn't break the config."""
    value = g.get("jump_screens", [])
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise ConfigError('[gaze].jump_screens must be a list of "device/screen" strings')
    known = {(s.device_id, s.id) for s in layout.screens()}
    out = set()
    for v in value:
        device_id, _, screen_id = v.partition("/")
        if (device_id, screen_id) in known:
            out.add((device_id, screen_id))
    return frozenset(out)


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
        gaze_within_device=bool(raw.get("gaze", {}).get("within_device", False)),
        gaze_jump_screens=jump_screens(raw.get("gaze", {}), layout),
        return_hotkey=host.get("return_hotkey", "ctrl+alt+home"),
        gaze_toggle_hotkey=host.get("gaze_toggle_hotkey", "ctrl+alt+g"),
        swap_cmd_ctrl=bool(host.get("swap_cmd_ctrl", True)),
    )
    if router.effect not in ("pop", "blink", "none"):
        raise ConfigError("[host].effect must be pop, blink or none")
    g = raw.get("gaze", {})
    defaults = GazeSettings()
    gaze = GazeConfig(
        enabled=g.get("enabled", False),
        camera=_int(g, "camera", 0),
        camera_name=str(g.get("camera_name", "") or ""),
        calibration=base_dir / g.get("calibration", "calibration.json"),
        preview=g.get("preview", False),
        settings=GazeSettings(
            max_distance_deg=float(g.get("max_distance_deg", defaults.max_distance_deg)),
            switch_margin_deg=float(g.get("switch_margin_deg", defaults.switch_margin_deg)),
            dwell_s=float(g.get("dwell_ms", defaults.dwell_s * 1000)) / 1000,
            pitch_weight=float(g.get("pitch_weight", defaults.pitch_weight)),
        ),
        confirm=ConfirmSettings(enabled=bool(g.get("confirm_with_agents", True))),
        facing=facing_settings(g, "[gaze]", "facing_dwell_ms"),
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
        udp=bool(host.get("udp", True)),
        relay=relay,
        router=router,
        gaze=gaze,
        pairing=pairing_config(raw, base_dir),
        files=bool(raw.get("files", {}).get("enabled", True)) if isinstance(raw.get("files", {}), dict) else True,
    )


def agent_config(raw: dict[str, Any], base_dir: Path = Path(".")) -> AgentConfig:
    agent = raw.get("agent", {})
    if not agent.get("device_id"):
        raise ConfigError("[agent].device_id is required")
    secret = _secret(raw)
    relay = relay_target(raw, secret)
    cfg = AgentConfig(
        device_id=agent["device_id"],
        secret=secret,
        host=agent.get("host", ""),
        relay=relay,
        port=_int(agent, "port", DEFAULT_PORT),
        name=agent.get("name", agent["device_id"]),
        host_id=agent.get("host_id", ""),
        modifier_map=dict(agent.get("modifier_map", {})),
        animations=agent.get("animations", True),
        smooth_pointer=bool(agent.get("smooth_pointer", True)),
        udp=bool(agent.get("udp", True)),
        pairing=pairing_config(raw, base_dir),
        gaze=agent_gaze_config(agent.get("gaze", {})),
        files=agent_files_config(agent.get("files", {})),
    )
    if not cfg.host and not cfg.relay and not cfg.pairing.enabled:
        raise ConfigError("set [agent].host (LAN), a [relay] (internet), or leave [pairing] enabled")
    return cfg


def facing_settings(g: dict[str, Any], where: str, dwell_key: str = "dwell_ms") -> FacingSettings:
    """Facing-detector settings; the host's ``[gaze].dwell_ms`` is the
    calibrated classifier's, so the host reads ``facing_dwell_ms``."""
    d = FacingSettings()
    try:
        settings = FacingSettings(
            facing_deg=float(g.get("facing_deg", d.facing_deg)),
            hysteresis_deg=float(g.get("hysteresis_deg", d.hysteresis_deg)),
            center_yaw_deg=float(g.get("center_yaw_deg", d.center_yaw_deg)),
            center_pitch_deg=float(g.get("center_pitch_deg", d.center_pitch_deg)),
            pitch_weight=float(g.get("pitch_weight", d.pitch_weight)),
            dwell_s=float(g.get(dwell_key, d.dwell_s * 1000)) / 1000,
        )
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"{where}: {exc}") from exc
    if settings.facing_deg <= 0:
        raise ConfigError(f"{where}.facing_deg must be positive")
    return settings


def agent_gaze_config(g: Any) -> AgentGazeConfig:
    if not isinstance(g, dict):
        raise ConfigError("[agent.gaze] must be a table")
    settings = facing_settings(g, "[agent.gaze]")
    return AgentGazeConfig(
        enabled=bool(g.get("enabled", False)),
        camera=_int(g, "camera", 0),
        camera_name=str(g.get("camera_name", "") or ""),
        preview=bool(g.get("preview", False)),
        settings=settings,
    )
