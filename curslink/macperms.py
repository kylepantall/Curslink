"""macOS privacy permissions Curslink needs, checked and requested.

* Accessibility: to move the pointer and type (agents) and to watch the
  keyboard and mouse (host).
* Input Monitoring: to watch the keyboard and mouse (host).
* Camera: for gaze switching (host). Checked and asked through AVFoundation.

Both belong to the Curslink executable, which the app and its linking
service share, so asking from the app covers the service too. The service
reads them when it starts, so it restarts once they're granted.

Everything is ctypes against system frameworks, so it needs nothing extra,
and anywhere but macOS every check says "granted".
"""

from __future__ import annotations

import ctypes
import logging
import subprocess
import sys
from dataclasses import dataclass

log = logging.getLogger(__name__)

MAC = sys.platform == "darwin"

ACCESSIBILITY = "accessibility"
INPUT_MONITORING = "input-monitoring"
CAMERA = "camera"

_SETTINGS_URL = {
    ACCESSIBILITY: "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility",
    INPUT_MONITORING: "x-apple.systempreferences:com.apple.preference.security?Privacy_ListenEvent",
    CAMERA: "x-apple.systempreferences:com.apple.preference.security?Privacy_Camera",
}

_kIOHIDRequestTypeListenEvent = 1
_kIOHIDAccessTypeGranted = 0


@dataclass(frozen=True)
class Permission:
    key: str
    title: str
    why: str


PERMISSIONS = {
    ACCESSIBILITY: Permission(
        ACCESSIBILITY, "Accessibility",
        "Lets Curslink move the pointer and type here, and pass your keyboard and mouse on."),
    INPUT_MONITORING: Permission(
        INPUT_MONITORING, "Input Monitoring",
        "Lets Curslink notice the keyboard and mouse, so they can follow you to your other devices."),
    CAMERA: Permission(
        CAMERA, "Camera",
        "Lets Curslink see which screen you're looking at. The picture never leaves this computer."),
}


def needed(role: str | None) -> list[str]:
    """What this computer's role needs on macOS (nothing elsewhere)."""
    if not MAC:
        return []
    # The host runs gaze switching, so it needs the camera too.
    return [ACCESSIBILITY, INPUT_MONITORING, CAMERA] if role == "host" else [ACCESSIBILITY]


def _lib(path: str):
    return ctypes.cdll.LoadLibrary(path)


def _ax():
    return _lib("/System/Library/Frameworks/ApplicationServices.framework/ApplicationServices")


def _iokit():
    return _lib("/System/Library/Frameworks/IOKit.framework/IOKit")


def granted(key: str) -> bool:
    if not MAC:
        return True
    try:
        if key == ACCESSIBILITY:
            fn = _ax().AXIsProcessTrusted
            fn.restype = ctypes.c_bool
            return bool(fn())
        if key == INPUT_MONITORING:
            fn = _iokit().IOHIDCheckAccess
            fn.restype = ctypes.c_uint32
            fn.argtypes = [ctypes.c_uint32]
            return fn(_kIOHIDRequestTypeListenEvent) == _kIOHIDAccessTypeGranted
        if key == CAMERA:
            return _camera_status() == _AV_AUTHORIZED
    except Exception:
        log.warning("couldn't check the %s permission", key, exc_info=True)
        return True  # can't tell: don't nag
    return True


def missing(role: str | None) -> list[str]:
    return [k for k in needed(role) if not granted(k)]


def check_command() -> list[str]:
    """Runs the checks in a new process (``curslink _macperms``)."""
    if getattr(sys, "frozen", False):
        return [sys.executable, "_macperms"]
    return [sys.executable, "-m", "curslink", "_macperms"]


def missing_fresh(role: str | None, timeout: float = 20.0) -> list[str]:
    """Like :func:`missing`, but asked of a new Curslink process, the way the
    linking service will see it. This process's own answer can stay "no"
    after you allow it in System Settings, until Curslink restarts."""
    want = needed(role)
    if not want:
        return []
    import json

    try:
        out = subprocess.run(check_command(), capture_output=True, text=True, timeout=timeout, check=True).stdout
        answers = json.loads(out.strip().splitlines()[-1])
        return [k for k in want if not answers.get(k, False)]
    except Exception:
        log.warning("couldn't check permissions in a new process; asking this one", exc_info=True)
        return missing(role)


def request(key: str) -> None:
    """Ask macOS for ``key``: this adds Curslink to the list in System
    Settings (with the system prompt the first time), then opens that list."""
    if not MAC:
        return
    try:
        if key == ACCESSIBILITY:
            _prompt_accessibility()
        elif key == INPUT_MONITORING:
            fn = _iokit().IOHIDRequestAccess
            fn.restype = ctypes.c_bool
            fn.argtypes = [ctypes.c_uint32]
            fn(_kIOHIDRequestTypeListenEvent)
        elif key == CAMERA and _camera_status() == _AV_NOT_DETERMINED:
            # First time: macOS shows its own prompt, no need for Settings.
            _request_camera()
            return
    except Exception:
        log.warning("couldn't request the %s permission", key, exc_info=True)
    open_settings(key)


_AV_NOT_DETERMINED, _AV_AUTHORIZED = 0, 3


def _camera_status() -> int:
    import AVFoundation

    return int(AVFoundation.AVCaptureDevice.authorizationStatusForMediaType_(AVFoundation.AVMediaTypeVideo))


def _request_camera() -> None:
    import AVFoundation

    def answered(ok):
        log.info("camera access %s", "allowed" if ok else "refused")

    AVFoundation.AVCaptureDevice.requestAccessForMediaType_completionHandler_(
        AVFoundation.AVMediaTypeVideo, answered)


def open_settings(key: str) -> None:
    if MAC:
        log.info("opening System Settings for %s", key)
        subprocess.Popen(["open", _SETTINGS_URL[key]])  # noqa: S603, S607


def _prompt_accessibility() -> None:
    """AXIsProcessTrustedWithOptions({kAXTrustedCheckOptionPrompt: true})."""
    cf = _lib("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
    ax = _ax()
    key = ctypes.c_void_p.in_dll(ax, "kAXTrustedCheckOptionPrompt")
    true = ctypes.c_void_p.in_dll(cf, "kCFBooleanTrue")
    keys = (ctypes.c_void_p * 1)(key.value)
    values = (ctypes.c_void_p * 1)(true.value)
    create = cf.CFDictionaryCreate
    create.restype = ctypes.c_void_p
    create.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_long,
                       ctypes.c_void_p, ctypes.c_void_p]
    key_cb = ctypes.c_void_p.in_dll(cf, "kCFTypeDictionaryKeyCallBacks")
    value_cb = ctypes.c_void_p.in_dll(cf, "kCFTypeDictionaryValueCallBacks")
    options = create(None, keys, values, 1, ctypes.addressof(key_cb), ctypes.addressof(value_cb))
    trusted = ax.AXIsProcessTrustedWithOptions
    trusted.restype = ctypes.c_bool
    trusted.argtypes = [ctypes.c_void_p]
    try:
        trusted(options)
    finally:
        release = cf.CFRelease
        release.argtypes = [ctypes.c_void_p]
        release(options)
