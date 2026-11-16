"""App mirror in the overlay helper process: capture one app window, show
another computer's app in a window of its own.

A capture in the ``mirror`` state with ``window`` ({wid, pid}) takes that
window's own picture (:class:`AppCapture`): on Windows with PrintWindow
(it works with the window behind others), on macOS from a ScreenCaptureKit
stream of just that window (:mod:`curslink.mirror.macstream`), or a
Quartz picture of it. Frames go as mirror mode's (tiles, only what changed),
with this computer's cursor drawn in where it's over the window.

A show with ``app`` is an :class:`AppWindow`: a borderless window with the
app's picture and nothing else (no frame of its own: the app brings its
title bar), its corners rounded like the app's on its own computer. Round
it, outside the picture, is a :class:`GripRing` (:data:`RING` px): drag it
to resize the mirror, any shape; each change is printed as ``app_view
{"id", "os"}``. Near a corner, the ring shows a short curved handle round
it, as the Isolink chat popups do. Next to it, while the pointer is near
(``hover``), a small glass tag names the app and has a box (``app_view
{"id", "full": true}``: fill the screen, or back) and a close button
(``app_view {"id", "closed": true}``). ``raise`` going up brings it to the
front (the pointer went into it). ``full``: it fills its screen, over the
taskbar and the menu bar, as a full screen app does.

The pointer in it is the app's own cursor (``mirror_shape``, sent from the
computer the app runs on), drawn by a :class:`PointerSprite`: a little
window of its own, moved to where the host says (``mirror_pointer``), so
the mirror's picture isn't drawn again for every move.

Each mirror has a taskbar button of its own (Windows), with the app's icon
(``app_icon {"id", "png"}``); on a Mac the helper takes a Dock icon while it
shows a mirror, the app's. A capture with ``window`` prints the app's icon
once, as ``app_icon {"id", "png"}``.
"""

from __future__ import annotations

import json
import logging
import sys
import time

import base64
import math

from PySide6.QtCore import QBuffer, QFileInfo, QIODevice, QPointF, QRect, QRectF, QSize, Qt, QTimer
from PySide6.QtGui import (QColor, QCursor, QFont, QFontMetricsF, QGuiApplication, QIcon, QImage, QLinearGradient,
                           QPainter, QPainterPath, QPen, QPixmap, QRegion)
from PySide6.QtWidgets import QWidget

from curslink.appmirror import RING
from curslink.mirror.helper import (Capture, MirrorWindow, _qscreen, mac_screen_recording, spec_to_logical,
                                    MAC_NO_RECORDING, cgimage_to_qimage)
from curslink.overlay import ACCENT

log = logging.getLogger(__name__)

# PrintWindow is work for the app being captured: never more than this a second.
WIN_APP_FPS = 60
MAC_APP_FPS = 60
STATS_S = 30.0  # how often a capture says how it's keeping up
POPUP_CHECK_S = 0.25  # how often to look for the app's menus, while it's in use
TAG_H = 26
# The corner handles, as the Isolink chat popups draw theirs: a quarter round
# this far outside the mirror's rounded corner, this thick, shown while the
# pointer is this near the corner; how strong, near it and on it (or dragging).
HANDLE_GAP = 5.0
HANDLE_W = 4.0
HANDLE_REACH = 72.0
HANDLE_ALPHA = (0.3, 0.55)
# How far along each side from a corner the ring resizes from the corner.
CORNER_GRIP = 22
# An app's icon goes as big as this, or smaller to fit in a frame.
ICON_SIZES = (128, 96, 64, 48, 32)


def bring_to_front(widget: QWidget, above_all: bool, over_menus: bool = False) -> None:
    """Put ``widget`` in front of every other app's windows, without taking
    the focus. ``above_all``: and keep it there (while it's carried, so it
    passes over every window; or filling the screen); otherwise it's an
    ordinary window again, at the front for now. ``over_menus``: over a
    Mac's menu bar and Dock too (a full screen mirror). Qt's raise_() alone
    can't do this from an app that isn't the active one: other apps'
    windows stay on top of it."""
    if not widget.isVisible():
        return
    platform = QGuiApplication.platformName()
    try:
        if platform == "windows" and sys.platform == "win32":  # pragma: no cover - Windows only
            import ctypes
            from ctypes import wintypes

            user32 = ctypes.windll.user32
            user32.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
                                            ctypes.c_int, ctypes.c_int, ctypes.c_uint]
            user32.SetWindowPos.restype = wintypes.BOOL
            hwnd = wintypes.HWND(int(widget.winId()))
            flags = 0x0001 | 0x0002 | 0x0010  # SWP_NOSIZE | SWP_NOMOVE | SWP_NOACTIVATE
            user32.SetWindowPos(hwnd, wintypes.HWND(-1), 0, 0, 0, 0, flags)  # HWND_TOPMOST: over everything
            if not above_all:
                user32.SetWindowPos(hwnd, wintypes.HWND(-2), 0, 0, 0, 0, flags)  # HWND_NOTOPMOST: front of the rest
            return
        if platform == "cocoa":  # pragma: no cover - macOS only
            import objc

            window = objc.objc_object(c_void_p=int(widget.winId())).window()
            if window is not None:
                # NSMainMenuWindowLevel + 1 / NSFloatingWindowLevel / NSNormalWindowLevel
                window.setLevel_(25 if over_menus else 3 if above_all else 0)
                window.orderFrontRegardless()
                return
    except Exception:
        log.debug("app mirror: couldn't bring the mirror to the front", exc_info=True)
    widget.raise_()


def logical_to_os(spec: dict, lx: float, ly: float, lw: float, lh: float) -> list[float]:
    """A rectangle in Qt's logical pixels back into the OS pixels the host
    placed it in (the inverse of :func:`curslink.mirror.helper.spec_to_logical`)."""
    whole = spec.get("screen_os")
    if isinstance(whole, list) and len(whole) == 4:
        try:
            ox, oy, ow, oh = (float(v) for v in whole)
            qs = _qscreen(str(spec.get("screen", "")), (ox, oy, ow, oh))
            if qs is not None and ow > 0 and oh > 0:
                g = qs.geometry()
                kx, ky = g.width() / ow, g.height() / oh
                return [round(ox + (lx - g.x()) / kx, 1), round(oy + (ly - g.y()) / ky, 1),
                        round(lw / kx, 1), round(lh / ky, 1)]
        except (TypeError, ValueError):
            pass
    k = 1.0
    if sys.platform == "win32":
        sc = QGuiApplication.screenAt(QPointF(lx + lw / 2, ly + lh / 2).toPoint()) or QGuiApplication.primaryScreen()
        k = sc.devicePixelRatio() or 1.0
    return [round(lx * k, 1), round(ly * k, 1), round(lw * k, 1), round(lh * k, 1)]


