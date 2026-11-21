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

    user32, _gdi32 = _win_dlls()
    info = CURSORINFO()
    info.cbSize = ctypes.sizeof(CURSORINFO)
    if not user32.GetCursorInfo(ctypes.byref(info)) or not info.flags & 1 or not info.hCursor:
        return None  # hidden
    handle = int(info.hCursor)
    if _win_last is not None and _win_last[0] == handle:
        return _win_last[1]
    out, blank = _win_read(handle, dpr)
    which = _win_system_id(handle)
    if not blank:
        if which is not None:
            _win_originals[which] = out
    else:
        # Curslink's own hidden cursor (curslink.cursorhide swaps the system's
        # cursors for blank ones while this computer's app is used through
        # its mirror on another screen): the look it stands for.
        out = (_win_originals.get(which) if which is not None else None) or _win_scheme(which, dpr) or arrow()
    _win_last = (handle, out)
    return out


def _win_read(handle: int, dpr: float) -> tuple[dict, bool]:  # pragma: no cover - Windows only
    """A cursor's look, and whether it's blank (all clear)."""
    import ctypes
    from ctypes import wintypes

    class ICONINFO(ctypes.Structure):
        _fields_ = [("fIcon", wintypes.BOOL), ("xHotspot", wintypes.DWORD), ("yHotspot", wintypes.DWORD),
                    ("hbmMask", wintypes.HBITMAP), ("hbmColor", wintypes.HBITMAP)]

    user32, gdi32 = _win_dlls()
    icon = ICONINFO()
    if not user32.GetIconInfo(handle, ctypes.byref(icon)):
        raise OSError(f"GetIconInfo failed ({ctypes.get_last_error()})")
    try:
        from PySide6.QtCore import QBuffer, QIODevice
        from PySide6.QtGui import QImage

        image = QImage.fromHICON(handle)
        if image.isNull():
            raise OSError("Qt couldn't read the cursor's picture")
        argb = image.convertToFormat(QImage.Format.Format_ARGB32)
        raw = bytes(argb.constBits())[: argb.sizeInBytes()]
        blank = not any(raw[3::4])
        buf = QBuffer()
        buf.open(QIODevice.OpenModeFlag.WriteOnly)
        image.save(buf, "PNG")
        png = bytes(buf.data())
    finally:
        for bitmap in (icon.hbmMask, icon.hbmColor):
            if bitmap:
                gdi32.DeleteObject(bitmap)
    k = max(0.5, float(dpr) or 1.0)
    return shape(png, image.width() / k, image.height() / k, icon.xHotspot / k, icon.yHotspot / k), blank


# The system's cursors (OCR_ / IDC_ ids): their names in the user's cursor
# scheme (HKCU\Control Panel\Cursors), and Windows' own files for them.
_WIN_CURSORS = {
    32512: ("Arrow", "aero_arrow.cur"), 32513: ("IBeam", "beam_r.cur"), 32514: ("Wait", "aero_busy.ani"),
    32515: ("Crosshair", "cross_r.cur"), 32516: ("UpArrow", "aero_up.cur"), 32642: ("SizeNWSE", "aero_nwse.cur"),
    32643: ("SizeNESW", "aero_nesw.cur"), 32644: ("SizeWE", "aero_ew.cur"), 32645: ("SizeNS", "aero_ns.cur"),
    32646: ("SizeAll", "aero_move.cur"), 32648: ("No", "aero_unavail.cur"), 32649: ("Hand", "aero_link.cur"),
    32650: ("AppStarting", "aero_working.ani"), 32651: ("Help", "aero_helpsel.cur"),
}
_win_originals: dict[int, dict] = {}  # system cursor id -> its look, as last seen not hidden


def _win_system_id(handle: int) -> int | None:  # pragma: no cover - Windows only
    """Which of the system's cursors ``handle`` is (they're shared: an app
    showing the arrow shows the system's arrow), if any."""
    import ctypes
    from ctypes import wintypes

    user32 = _win_dlls()[0]
    user32.LoadCursorW.argtypes = [wintypes.HINSTANCE, ctypes.c_void_p]
    user32.LoadCursorW.restype = wintypes.HANDLE
    for which in _WIN_CURSORS:
        h = user32.LoadCursorW(None, ctypes.c_void_p(which))
        if h and int(h) == handle:
            return which
    return None


def _win_scheme(which: int | None, dpr: float) -> dict | None:  # pragma: no cover - Windows only
    """A system cursor's look from the user's scheme's file (or Windows' own),
    when it hasn't been seen unhidden yet."""
    if which is None or which not in _WIN_CURSORS:
        return None
    import ctypes
    import os
    import winreg
    from ctypes import wintypes

    name, default = _WIN_CURSORS[which]
    path = ""
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Control Panel\Cursors") as key:
            path = os.path.expandvars(str(winreg.QueryValueEx(key, name)[0] or ""))
    except OSError:
        pass
    if not path or not os.path.exists(path):
        path = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "Cursors", default)
    user32 = _win_dlls()[0]
    user32.LoadImageW.argtypes = [wintypes.HINSTANCE, wintypes.LPCWSTR, wintypes.UINT, ctypes.c_int, ctypes.c_int,
                                  wintypes.UINT]
    user32.LoadImageW.restype = wintypes.HANDLE
    h = user32.LoadImageW(None, path, 2, 0, 0, 0x0010 | 0x0040)  # IMAGE_CURSOR, LR_LOADFROMFILE | LR_DEFAULTSIZE
    if not h:
        return None
    try:
        out, blank = _win_read(int(h), dpr)
    finally:
        user32.DestroyCursor.argtypes = [wintypes.HANDLE]
        user32.DestroyCursor(h)
    if blank:
        return None
    _win_originals[which] = out
    return out
