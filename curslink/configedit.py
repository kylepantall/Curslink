"""Reading and changing ``curslink.toml`` from the desktop app.

The command line only ever reads the config; the desktop app also writes it.
It uses tomlkit, which keeps the file's comments and layout, so a config
someone wrote by hand still reads the same after the app has changed one
setting in it.
"""

from __future__ import annotations

import contextlib
import os
import re
import shutil
import socket
import tempfile
from pathlib import Path
from typing import Any

import logging

import tomlkit
from tomlkit.items import AoT, Table

from curslink.pairing.store import default_dir

log = logging.getLogger(__name__)


def default_config_path() -> Path:
    return default_dir() / "curslink.toml"


def slug(text: str, fallback: str = "device") -> str:
    """A device id from a display name: ``"Kyle's PC"`` -> ``"kyles-pc"``."""
    s = re.sub(r"[^a-z0-9]+", "-", text.lower().replace("'", "")).strip("-")
    return s[:40] or fallback


def this_computer_name() -> str:
    name = socket.gethostname().split(".")[0]
    return name or "This computer"


def role_of(raw: dict[str, Any]) -> str | None:
    """``host`` or ``agent`` for a config with exactly one of those tables."""
    has_host, has_agent = "host" in raw, "agent" in raw
    if has_host == has_agent:
        return None
    return "host" if has_host else "agent"


def is_whole(raw: dict[str, Any]) -> bool:
    """More than a stray setting: a host with its [[devices]], or an agent
    with its device_id. (A page saving one setting into a deleted config
    used to leave just ``[host] effect = "pop"``.)"""
    role = role_of(raw)
    if role == "host":
        return "devices" in raw
    if role == "agent":
        return "device_id" in raw["agent"]
    return False


def _screens_aot(screens: list[dict[str, Any]]) -> AoT:
    aot = tomlkit.aot()
    for s in screens:
        t = tomlkit.table()
        for key in ("id", "x", "y", "width", "height", "os_x", "os_y", "z"):
            if key in s:
                t[key] = s[key]
        aot.append(t)
    return aot


def _device_table(device_id: str, name: str, screens: list[dict[str, Any]]) -> Table:
    t = tomlkit.table()
    t["id"] = device_id
    t["name"] = name
    t["screens"] = _screens_aot(screens)
    return t


def new_host_config(device_id: str, name: str, screens: list[dict[str, Any]]) -> str:
    """A starting config for a host whose own screens are ``screens``
    (in its display settings' coordinates, which also become the layout's)."""
    doc = tomlkit.document()
    doc.add(tomlkit.comment("Curslink settings, written by the Curslink app. You can edit it by hand too:"))
    doc.add(tomlkit.comment("see examples/curslink.toml in the Curslink repository for every option."))
    doc.add(tomlkit.nl())
    doc["host"] = {"device_id": device_id, "effect": "pop", "edge_switching": True}
    doc["gaze"] = {"enabled": False, "camera": 0, "calibration": "calibration.json"}
    doc["update"] = {"check": True, "auto": True}
    shifted = _normalise(screens)
    devices = tomlkit.aot()
    devices.append(_device_table(device_id, name, shifted))
    doc["devices"] = devices
    return tomlkit.dumps(doc)


def new_agent_config(device_id: str, name: str) -> str:
    doc = tomlkit.document()
    doc.add(tomlkit.comment("Curslink settings, written by the Curslink app. You can edit it by hand too."))
    doc.add(tomlkit.nl())
    doc["agent"] = {"device_id": device_id, "name": name, "animations": True}
    doc["update"] = {"check": True, "auto": True}
    return tomlkit.dumps(doc)