# ----------------------------------------------------------------- capturing
def win_print_window(hwnd: int) -> tuple[QImage, list[int]] | None:  # pragma: no cover - Windows only
    """Windows: the window's own picture (even behind other windows), cut to
    its visible frame, and where that frame is (physical px)."""
    import ctypes
    import ctypes.wintypes as wt

    from curslink.appmirror.windows import _win_rect

    u, g = ctypes.windll.user32, ctypes.windll.gdi32
    for fn, res, args in ((u.GetDC, wt.HDC, [wt.HWND]), (u.ReleaseDC, ctypes.c_int, [wt.HWND, wt.HDC]),
                          (g.CreateCompatibleDC, wt.HDC, [wt.HDC]), (g.DeleteDC, wt.BOOL, [wt.HDC]),
                          (g.CreateCompatibleBitmap, wt.HBITMAP, [wt.HDC, ctypes.c_int, ctypes.c_int]),
                          (g.SelectObject, wt.HGDIOBJ, [wt.HDC, wt.HGDIOBJ]), (g.DeleteObject, wt.BOOL, [wt.HGDIOBJ]),
                          (u.PrintWindow, wt.BOOL, [wt.HWND, wt.HDC, wt.UINT])):
        fn.restype, fn.argtypes = res, args
    h = wt.HWND(hwnd)
    outer = wt.RECT()
    if u.IsIconic(h) or not u.GetWindowRect(h, ctypes.byref(outer)):
        return None
    inner = _win_rect(hwnd)
    if inner is None:
        return None
    w, ht = outer.right - outer.left, outer.bottom - outer.top
    if w <= 0 or ht <= 0 or w > 16384 or ht > 16384:
        return None
    screen = u.GetDC(None)
    mem = g.CreateCompatibleDC(screen)
    bmp = g.CreateCompatibleBitmap(screen, w, ht)
    old = g.SelectObject(mem, bmp)
    try:
        if not u.PrintWindow(h, mem, 2):  # PW_RENDERFULLCONTENT
            return None

        class BITMAPINFOHEADER(ctypes.Structure):
            _fields_ = [("biSize", wt.DWORD), ("biWidth", wt.LONG), ("biHeight", wt.LONG), ("biPlanes", wt.WORD),
                        ("biBitCount", wt.WORD), ("biCompression", wt.DWORD), ("biSizeImage", wt.DWORD),
                        ("biXPelsPerMeter", wt.LONG), ("biYPelsPerMeter", wt.LONG), ("biClrUsed", wt.DWORD),
                        ("biClrImportant", wt.DWORD)]

        bih = BITMAPINFOHEADER(ctypes.sizeof(BITMAPINFOHEADER), w, -ht, 1, 32, 0, 0, 0, 0, 0, 0)
        buf = ctypes.create_string_buffer(w * ht * 4)
        g.GetDIBits.argtypes = [wt.HDC, wt.HBITMAP, wt.UINT, wt.UINT, ctypes.c_void_p, ctypes.c_void_p, wt.UINT]
        if not g.GetDIBits(mem, bmp, 0, ht, buf, ctypes.byref(bih), 0):
            return None
        img = QImage(bytes(buf), w, ht, w * 4, QImage.Format.Format_RGB32)
    finally:
        g.SelectObject(mem, old)
        g.DeleteObject(bmp)
        g.DeleteDC(mem)
        u.ReleaseDC(None, screen)
    dx, dy = inner[0] - outer.left, inner[1] - outer.top
    return img.copy(dx, dy, inner[2], inner[3]), inner


def fit_in(r: QRectF, w: float, h: float) -> QRectF:
    """``w`` x ``h`` made as big as fits in ``r`` (the same shape), centred:
    while the app catches up with its mirror's new shape. Within a pixel or
    two, all of ``r``."""
    if w <= 0 or h <= 0 or r.width() <= 0 or r.height() <= 0:
        return r
    k = min(r.width() / w, r.height() / h)
    fw, fh = w * k, h * k
    if r.width() - fw < 3 and r.height() - fh < 3:
        return r
    return QRectF(r.x() + (r.width() - fw) / 2, r.y() + (r.height() - fh) / 2, fw, fh)


def popup_image(wid: int, rect) -> QImage | None:
    """A menu's picture: it shows on the screen, so it's copied from there
    (on Windows; PrintWindow gives menus black), or taken by itself (macOS)."""
    try:
        if sys.platform == "win32":  # pragma: no cover - Windows only
            from curslink.mirror.helper import win_grab

            return win_grab(*rect)
        if sys.platform == "darwin":  # pragma: no cover - macOS only
            return mac_window_image(wid)
    except Exception:
        log.debug("app mirror: couldn't take a menu's picture", exc_info=True)
    return None


def mostly_black(img: QImage) -> bool:
    """A picture some apps give PrintWindow when they draw with the GPU."""
    small = img.scaled(16, 16, Qt.AspectRatioMode.IgnoreAspectRatio, Qt.TransformationMode.FastTransformation)
    dark = sum(1 for y in range(small.height()) for x in range(small.width())
               if QColor(small.pixel(x, y)).value() < 6)
    return dark >= small.width() * small.height() * 0.97


def mac_window_image(wid: int) -> QImage | None:  # pragma: no cover - macOS only
    """macOS: one window's picture through Quartz, without its shadow."""
    try:
        import Quartz
    except ImportError:
        return None
    create = getattr(Quartz, "CGWindowListCreateImage", None)
    if create is None:
        return None
    try:
        cg = create(Quartz.CGRectNull, Quartz.kCGWindowListOptionIncludingWindow, int(wid),
                    Quartz.kCGWindowImageBoundsIgnoreFraming | Quartz.kCGWindowImageBestResolution)
        return cgimage_to_qimage(Quartz, cg) if cg is not None else None
    except Exception:
        log.debug("app mirror: Quartz couldn't take the window", exc_info=True)
        return None


def process_path(pid: int) -> str:
    """The app's file (its .exe, or its .app bundle on a Mac), or ""."""
    try:
        if sys.platform == "win32":  # pragma: no cover - Windows only
            from curslink.appmirror.windows import _win_process_path

            return _win_process_path(pid)
        if sys.platform == "darwin":  # pragma: no cover - macOS only
            import AppKit

            app = AppKit.NSRunningApplication.runningApplicationWithProcessIdentifier_(int(pid))
            url = app.bundleURL() if app is not None else None
            return str(url.path()) if url is not None else ""
    except Exception:
        log.debug("app mirror: couldn't find the app's file", exc_info=True)
    return ""


