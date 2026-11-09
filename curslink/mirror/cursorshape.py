"""The pointer's real look on this computer, for the mirrors that show it.

The capture never has the pointer in it, so a mirror draws the pointer where
the host says it is. Drawn as a plain arrow it stays an arrow over a window's
edge or a text field. Instead, the capture sends the system's current cursor
(its picture and hot spot) and the mirror draws that.

:func:`current` gives ``{"key", "png", "w", "h", "hx", "hy"}``. ``w``, ``h``
and the hot spot ``hx``, ``hy`` are in Qt's logical pixels (points on macOS).
``png`` is base64. ``key`` tells cursors apart cheaply, and only the key is
compared. Returns None where the cursor can't be read (Linux, or something
failed), and the mirror then draws its arrow.
"""

from __future__ import annotations

import base64
import hashlib
import logging
import sys

log = logging.getLogger(__name__)

_failed = False


def current(dpr: float = 1.0) -> dict | None:
    """This computer's cursor now. ``dpr``: the device pixel ratio of the
    screen it's on (Windows gives the cursor in physical pixels)."""
    global _failed
    if _failed:
        return None
    try:
        if sys.platform == "darwin":
            return _mac()
        if sys.platform == "win32":
            return _windows(dpr)
    except Exception:  # pragma: no cover - platform trouble
        log.warning("mirror: couldn't read the cursor's look; drawing an arrow instead", exc_info=True)
        _failed = True
    return None


def shape(png: bytes, w: float, h: float, hx: float, hy: float) -> dict:
    return {"key": hashlib.blake2b(png, digest_size=10).hexdigest() + f"{hx:.1f},{hy:.1f}",
            "png": base64.b64encode(png).decode("ascii"),
            "w": round(w, 2), "h": round(h, 2), "hx": round(hx, 2), "hy": round(hy, 2)}


_mac_last: tuple[bytes, dict] | None = None


def _mac() -> dict | None:  # pragma: no cover - macOS only
    global _mac_last
    import AppKit

    cursor = AppKit.NSCursor.currentSystemCursor()
    if cursor is None:
        return None
    image = cursor.image()
    if image is None:
        return None
    tiff = bytes(image.TIFFRepresentation())
    if _mac_last is not None and _mac_last[0] == tiff:
        return _mac_last[1]
    reps = AppKit.NSBitmapImageRep.imageRepsWithData_(image.TIFFRepresentation()) or []
    if not reps:
        return None
    best = max(reps, key=lambda r: r.pixelsWide())
    png = best.representationUsingType_properties_(AppKit.NSBitmapImageFileTypePNG, {})
    if png is None:
        return None
    size, hot = image.size(), cursor.hotSpot()
    out = shape(bytes(png), size.width, size.height, hot.x, hot.y)
    _mac_last = (tiff, out)
    return out


_win_last: tuple[int, dict] | None = None


def _windows(dpr: float) -> dict | None:  # pragma: no cover - Windows only
    global _win_last
    import ctypes
    from ctypes import wintypes

    class CURSORINFO(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.DWORD), ("flags", wintypes.DWORD),
                    ("hCursor", wintypes.HANDLE), ("ptScreenPos", wintypes.POINT)]

    class ICONINFO(ctypes.Structure):
        _fields_ = [("fIcon", wintypes.BOOL), ("xHotspot", wintypes.DWORD), ("yHotspot", wintypes.DWORD),
                    ("hbmMask", wintypes.HBITMAP), ("hbmColor", wintypes.HBITMAP)]

    user32, gdi32 = ctypes.windll.user32, ctypes.windll.gdi32
    info = CURSORINFO()
    info.cbSize = ctypes.sizeof(CURSORINFO)
    if not user32.GetCursorInfo(ctypes.byref(info)) or not info.flags & 1 or not info.hCursor:
        return None  # hidden
    handle = int(info.hCursor)
    if _win_last is not None and _win_last[0] == handle:
        return _win_last[1]
    icon = ICONINFO()
    if not user32.GetIconInfo(wintypes.HANDLE(handle), ctypes.byref(icon)):
        return None
    try:
        from PySide6.QtCore import QBuffer, QIODevice
        from PySide6.QtGui import QImage

        image = QImage.fromHICON(handle)
        if image.isNull():
            return None
        buf = QBuffer()
        buf.open(QIODevice.OpenModeFlag.WriteOnly)
        image.save(buf, "PNG")
        png = bytes(buf.data())
    finally:
        for bitmap in (icon.hbmMask, icon.hbmColor):
            if bitmap:
                gdi32.DeleteObject(bitmap)
    k = max(0.5, float(dpr) or 1.0)
    out = shape(png, image.width() / k, image.height() / k, icon.xHotspot / k, icon.yHotspot / k)
    _win_last = (handle, out)
    return out
