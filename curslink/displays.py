"""This computer's monitors, as an agent reports them to its host.

An agent says what screens it has in ``hello.screens`` (and again in a
``screens`` frame when a monitor is plugged in, taken away or rearranged),
so the host's layout can show each of them and the pointer can reach a
monitor attached to the agent, not just its first screen.

:func:`os_screens` gives ``{"id", "name", "x", "y", "width", "height"}`` in
this process's OS coordinates, the same ones the host's ``move`` frames use
(macOS: points from the top left of the main display; Windows: pixels as
this process sees them, as ``SetCursorPos`` takes them: physical, as
the agent is DPI aware since 0.4.75). ``id`` is stable
while the same monitors are attached: "main" for the primary one, then
"screenN" (on Windows, N is its DISPLAYn number). ``name`` repeats it:
hosts before 0.4.69 used that as the screen's id.

This used to come from the optional ``screeninfo`` package, which the
packaged apps never had: every desktop agent reported no screens, and the
host gave it one made-up 1920x1080 screen.
"""

from __future__ import annotations

import ctypes
import logging
import re
import subprocess
import sys

log = logging.getLogger(__name__)

MAX_SCREENS = 16


def os_screens() -> list[dict]:
    try:
        if sys.platform == "darwin":
            found = _mac()
        elif sys.platform == "win32":
            found = _windows()
        else:
            found = _linux()
    except Exception:  # noqa: BLE001 - best effort: the host keeps what it had
        log.debug("couldn't list the monitors", exc_info=True)
        found = []
    return name_screens(found)


def name_screens(found: list[dict]) -> list[dict]:
    """Give each monitor (``x``, ``y``, ``width``, ``height``, optional
    ``primary`` and ``number``) its id: "main" for the primary one (else the
    one at 0, 0, else the first), the rest "screenN" by ``number`` (Windows'
    DISPLAYn) or left to right, top to bottom."""
    screens = [s for s in found
               if all(isinstance(s.get(k), (int, float)) and not isinstance(s.get(k), bool)
                      for k in ("x", "y", "width", "height"))
               and s["width"] > 0 and s["height"] > 0][:MAX_SCREENS]
    if not screens:
        return []
    main = next((s for s in screens if s.get("primary")), None)
    if main is None:
        main = next((s for s in screens if (s["x"], s["y"]) == (0, 0)), screens[0])
    rest = sorted((s for s in screens if s is not main), key=lambda s: (s["x"], s["y"]))
    out = [main] + rest
    used = {"main"}
    named = []
    for i, s in enumerate(out):
        if s is main:
            sid = "main"
        else:
            number = s.get("number")
            sid = f"screen{number}" if isinstance(number, int) and number > 0 else ""
            if not sid or sid in used:
                n = i + 1
                while f"screen{n}" in used:
                    n += 1
                sid = f"screen{n}"
        used.add(sid)
        named.append({"id": sid, "name": sid, "x": int(round(s["x"])), "y": int(round(s["y"])),
                      "width": int(round(s["width"])), "height": int(round(s["height"]))})
    return named


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
    cg.CGMainDisplayID.restype = ctypes.c_uint32
    cg.CGDisplayIsInMirrorSet.argtypes = [ctypes.c_uint32]
    cg.CGDisplayIsInMirrorSet.restype = ctypes.c_uint32
    cg.CGDisplayMirrorsDisplay.argtypes = [ctypes.c_uint32]
    cg.CGDisplayMirrorsDisplay.restype = ctypes.c_uint32
    ids = (ctypes.c_uint32 * MAX_SCREENS)()
    count = ctypes.c_uint32(0)
    if cg.CGGetActiveDisplayList(MAX_SCREENS, ids, ctypes.byref(count)) != 0:
        return []
    main = cg.CGMainDisplayID()
    out = []
    for i in range(count.value):
        did = ids[i]
        # A display mirroring another (macOS's own mirroring) is the same place twice.
        if cg.CGDisplayIsInMirrorSet(did) and cg.CGDisplayMirrorsDisplay(did) != 0:
            continue
        b = cg.CGDisplayBounds(did)
        out.append({"x": b.origin.x, "y": b.origin.y, "width": b.size.width, "height": b.size.height,
                    "primary": did == main})
    return out


def _windows() -> list[dict]:  # pragma: no cover - Windows only
    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32")

    class MONITORINFOEXW(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT), ("rcWork", wintypes.RECT),
                    ("dwFlags", wintypes.DWORD), ("szDevice", wintypes.WCHAR * 32)]

    MONITORINFOF_PRIMARY = 1
    user32.GetMonitorInfoW.argtypes = [wintypes.HMONITOR, ctypes.c_void_p]
    user32.GetMonitorInfoW.restype = wintypes.BOOL
    proc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HMONITOR, wintypes.HDC, ctypes.POINTER(wintypes.RECT),
                              wintypes.LPARAM)
    out: list[dict] = []

    def each(hmon, _hdc, _rect, _data):
        info = MONITORINFOEXW()
        info.cbSize = ctypes.sizeof(MONITORINFOEXW)
        if not user32.GetMonitorInfoW(hmon, ctypes.byref(info)):
            return True
        r = info.rcMonitor
        m = re.search(r"DISPLAY(\d+)$", info.szDevice or "")
        out.append({"x": r.left, "y": r.top, "width": r.right - r.left, "height": r.bottom - r.top,
                    "primary": bool(info.dwFlags & MONITORINFOF_PRIMARY),
                    "number": int(m.group(1)) if m else None})
        return True

    user32.EnumDisplayMonitors(None, None, proc(each), 0)
    return out


XRANDR_LINE = re.compile(r"^(\S+) connected (primary )?(\d+)x(\d+)\+(-?\d+)\+(-?\d+)")


def parse_xrandr(text: str) -> list[dict]:
    out = []
    for line in text.splitlines():
        m = XRANDR_LINE.match(line)
        if m:
            out.append({"x": int(m.group(5)), "y": int(m.group(6)),
                        "width": int(m.group(3)), "height": int(m.group(4)), "primary": bool(m.group(2))})
    return out


def _linux() -> list[dict]:
    try:
        from screeninfo import get_monitors  # optional

        found = [{"x": m.x, "y": m.y, "width": m.width, "height": m.height,
                  "primary": bool(getattr(m, "is_primary", False))} for m in get_monitors()]
        if found:
            return found
    except Exception:  # noqa: BLE001 - not installed, or no display
        pass
    try:
        text = subprocess.run(["xrandr", "--query"], capture_output=True, text=True, timeout=3).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    return parse_xrandr(text)