def _normalise(screens: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Layout copies of OS screens, moved so the layout starts at 0, 0 and with ids."""
    if not screens:
        return []
    min_x = min(s["x"] for s in screens)
    min_y = min(s["y"] for s in screens)
    out = []
    for i, s in enumerate(screens):
        out.append({
            "id": s.get("id") or f"screen{i + 1}",
            "x": s["x"] - min_x, "y": s["y"] - min_y,
            "width": s["width"], "height": s["height"],
            "os_x": s.get("os_x", s["x"]), "os_y": s.get("os_y", s["y"]),
        })
    return out


class ConfigFile:
    """One ``curslink.toml``, edited in place."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.doc = tomlkit.parse(self.path.read_text(encoding="utf-8")) if self.path.exists() else tomlkit.document()

    def reload(self) -> None:
        self.doc = tomlkit.parse(self.path.read_text(encoding="utf-8"))

    def raw(self) -> dict[str, Any]:
        """Plain Python values, as :func:`curslink.config.load_raw` would give."""
        return self.doc.unwrap()

    @property
    def role(self) -> str | None:
        return role_of(self.raw())

    def save(self) -> None:
        if not self.path.exists() and not is_whole(self.raw()):
            # The file went (deleted by hand) and this is one setting, not a
            # whole config: writing it would leave a config that can't load.
            log.warning("config not saved: the file is gone and this isn't a whole config")
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".curslink-", suffix=".toml", dir=self.path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
                fh.write(tomlkit.dumps(self.doc))
            os.replace(tmp, self.path)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(tmp)
            raise

    # -- simple values --------------------------------------------------------
    # ``table`` may be dotted for a sub-table: ``"agent.gaze"``.
    def get(self, table: str, key: str, default: Any = None) -> Any:
        t: Any = self.raw()
        for part in table.split("."):
            t = t.get(part, {}) if isinstance(t, dict) else {}
        return t.get(key, default) if isinstance(t, dict) else default

    def _table(self, table: str, create: bool):
        t: Any = self.doc
        for part in table.split("."):
            if part not in t:
                if not create:
                    return None
                t[part] = tomlkit.table()
            t = t[part]
        return t

    def set(self, table: str, key: str, value: Any) -> None:
        self._table(table, create=True)[key] = value

    def remove(self, table: str, key: str) -> None:
        t = self._table(table, create=False)
        if t is not None and key in t:
            del t[key]

    # -- calibrated edges -----------------------------------------------------
    def links(self) -> list[dict[str, Any]]:
        return list(self.raw().get("layout", {}).get("links", []))

    def set_link(self, a: str, a_side: str, a_span, b: str, b_side: str, b_span) -> None:
        """Calibrate the edge between screens ``a`` and ``b`` ("device/screen");
        replaces an earlier calibration of the same pair."""
        self.remove_link(a, b)
        layout = self._table("layout", create=True)
        if "links" not in layout:
            layout["links"] = tomlkit.aot()
        t = tomlkit.table()
        t["a"], t["a_side"], t["a_span"] = a, a_side, [round(v) for v in a_span]
        t["b"], t["b_side"], t["b_span"] = b, b_side, [round(v) for v in b_span]
        layout["links"].append(t)

    def remove_link(self, a: str, b: str) -> bool:
        layout = self._table("layout", create=False)
        if layout is None or "links" not in layout:
            return False
        aot = layout["links"]
        keep = [t for t in aot if {t.get("a"), t.get("b")} != {a, b}]
        if len(keep) == len(aot):
            return False
        if keep:
            new = tomlkit.aot()
            for t in keep:
                new.append(t)
            layout["links"] = new
        else:
            del layout["links"]
        return True

    # -- the layout -----------------------------------------------------------
    def devices(self) -> list[dict[str, Any]]:
        return list(self.raw().get("devices", []))

    def _device(self, device_id: str):
        for d in self.doc.get("devices", []):
            if d.get("id") == device_id:
                return d
        return None

    def has_device(self, device_id: str) -> bool:
        return self._device(device_id) is not None

    def move_screen(self, device_id: str, screen_id: str, x: int, y: int) -> None:
        device = self._device(device_id)
        if device is None:
            raise KeyError(device_id)
        for i, s in enumerate(device.get("screens", [])):
            if str(s.get("id", f"screen{i}")) == screen_id:
                s["x"], s["y"] = int(x), int(y)
                return
        raise KeyError(f"{device_id}/{screen_id}")

    def resize_screen(self, device_id: str, screen_id: str, width: int, height: int) -> None:
        device = self._device(device_id)
        if device is None:
            raise KeyError(device_id)
        for i, s in enumerate(device.get("screens", [])):
            if str(s.get("id", f"screen{i}")) == screen_id:
                s["width"], s["height"] = int(width), int(height)
                return
        raise KeyError(f"{device_id}/{screen_id}")

    def set_screen_z(self, device_id: str, screen_id: str, z: int | None) -> None:
        """Stacking order where screens overlap; None drops it (the default, 0)."""
        device = self._device(device_id)
        if device is None:
            raise KeyError(device_id)
        for i, s in enumerate(device.get("screens", [])):
            if str(s.get("id", f"screen{i}")) == screen_id:
                if z is None:
                    if "z" in s:
                        del s["z"]
                else:
                    s["z"] = int(z)
                return
        raise KeyError(f"{device_id}/{screen_id}")

    def add_device(self, device_id: str, name: str, screens: list[dict[str, Any]] | None = None) -> None:
        """Add a device to the layout, placed to the right of everything there.

        ``screens`` are its OS screens (what the agent reported); without them
        it gets one 1920x1080 screen to adjust on the layout page.
        """
        if self.has_device(device_id):
            return
        screens = _normalise(screens or [{"id": "main", "x": 0, "y": 0, "width": 1920, "height": 1080}])
        right = 0
        for d in self.devices():
            for s in d.get("screens", []):
                right = max(right, int(s.get("x", 0)) + int(s.get("width", 0)))
        for s in screens:
            s["x"] += right
        if "devices" not in self.doc:
            self.doc["devices"] = tomlkit.aot()
        self.doc["devices"].append(_device_table(device_id, name, screens))

    def replace_screens(self, device_id: str, screens: list[dict[str, Any]]) -> None:
        """Swap a device's screens for these (already in layout coordinates)."""
        device = self._device(device_id)
        if device is None:
            raise KeyError(device_id)
        device["screens"] = _screens_aot(screens)

    def _box(self, device_id: str) -> tuple[int, int, int, int] | None:
        device = self._device(device_id)
        screens = list(device.get("screens", [])) if device is not None else []
        if not screens:
            return None
        x0 = min(int(s.get("x", 0)) for s in screens)
        y0 = min(int(s.get("y", 0)) for s in screens)
        x1 = max(int(s.get("x", 0)) + int(s.get("width", 0)) for s in screens)
        y1 = max(int(s.get("y", 0)) + int(s.get("height", 0)) for s in screens)
        return x0, y0, x1, y1

    def side_of(self, device_id: str, anchor_id: str) -> str | None:
        """Which edge of ``anchor_id``'s screens ``device_id`` sits against:
        "left", "right", "top" or "bottom" (None if it isn't beside them)."""
        a, d = self._box(anchor_id), self._box(device_id)
        if a is None or d is None:
            return None
        gaps = {"right": d[0] - a[2], "left": a[0] - d[2], "bottom": d[1] - a[3], "top": a[1] - d[3]}
        side, gap = max(gaps.items(), key=lambda kv: kv[1])
        return side if gap >= -1 else None

    def place_beside(self, device_id: str, anchor_id: str, side: str) -> None:
        """Move ``device_id`` (all its screens) against one edge of
        ``anchor_id``'s screens, centred along it: pushing the pointer off
        that edge then lands on it. Edges calibrated for it are dropped."""
        a, d = self._box(anchor_id), self._box(device_id)
        if a is None or d is None:
            raise KeyError(device_id if d is None else anchor_id)
        w, h = d[2] - d[0], d[3] - d[1]
        if side == "right":
            nx, ny = a[2], a[1] + ((a[3] - a[1]) - h) // 2
        elif side == "left":
            nx, ny = a[0] - w, a[1] + ((a[3] - a[1]) - h) // 2
        elif side == "top":
            nx, ny = a[0] + ((a[2] - a[0]) - w) // 2, a[1] - h
        elif side == "bottom":
            nx, ny = a[0] + ((a[2] - a[0]) - w) // 2, a[3]
        else:
            raise ValueError(f"unknown side {side!r}")
        dx, dy = nx - d[0], ny - d[1]
        for s in self._device(device_id).get("screens", []):
            s["x"] = int(s.get("x", 0)) + dx
            s["y"] = int(s.get("y", 0)) + dy
        for link in self.links():
            ends = (str(link.get("a", "")), str(link.get("b", "")))
            if any(e.split("/")[0] == device_id for e in ends):
                self.remove_link(*ends)

    def remove_device(self, device_id: str) -> bool:
        devices = self.doc.get("devices")
        if devices is None:
            return False
        for i, d in enumerate(devices):
            if d.get("id") == device_id:
                del devices[i]
                return True
        return False


def touching_edges(devices: list[dict[str, Any]], tolerance: int = 64) -> list[tuple[str, str, str]]:
    """Where screens of different devices meet: ``(from "dev/screen", edge, to "dev/screen")``.

    A rough, symmetric check for the layout page; the router's own
    ``Layout.find_crossing`` is what actually decides at runtime.
    """
    screens = []
    for d in devices:
        for i, s in enumerate(d.get("screens", [])):
            screens.append((d.get("id"), str(s.get("id", f"screen{i}")), int(s.get("x", 0)), int(s.get("y", 0)),
                            int(s.get("width", 0)), int(s.get("height", 0))))
    out = []
    for a in screens:
        for b in screens:
            if a[0] == b[0]:
                continue
            ad, aid, ax, ay, aw, ah = a
            bd, bid, bx, by, bw, bh = b
            v_overlap = min(ay + ah, by + bh) - max(ay, by)
            h_overlap = min(ax + aw, bx + bw) - max(ax, bx)
            if v_overlap > 0 and abs(ax + aw - bx) <= tolerance:
                out.append((f"{ad}/{aid}", "right", f"{bd}/{bid}"))
            if v_overlap > 0 and abs(bx + bw - ax) <= tolerance:
                out.append((f"{ad}/{aid}", "left", f"{bd}/{bid}"))
            if h_overlap > 0 and abs(ay + ah - by) <= tolerance:
                out.append((f"{ad}/{aid}", "bottom", f"{bd}/{bid}"))
            if h_overlap > 0 and abs(by + bh - ay) <= tolerance:
                out.append((f"{ad}/{aid}", "top", f"{bd}/{bid}"))
    return out


def snap(rect: tuple[int, int, int, int], others: list[tuple[int, int, int, int]],
         distance: int = 80) -> tuple[int, int]:
    """Nudge a dragged screen ``(x, y, w, h)`` so its edges line up with
    nearby screens' edges; returns the new ``(x, y)``."""
    x, y, w, h = rect
    best_x, best_y = (distance + 1, x), (distance + 1, y)
    for ox, oy, ow, oh in others:
        for cand in (ox + ow, ox - w, ox, ox + ow - w):   # beside, or edges aligned
            d = abs(cand - x)
            if d < best_x[0]:
                best_x = (d, cand)
        for cand in (oy + oh, oy - h, oy, oy + oh - h):
            d = abs(cand - y)
            if d < best_y[0]:
                best_y = (d, cand)
    return best_x[1], best_y[1]


# -- switching between host and agent -----------------------------------------
# Tables both roles use, carried over when a role is set up for the first time.
SHARED_TABLES = ("pairing", "update", "relay")


def stash_path(path: Path, role: str) -> Path:
    """Where the role this computer isn't playing keeps its settings:
    ``curslink.toml`` -> ``curslink.agent.toml``."""
    return path.with_name(f"{path.stem}.{role}{path.suffix}")


def other_role(role: str | None) -> str:
    return "agent" if role == "host" else "host"


def name_and_id(raw: dict[str, Any]) -> tuple[str, str]:
    """This computer's (name, device id) in a config of either role."""
    role = role_of(raw)
    if role == "host":
        me = raw.get("host", {}).get("device_id", "")
        name = next((d.get("name", me) for d in raw.get("devices", []) if d.get("id") == me), me)
        return name or this_computer_name(), me or slug(name or this_computer_name())
    agent = raw.get("agent", {})
    name = agent.get("name") or agent.get("device_id") or this_computer_name()
    return name, agent.get("device_id") or slug(name)


def switch_role(path: str | Path, screens: list[dict[str, Any]] | None = None) -> str:
    """Make this computer the other role, keeping both roles' settings.

    The current config is put aside as ``curslink.<role>.toml``; the other
    role's earlier settings come back from theirs, or, the first time, a new
    config is written with the same name and device id (and the shared
    pairing, update and relay settings). Pairings live in their own store, so
    they're kept either way. Returns the new role."""
    path = Path(path)
    current = ConfigFile(path)
    role = current.role
    if role is None:
        raise ValueError("set this computer up first")
    target = other_role(role)
    old, new = stash_path(path, role), stash_path(path, target)
    text = None
    if new.exists():
        try:
            raw = tomlkit.parse(new.read_text(encoding="utf-8")).unwrap()
            if role_of(raw) == target and is_whole(raw):
                text = new.read_text(encoding="utf-8")
        except Exception:  # a broken side file: start that role afresh
            log.warning("ignoring %s: it isn't a whole %s config", new, target)
    if text is None:
        raw = current.raw()
        name, device_id = name_and_id(raw)
        if target == "host":
            screens = screens or [{"id": "main", "x": 0, "y": 0, "width": 1920, "height": 1080}]
            text = new_host_config(device_id, name, screens)
        else:
            text = new_agent_config(device_id, name)
        doc = tomlkit.parse(text)
        for table in SHARED_TABLES:
            if table in current.doc:
                doc[table] = current.doc[table]
        text = tomlkit.dumps(doc)
    shutil.copy2(path, old)
    fd, tmp = tempfile.mkstemp(prefix=".curslink-", suffix=".toml", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise
    with contextlib.suppress(OSError):
        new.unlink()  # it's the live config now
    return target
