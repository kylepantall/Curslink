"""macOS privacy permissions Curslink needs, checked and requested.

* Accessibility: to move the pointer and type (agents) and to watch the
  keyboard and mouse (host).
* Input Monitoring: to watch the keyboard and mouse (host).
* Camera: for gaze switching (host). Checked and asked through AVFoundation.
* Screen Recording: for mirror mode, to show the part of this Mac's screen
  another screen covers (host, when mirror mode is on).
* Local Network: to find and reach the other computers (both). macOS has no
  API to ask, so Curslink sends one mDNS packet: macOS refuses it with "No
  route to host" while access is off, and shows its prompt the first time.

All belong to Curslink.app (the packaged executable lives inside it), which
the app and its linking service share, so asking from the app covers the
service too. macOS keeps them only while the app's signature stays the same:
after an update to a differently signed build, Settings can still show
Curslink switched on while the check says no. The service
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
LOCAL_NETWORK = "local-network"
SCREEN_RECORDING = "screen-recording"

_SETTINGS_URL = {
    ACCESSIBILITY: "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility",
    INPUT_MONITORING: "x-apple.systempreferences:com.apple.preference.security?Privacy_ListenEvent",
    CAMERA: "x-apple.systempreferences:com.apple.preference.security?Privacy_Camera",
    LOCAL_NETWORK: "x-apple.systempreferences:com.apple.preference.security?Privacy_LocalNetwork",
    SCREEN_RECORDING: "x-apple.systempreferences:com.apple.preference.security?Privacy_ScreenCapture",
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
    LOCAL_NETWORK: Permission(
        LOCAL_NETWORK, "Local Network",
        "Lets Curslink find your other computers on this network and pair with them."),
    SCREEN_RECORDING: Permission(
        SCREEN_RECORDING, "Screen Recording",
        "For mirror mode: lets Curslink show the part of this screen that another screen covers. "
        "The picture only goes to your own devices."),
}


def needed(role: str | None, mirror: bool = False) -> list[str]:
    """What this computer's role needs on macOS (nothing elsewhere).
    ``mirror``: mirror mode is on, so the screen is captured too."""
    if not MAC:
        return []
    # The host runs gaze switching, so it needs the camera too.
    if role == "host":
        want = [ACCESSIBILITY, INPUT_MONITORING, CAMERA, LOCAL_NETWORK]
    else:
        want = [ACCESSIBILITY, LOCAL_NETWORK]
    return want + [SCREEN_RECORDING] if mirror else want


def mirror_on(raw: dict) -> bool:
    """Is mirror mode on in this (host) config's raw settings?"""
    layout = raw.get("layout") if isinstance(raw, dict) else None
    return isinstance(layout, dict) and layout.get("mirror") is True and layout.get("allow_overlap") is True


def _lib(path: str):
    return ctypes.cdll.LoadLibrary(path)


def _ax():
    return _lib("/System/Library/Frameworks/ApplicationServices.framework/ApplicationServices")


def _cg():
    return _lib("/System/Library/Frameworks/CoreGraphics.framework/CoreGraphics")


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
        if key == LOCAL_NETWORK:
            return _local_network_ok()
        if key == SCREEN_RECORDING:
            fn = _cg().CGPreflightScreenCaptureAccess
            fn.restype = ctypes.c_bool
            return bool(fn())
    except Exception:
        log.warning("couldn't check the %s permission", key, exc_info=True)
        return True  # can't tell: don't nag
    return True


def missing(role: str | None, mirror: bool = False) -> list[str]:
    return [k for k in needed(role, mirror) if not granted(k)]


def check_command() -> list[str]:
    """Runs the checks in a new process (``curslink _macperms``)."""
    if getattr(sys, "frozen", False):
        return [sys.executable, "_macperms"]
    return [sys.executable, "-m", "curslink", "_macperms"]


def missing_fresh(role: str | None, timeout: float = 20.0, mirror: bool = False) -> list[str]:
    """Like :func:`missing`, but asked of a new Curslink process, the way the
    linking service will see it. This process's own answer can stay "no"
    after you allow it in System Settings, until Curslink restarts."""
    want = needed(role, mirror)
    if not want:
        return []
    import json

    try:
        out = subprocess.run(check_command(), capture_output=True, text=True, timeout=timeout, check=True).stdout
        answers = json.loads(out.strip().splitlines()[-1])
        return [k for k in want if not answers.get(k, False)]
    except Exception:
        log.warning("couldn't check permissions in a new process; asking this one", exc_info=True)
        return missing(role, mirror)


def request(key: str, open_settings_too: bool = True) -> None:
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
        elif key == LOCAL_NETWORK:
            if _local_network_ok():  # the first try shows macOS's prompt
                return
        elif key == SCREEN_RECORDING:
            # The system prompt the first time (it offers System Settings).
            fn = _cg().CGRequestScreenCaptureAccess
            fn.restype = ctypes.c_bool
            if fn():
                return
        elif key == CAMERA and _camera_status() == _AV_NOT_DETERMINED:
            # First time: macOS shows its own prompt, no need for Settings.
            _request_camera()
            return
    except Exception:
        log.warning("couldn't request the %s permission", key, exc_info=True)
    if open_settings_too:
        open_settings(key)


_AV_NOT_DETERMINED, _AV_AUTHORIZED = 0, 3


def _camera_status() -> int:
    import AVFoundation

    return int(AVFoundation.AVCaptureDevice.authorizationStatusForMediaType_(AVFoundation.AVMediaTypeVideo))


def _request_camera():
    import threading

    import AVFoundation

    done = threading.Event()

    def answered(ok):
        log.info("camera access %s", "allowed" if ok else "refused")
        done.set()

    AVFoundation.AVCaptureDevice.requestAccessForMediaType_completionHandler_(
        AVFoundation.AVMediaTypeVideo, answered)
    return done


def ensure_camera(timeout: float = 60.0) -> bool:
    """Before opening the camera: if macOS hasn't asked yet, ask and wait
    for the answer. (OpenCV fails at once while the prompt is still up.)"""
    if not MAC:
        return True
    try:
        status = _camera_status()
        if status == _AV_NOT_DETERMINED:
            _request_camera().wait(timeout)
            status = _camera_status()
        return status == _AV_AUTHORIZED
    except Exception:
        log.warning("couldn't check camera access", exc_info=True)
        return True


# An mDNS query with no questions: harmless, and enough for macOS to decide.
_MDNS_PROBE = bytes(12)


def _local_network_ok() -> bool:
    import errno
    import socket

    from curslink.pairing.discovery import local_addresses

    addresses = local_addresses()
    if not addresses:
        return True  # no network at all: not a permission problem
    blocked = 0
    for addr in addresses:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            # Bound to the address, like zeroconf's own sockets: unbound, the
            # send can succeed while macOS quietly drops the packet.
            sock.bind((addr, 0))
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(addr))
            sock.sendto(_MDNS_PROBE, ("224.0.0.251", 5353))
            return True
        except OSError as exc:
            if exc.errno in (errno.EHOSTUNREACH, errno.EPERM, errno.EACCES):
                blocked += 1
            # anything else is network trouble on that address, not the permission
        finally:
            sock.close()
    if not blocked:
        return True
    log.info("Local Network access looks off: mDNS refused on %d of %d address(es)", blocked, len(addresses))
    return False


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
