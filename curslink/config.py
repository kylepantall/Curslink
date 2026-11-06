"""Loading ``curslink.toml`` for the host and agents."""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from curslink.gaze.zones import GazeSettings
from curslink.layout import Device, Layout, Screen
from curslink.pairing.service import DEFAULT_PAIRING_PORT
from curslink.protocol import DEFAULT_PORT
from curslink.router import RouterSettings


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
    router: RouterSettings = field(default_factory=RouterSettings)
    gaze: GazeConfig = field(default_factory=GazeConfig)
    pairing: PairingConfig = field(default_factory=PairingConfig)

    @property
    def name(self) -> str:
        return self.layout.devices[self.device_id].name


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
    pairing: PairingConfig = field(default_factory=PairingConfig)


def _secret(raw: dict[str, Any]) -> str | None:
    """The optional shared secret. Without one, devices must be paired."""
    secret = os.environ.get("CURSLINK_SECRET") or raw.get("secret")
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
    return HostConfig(
        device_id=device_id,
        secret=_secret(raw),
        layout=layout,
        listen=host.get("listen", "0.0.0.0"),
        port=_int(host, "port", DEFAULT_PORT),
        router=router,
        gaze=gaze,
        pairing=pairing_config(raw, base_dir),
    )


def agent_config(raw: dict[str, Any], base_dir: Path = Path(".")) -> AgentConfig:
    agent = raw.get("agent", {})
    if not agent.get("device_id"):
        raise ConfigError("[agent].device_id is required")
    cfg = AgentConfig(
        device_id=agent["device_id"],
        secret=_secret(raw),
        host=agent.get("host", ""),
        port=_int(agent, "port", DEFAULT_PORT),
        name=agent.get("name", agent["device_id"]),
        host_id=agent.get("host_id", ""),
        modifier_map=dict(agent.get("modifier_map", {})),
        animations=agent.get("animations", True),
        pairing=pairing_config(raw, base_dir),
    )
    if not cfg.host and not cfg.pairing.enabled:
        raise ConfigError("set [agent].host, or leave [pairing] enabled so the host can be found")
    return cfg