def icon_png(path: str, px: int = 64) -> str | None:
    """The icon of the file at ``path`` (an app), as base64 PNG: what Finder
    or Explorer shows for it."""
    if not path:
        return None
    try:
        from PySide6.QtWidgets import QFileIconProvider

        icon = QFileIconProvider().icon(QFileInfo(path))
        pix = icon.pixmap(QSize(px, px))
    except Exception:  # pragma: no cover - platform trouble
        log.debug("app mirror: couldn't read the app's icon", exc_info=True)
        return None
    if pix.isNull():
        return None
    buf = QBuffer()
    buf.open(QIODevice.OpenModeFlag.WriteOnly)
    if not pix.toImage().save(buf, "PNG"):
        return None
    return base64.b64encode(bytes(buf.data())).decode()


class AppCapture(Capture):
    """A :class:`curslink.mirror.helper.Capture` of one window, wherever it is."""

    def __init__(self, spec: dict, out):
        self.window_stream = None
        self.window_seq = 0
        self.shape_key: str | None = None  # the pointer's look last sent
        self.popups: list = []  # the app's menus showing, as last found
        self.popups_at = 0.0
        self.stats = {"since": time.monotonic(), "frames": 0, "busy": 0.0, "bytes": 0}
        self.raw_out = out
        self.icon_sent = False
        super().__init__(spec, self._out)

    def set(self, spec: dict) -> None:
        if spec.get("use") and not self.spec.get("use"):
            # Used again: its pointer's look goes again too (the mirror may
            # be a new one, on another device, that never had it).
            self.shape_key = None
        super().set(spec)

    def send_icon(self) -> None:
        """The app's icon, once: for its mirror's taskbar button (or the Dock)."""
        self.icon_sent = True
        win = self.spec.get("window") or {}
        try:
            pid = int(win.get("pid") or 0)
        except (TypeError, ValueError):
            return
        from curslink.appmirror import MAX_ICON

        path = process_path(pid) if pid else ""
        for px in ICON_SIZES if path else ():
            png = icon_png(path, px)
            if png is None:
                return
            if len(png) <= MAX_ICON:
                self.raw_out("app_icon " + json.dumps({"id": self.id, "png": png}, separators=(",", ":")))
                return

    def grab(self) -> None:
        if not self.icon_sent:
            self.send_icon()
        if self.spec.get("pointer"):
            try:
                self.send_shape()
            except Exception:  # pragma: no cover - platform trouble
                log.debug("app mirror: couldn't send the pointer's look", exc_info=True)
        t0 = time.monotonic()
        sent = self.stats["bytes"]
        super().grab()
        took = time.monotonic() - t0
        st = self.stats
        if st["bytes"] != sent:
            st["frames"] += 1
            st["busy"] += took
        if t0 - st["since"] >= STATS_S:
            # How it's keeping up, in the log: frames a second sent, the time
            # each took here (capture, scale, encode) and the link it took.
            span = max(1e-3, t0 - st["since"])
            if st["frames"]:
                # On stderr itself: the helper's log only passes on warnings,
                # and its stderr goes into the app's log.
                print("app mirror: %s: %.0f fps of %d asked, %.0f ms each here, %.0f KB/s" % (
                    self.id, st["frames"] / span, self.fps(), 1000 * st["busy"] / st["frames"],
                    st["bytes"] / 1024 / span), file=sys.stderr, flush=True)
            self.stats = {"since": t0, "frames": 0, "busy": 0.0, "bytes": 0}

    def _out(self, line: str) -> None:
        self.stats["bytes"] += len(line)
        self.raw_out(line)

    def send_shape(self) -> None:
        """The pointer's look, when it changed while it's over the window
        (the mirror draws the pointer itself, where the host says)."""
        if self.area is None:
            return
        lx, ly, lw, lh, index = self.area
        pos = QCursor.pos()
        if not (lx <= pos.x() <= lx + lw and ly <= pos.y() <= ly + lh):
            return
        from curslink.mirror import cursorshape

        screens = QGuiApplication.screens()
        found = cursorshape.current(screens[index].devicePixelRatio() if 0 <= index < len(screens) else 1.0)
        if found is None or found["key"] == self.shape_key:
            return
        self.shape_key = found["key"]
        look = {k: found[k] for k in ("png", "w", "h", "hx", "hy")}
        look["cw"] = round(lw, 2)
        # A frame with no picture, only the look: it goes the way the
        # picture's tiles go, and is drawn by nothing.
        frame = {"id": self.id, "jpg": "", "w": 1, "h": 1, "tile": [0, 0, 0, 0], "cursor": look}
        self.out("mirror_frame " + json.dumps(frame, separators=(",", ":")))

    def finish(self, img: QImage | None) -> QImage | None:
        """The window's picture with its menus on it (while it's in use),
        and no bigger than the mirror shows it."""
        if img is None:
            return None
        if self.spec.get("use"):
            img = self.with_popups(img)
        out = self.spec.get("out")
        if isinstance(out, list) and len(out) == 2:
            try:
                ow, oh = max(16, int(out[0])), max(16, int(out[1]))
            except (TypeError, ValueError):
                return img
            if img.width() > ow * 1.05 or img.height() > oh * 1.05:
                img = img.scaled(ow, oh, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)
        return img

    def with_popups(self, img: QImage) -> QImage:
        """The app's menus, dropdowns and tooltips drawn onto its window:
        they're windows of their own, which the window's picture leaves out."""
        from curslink.appmirror import windows

        win = self.spec.get("window") or {}
        now = time.monotonic()
        if now - self.popups_at >= POPUP_CHECK_S:
            self.popups_at = now
            self.popups = windows.popups(win)
        if not self.popups:
            return img
        home = windows.window_rect(win)
        if home is None or home[2] <= 0:
            return img
        k = img.width() / home[2]  # the picture's pixels per OS pixel
        img = img.copy() if img.format() == QImage.Format.Format_RGB32 else img.convertToFormat(QImage.Format.Format_RGB32)
        p = QPainter(img)
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        for wid, rect in self.popups:
            part = popup_image(wid, rect)
            if part is None:
                continue
            p.drawImage(QRectF((rect[0] - home[0]) * k, (rect[1] - home[1]) * k, rect[2] * k, rect[3] * k), part,
                        QRectF(part.rect()))
        p.end()
        return img

    def fps(self) -> int:
        cap = WIN_APP_FPS if sys.platform == "win32" else MAC_APP_FPS
        return min(cap, super().fps())

    def _area(self, os_rect) -> None:
        """Where the window is now, in Qt's logical pixels (for the cursor)."""
        found = spec_to_logical(dict(self.spec, os=list(os_rect)))
        if found is not None:
            self.area = found

    def image(self) -> QImage | None:
        from curslink.appmirror import windows

        self.has_cursor = False
        self.skip = False
        win = self.spec.get("window") or {}
        try:
            wid = int(win.get("wid"))
        except (TypeError, ValueError):
            self.why = "no window to capture"
            return None
        now = time.monotonic()
        if now - self.shot_at < 1 / self.fps() - 0.004 and self.last_sent:
            self.skip = True
            return None
        self.shot_at = now
        rect = windows.window_rect(win)
        if rect is not None:
            self._area(rect)
        if sys.platform == "win32":
            got = win_print_window(wid)
            if got is not None and not mostly_black(got[0]):
                self._area(got[1])
                return self.finish(got[0])
            # Drawn by the GPU where PrintWindow can't see it: the screen
            # where the window is (it's in front while it's used), so it
            # can't be hidden there.
            if self.spec.get("hide"):
                windows.show_window(win, None)
            if rect is not None:
                self.spec = dict(self.spec, os=list(rect))
            return self.finish(super().image())
        if sys.platform == "darwin":
            if not mac_screen_recording(ask=True):
                self.why = MAC_NO_RECORDING
                return None
            img = self.mac_window_stream(wid, rect)
            if img is not None or self.skip:
                return self.finish(img)
            img = mac_window_image(wid)
            if img is not None:
                return self.finish(img)
            self.why = "macOS didn't give the window's picture"
            return None
        if rect is not None:
            self.spec = dict(self.spec, os=list(rect))
        return self.finish(super().image())

    def mac_window_stream(self, wid: int, rect) -> QImage | None:  # pragma: no cover - macOS only
        from curslink.mirror import macstream

        if QGuiApplication.platformName() != "cocoa" or rect is None:
            return None
        try:
            stream = macstream.window_stream_for(wid)
        except Exception:
            log.debug("app mirror: no ScreenCaptureKit stream of the window", exc_info=True)
            stream = None
        if stream is not self.window_stream:
            if self.window_stream is not None:
                self.window_stream.release()
            self.window_stream, self.window_seq = stream, 0
            if stream is not None:
                stream.acquire()
        if stream is None:
            return None
        sc = QGuiApplication.screenAt(QPointF(rect[0] + rect[2] / 2, rect[1] + 10).toPoint())
        dpr = (sc.devicePixelRatio() if sc is not None else 2.0) or 2.0
        stream.configure((0, 0, rect[2], rect[3]), dpr, self.fps(), False, None)
        if stream.stalled():
            return None
        got = stream.frame()
        if got is None or (got[0] == self.window_seq and time.monotonic() - self.last_sent < 2.0):
            self.skip = True
            return None
        self.window_seq = got[0]
        return got[1]

    def stop(self) -> None:
        super().stop()
        if self.window_stream is not None:
            self.window_stream.release()
            self.window_stream = None
            from curslink.mirror import macstream

            QTimer.singleShot(round(macstream.LINGER_S * 1000) + 200, macstream.reap)


