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
title bar), its corners rounded like the app's on its own computer. Its
outer :data:`curslink.appmirror.GRIP` px resize it, keeping its shape;
each change is printed as ``app_view {"id", "os"}``. Next to it, while the
pointer is near (``hover``), a small glass tag names the app and has a
close button (``app_view {"id", "closed": true}``). ``raise`` going up
brings it to the front (the pointer went into it).
"""

from __future__ import annotations

import json
import logging
import sys
import time

from PySide6.QtCore import QPointF, QRect, QRectF, Qt, QTimer
from PySide6.QtGui import QColor, QFont, QFontMetricsF, QGuiApplication, QImage, QLinearGradient, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QWidget

from curslink.appmirror import GRIP
from curslink.mirror.helper import (FADE_S, Capture, MirrorWindow, _qscreen, mac_screen_recording, spec_to_logical,
                                    MAC_NO_RECORDING, cgimage_to_qimage)
from curslink.overlay import ACCENT

log = logging.getLogger(__name__)

# PrintWindow is work for the app being captured: never more than this a second.
WIN_APP_FPS = 30
MAC_APP_FPS = 30
TAG_H = 26


def bring_to_front(widget: QWidget, above_all: bool) -> None:
    """Put ``widget`` in front of every other app's windows, without taking
    the focus. ``above_all``: and keep it there (while it's carried, so it
    passes over every window); otherwise it's an ordinary window again, at
    the front for now. Qt's raise_() alone can't do this from an app that
    isn't the active one: other apps' windows stay on top of it."""
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
                window.setLevel_(3 if above_all else 0)  # NSFloatingWindowLevel / NSNormalWindowLevel
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


class AppCapture(Capture):
    """A :class:`curslink.mirror.helper.Capture` of one window, wherever it is."""

    def __init__(self, spec: dict, out):
        self.window_stream = None
        self.window_seq = 0
        super().__init__(spec, out)

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
                return got[0]
            # Drawn by the GPU where PrintWindow can't see it: the screen
            # where the window is (it's in front while it's used).
            if rect is not None:
                self.spec = dict(self.spec, os=list(rect))
            return super().image()
        if sys.platform == "darwin":
            if not mac_screen_recording(ask=True):
                self.why = MAC_NO_RECORDING
                return None
            img = self.mac_window_stream(wid, rect)
            if img is not None or self.skip:
                return img
            img = mac_window_image(wid)
            if img is not None:
                return img
            self.why = "macOS didn't give the window's picture"
            return None
        if rect is not None:
            self.spec = dict(self.spec, os=list(rect))
        return super().image()

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
    """The small glass tag by a mirror: the app's name and where it runs,
    and an x that closes the mirror."""

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
        self.hot = False
        self.setMouseTracking(True)

    def place(self, text: str, r: QRect) -> None:
        self.text = text
        m = QFontMetricsF(self.font_)
        w = round(m.horizontalAdvance(text) + 18 + 30 + 14)
        x = r.right() - w
        y = r.top() - TAG_H - 6
        screen = self.owner.screen().geometry() if self.owner.screen() is not None else None
        if screen is not None and y < screen.top():
            y = r.bottom() + 6  # no room above: below it
        self.setGeometry(QRect(x, y, w, TAG_H))
        self.update()

    def close_box(self) -> QRectF:
        return QRectF(self.width() - TAG_H, 0, TAG_H, TAG_H).adjusted(4, 4, -4, -4)

    def mouseMoveEvent(self, e) -> None:  # noqa: N802
        hot = self.close_box().contains(e.position())
        if hot != self.hot:
            self.hot = hot
            self.update()

    def leaveEvent(self, _e) -> None:  # noqa: N802
        self.hot = False
        self.update()

    def mousePressEvent(self, e) -> None:  # noqa: N802
        if self.close_box().adjusted(-6, -4, 4, 4).contains(e.position()):
            self.owner.closed()

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
        p.drawText(QRectF(22, 0, self.width() - 22 - TAG_H, self.height()),
                   Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, self.text)
        box = self.close_box()
        if self.hot:
            p.setBrush(QColor(255, 80, 80, 220))
            p.setPen(Qt.PenStyle.NoPen)
            p.drawEllipse(box)
        c, k = box.center(), 3.6
        pen = QPen(QColor("white"), 1.6)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        p.setPen(pen)
        p.drawLine(c + QPointF(-k, -k), c + QPointF(k, k))
        p.drawLine(c + QPointF(-k, k), c + QPointF(k, -k))
        p.end()


class AppWindow(MirrorWindow):
    """Another computer's app, in a window of its own with nothing around it."""

    def __init__(self, spec: dict, out=None):
        self.tag: AppTag | None = None
        self._resize: tuple[str, QPointF, QRect] | None = None
        self._raised = spec.get("raise", 0)
        self._carried = bool(spec.get("carried"))
        self.app_out = out
        self._gone = False
        super().__init__(dict(spec, tune=False), out)
        flags = (Qt.WindowType.FramelessWindowHint | Qt.WindowType.Tool | Qt.WindowType.WindowDoesNotAcceptFocus
                 | Qt.WindowType.NoDropShadowWindowHint)
        self.setWindowFlags(flags)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, False)
        self.setMouseTracking(True)
        self.tag = AppTag(self)
        if self.ok:
            self.set(spec)

    def set(self, spec: dict) -> bool:
        ok = super().set(dict(spec, tune=False))
        if not ok or self.tag is None:
            return ok
        carried = bool(spec.get("carried"))
        if spec.get("raise", 0) != self._raised or carried != self._carried:
            # Pointed into, or picked up / put down: in front of every app
            # here, and over all of them while it's carried.
            self._raised = spec.get("raise", 0)
            self._carried = carried
            self._to_front()
        self.setWindowOpacity(0.88 if spec.get("carried") and self.isVisible() else
                              (1.0 if self.isVisible() and time.monotonic() - self.shown_at > FADE_S else self.windowOpacity()))
        self._place_tag()
        return ok

    def _place_tag(self) -> None:
        if self.tag is None:
            return
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
            self._to_front()
            self._place_tag()

    def _to_front(self) -> None:
        bring_to_front(self, self._carried)
        if self.tag is not None and self.tag.isVisible():
            self.tag.raise_()

    def closed(self) -> None:
        self._tell_view(closed=True)
        self.close_soon()

    def close_soon(self) -> None:
        # Closed by its tag's x, then again when the host's next state drops
        # it: by then Qt may have deleted it.
        if self._gone:
            return
        self._gone = True
        if self.tag is not None:
            self.tag.hide()
            self.tag.deleteLater()
            self.tag = None
        super().close_soon()

    def tick(self) -> None:
        super().tick()
        if self.spec.get("carried"):
            self.setWindowOpacity(min(self.windowOpacity(), 0.88))

    def _tell_view(self, **data) -> None:
        if self.app_out is not None:
            self.app_out("app_view " + json.dumps({"id": self.spec.get("id"), **data}, separators=(",", ":")))

    # -- its own outer pixels: resize, keeping its shape ---------------------------
    def _grip_sides(self, pos: QPointF) -> str:
        w, h = self.width(), self.height()
        out = ""
        if pos.x() < GRIP:
            out += "l"
        elif pos.x() > w - GRIP:
            out += "r"
        if pos.y() < GRIP:
            out += "t"
        elif pos.y() > h - GRIP:
            out += "b"
        return out

    def mousePressEvent(self, e) -> None:  # noqa: N802
        sides = self._grip_sides(e.position())
        if sides:
            self._resize = (sides, e.globalPosition(), self.geometry())

    def mouseMoveEvent(self, e) -> None:  # noqa: N802
        if self._resize is None:
            sides = self._grip_sides(e.position())
            shapes = {"l": Qt.CursorShape.SizeHorCursor, "r": Qt.CursorShape.SizeHorCursor,
                      "t": Qt.CursorShape.SizeVerCursor, "b": Qt.CursorShape.SizeVerCursor,
                      "lt": Qt.CursorShape.SizeFDiagCursor, "rb": Qt.CursorShape.SizeFDiagCursor,
                      "rt": Qt.CursorShape.SizeBDiagCursor, "lb": Qt.CursorShape.SizeBDiagCursor}
            self.setCursor(shapes.get(sides, Qt.CursorShape.ArrowCursor))
            return
        sides, start, g = self._resize
        now = e.globalPosition()
        dx, dy = now.x() - start.x(), now.y() - start.y()
        ratio = float(self.spec.get("ratio") or (g.width() / max(1, g.height())))
        grow_w = (dx if "r" in sides else -dx if "l" in sides else 0)
        grow_h = (dy if "b" in sides else -dy if "t" in sides else 0)
        w = g.width() + (grow_w if abs(grow_w) >= abs(grow_h * ratio) else grow_h * ratio)
        w = max(160.0, w)
        h = w / ratio
        x = g.right() + 1 - w if "l" in sides else g.x()
        y = g.bottom() + 1 - h if "t" in sides else g.y()
        self.setGeometry(QRect(round(x), round(y), round(w), round(h)))
        self._place_tag()
        self.update()

    def mouseReleaseEvent(self, _e) -> None:  # noqa: N802
        if self._resize is not None:
            self._resize = None
            g = self.geometry()
            self._tell_view(os=logical_to_os(self.spec, g.x(), g.y(), g.width(), g.height()))

    def wheelEvent(self, _e) -> None:  # noqa: N802
        return

    def paintEvent(self, _e) -> None:  # noqa: N802
        whole = self.canvas is not None and self.got.get("whole")
        if self.pixmap is None and not whole:
            return
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        r = QRectF(self.rect())
        corner = float(self.spec.get("corner") or 0)
        if corner > 0:
            path = QPainterPath()
            path.addRoundedRect(r, corner, corner)
            p.setClipPath(path)
        if whole:
            p.drawImage(r, self.canvas, QRectF(self.canvas.rect()))
        else:
            p.drawPixmap(r, self.pixmap, QRectF(self.pixmap.rect()))
        p.end()
