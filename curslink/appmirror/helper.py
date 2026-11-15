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
from PySide6.QtGui import (QColor, QCursor, QFont, QFontMetricsF, QGuiApplication, QImage, QLinearGradient, QPainter,
                           QPainterPath, QPen, QRegion)
from PySide6.QtWidgets import QWidget

from curslink.appmirror import GRIP
from curslink.mirror.helper import (FADE_S, Capture, MirrorWindow, _qscreen, mac_screen_recording, spec_to_logical,
                                    MAC_NO_RECORDING, cgimage_to_qimage)
from curslink.overlay import ACCENT

log = logging.getLogger(__name__)

# PrintWindow is work for the app being captured: never more than this a second.
WIN_APP_FPS = 60
MAC_APP_FPS = 60
STATS_S = 15.0  # how often a capture says how it's keeping up
POPUP_CHECK_S = 0.25  # how often to look for the app's menus, while it's in use
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
        super().__init__(spec, self._out)

    def grab(self) -> None:
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
                log.info("app mirror: %s: %.0f fps of %d asked, %.0f ms each here, %.0f KB/s", self.id,
                         st["frames"] / span, self.fps(), 1000 * st["busy"] / st["frames"], st["bytes"] / 1024 / span)
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
        if screen is not None and y < screen.top():
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
        if self.owner.maxed is None:
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
        # Solid: no see-through window (on Windows a see-through window is
        # redrawn by the processor, every frame), and its round corners cut
        # out by a mask instead.
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, False)
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground, False)
        self.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent, True)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, False)
        self.maxed: QRect | None = None  # filling its screen: where it was before
        self._masked: tuple | None = None
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
        self._mask()
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
            # Its screen's pixel ratio (a Windows screen's scaling): the host
            # sizes the app for it, and sends pictures at its pixels.
            sc = self.screen()
            if sc is not None:
                self._tell_view(dpr=round(float(sc.devicePixelRatio() or 1.0), 3))

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

    def toggle_max(self) -> None:
        """Fill its screen (as much as apps get: not the taskbar or the menu
        bar), or back to where it was; the app itself takes the size."""
        sc = self.screen()
        if sc is None:
            return
        if self.maxed is None:
            self.maxed = self.geometry()
            g = sc.availableGeometry()
        else:
            g, self.maxed = self.maxed, None
        self.setGeometry(g)
        self._place_tag()
        if self.tag is not None:
            self.tag.update()
        self._tell_view(os=logical_to_os(self.spec, g.x(), g.y(), g.width(), g.height()))

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
        # Any shape: the app itself is resized to it, and lays itself out
        # again (the picture is fitted in until it has).
        w = max(160.0, g.width() + (dx if "r" in sides else -dx if "l" in sides else 0))
        h = max(100.0, g.height() + (dy if "b" in sides else -dy if "t" in sides else 0))
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
        if self.pointer is not None:
            # Drawn here, where the host says it is: it moves as fast as the
            # pointer does, not as fast as pictures come.
            self.paint_pointer(p, QPointF(r.x() + self.pointer[0] * r.width(), r.y() + self.pointer[1] * r.height()))
        p.end()
