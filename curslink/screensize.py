"""How big each monitor really is, in millimetres, from the OS (the
monitor's EDID), for mirror mode.

Screens are laid out in their own pixels (curslink.layout), which aren't the
same size on a 34" ultrawide and a 15" laptop. Knowing each one's size in mm
gives how many of one's pixels make one of the other's, so a mirror comes
out the right size before any lining up (see Layout.covered_parts).

:func:`physical_sizes` gives this computer's monitors as ``{"x", "y",
"width", "height", "width_mm", "height_mm"}`` in its OS coordinates (macOS:
points; Windows: physical pixels), or ``[]`` where it can't tell.
"""

from __future__ import annotations

import ctypes
import logging
import sys

log = logging.getLogger(__name__)

# Sizes a real monitor can have: 0.5 to 40 OS pixels per mm, and an aspect
# ratio near its pixels' (EDIDs of TVs and projectors often make one up).
MIN_PPMM, MAX_PPMM = 0.5, 40.0
ASPECT_SLACK = 0.15


def plausible(width: float, height: float, width_mm: float, height_mm: float) -> bool:
    if min(width, height, width_mm, height_mm) <= 0:
        return False
    px, py = width / width_mm, height / height_mm
    if not (MIN_PPMM <= px <= MAX_PPMM and MIN_PPMM <= py <= MAX_PPMM):
        return False
    return abs(px / py - 1) <= ASPECT_SLACK


def physical_sizes() -> list[dict]:
    try:
        if sys.platform == "darwin":
            found = _mac()
        elif sys.platform == "win32":
            found = _windows()
        else:
            found = []
    except Exception:  # noqa: BLE001 - best effort: mirrors fall back to the layout's sizes
        log.debug("couldn't read the monitors' sizes", exc_info=True)
        found = []
    return [s for s in found if plausible(s["width"], s["height"], s["width_mm"], s["height_mm"])]


def match(sizes: list[dict], os_x: int, os_y: int, width: int, height: int) -> tuple[float, float] | None:
    """The (width_mm, height_mm) of the monitor in ``sizes`` that is this
    screen: the one at the same place and size, else the only one that size."""
    def mm(s: dict) -> tuple[float, float]:
        return (float(s["width_mm"]), float(s["height_mm"]))

    same_size = []
    for s in sizes:
        try:
            if (round(s["width"]), round(s["height"])) != (width, height):
                continue
            if (round(s.get("x", 1 << 30)), round(s.get("y", 1 << 30))) == (os_x, os_y):
                return mm(s)
            same_size.append(s)
        except (KeyError, TypeError, ValueError):
            continue
    if len(same_size) == 1:
        return mm(same_size[0])
    # The only monitor, in other pixels (Windows scaling: the layout may have
    # it in logical pixels and the OS here in physical ones): same shape will do.
    if len(sizes) == 1 and height > 0:
        try:
            s = sizes[0]
            if abs((s["width"] / s["height"]) / (width / height) - 1) < 0.02:
                return mm(s)
        except (KeyError, TypeError, ValueError, ZeroDivisionError):
            pass
    return None


def _mac() -> list[dict]:  # pragma: no cover - macOS only
    from curslink.macperms import _cg

    cg = _cg()

    class CGSize(ctypes.Structure):
        _fields_ = [("width", ctypes.c_double), ("height", ctypes.c_double)]

    class CGPoint(ctypes.Structure):
        _fields_ = [("x", ctypes.c_double), ("y", ctypes.c_double)]

    class CGRect(ctypes.Structure):
        _fields_ = [("origin", CGPoint), ("size", CGSize)]

    cg.CGGetActiveDisplayList.argtypes = [ctypes.c_uint32, ctypes.POINTER(ctypes.c_uint32),
                                          ctypes.POINTER(ctypes.c_uint32)]
    cg.CGGetActiveDisplayList.restype = ctypes.c_int32
    cg.CGDisplayBounds.argtypes = [ctypes.c_uint32]
    cg.CGDisplayBounds.restype = CGRect
    cg.CGDisplayScreenSize.argtypes = [ctypes.c_uint32]
    cg.CGDisplayScreenSize.restype = CGSize
    ids = (ctypes.c_uint32 * 16)()
    count = ctypes.c_uint32(0)
    if cg.CGGetActiveDisplayList(16, ids, ctypes.byref(count)) != 0:
        return []
    out = []
    for i in range(count.value):
        b = cg.CGDisplayBounds(ids[i])
        mm = cg.CGDisplayScreenSize(ids[i])
        out.append({"x": round(b.origin.x), "y": round(b.origin.y),
                    "width": round(b.size.width), "height": round(b.size.height),
                    "width_mm": float(mm.width), "height_mm": float(mm.height)})
    return out


def _windows() -> list[dict]:  # pragma: no cover - Windows only
    from ctypes import wintypes

    user32, gdi32 = ctypes.windll.user32, ctypes.windll.gdi32

    class MONITORINFOEXW(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT), ("rcWork", wintypes.RECT),
                    ("dwFlags", wintypes.DWORD), ("szDevice", wintypes.WCHAR * 32)]

    HORZSIZE, VERTSIZE, DESKTOPVERTRES, DESKTOPHORZRES = 4, 6, 117, 118
    gdi32.CreateDCW.restype = wintypes.HDC
    gdi32.CreateDCW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.LPCWSTR, ctypes.c_void_p]
    gdi32.GetDeviceCaps.argtypes = [wintypes.HDC, ctypes.c_int]
    gdi32.DeleteDC.argtypes = [wintypes.HDC]
    out: list[dict] = []
    proc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HMONITOR, wintypes.HDC, ctypes.POINTER(wintypes.RECT),
                              wintypes.LPARAM)

    def each(hmon, _hdc, _rect, _data):
        info = MONITORINFOEXW()
        info.cbSize = ctypes.sizeof(MONITORINFOEXW)
        if not user32.GetMonitorInfoW(hmon, ctypes.byref(info)):
            return True
        dc = gdi32.CreateDCW("DISPLAY", info.szDevice, None, None)
        if not dc:
            return True
        try:
            w, h = gdi32.GetDeviceCaps(dc, DESKTOPHORZRES), gdi32.GetDeviceCaps(dc, DESKTOPVERTRES)
            wmm, hmm = gdi32.GetDeviceCaps(dc, HORZSIZE), gdi32.GetDeviceCaps(dc, VERTSIZE)
        finally:
            gdi32.DeleteDC(dc)
        r = info.rcMonitor
        # rcMonitor is in physical pixels only when this process is DPI
        # aware: scale the origin by how far its size is from the real one.
        k = w / max(1, r.right - r.left)
        out.append({"x": round(r.left * k), "y": round(r.top * k), "width": w, "height": h,
                    "width_mm": float(wmm), "height_mm": float(hmm)})
        return True

    user32.EnumDisplayMonitors(None, None, proc(each), 0)
    return out