# -------------------------------------------------------------------- showing
class AppTag(QWidget):
    """The small glass tag by a mirror: the app's name and where it runs, a
    box that makes the mirror fill its screen (and back), and an x that
    closes it."""

    def __init__(self, owner: "AppWindow"):
        flags = (Qt.WindowType.FramelessWindowHint | Qt.WindowType.Tool | Qt.WindowType.WindowStaysOnTopHint
                 | Qt.WindowType.WindowDoesNotAcceptFocus | Qt.WindowType.NoDropShadowWindowHint)
        super().__init__(None, flags)
        for attr in (Qt.WidgetAttribute.WA_TranslucentBackground, Qt.WidgetAttribute.WA_ShowWithoutActivating,
                     Qt.WidgetAttribute.WA_MacAlwaysShowToolWindow):
            self.setAttribute(attr, True)
        self.owner = owner
        self.text = ""
        self.font_ = QFont()
        self.font_.setPixelSize(12)
        self.font_.setWeight(QFont.Weight.DemiBold)
        self.hot: str | None = None  # "close" or "max": the button the pointer is on
        self.setMouseTracking(True)

    def place(self, text: str, r: QRect) -> None:
        self.text = text
        m = QFontMetricsF(self.font_)
        w = round(m.horizontalAdvance(text) + 18 + 30 + 14 + TAG_H)
        x = r.right() - w
        y = r.top() - TAG_H - 6
        screen = self.owner.screen().geometry() if self.owner.screen() is not None else None
        if self.owner.spec.get("full"):
            x, y = r.center().x() - w // 2, r.top() + 8  # filling the screen: at its top, inside
        elif screen is not None and y < screen.top():
            y = r.bottom() + 6  # no room above: below it
        self.setGeometry(QRect(x, y, w, TAG_H))
        self.update()

    def close_box(self) -> QRectF:
        return QRectF(self.width() - TAG_H, 0, TAG_H, TAG_H).adjusted(4, 4, -4, -4)

    def max_box(self) -> QRectF:
        return QRectF(self.width() - 2 * TAG_H, 0, TAG_H, TAG_H).adjusted(4, 4, -4, -4)

    def _hit(self, pos) -> str | None:
        if self.close_box().adjusted(-2, -4, 4, 4).contains(pos):
            return "close"
        if self.max_box().adjusted(-4, -4, 2, 4).contains(pos):
            return "max"
        return None

    def mouseMoveEvent(self, e) -> None:  # noqa: N802
        hot = self._hit(e.position())
        if hot != self.hot:
            self.hot = hot
            self.update()

    def leaveEvent(self, _e) -> None:  # noqa: N802
        self.hot = None
        self.update()

    def mousePressEvent(self, e) -> None:  # noqa: N802
        hit = self._hit(e.position())
        if hit == "close":
            self.owner.closed()
        elif hit == "max":
            self.owner.toggle_max()

    def mouseDoubleClickEvent(self, e) -> None:  # noqa: N802
        if self._hit(e.position()) is None:
            self.owner.toggle_max()

    def paintEvent(self, _e) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        grad = QLinearGradient(r.topLeft(), r.bottomLeft())
        grad.setColorAt(0, QColor(60, 60, 70, 215))
        grad.setColorAt(1, QColor(25, 25, 30, 215))
        p.setPen(QPen(QColor(255, 255, 255, 90), 1))
        p.setBrush(grad)
        p.drawRoundedRect(r, r.height() / 2, r.height() / 2)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(ACCENT))
        p.drawEllipse(QPointF(13, r.center().y()), 3.2, 3.2)
        p.setFont(self.font_)
        p.setPen(QColor("white"))
        p.drawText(QRectF(22, 0, self.width() - 22 - 2 * TAG_H, self.height()),
                   Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, self.text)
        pen = QPen(QColor("white"), 1.6)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        # Fill the screen (or back): a square, or two overlapping when it does.
        box = self.max_box()
        if self.hot == "max":
            p.setBrush(QColor(255, 255, 255, 60))
            p.setPen(Qt.PenStyle.NoPen)
            p.drawEllipse(box)
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.setPen(pen)
        c, k = box.center(), 3.4
        if not self.owner.spec.get("full"):
            p.drawRoundedRect(QRectF(c.x() - k, c.y() - k, 2 * k, 2 * k), 1.2, 1.2)
        else:
            p.drawRoundedRect(QRectF(c.x() - k, c.y() - k + 1.6, 2 * k - 1.6, 2 * k - 1.6), 1.0, 1.0)
            p.drawLine(QPointF(c.x() - k + 1.6, c.y() - k), QPointF(c.x() + k, c.y() - k))
            p.drawLine(QPointF(c.x() + k, c.y() - k), QPointF(c.x() + k, c.y() + k - 1.6))
        box = self.close_box()
        if self.hot == "close":
            p.setBrush(QColor(255, 80, 80, 220))
            p.setPen(Qt.PenStyle.NoPen)
            p.drawEllipse(box)
        c, k = box.center(), 3.6
        p.setPen(pen)
        p.drawLine(c + QPointF(-k, -k), c + QPointF(k, k))
        p.drawLine(c + QPointF(-k, k), c + QPointF(k, -k))
        p.end()


