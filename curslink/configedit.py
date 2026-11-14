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


OPPOSITE = {"left": "right", "right": "left", "top": "bottom", "bottom": "top"}


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

    # -- key mappings ---------------------------------------------------------
    def keymap(self, name: str) -> dict[str, str] | None:
        """A ``[keymap]`` table (``"mac>windows"``, ``"device:<id>"``), or
        ``None`` when that pair uses the built-in mapping."""
        table = self.raw().get("keymap", {}).get(name)
        return dict(table) if isinstance(table, dict) else None

    def set_keymap(self, name: str, mapping: dict[str, str] | None) -> None:
        """Write a pair's mapping; ``None`` goes back to the built-in one."""
        if mapping is None:
            t = self._table("keymap", create=False)
            if t is not None and name in t:
                del t[name]
                if not len(t):
                    del self.doc["keymap"]
            return
        t = self._table("keymap", create=True)
        inline = tomlkit.inline_table()
        inline.update(mapping)
        t[name] = inline

    def set_modifier_map(self, mapping: dict[str, str]) -> None:
        """``[agent].modifier_map``: this computer's own fixes for the keys the host sends."""
        if not mapping:
            self.remove("agent", "modifier_map")
            return
        inline = tomlkit.inline_table()
        inline.update(mapping)
        self.set("agent", "modifier_map", inline)

    # -- pointer speed per device ---------------------------------------------
    def pointer_feel(self, device_id: str) -> dict[str, Any] | None:
        """``[pointer.device."<id>"]``: {feel, speed}, or None (the defaults)."""
        t = self.get("pointer", "device", {})
        v = t.get(device_id) if isinstance(t, dict) else None
        return dict(v) if isinstance(v, dict) else None

    def set_pointer_feel(self, device_id: str, value: dict[str, Any] | None) -> None:
        """Set how the pointer moves on ``device_id``; None goes back to the defaults."""
        if value is None:
            t = self._table("pointer.device", create=False)
            if t is not None and device_id in t:
                del t[device_id]
                if not len(t):
                    del self.doc["pointer"]["device"]
                    if not len(self.doc["pointer"]):
                        del self.doc["pointer"]
            return
        inline = tomlkit.inline_table()
        inline.update(value)
        self._table("pointer.device", create=True)[device_id] = inline

    def set_mirror_joined(self, table: dict[str, Any]) -> None:
        """``[layout].mirror_joined``: ``"device/screen"`` behind -> ``"all"``
        or a list of the joined screens that keep it mirrored."""
        if not table:
            self.remove("layout", "mirror_joined")
            return
        inline = tomlkit.inline_table()
        inline.update(table)
        self.set("layout", "mirror_joined", inline)

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

    def fits(self) -> list[dict[str, Any]]:
        return list(self.raw().get("layout", {}).get("fit", []))

    def set_fit(self, behind: str, front: str, x: float, y: float, scale: float) -> None:
        """Calibrate where screen ``front`` really stands on ``behind``
        ("device/screen", see curslink.layout.Fit); replaces an earlier one."""
        self.remove_fit(behind, front)
        layout = self._table("layout", create=True)
        if "fit" not in layout:
            layout["fit"] = tomlkit.aot()
        t = tomlkit.table()
        t["behind"], t["front"] = behind, front
        t["x"], t["y"], t["scale"] = round(x, 1), round(y, 1), round(scale, 4)
        layout["fit"].append(t)

    def remove_fit(self, behind: str, front: str) -> bool:
        layout = self._table("layout", create=False)
        if layout is None or "fit" not in layout:
            return False
        aot = layout["fit"]
        keep = [t for t in aot if (t.get("behind"), t.get("front")) != (behind, front)]
        if len(keep) == len(aot):
            return False
        if keep:
            new = tomlkit.aot()
            for t in keep:
                new.append(t)
            layout["fit"] = new
        else:
            del layout["fit"]
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

    def sync_screens(self, device_id: str, reported: list[dict[str, Any]]) -> dict[str, list[str]] | None:
        """Make ``device_id``'s screens the ones it reported (its OS screens,
        as in hello.screens): a monitor plugged into it gets a screen, one
        taken away loses its screen, and each keeps its OS size and place.

        Screens already laid out keep where they are on the canvas; a new
        one goes where it sits in that device's own display arrangement,
        next to one that's already laid out (or where the device was, for
        the 1920x1080 placeholder it got when it reported nothing).

        Returns ``{"added", "removed", "changed"}`` (screen ids), or None
        when nothing changed."""
        device = self._device(device_id)
        if device is None:
            raise KeyError(device_id)
        wanted = []
        for i, s in enumerate(reported or []):
            if not isinstance(s, dict) or not all(
                    isinstance(s.get(k), (int, float)) and not isinstance(s.get(k), bool)
                    for k in ("x", "y", "width", "height")):
                continue
            if s["width"] <= 0 or s["height"] <= 0:
                continue
            sid = str(s.get("id") or s.get("name") or f"screen{i + 1}")
            if sid in (w["id"] for w in wanted):
                continue
            wanted.append({"id": sid, "os_x": int(s["x"]), "os_y": int(s["y"]),
                           "width": int(s["width"]), "height": int(s["height"])})
        if not wanted:
            return None
        current = {}
        for i, s in enumerate(device.get("screens", [])):
            sid = str(s.get("id", f"screen{i}"))
            current[sid] = {"id": sid, "x": int(s.get("x", 0)), "y": int(s.get("y", 0)),
                            "width": int(s.get("width", 0)), "height": int(s.get("height", 0)),
                            "os_x": int(s.get("os_x", 0)), "os_y": int(s.get("os_y", 0)),
                            **({"z": int(s["z"])} if "z" in s else {})}
        kept = [w for w in wanted if w["id"] in current]
        if kept:
            anchor = next((w for w in kept if w["id"] == "main"), kept[0])
            base_x = current[anchor["id"]]["x"] - anchor["os_x"]
            base_y = current[anchor["id"]]["y"] - anchor["os_y"]
        else:
            base_x = min((c["x"] for c in current.values()), default=0) - min(w["os_x"] for w in wanted)
            base_y = min((c["y"] for c in current.values()), default=0) - min(w["os_y"] for w in wanted)
        screens, added, changed = [], [], []
        for w in wanted:
            old = current.get(w["id"])
            if old is None:
                screens.append({"id": w["id"], "x": base_x + w["os_x"], "y": base_y + w["os_y"],
                                "width": w["width"], "height": w["height"], "os_x": w["os_x"], "os_y": w["os_y"]})
                added.append(w["id"])
                continue
            new = {**old, "width": w["width"], "height": w["height"], "os_x": w["os_x"], "os_y": w["os_y"]}
            if new != old:
                changed.append(w["id"])
            screens.append(new)
        removed = [sid for sid in current if sid not in {w["id"] for w in wanted}]
        if not (added or removed or changed):
            return None
        self.replace_screens(device_id, screens)
        return {"added": added, "removed": removed, "changed": changed}

    def move_clear_of_others(self, device_id: str) -> None:
        """Move ``device_id``'s screens, together, to the right of every other
        device's (as a new device goes), so none overlap."""
        device = self._device(device_id)
        box = self._box(device_id)
        if device is None or box is None:
            raise KeyError(device_id)
        right = max((int(s.get("x", 0)) + int(s.get("width", 0))
                     for d in self.devices() if d.get("id") != device_id for s in d.get("screens", [])),
                    default=box[0])
        dx = right - box[0]
        for s in device.get("screens", []):
            s["x"] = int(s.get("x", 0)) + dx

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

    def place_beside(self, device_id: str, anchor_id: str, side: str, whole_side: bool = False) -> None:
        """Move ``device_id`` (all its screens) against one edge of
        ``anchor_id``'s screens, centred along it: pushing the pointer off
        that edge then lands on it. Edges calibrated for it are dropped.

        ``whole_side`` (a phone: one small screen): link every one of the
        anchor's screens that's open on that side to it, end to end, so the
        whole side leads to it, across monitors of any size and however
        they're staggered, not just the stretch it happens to touch."""
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
        screens = list(self._device(device_id).get("screens", []))
        if whole_side and len(screens) == 1:
            mine = screens[0]
            facing = OPPOSITE[side]
            length = int(mine.get("width", 0)) if side in ("top", "bottom") else int(mine.get("height", 0))
            target = f"{device_id}/{mine.get('id', 'screen0')}"
            for screen_id, span in self.open_stretches(anchor_id, side):
                self.set_link(f"{anchor_id}/{screen_id}", side, span, target, facing, (0, length))

    def open_stretches(self, device_id: str, side: str, min_px: int = 40) -> list[tuple[str, tuple[int, int]]]:
        """For each of a device's screens, the longest stretch of its ``side``
        edge with none of the device's other screens beyond it, as
        ``(screen id, (start, end))`` along that edge (px from its top or left)."""
        rects = [(str(s.get("id", f"screen{i}")), int(s.get("x", 0)), int(s.get("y", 0)),
                  int(s.get("width", 0)), int(s.get("height", 0)))
                 for i, s in enumerate((self._device(device_id) or {}).get("screens", []))]
        out = []
        for sid, x, y, w, h in rects:
            horizontal = side in ("top", "bottom")
            edge = {"top": y, "bottom": y + h, "left": x, "right": x + w}[side]
            lo, hi = (x, x + w) if horizontal else (y, y + h)
            covered = []
            for oid, ox, oy, ow, oh in rects:
                if oid == sid:
                    continue
                near = {"top": oy + oh, "bottom": oy, "left": ox + ow, "right": ox}[side]
                if abs(near - edge) > 8:
                    continue
                a, b = (ox, ox + ow) if horizontal else (oy, oy + oh)
                if min(b, hi) > max(a, lo):
                    covered.append((max(a, lo), min(b, hi)))
            best, at = None, lo
            for a, b in sorted(covered) + [(hi, hi)]:
                if a - at >= min_px and (best is None or a - at > best[1] - best[0]):
                    best = (at, a)
                at = max(at, b)
            if best is not None:
                out.append((sid, (best[0] - lo, best[1] - lo)))
        return out

    def remove_device(self, device_id: str) -> bool:
        devices = self.doc.get("devices")
        if devices is None:
            return False
        for i, d in enumerate(devices):
            if d.get("id") == device_id:
                del devices[i]
                return True
        return False

    def _refs(self, device_id: str, rewrite) -> None:
        """Run every "device/screen" and "device:<id>" reference to
        ``device_id`` through ``rewrite(ref) -> new ref or None`` (None drops
        it): calibrated edges, fits, mirror_joined, gaze jump screens, keymaps."""
        prefix = f"{device_id}/"

        def mine(ref: Any) -> bool:
            return isinstance(ref, str) and ref.startswith(prefix)

        layout = self._table("layout", create=False)
        for name, keys in (("links", ("a", "b")), ("fit", ("behind", "front"))):
            if layout is None or name not in layout:
                continue
            keep = tomlkit.aot()
            for t in layout[name]:
                if any(mine(t.get(k)) for k in keys):
                    new = {k: rewrite(t.get(k)) if mine(t.get(k)) else t.get(k) for k in keys}
                    if any(v is None for v in new.values()):
                        continue
                    for k, v in new.items():
                        t[k] = v
                keep.append(t)
            if len(keep):
                layout[name] = keep
            else:
                del layout[name]
        joined = self.raw().get("layout", {}).get("mirror_joined")
        if isinstance(joined, dict):
            table: dict[str, Any] = {}
            for key, which in joined.items():
                key = rewrite(key) if mine(key) else key
                if key is None:
                    continue
                if isinstance(which, list):
                    which = [w for w in (rewrite(v) if mine(v) else v for v in which) if w is not None]
                    if not which:
                        continue
                table[key] = which
            if table != joined:
                self.set_mirror_joined(table)
        jump = self.get("gaze", "jump_screens")
        if isinstance(jump, list) and any(mine(v) for v in jump):
            self.set("gaze", "jump_screens", [w for w in (rewrite(v) if mine(v) else v for v in jump) if w is not None])
        feel = self.pointer_feel(device_id)
        if feel is not None:
            self.set_pointer_feel(device_id, None)
            renamed = rewrite(prefix)
            if renamed is not None:
                self.set_pointer_feel(renamed[:-1], feel)
        mapping = self.keymap(f"device:{device_id}")
        if mapping is not None:
            self.set_keymap(f"device:{device_id}", None)
            renamed = rewrite(prefix)
            if renamed is not None:
                self.set_keymap(f"device:{renamed[:-1]}", mapping)

    def forget_device(self, device_id: str) -> bool:
        """Take a device out of the layout along with everything that points
        at it (calibrated edges, fits, mirror and gaze screens, its keymap).
        True if it was there."""
        if not self.remove_device(device_id):
            return False
        self._refs(device_id, lambda _ref: None)
        return True

    def rename_device(self, old_id: str, new_id: str, name: str | None = None) -> bool:
        """Give a laid-out device a new id (and name), keeping where it sits
        and everything calibrated against it: a phone or computer that paired
        again under a new id takes over its old place."""
        device = self._device(old_id)
        if device is None or old_id == new_id or self.has_device(new_id):
            return False
        device["id"] = new_id
        if name:
            device["name"] = name
        self._refs(old_id, lambda ref: new_id + ref[len(old_id):])
        return True

    def adopt_paired(self, peers, host_id: str) -> list[str]:
        """Lay out paired agents that aren't in the layout yet (``peers`` are
        pairing-store peers). A laid-out device that's no longer paired but
        has the same name is taken over instead (it paired again under a new
        id), so it keeps its place. Returns the names of what was added."""
        paired = {p.device_id for p in peers}
        changed = []
        for p in peers:
            if p.role != "agent" or p.device_id == host_id or self.has_device(p.device_id):
                continue
            stale = next((str(d.get("id")) for d in self.devices()
                          if d.get("id") not in paired and d.get("id") != host_id
                          and str(d.get("name", "")).strip().casefold() == p.name.strip().casefold()), None)
            if stale is None or not self.rename_device(stale, p.device_id, p.name):
                self.add_device(p.device_id, p.name)
            changed.append(p.name)
        return changed

    def unpaired_devices(self, peers, host_id: str) -> list[dict[str, Any]]:
        """Laid-out devices with no pairing: left over from an unpair, or from
        before a switch of roles. Without a shared secret they can't connect."""
        paired = {p.device_id for p in peers}
        return [d for d in self.devices() if d.get("id") != host_id and d.get("id") not in paired]


