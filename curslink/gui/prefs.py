"""The desktop app's own preferences (not Curslink's config file).

Kept as JSON in the per-user Curslink folder, beside the pairing store::

    {"config": "/home/kyle/.config/curslink/curslink.toml",
     "start_minimized": true, "start_linking": true, "told_about_tray": false,
     "theme": "system", "glass": true}

Whether Curslink starts at sign-in is not stored here: the operating system
already knows (see :mod:`curslink.autostart`).
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, fields
from pathlib import Path

from curslink.configedit import default_config_path
from curslink.pairing.store import default_dir


def prefs_path() -> Path:
    return default_dir() / "desktop.json"


@dataclass
class Prefs:
    config: str = ""                 # empty = the default config path
    start_minimized: bool = True     # open to the tray, not the window
    start_linking: bool = True       # start the host/agent when the app opens
    told_about_tray: bool = False    # said "still running in the tray" once
    theme: str = "system"            # system / light / dark
    glass: bool = True               # Mica (Windows 11) or vibrancy (macOS) behind the window

    @property
    def config_path(self) -> Path:
        return Path(self.config).expanduser() if self.config else default_config_path()

    @classmethod
    def load(cls, path: Path | None = None) -> "Prefs":
        path = path or prefs_path()
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return cls()
        if not isinstance(data, dict):
            return cls()
        known = {f.name: f.type for f in fields(cls)}
        clean = {}
        for key, value in data.items():
            if key in known and isinstance(value, (bool if known[key] == "bool" else str)):
                clean[key] = value
        return cls(**clean)

    def save(self, path: Path | None = None) -> None:
        path = path or prefs_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(asdict(self), indent=2), encoding="utf-8")
        os.replace(tmp, path)