class GripRing(QWidget):
    """The ring round a mirror, outside its picture (:data:`RING` px): drag
    it to resize the mirror, any shape (the app is resized to it), with
    the resize arrows of the computer showing it. Near a corner a short
    curved handle shows round it, as the Isolink chat popups draw theirs;
    stronger on it, or dragging. All but clear: presses on it come here
    (a fully clear pixel lets them through to what's behind)."""

    def __init__(self, owner: "AppWindow"):
        flags = (Qt.WindowType.FramelessWindowHint | Qt.WindowType.Tool | Qt.WindowType.WindowDoesNotAcceptFocus
                 | Qt.WindowType.NoDropShadowWindowHint)
        super().__init__(None, flags)
        for attr in (Qt.WidgetAttribute.WA_TranslucentBackground, Qt.WidgetAttribute.WA_ShowWithoutActivating,
                     Qt.WidgetAttribute.WA_MacAlwaysShowToolWindow):
            self.setAttribute(attr, True)
        self.owner = owner
        self.setMouseTracking(True)
        self.near: str | None = None  # the corner the pointer is near: "lt", "rt", "lb" or "rb"
        self.hot = False              # the pointer is on the ring
        self.drag: tuple[str, QPointF, QRect] | None = None
        self._masked: tuple | None = None
        self._kept_out = False
        self.timer = QTimer(self)
        self.timer.setInterval(33)
        self.timer.timeout.connect(self.look)

    def place(self) -> None:
        g = self.owner.geometry()
        self.setGeometry(g.adjusted(-RING, -RING, RING, RING))
        corner = float(self.owner.spec.get("corner") or 0)
        key = (g.width(), g.height(), round(corner, 1))
        if key != self._masked:
            self._masked = key
            # The ring, and the bits of the corners the mirror's rounding
            # cuts away (they'd let presses through to what's behind).
            inner = QPainterPath()
            inner.addRoundedRect(QRectF(RING, RING, g.width(), g.height()), corner, corner)
            ring = QRegion(self.rect()).subtracted(QRegion(inner.toFillPolygon().toPolygon()))
            self.setMask(ring)
        self.update()

    def showEvent(self, e) -> None:  # noqa: N802
        super().showEvent(e)
        if not self._kept_out:
            self._kept_out = True
            from curslink.capturehide import exclude_from_capture

            exclude_from_capture(self)
        self.timer.start()

    def hideEvent(self, e) -> None:  # noqa: N802
        super().hideEvent(e)
        self.timer.stop()
        if self.near is not None:
            self.near = None

    # -- where the pointer is ----------------------------------------------------
    def corners(self) -> dict[str, QPointF]:
        g = self.owner.geometry()
        return {"lt": QPointF(g.left(), g.top()), "rt": QPointF(g.right() + 1, g.top()),
                "lb": QPointF(g.left(), g.bottom() + 1), "rb": QPointF(g.right() + 1, g.bottom() + 1)}

    def look(self) -> None:
        """The pointer's place (it may be over the mirror, which has it):
        the handle of the corner it's near shows."""
        at = QPointF(QCursor.pos())
        near = None
        if self.drag is None and self.owner.pointer is None:  # (in the app, it's the app's)
            best = HANDLE_REACH
            for name, c in self.corners().items():
                d = math.hypot(at.x() - c.x(), at.y() - c.y())
                if d <= best:
                    near, best = name, d
        else:
            near = self.drag[0] if len(self.drag[0]) == 2 else None
        if near != self.near:
            self.near = near
            self.update()

    def sides(self, pos: QPointF) -> str:
        """Which sides a press at ``pos`` (on the ring) drags: "l", "r", "t",
        "b", or a corner's two."""
        x, y = pos.x() - RING, pos.y() - RING
        w, h = self.owner.width(), self.owner.height()
        if 0 <= x < w and 0 <= y < h:
            # In a rounded-off corner of the mirror itself.
            return ("l" if x < w / 2 else "r") + ("t" if y < h / 2 else "b")
        out = ""
        if x < 0 or (x < CORNER_GRIP and (y < 0 or y >= h)):
            out += "l"
        elif x >= w or (x >= w - CORNER_GRIP and (y < 0 or y >= h)):
            out += "r"
        if y < 0 or (y < CORNER_GRIP and (x < 0 or x >= w)):
            out += "t"
        elif y >= h or (y >= h - CORNER_GRIP and (x < 0 or x >= w)):
            out += "b"
        return out

    def mousePressEvent(self, e) -> None:  # noqa: N802
        sides = self.sides(e.position())
        if sides:
            self.drag = (sides, e.globalPosition(), self.owner.geometry())
            self.update()

    def mouseMoveEvent(self, e) -> None:  # noqa: N802
        if self.drag is None:
            sides = self.sides(e.position())
            shapes = {"l": Qt.CursorShape.SizeHorCursor, "r": Qt.CursorShape.SizeHorCursor,
                      "t": Qt.CursorShape.SizeVerCursor, "b": Qt.CursorShape.SizeVerCursor,
                      "lt": Qt.CursorShape.SizeFDiagCursor, "rb": Qt.CursorShape.SizeFDiagCursor,
                      "rt": Qt.CursorShape.SizeBDiagCursor, "lb": Qt.CursorShape.SizeBDiagCursor}
            self.setCursor(shapes.get(sides, Qt.CursorShape.ArrowCursor))
            if not self.hot:
                self.hot = True
                self.update()
            return
        sides, start, g = self.drag
        now = e.globalPosition()
        dx, dy = now.x() - start.x(), now.y() - start.y()
        # Any shape: the app itself is resized to it, and lays itself out
        # again (the picture is fitted in until it has).
        w = max(160.0, g.width() + (dx if "r" in sides else -dx if "l" in sides else 0))
        h = max(100.0, g.height() + (dy if "b" in sides else -dy if "t" in sides else 0))
        x = g.right() + 1 - w if "l" in sides else g.x()
        y = g.bottom() + 1 - h if "t" in sides else g.y()
        self.owner.setGeometry(QRect(round(x), round(y), round(w), round(h)))
        self.place()
        self.owner._place_tag()
        self.owner.update()

    def mouseReleaseEvent(self, _e) -> None:  # noqa: N802
        if self.drag is not None:
            self.drag = None
            g = self.owner.geometry()
            self.owner._tell_view(os=logical_to_os(self.owner.spec, g.x(), g.y(), g.width(), g.height()))
            self.update()

    def leaveEvent(self, _e) -> None:  # noqa: N802
        if self.hot:
            self.hot = False
            self.update()

    def paintEvent(self, _e) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.fillRect(self.rect(), QColor(0, 0, 0, 2))
        corner = self.near if self.drag is None else (self.drag[0] if len(self.drag[0]) == 2 else None)
        if corner is not None:
            strong = self.hot or self.drag is not None
            self.paint_handle(p, corner, HANDLE_ALPHA[1] if strong else HANDLE_ALPHA[0])
        p.end()

    def paint_handle(self, p: QPainter, corner: str, alpha: float) -> None:
        """A quarter round about the mirror's rounded corner, just outside
        it, a little short of each end: the Isolink chat popups' handle."""
        radius = max(8.0, float(self.owner.spec.get("corner") or 0))
        w, h = self.owner.width(), self.owner.height()
        left, top = corner[0] == "l", corner[1] == "t"
        cx = RING + radius if left else RING + w - radius
        cy = RING + radius if top else RING + h - radius
        r = radius + HANDLE_GAP
        # Qt's angles go anticlockwise from three o'clock; this corner's
        # quarter, clockwise, starts at (Isolink's drawArc start):
        start = {"rb": 0.0, "lb": 90.0, "lt": 180.0, "rt": 270.0}[corner]
        trim = math.degrees(0.2)
        dark = True
        try:
            dark = QGuiApplication.styleHints().colorScheme() != Qt.ColorScheme.Light
        except AttributeError:  # pragma: no cover - an older Qt
            pass
        color = QColor(255, 255, 255) if dark else QColor(0, 0, 0)
        color.setAlphaF(alpha)
        pen = QPen(color, HANDLE_W)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        p.setPen(pen)
        p.setBrush(Qt.BrushStyle.NoBrush)
        rect = QRectF(cx - r, cy - r, 2 * r, 2 * r)
        p.drawArc(rect, round(-(start + trim) * 16), round(-(90 - 2 * trim) * 16))