def sync_agent_screens(cfg: ConfigFile, device_id: str, reported: list[dict[str, Any]]) -> dict[str, list[str]] | None:
    """Host: lay out the screens an agent reported (:meth:`ConfigFile.sync_screens`)
    and save, if that changed anything and still makes a layout the host
    can run (moving the device clear of the others if its screens, as they
    are, would overlap theirs). Returns what changed, or None."""
    from curslink.config import ConfigError, parse_layout

    if not cfg.has_device(device_id):
        return None
    done = cfg.sync_screens(device_id, reported)
    if done is None:
        return None
    try:
        parse_layout(cfg.raw())
    except ConfigError:
        cfg.move_clear_of_others(device_id)
        try:
            parse_layout(cfg.raw())
        except ConfigError as exc:
            log.warning("couldn't lay out %s's screens: %s", device_id, exc)
            cfg.reload()
            return None
        done = {**done, "moved": True}
    cfg.save()
    return done


def screens_note(name: str, done: dict[str, Any]) -> str:
    """What :func:`sync_agent_screens` did, in words."""
    parts = []
    if done.get("added"):
        parts.append(f"added {', '.join(done['added'])}")
    if done.get("removed"):
        parts.append(f"took away {', '.join(done['removed'])} (unplugged)")
    if done.get("changed"):
        parts.append(f"resized {', '.join(done['changed'])}")
    text = f"Updated {name}'s screens on the Layout page to match its monitors: " + "; ".join(parts) + "."
    if done.get("moved"):
        text += " They'd have overlapped yours, so it's now to the right of your screens: arrange it there."
    return text


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
