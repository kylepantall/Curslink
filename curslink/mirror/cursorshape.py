"""The pointer's real look on this computer, for the mirrors that show it.

The capture never has the pointer in it, so a mirror draws the pointer where
the host says it is. Drawn as a plain arrow it stays an arrow over a window's
edge or a text field. Instead, the capture sends the system's current cursor
(its picture and hot spot) and the mirror draws that.

:func:`current` gives ``{"key", "png", "w", "h", "hx", "hy"}``. ``w``, ``h``
and the hot spot ``hx``, ``hy`` are in Qt's logical pixels (points on macOS).
``png`` is base64. ``key`` tells cursors apart cheaply, and only the key is
compared. Returns None only while the cursor is hidden. Where its look
can't be read (Linux, or something failed) it gives a plain arrow
(:func:`arrow`), so a capture still shows where the pointer is.
"""

from __future__ import annotations

import base64
import hashlib
import logging
import sys
import time

log = logging.getLogger(__name__)

RETRY_S = 30.0  # after the look couldn't be read, try again this long after
_failed = 0.0  # when it last couldn't be read


def current(dpr: float = 1.0) -> dict | None:
    """This computer's cursor now. ``dpr``: the device pixel ratio of the
    screen it's on (Windows gives the cursor in physical pixels)."""
    global _failed
    if _failed and time.monotonic() - _failed < RETRY_S:
        return arrow()
    try:
        if sys.platform == "darwin":
            return _mac()
        if sys.platform == "win32":
            return _windows(dpr)
    except Exception:  # pragma: no cover - platform trouble
        if not _failed:
            log.warning("mirror: couldn't read the cursor's look; drawing an arrow instead", exc_info=True)
        _failed = time.monotonic()
    return arrow()


# The arrow drawn when the real look can't be read: the mirror window's own
# pointer (curslink.mirror.helper.ARROW), in logical pixels, hot spot at its tip.
ARROW = [(0, 0), (0, 17.5), (4.4, 13.4), (7.4, 20.6), (10.4, 19.4), (7.5, 12.4), (13.2, 12.4)]
_arrow: dict | None = None


def arrow() -> dict | None:
    """A plain white arrow with a dark outline, as a look, or None without Qt."""
    global _arrow
    if _arrow is not None:
        return _arrow
    try:
        from PySide6.QtCore import QBuffer, QIODevice, QPointF, Qt
        from PySide6.QtGui import QColor, QImage, QPainter, QPainterPath, QPen

        k, pad = 4, 2  # drawn at 4x so it stays sharp on any screen
        w, h = 16 + pad, 23 + pad
        img = QImage(w * k, h * k, QImage.Format.Format_ARGB32_Premultiplied)
        img.fill(Qt.GlobalColor.transparent)
        path = QPainterPath()
        path.moveTo(QPointF(1, 1) + QPointF(*ARROW[0]))
        for x, y in ARROW[1:]:
            path.lineTo(QPointF(1, 1) + QPointF(x, y))
        path.closeSubpath()
        p = QPainter(img)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.scale(k, k)
        p.setPen(QPen(QColor(0, 0, 0, 70), 3.2))
        p.drawPath(path.translated(0.6, 1.0))
        p.setPen(QPen(QColor(17, 17, 17), 1.3))
        p.setBrush(QColor("white"))
        p.drawPath(path)
        p.end()
        buf = QBuffer()
        buf.open(QIODevice.OpenModeFlag.WriteOnly)
        img.save(buf, "PNG")
        _arrow = shape(bytes(buf.data()), w, h, 1, 1)
    except Exception:  # pragma: no cover - no Qt
        log.debug("mirror: couldn't draw the arrow", exc_info=True)
        return None
    return _arrow


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
_dlls = None


def _win_dlls():  # pragma: no cover - Windows only
    """user32 and gdi32 with their argument types set. Handles are 64-bit:
    passed with no argtypes ctypes makes them a C int and a big one fails
    ("int too long to convert"). Loaded on their own, so other modules'
    use of ctypes.windll keeps its own settings."""
    global _dlls
    if _dlls is None:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.WinDLL("user32", use_last_error=True)
        gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
        user32.GetCursorInfo.argtypes = [ctypes.c_void_p]
        user32.GetCursorInfo.restype = wintypes.BOOL
        user32.GetIconInfo.argtypes = [wintypes.HANDLE, ctypes.c_void_p]
        user32.GetIconInfo.restype = wintypes.BOOL
        gdi32.DeleteObject.argtypes = [wintypes.HGDIOBJ]
        gdi32.DeleteObject.restype = wintypes.BOOL
        _dlls = (user32, gdi32)
    return _dlls


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

    user32, gdi32 = _win_dlls()
    info = CURSORINFO()
    info.cbSize = ctypes.sizeof(CURSORINFO)
    if not user32.GetCursorInfo(ctypes.byref(info)) or not info.flags & 1 or not info.hCursor:
        return None  # hidden
    handle = int(info.hCursor)
    if _win_last is not None and _win_last[0] == handle:
        return _win_last[1]
    icon = ICONINFO()
    if not user32.GetIconInfo(handle, ctypes.byref(icon)):
        raise OSError(f"GetIconInfo failed ({ctypes.get_last_error()})")
    try:
        from PySide6.QtCore import QBuffer, QIODevice
        from PySide6.QtGui import QImage

        image = QImage.fromHICON(handle)
        if image.isNull():
            raise OSError("Qt couldn't read the cursor's picture")
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