class PointerSprite(QWidget):
    """The app's own cursor over its mirror: a little window of its own,
    moved to where the host says the pointer is (so the mirror isn't drawn
    again for each move), in the look sent from the app's computer."""

    def __init__(self):
        flags = (Qt.WindowType.FramelessWindowHint | Qt.WindowType.Tool | Qt.WindowType.WindowStaysOnTopHint
                 | Qt.WindowType.WindowDoesNotAcceptFocus | Qt.WindowType.NoDropShadowWindowHint
                 | Qt.WindowType.WindowTransparentForInput)
        super().__init__(None, flags)
        for attr in (Qt.WidgetAttribute.WA_TranslucentBackground, Qt.WidgetAttribute.WA_ShowWithoutActivating,
                     Qt.WidgetAttribute.WA_MacAlwaysShowToolWindow, Qt.WidgetAttribute.WA_TransparentForMouseEvents):
            self.setAttribute(attr, True)
        self.look: tuple | None = None  # (picture, w, h, hx, hy) in this screen's logical pixels
        self.hot = QPointF(0, 0)
        self._kept_out = False

    def show_at(self, at: QPointF, shape, k: float) -> None:
        """At ``at`` (global, logical px), the hot spot of ``shape`` (a
        parsed mirror_shape, or None: an arrow), ``k`` times its size."""
        if shape is not None:
            pix, w, h, hx, hy, _cw = shape
            look = (pix, w * k, h * k, hx * k, hy * k)
        else:
            look = (None, 16.0, 24.0, 1.0, 1.0)
        if look != self.look:
            self.look = look
            self.resize(max(1, math.ceil(look[1]) + 2), max(1, math.ceil(look[2]) + 2))
            self.update()
        self.hot = QPointF(look[3], look[4])
        self.move(round(at.x() - look[3]), round(at.y() - look[4]))
        if not self.isVisible():
            self.show()
            if not self._kept_out:
                self._kept_out = True
                from curslink.capturehide import exclude_from_capture

                exclude_from_capture(self)
            bring_to_front(self, True, over_menus=True)

    def paintEvent(self, _e) -> None:  # noqa: N802
        from curslink.mirror.helper import ARROW

        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        if self.look is not None and self.look[0] is not None:
            pix, w, h = self.look[0], self.look[1], self.look[2]
            p.drawPixmap(QRectF(0, 0, w, h), pix, QRectF(pix.rect()))
        else:
            at = QPointF(1, 1)
            path = QPainterPath()
            path.moveTo(at + QPointF(*ARROW[0]))
            for x, y in ARROW[1:]:
                path.lineTo(at + QPointF(x, y))
            path.closeSubpath()
            p.setPen(QPen(QColor(17, 17, 17), 1.3))
            p.setBrush(QColor("white"))
            p.drawPath(path)
        p.end()


class DockPresence:
    """macOS: while the helper shows another computer's app, it has a Dock
    icon (and a place in Cmd+Tab), the latest app's; clicking it brings the
    mirrors to the front. Back to no Dock icon when there are none."""

    def __init__(self):
        self.windows: dict[str, "AppWindow"] = {}
        self.on = False
        self._hooked = False

    def update(self, window: "AppWindow", alive: bool) -> None:
        sid = str(window.spec.get("id"))
        if alive:
            self.windows[sid] = window
        else:
            self.windows.pop(sid, None)
        if sys.platform != "darwin" or QGuiApplication.platformName() != "cocoa":
            return
        try:  # pragma: no cover - macOS only
            import AppKit

            app = AppKit.NSApplication.sharedApplication()
            want = bool(self.windows)
            if want != self.on:
                self.on = want
                app.setActivationPolicy_(0 if want else 1)  # Regular / Accessory
                if not want:
                    app.setApplicationIconImage_(None)
            if want:
                latest = list(self.windows.values())[-1]
                if latest.icon_png:
                    data = AppKit.NSData.dataWithBytes_length_(latest.icon_png, len(latest.icon_png))
                    image = AppKit.NSImage.alloc().initWithData_(data)
                    if image is not None:
                        app.setApplicationIconImage_(image)
            if not self._hooked:
                self._hooked = True
                QGuiApplication.instance().applicationStateChanged.connect(self._state)
        except Exception:
            log.debug("app mirror: couldn't show the mirror in the Dock", exc_info=True)

    def _state(self, state) -> None:  # pragma: no cover - macOS only
        if state == Qt.ApplicationState.ApplicationActive:
            for window in list(self.windows.values()):
                if not window._gone:
                    window._to_front()


