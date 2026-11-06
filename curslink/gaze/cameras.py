"""The cameras on this computer, by name, in the order OpenCV numbers them.

OpenCV opens cameras by index; people know them by name. Each platform's
list comes from the same system API OpenCV's default backend enumerates,
so position ``i`` here is ``cv2.VideoCapture(i)``:

* Linux: ``/sys/class/video4linux/videoN`` (N is the V4L2 index).
* macOS: AVFoundation (``pyobjc-framework-AVFoundation``), the same
  device list OpenCV's AVFoundation backend walks.
* Windows: Media Foundation's video capture sources, which the MSMF
  backend walks; read through PowerShell so nothing extra is needed.

Anything that fails returns an empty list, and the app falls back to
"Camera 1", "Camera 2", and so on.
"""

from __future__ import annotations

import json
import logging
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Camera:
    index: int
    name: str
    kind: str = ""  # "Built in", "USB", "Continuity", "Virtual", or ""

    @property
    def label(self) -> str:
        return f"{self.name} ({self.kind})" if self.kind else self.name


_VIRTUAL = ("obs", "virtual", "snap camera", "manycam", "xsplit", "droidcam", "epoccam", "nvidia broadcast")


def _kind(name: str, builtin_hint: bool = False, usb_hint: bool = False) -> str:
    low = name.lower()
    if any(v in low for v in _VIRTUAL):
        return "Virtual"
    if "iphone" in low or "continuity" in low:
        return "Continuity"
    if builtin_hint or any(w in low for w in ("facetime", "integrated", "built-in", "builtin", "internal")):
        return "Built in"
    if usb_hint or "usb" in low:
        return "USB"
    return ""


def list_cameras() -> list[Camera]:
    try:
        if sys.platform.startswith("linux"):
            cams = _linux()
        elif sys.platform == "darwin":
            cams = _macos()
        elif sys.platform == "win32":
            cams = _windows()
        else:
            cams = []
    except Exception:  # never let a camera list break a settings page
        log.warning("couldn't list cameras", exc_info=True)
        cams = []
    log.info("cameras: %s", ", ".join(f"{c.index}={c.label}" for c in cams) or "none found")
    return cams


def find(cameras: list[Camera], name: str) -> Camera | None:
    """The camera called ``name`` (exact, then case-insensitive)."""
    for c in cameras:
        if c.name == name:
            return c
    low = name.casefold()
    return next((c for c in cameras if c.name.casefold() == low), None)


def resolve(index: int, name: str | None) -> int:
    """The index to open: the named camera's current index if it moved
    (another camera was plugged in), else ``index``."""
    if not name:
        return index
    cams = list_cameras()
    found = find(cams, name)
    if found is None:
        log.warning("camera %r isn't connected; trying camera %d", name, index)
        return index
    if found.index != index:
        log.info("camera %r is now index %d (was %d)", name, found.index, index)
    return found.index


# ------------------------------------------------------------------ platforms

def _linux(root: Path = Path("/sys/class/video4linux")) -> list[Camera]:
    out = []
    if not root.exists():
        return out
    for dev in sorted(root.glob("video*"), key=lambda p: int(p.name[5:]) if p.name[5:].isdigit() else 999):
        num = dev.name[5:]
        if not num.isdigit():
            continue
        # Each camera usually has a second node for metadata; only index 0
        # nodes capture video.
        try:
            if (dev / "index").read_text().strip() not in ("0", ""):
                continue
            name = (dev / "name").read_text().strip()
        except OSError:
            continue
        usb = "usb" in str((dev / "device").resolve()) if (dev / "device").exists() else False
        out.append(Camera(int(num), name, _kind(name, usb_hint=usb)))
    return out


def _macos() -> list[Camera]:
    try:
        import AVFoundation  # pyobjc-framework-AVFoundation
    except ImportError:
        return _macos_profiler()
    devices = AVFoundation.AVCaptureDevice.devicesWithMediaType_(AVFoundation.AVMediaTypeVideo)
    out = []
    for i, d in enumerate(devices or []):
        name = str(d.localizedName())
        builtin = False
        try:
            builtin = str(d.deviceType()) == str(AVFoundation.AVCaptureDeviceTypeBuiltInWideAngleCamera)
        except AttributeError:
            pass
        usb = False
        try:
            usb = int(d.transportType()) == 0x75736220  # 'usb '
        except (AttributeError, TypeError, ValueError):
            pass
        out.append(Camera(i, name, _kind(name, builtin_hint=builtin, usb_hint=usb)))
    return out


def _macos_profiler() -> list[Camera]:
    """Without pyobjc: names from system_profiler (order is a best guess)."""
    res = subprocess.run(["system_profiler", "-json", "SPCameraDataType"], capture_output=True, text=True,
                         timeout=10)
    data = json.loads(res.stdout or "{}").get("SPCameraDataType", [])
    return [Camera(i, str(d.get("_name", f"Camera {i + 1}")), _kind(str(d.get("_name", ""))))
            for i, d in enumerate(data)]


_WINDOWS_PS = r"""
$ErrorActionPreference = 'Stop'
Get-CimInstance Win32_PnPEntity |
  Where-Object { $_.PNPClass -in @('Camera','Image') -and $_.Status -eq 'OK' } |
  Select-Object Name, PNPDeviceID |
  ConvertTo-Json -Compress
"""


def _windows() -> list[Camera]:
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    res = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", _WINDOWS_PS],
                         capture_output=True, text=True, timeout=15, creationflags=flags)
    if res.returncode != 0 or not res.stdout.strip():
        return []
    data = json.loads(res.stdout)
    if isinstance(data, dict):
        data = [data]
    out = []
    for i, d in enumerate(data):
        name = str(d.get("Name") or f"Camera {i + 1}")
        pnp = str(d.get("PNPDeviceID") or "")
        out.append(Camera(i, name, _kind(name, usb_hint=pnp.upper().startswith("USB"))))
    return out
