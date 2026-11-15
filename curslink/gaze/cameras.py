"""The cameras on this computer, by name, in the order OpenCV numbers them.

OpenCV opens cameras by index; people know them by name. Each platform's
list comes from the same system API OpenCV's default backend enumerates,
so position ``i`` here is ``cv2.VideoCapture(i)``:

* Linux: ``/sys/class/video4linux/videoN`` (N is the V4L2 index).
* macOS: AVFoundation (``pyobjc-framework-AVFoundation``), the same
  device list OpenCV's AVFoundation backend walks.
* Windows: Media Foundation's video capture sources, which the MSMF
  backend walks (``MFEnumDeviceSources``, called directly). Until 0.4.114
  the names came from the PnP device list instead, whose order isn't
  OpenCV's: see ``renamed``.

Anything that fails returns an empty list, and the app falls back to
"Camera 1", "Camera 2", and so on.
"""

from __future__ import annotations

import json
import logging
import re
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


def infrared(name: str) -> bool:
    """Is this the infrared camera beside a webcam (the one Windows Hello
    uses)? Its picture is grey and lit by its own lamp: no good for gaze."""
    return bool(re.search(r"(?<![a-z])(ir|infrared)(?![a-z])", name.lower()))


def colour_first(cameras: list["Camera"], index: int) -> "Camera | None":
    """The camera to start with when nobody has picked one: number
    ``index`` unless that's infrared and another isn't."""
    at = next((c for c in cameras if c.index == index), None)
    if at is not None and not infrared(at.name):
        return at
    return next((c for c in cameras if not infrared(c.name)), at or (cameras[0] if cameras else None))


def _kind(name: str, builtin_hint: bool = False, usb_hint: bool = False) -> str:
    low = name.lower()
    if infrared(name):
        return "Infrared"
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


def renamed(index: int, cameras: list[Camera]) -> tuple[int, str]:
    """A camera saved before the list was in OpenCV's order (Windows, up to
    0.4.114): its number is what was really opened, its name may be
    another camera's. So keep the number and take the name that goes with
    it, unless that's the infrared camera and there's a colour one."""
    pick = colour_first(cameras, index)
    return (pick.index, pick.name) if pick is not None else (index, "")


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
    """The cameras as OpenCV numbers them: it opens camera N as the Nth
    device Media Foundation lists, so that's the list asked for. (The
    PnP list below it is in an order of its own: on Kyle's PC it put the
    infrared camera's name on the colour camera's number, so picking one
    opened the other.)"""
    try:
        names = media_foundation_names()
    except Exception:
        log.warning("couldn't ask Media Foundation for the cameras; going by the device list", exc_info=True)
        names = None
    if names is not None:
        return [Camera(i, name, _kind(name, usb_hint="usb" in name.lower())) for i, name in enumerate(names)]
    return _windows_pnp()


def media_foundation_names() -> list[str]:
    """The video capture devices' names, in Media Foundation's order
    (``MFEnumDeviceSources``), which is the order of OpenCV's indexes."""
    import ctypes
    from ctypes import wintypes

    class GUID(ctypes.Structure):
        _fields_ = [("a", wintypes.DWORD), ("b", wintypes.WORD), ("c", wintypes.WORD), ("d", ctypes.c_ubyte * 8)]

        def __init__(self, text: str):
            super().__init__()
            import uuid

            raw = uuid.UUID(text).bytes_le
            ctypes.memmove(ctypes.byref(self), raw, 16)

    source_type = GUID("c60ac5fe-252a-478f-a0ef-bc8fa5f7cad3")  # MF_DEVSOURCE_ATTRIBUTE_SOURCE_TYPE
    video = GUID("8ac3587a-4ae7-42d8-99e0-0a6013eef90f")  # ..._SOURCE_TYPE_VIDCAP_GUID
    friendly = GUID("60d0e559-52f8-4fa2-bbce-acdb34a8ec01")  # MF_DEVSOURCE_ATTRIBUTE_FRIENDLY_NAME
    mfplat = ctypes.WinDLL("mfplat")
    mf = ctypes.WinDLL("mf")
    ole32 = ctypes.WinDLL("ole32")
    ole32.CoTaskMemFree.argtypes = [ctypes.c_void_p]
    ole32.CoTaskMemFree.restype = None
    hresult = ctypes.c_long

    def method(obj: ctypes.c_void_p, index: int, *argtypes):
        """Method ``index`` of a COM object's vtable."""
        vtable = ctypes.cast(obj, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
        return ctypes.WINFUNCTYPE(hresult, ctypes.c_void_p, *argtypes)(vtable[index])

    def release(obj: ctypes.c_void_p) -> None:
        ctypes.WINFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p)(
            ctypes.cast(obj, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents[2])(obj)

    def name_of(obj: ctypes.c_void_p) -> str:
        text = ctypes.c_void_p()
        length = wintypes.UINT(0)
        # IMFAttributes::GetAllocatedString
        got = method(obj, 13, ctypes.POINTER(GUID), ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(wintypes.UINT))(
            obj, ctypes.byref(friendly), ctypes.byref(text), ctypes.byref(length))
        if got < 0 or not text.value:
            return ""
        try:
            return ctypes.wstring_at(text.value, length.value)
        finally:
            ole32.CoTaskMemFree(text)

    mfplat.MFStartup.argtypes = [wintypes.ULONG, wintypes.DWORD]
    mfplat.MFStartup.restype = hresult
    mfplat.MFShutdown.restype = hresult
    mfplat.MFCreateAttributes.argtypes = [ctypes.POINTER(ctypes.c_void_p), wintypes.UINT]
    mfplat.MFCreateAttributes.restype = hresult
    mf.MFEnumDeviceSources.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p)),
                                       ctypes.POINTER(wintypes.UINT)]
    mf.MFEnumDeviceSources.restype = hresult
    if mfplat.MFStartup(0x00020070, 1) < 0:  # MF_VERSION, MFSTARTUP_LITE
        raise OSError("MFStartup failed")
    try:
        attributes = ctypes.c_void_p()
        if mfplat.MFCreateAttributes(ctypes.byref(attributes), 1) < 0 or not attributes.value:
            raise OSError("MFCreateAttributes failed")
        try:
            # IMFAttributes::SetGUID
            if method(attributes, 24, ctypes.POINTER(GUID), ctypes.POINTER(GUID))(
                    attributes, ctypes.byref(source_type), ctypes.byref(video)) < 0:
                raise OSError("SetGUID failed")
            devices = ctypes.POINTER(ctypes.c_void_p)()
            count = wintypes.UINT(0)
            if mf.MFEnumDeviceSources(attributes, ctypes.byref(devices), ctypes.byref(count)) < 0:
                raise OSError("MFEnumDeviceSources failed")
            names = []
            try:
                for i in range(count.value):
                    device = ctypes.c_void_p(devices[i])
                    try:
                        names.append(name_of(device) or f"Camera {i + 1}")
                    finally:
                        release(device)
            finally:
                if devices:
                    ole32.CoTaskMemFree(ctypes.cast(devices, ctypes.c_void_p))
            return names
        finally:
            release(attributes)
    finally:
        mfplat.MFShutdown()


def _windows_pnp() -> list[Camera]:
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