DOCK = DockPresence()


def win_taskbar_button(widget: QWidget, app: str) -> None:  # pragma: no cover - Windows only
    """Windows: a taskbar button of its own for the mirror (a window that
    takes no focus has none unless it says it's an app window), grouped as
    the app, not as Curslink, so its button shows the app's icon."""
    import ctypes
    from ctypes import wintypes

    hwnd = int(widget.winId())
    user32 = ctypes.windll.user32
    get = getattr(user32, "GetWindowLongPtrW", user32.GetWindowLongW)
    put = getattr(user32, "SetWindowLongPtrW", user32.SetWindowLongW)
    get.restype, get.argtypes = ctypes.c_ssize_t, [wintypes.HWND, ctypes.c_int]
    put.restype, put.argtypes = ctypes.c_ssize_t, [wintypes.HWND, ctypes.c_int, ctypes.c_ssize_t]
    ex = get(hwnd, -20)  # GWL_EXSTYLE
    put(hwnd, -20, (ex | 0x00040000) & ~0x00000080)  # + WS_EX_APPWINDOW, - WS_EX_TOOLWINDOW

    class GUID(ctypes.Structure):
        _fields_ = [("d1", ctypes.c_uint32), ("d2", ctypes.c_uint16), ("d3", ctypes.c_uint16),
                    ("d4", ctypes.c_ubyte * 8)]

    class PROPERTYKEY(ctypes.Structure):
        _fields_ = [("fmtid", GUID), ("pid", ctypes.c_uint32)]

    class PROPVARIANT(ctypes.Structure):
        _fields_ = [("vt", ctypes.c_ushort), ("r1", ctypes.c_ushort), ("r2", ctypes.c_ushort),
                    ("r3", ctypes.c_ushort), ("value", ctypes.c_void_p), ("pad", ctypes.c_void_p)]

    def guid(text: str) -> GUID:
        import uuid

        b = uuid.UUID(text).bytes_le
        return GUID.from_buffer_copy(b)

    store = ctypes.c_void_p()
    iid = guid("886d8eeb-8cf2-4446-8d02-cdba1dbdcf99")  # IPropertyStore
    shell32 = ctypes.windll.shell32
    shell32.SHGetPropertyStoreForWindow.argtypes = [wintypes.HWND, ctypes.POINTER(GUID), ctypes.POINTER(ctypes.c_void_p)]
    if shell32.SHGetPropertyStoreForWindow(hwnd, ctypes.byref(iid), ctypes.byref(store)) != 0 or not store:
        return
    vtable = ctypes.cast(ctypes.cast(store, ctypes.POINTER(ctypes.c_void_p))[0], ctypes.POINTER(ctypes.c_void_p))
    set_value = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, ctypes.POINTER(PROPERTYKEY),
                                   ctypes.POINTER(PROPVARIANT))(vtable[6])
    commit = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p)(vtable[7])
    release = ctypes.WINFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p)(vtable[2])
    try:
        key = PROPERTYKEY(guid("9f4c2855-9f79-4b39-a8d0-e1d42de1d5f3"), 5)  # PKEY_AppUserModel_ID
        name = "Curslink.Mirror." + "".join(c for c in app if c.isalnum())[:60]
        text = ctypes.create_unicode_buffer(name)
        value = PROPVARIANT(31, 0, 0, 0, ctypes.cast(text, ctypes.c_void_p), None)  # VT_LPWSTR
        if set_value(store, ctypes.byref(key), ctypes.byref(value)) == 0:
            commit(store)
    finally:
        release(store)


class AppWindow(MirrorWindow):
    """Another computer's app, in a window of its own with nothing around it."""

    def __init__(self, spec: dict, out=None):
        self.tag: AppTag | None = None
        self.ring: GripRing | None = None
        self.sprite: PointerSprite | None = None
        self._raised = spec.get("raise", 0)
        self._carried = bool(spec.get("carried"))
        self._full = bool(spec.get("full"))
        self.app_out = out
        self._gone = False
        self.icon_png: bytes = b""
        self._taskbar = False
        super().__init__(dict(spec, tune=False), out)
        flags = (Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowDoesNotAcceptFocus
                 | Qt.WindowType.NoDropShadowWindowHint)
        if sys.platform != "win32":
            # A Windows mirror has a taskbar button of its own (see
            # win_taskbar_button); a Mac's goes in the Dock as the helper's.
            flags |= Qt.WindowType.Tool
        self.setWindowFlags(flags)
        # Solid: no see-through window (on Windows a see-through window is
        # redrawn by the processor, every frame), and its round corners cut
        # out by a mask instead.
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, False)
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground, False)
        self.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent, True)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, False)
        self._masked: tuple | None = None
        self.setWindowTitle(str(spec.get("label") or "App"))
        self.tag = AppTag(self)
        self.ring = GripRing(self)
        self.sprite = PointerSprite()
        if self.ok:
            self.set(spec)
        DOCK.update(self, True)

    @property
    def maxed(self):
        """Filling its screen (the host says: spec ``full``)."""
        return True if self.spec.get("full") else None

    def set(self, spec: dict) -> bool:
        ok = super().set(dict(spec, tune=False))
        if not ok or self.tag is None:
            return ok
        carried = bool(spec.get("carried"))
        full = bool(spec.get("full"))
        if spec.get("raise", 0) != self._raised or carried != self._carried or full != self._full:
            # Pointed into, or picked up / put down, or filling its screen:
            # in front of every app here (over all of them while it's carried
            # or fills the screen).
            self._raised = spec.get("raise", 0)
            self._carried = carried
            self._full = full
            self._to_front()
        title = str(spec.get("label") or "App")
        if title != self.windowTitle():
            self.setWindowTitle(title)
        self._mask()
        self._place_tag()
        return ok

    def set_icon(self, png_b64: str) -> None:
        """The app's icon: its taskbar button's (and the Dock's, on a Mac)."""
        try:
            raw = base64.b64decode(png_b64, validate=True)
        except (ValueError, TypeError):
            return
        pix = QPixmap()
        if not pix.loadFromData(raw, "PNG"):
            return
        self.icon_png = raw
        self.setWindowIcon(QIcon(pix))
        DOCK.update(self, not self._gone)

    def _place_tag(self) -> None:
        if self.tag is None:
            return
        if self.ring is not None:
            if self.isVisible() and not self.spec.get("carried") and not self.spec.get("full"):
                self.ring.place()
                if not self.ring.isVisible():
                    self.ring.show()
                    bring_to_front(self.ring, False)
            else:
                self.ring.hide()
        if self.spec.get("hover") and self.isVisible() and not self.spec.get("carried"):
            self.tag.place(str(self.spec.get("label") or "App"), self.geometry())
            self.tag.show()
            self.tag.raise_()
        else:
            self.tag.hide()

    def image(self, data: dict) -> None:
        was = self.isVisible()
        super().image(data)
        if self.isVisible() and not was:
            if sys.platform == "win32" and not self._taskbar:
                self._taskbar = True
                try:
                    win_taskbar_button(self, str(self.spec.get("app_name") or self.spec.get("label") or "App"))
                except Exception:  # pragma: no cover - platform trouble
                    log.debug("app mirror: no taskbar button of its own", exc_info=True)
            self._to_front()
            self._place_tag()
            # Its screen's pixel ratio (a Windows screen's scaling): the host
            # sizes the app for it, and sends pictures at its pixels.
            sc = self.screen()
            if sc is not None:
                self._tell_view(dpr=round(float(sc.devicePixelRatio() or 1.0), 3))

    def _to_front(self) -> None:
        full = bool(self.spec.get("full"))
        if self.ring is not None and self.ring.isVisible():
            bring_to_front(self.ring, self._carried)
        bring_to_front(self, self._carried or full, over_menus=full)
        if self.tag is not None and self.tag.isVisible():
            self.tag.raise_()
        if self.sprite is not None and self.sprite.isVisible():
            bring_to_front(self.sprite, True, over_menus=True)  # the pointer over it, always

    def changeEvent(self, e) -> None:  # noqa: N802
        super().changeEvent(e)
        # Its taskbar button pressed: Windows minimises it. A mirror isn't
        # put away like that: it comes to the front instead.
        from PySide6.QtCore import QEvent

        if e.type() == QEvent.Type.WindowStateChange and self.windowState() & Qt.WindowState.WindowMinimized:
            self.setWindowState(self.windowState() & ~Qt.WindowState.WindowMinimized)
            self.showNormal()
            QTimer.singleShot(0, self._to_front)

    def closed(self) -> None:
        self._tell_view(closed=True)
        self.close_soon()

    def close_soon(self) -> None:
        # Closed by its tag's x, then again when the host's next state drops
        # it: by then Qt may have deleted it.
        if self._gone:
            return
        self._gone = True
        DOCK.update(self, False)
        for part in (self.tag, self.ring, self.sprite):
            if part is not None:
                part.hide()
                part.deleteLater()
        self.tag = self.ring = self.sprite = None
        super().close_soon()

    def setWindowOpacity(self, _level: float) -> None:  # noqa: N802
        """Never see-through (mirror mode's mirrors fade in and out; an app
        doesn't): it would make Windows draw it the slow way."""

    def _mask(self) -> None:
        """Round corners like the app's own, cut out of the solid window."""
        corner = float(self.spec.get("corner") or 0)
        key = (self.width(), self.height(), round(corner, 1))
        if key == self._masked:
            return
        self._masked = key
        if corner <= 0:
            self.clearMask()
            return
        path = QPainterPath()
        path.addRoundedRect(QRectF(self.rect()), corner, corner)
        self.setMask(QRegion(path.toFillPolygon().toPolygon()))

    def resizeEvent(self, e) -> None:  # noqa: N802
        super().resizeEvent(e)
        self._mask()

    def moveEvent(self, e) -> None:  # noqa: N802
        super().moveEvent(e)
        if self.ring is not None and self.ring.isVisible():
            self.ring.place()

    def toggle_max(self) -> None:
        """Fill its screen, or back (the tag's box): the host does it, so the
        app takes that screen's shape."""
        self._tell_view(full=True)

    def _tell_view(self, **data) -> None:
        if self.app_out is not None:
            self.app_out("app_view " + json.dumps({"id": self.spec.get("id"), **data}, separators=(",", ":")))

    # -- the pointer: the app's own cursor, in a little window of its own -----------
    def picture_rect(self) -> QRectF:
        whole = self.canvas is not None and self.got.get("whole")
        if self.pixmap is None and not whole:
            return QRectF(self.rect())
        pic = QRectF(self.canvas.rect()) if whole else QRectF(self.pixmap.rect())
        return fit_in(QRectF(self.rect()), pic.width(), pic.height())

    def set_pointer(self, p: tuple[float, float] | None) -> None:
        self.pointer = p
        if self.sprite is None:
            return
        if p is None or not self.isVisible():
            self.sprite.hide()
            return
        r = self.picture_rect()
        g = self.geometry()
        at = QPointF(g.x() + r.x() + p[0] * r.width(), g.y() + r.y() + p[1] * r.height())
        k = self.width() / self.shape[5] if self.shape is not None and self.shape[5] > 0 else 1.0
        self.sprite.show_at(at, self.shape, k)

    def set_shape(self, data: dict | None) -> None:
        super().set_shape(data)
        if self.pointer is not None:
            self.set_pointer(self.pointer)

    # A press on the mirror itself (its last pixel or two, or a rounded-off
    # corner): nothing; the ring round it resizes it.
    def mousePressEvent(self, _e) -> None:  # noqa: N802
        return

    def wheelEvent(self, _e) -> None:  # noqa: N802
        return

    def paintEvent(self, _e) -> None:  # noqa: N802
        whole = self.canvas is not None and self.got.get("whole")
        if self.pixmap is None and not whole:
            return
        p = QPainter(self)
        p.fillRect(self.rect(), QColor(0, 0, 0))  # solid: what the picture doesn't cover
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        pic = QRectF(self.canvas.rect()) if whole else QRectF(self.pixmap.rect())
        r = fit_in(QRectF(self.rect()), pic.width(), pic.height())
        p.save()
        corner = float(self.spec.get("corner") or 0)
        if corner > 0:
            path = QPainterPath()
            path.addRoundedRect(r, corner, corner)
            p.setClipPath(path)
        if whole:
            p.drawImage(r, self.canvas, pic)
        else:
            p.drawPixmap(r, self.pixmap, pic)
        p.restore()
        p.end()
