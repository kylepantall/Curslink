"""Mirror mode in the overlay helper process: capture covered areas, draw mirrors.

Commands (one line each, from :class:`curslink.mirror.MirrorPeer`)::

    mirror {"captures": [{"id", "screen", "os": [x, y, w, h], "fps"}],
            "shows": [{"id", "screen", "os": [x, y, w, h], "label", "edges"}]}
    mirror_image {"id", "jpg", "w", "h"}
    mirror_pointer {"id", "x", "y"} | {"id", "hide": true}

``os`` rectangles are in this computer's OS pixels (physical on Windows,
points on macOS), like every other position the helper gets. Each capture
prints its frames on stdout as ``mirror_frame {"id", "jpg", "w", "h"}``:
the covered rectangle as a base64 JPEG small enough for one protocol frame,
only when it changed (or every couple of seconds).

A mirror is a borderless, click-through window over the covered part of the
screen in front. It shows nothing until its first frame, fades in, flashes
its inner edges once in the accent colour, and shows a small glass tag with
the device behind's name for a few seconds. It draws the pointer of the
screen behind where the host says it is.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import sys
import time

from PySide6.QtCore import QBuffer, QByteArray, QIODevice, QPoint, QPointF, QRect, QRectF, Qt, QTimer
from PySide6.QtGui import (QColor, QFont, QFontMetricsF, QGuiApplication, QImage, QLinearGradient, QPainter,
                           QPainterPath, QPen, QPixmap)
from PySide6.QtWidgets import QWidget

from curslink.overlay import ACCENT

log = logging.getLogger(__name__)

# A frame's base64 JPEG must fit in one protocol frame (64 KB, with the rest).
MAX_B64 = 60_000
# Never send more pixels across than this along either side.
MAX_SIDE = 1600
FADE_S = 0.22
FLASH_S = 0.6
TAG_S = 3.5
REFRESH_S = 2.0


def _screens():
    out = []
    for sc in QGuiApplication.screens():
        g = sc.geometry()
        out.append((g.x(), g.y(), g.width(), g.height(), sc.devicePixelRatio() or 1.0))
    return out


def os_to_logical(screens, rect, scale_positions: bool) -> tuple[float, float, float, float, int] | None:
    """An OS pixel rectangle -> Qt's logical one, and which screen (index in
    ``screens``) it's on. On Windows OS pixels are physical, so the size is
    scaled by that screen's ratio as well as the position (see
    :func:`curslink.overlay.native_to_logical`)."""
    try:
        x, y, w, h = (float(v) for v in rect)
    except (TypeError, ValueError):
        return None
    if w <= 0 or h <= 0:
        return None
    cx, cy = x + w / 2, y + h / 2
    for i, (gx, gy, gw, gh, dpr) in enumerate(screens):
        k = dpr if scale_positions else 1.0
        if gx <= cx < gx + gw * k and gy <= cy < gy + gh * k:
            return gx + (x - gx) / k, gy + (y - gy) / k, w / k, h / k, i
    return None


def fit_jpeg(image: QImage, quality: int, scale: float) -> tuple[bytes, int, float]:
    """``image`` as a JPEG whose base64 fits :data:`MAX_B64`: lowers the
    quality, then the size, until it does. Returns the JPEG and the quality
    and scale it took (to start from next time)."""
    w, h = image.width(), image.height()
    scale = min(scale, MAX_SIDE / max(1, w), MAX_SIDE / max(1, h), 1.0)
    for _ in range(12):
        img = image if scale >= 0.999 else image.scaled(max(1, round(w * scale)), max(1, round(h * scale)),
                                                        Qt.AspectRatioMode.IgnoreAspectRatio,
                                                        Qt.TransformationMode.SmoothTransformation)
        data = QByteArray()
        buf = QBuffer(data)
        buf.open(QIODevice.OpenModeFlag.WriteOnly)
        img.save(buf, "JPG", quality)
        buf.close()
        raw = bytes(data.data())
        if len(raw) * 4 / 3 + 4 <= MAX_B64:
            return raw, quality, scale
        if quality > 45:
            quality -= 10
        else:
            scale *= 0.8
    return raw, quality, scale


MAC_NO_RECORDING = ("macOS hasn't allowed Screen Recording for Curslink. Allow it in System Settings > "
                    "Privacy & Security > Screen & System Audio Recording, then restart Curslink")
_recording: bool | None = None


def mac_screen_recording(ask: bool = False) -> bool:
    """macOS: may Curslink see the screen? Without Screen Recording a grab
    comes back empty or with just the wallpaper. macOS keeps a process's
    first answer, so this checks once. With ``ask``, a "no" asks macOS (its
    prompt, and Curslink listed in System Settings), once."""
    global _recording
    from curslink import macperms

    if _recording is None:
        _recording = macperms.granted(macperms.SCREEN_RECORDING)
        if not _recording and ask:
            log.warning("mirror: %s", MAC_NO_RECORDING)
            macperms.request(macperms.SCREEN_RECORDING, open_settings_too=False)
    return _recording


_listed_ok = True


def mac_grab(x: float, y: float, w: float, h: float) -> QImage | None:
    """macOS: the screen in a rectangle of points, through Quartz, without
    this process's own windows (the edge indicator, mirrors), or None."""
    try:
        import Quartz
    except ImportError:  # pragma: no cover - macOS only
        return None
    create = getattr(Quartz, "CGWindowListCreateImage", None)
    if create is None:  # pragma: no cover - macOS only
        return None
    try:
        rect = Quartz.CGRectMake(x, y, w, h)
        cg = None
        listed = getattr(Quartz, "CGWindowListCreateImageFromArray", None)
        global _listed_ok
        if listed is not None and _listed_ok:
            try:
                import os

                me = os.getpid()
                infos = Quartz.CGWindowListCopyWindowInfo(Quartz.kCGWindowListOptionOnScreenOnly,
                                                          Quartz.kCGNullWindowID) or []
                ids = [int(i["kCGWindowNumber"]) for i in infos
                       if int(i.get("kCGWindowOwnerPID", -1)) != me and "kCGWindowNumber" in i]
                if ids:
                    cg = listed(rect, ids, Quartz.kCGWindowImageDefault)
            except Exception:  # pragma: no cover - macOS only
                log.warning("mirror: couldn't leave Curslink's own windows out of the capture", exc_info=True)
                _listed_ok = False
        if cg is None:
            cg = create(rect, Quartz.kCGWindowListOptionOnScreenOnly, Quartz.kCGNullWindowID,
                        Quartz.kCGWindowImageDefault)
        if cg is None:
            return None
        return cgimage_to_qimage(Quartz, cg)
    except Exception:  # pragma: no cover - macOS only
        log.debug("mirror: Quartz couldn't grab the screen", exc_info=True)
        return None


# CGBitmapInfo: the byte order bits, and CGImageAlphaInfo values.
_CG_ORDER_MASK, _CG_ORDER_32_LITTLE = 0x7000, 0x2000
_CG_ALPHA_LAST = (1, 3, 5)  # premultiplied last, last, none skip last


def cgimage_to_qimage(quartz, cg) -> QImage | None:
    """A 32-bit CGImage as a QImage (a copy)."""
    w, h = int(quartz.CGImageGetWidth(cg)), int(quartz.CGImageGetHeight(cg))
    if w <= 0 or h <= 0 or int(quartz.CGImageGetBitsPerPixel(cg)) != 32:
        return None
    bpr = int(quartz.CGImageGetBytesPerRow(cg))
    info = int(quartz.CGImageGetBitmapInfo(cg))
    alpha = int(quartz.CGImageGetAlphaInfo(cg))
    if info & _CG_ORDER_MASK == _CG_ORDER_32_LITTLE:
        fmt = QImage.Format.Format_ARGB32_Premultiplied  # BGRA in memory, like Qt's ARGB32 here
    elif alpha in _CG_ALPHA_LAST:
        fmt = QImage.Format.Format_RGBA8888_Premultiplied
    else:
        return None
    data = bytes(quartz.CGDataProviderCopyData(quartz.CGImageGetDataProvider(cg)))
    if len(data) < bpr * h:
        return None
    return QImage(data, w, h, bpr, fmt).copy()


class Capture:
    """Grabs one covered rectangle ``fps`` times a second and prints it."""

    def __init__(self, spec: dict, out):
        self.id = spec["id"]
        self.out = out
        self.spec = spec
        self.quality = 72
        self.scale = 1.0
        self.last_hash = b""
        self.last_sent = 0.0
        self.failed = False
        self.why = ""
        self.timer = QTimer()
        self.timer.timeout.connect(self.grab)
        self.set(spec)

    def set(self, spec: dict) -> None:
        if spec.get("os") != self.spec.get("os"):
            self.last_hash = b""
        self.spec = spec
        fps = max(1, min(30, int(spec.get("fps", 15) or 15)))
        self.timer.start(round(1000 / fps))
        QTimer.singleShot(0, self.grab)

    def image(self) -> QImage | None:
        found = os_to_logical(_screens(), self.spec.get("os"), sys.platform == "win32")
        if found is None:
            self.why = "no screen of this computer is there"
            return None
        lx, ly, lw, lh, index = found
        if sys.platform == "darwin" and not mac_screen_recording(ask=True):
            self.why = MAC_NO_RECORDING
            return None
        if sys.platform == "darwin":
            # Through Quartz first: it can leave out this process's own
            # windows (the edge indicator), which would show up twice.
            img = mac_grab(lx, ly, lw, lh)
            if img is not None:
                return img
        qscreen = QGuiApplication.screens()[index]
        g = qscreen.geometry()
        # Relative to the screen, in Qt's logical pixels; the pixmap comes
        # back at the screen's real resolution.
        pix = qscreen.grabWindow(0, round(lx - g.x()), round(ly - g.y()), max(1, round(lw)), max(1, round(lh)))
        if not pix.isNull() and pix.width() > 0:
            return pix.toImage()
        self.why = "the screen grab came back empty"
        return None

    def grab(self) -> None:
        try:
            img = self.image()
        except Exception:  # pragma: no cover - platform capture trouble
            if not self.failed:
                log.exception("mirror: couldn't capture %s", self.spec.get("os"))
            self.failed = True
            return
        if img is None:
            if not self.failed:
                log.warning("mirror: couldn't capture %s: %s", self.spec.get("os"), self.why)
            self.failed = True
            return
        if self.failed or not self.last_sent:
            log.info("mirror: capturing %s at %dx%d", self.spec.get("os"), img.width(), img.height())
        self.failed = False
        small = img.scaled(64, 36, Qt.AspectRatioMode.IgnoreAspectRatio, Qt.TransformationMode.FastTransformation)
        small = small.convertToFormat(QImage.Format.Format_RGB32)
        digest = hashlib.blake2b(bytes(small.constBits())[: small.sizeInBytes()], digest_size=12).digest()
        now = time.monotonic()
        if digest == self.last_hash and now - self.last_sent < REFRESH_S:
            return
        raw, self.quality, self.scale = fit_jpeg(img, self.quality, self.scale)
        if self.quality < 72 and len(raw) * 4 / 3 < MAX_B64 * 0.6:
            self.quality = min(72, self.quality + 4)  # room again: creep back up
        self.last_hash, self.last_sent = digest, now
        frame = {"id": self.id, "jpg": base64.b64encode(raw).decode("ascii"), "w": img.width(), "h": img.height()}
        self.out("mirror_frame " + json.dumps(frame, separators=(",", ":")))

    def stop(self) -> None:
        self.timer.stop()


ARROW = [(0, 0), (0, 17.5), (4.4, 13.4), (7.4, 20.6), (10.4, 19.4), (7.5, 12.4), (13.2, 12.4)]


class MirrorWindow(QWidget):
    def __init__(self, spec: dict):
        flags = (Qt.WindowType.FramelessWindowHint | Qt.WindowType.Tool | Qt.WindowType.WindowStaysOnTopHint
                 | Qt.WindowType.WindowTransparentForInput | Qt.WindowType.WindowDoesNotAcceptFocus
                 | Qt.WindowType.NoDropShadowWindowHint)
        if sys.platform.startswith("linux"):
            flags |= Qt.WindowType.X11BypassWindowManagerHint
        super().__init__(None, flags)
        for attr in (Qt.WidgetAttribute.WA_TranslucentBackground, Qt.WidgetAttribute.WA_ShowWithoutActivating,
                     Qt.WidgetAttribute.WA_TransparentForMouseEvents, Qt.WidgetAttribute.WA_MacAlwaysShowToolWindow):
            self.setAttribute(attr, True)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        from curslink.capturehide import exclude_from_capture

        exclude_from_capture(self)  # a mirror never ends up in another one
        self.pixmap: QPixmap | None = None
        self.pointer: tuple[float, float] | None = None
        self.spec: dict = {}
        self.shown_at = 0.0
        self.closing_at = 0.0
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.tick)
        self.ok = self.set(spec)

    def set(self, spec: dict) -> bool:
        found = os_to_logical(_screens(), spec.get("os"), sys.platform == "win32")
        if found is None:
            return False
        lx, ly, lw, lh, index = found
        self.spec = spec
        self.closing_at = 0.0
        screen = QGuiApplication.screens()[index]
        if self.screen() is not screen:
            self.setScreen(screen)
        self.setGeometry(QRect(round(lx), round(ly), max(1, round(lw)), max(1, round(lh))))
        if self.isVisible() and self.windowOpacity() < 1:
            self.timer.start(16)
        return True

    def image(self, data: dict) -> None:
        try:
            raw = base64.b64decode(data.get("jpg", ""), validate=True)
        except (ValueError, TypeError):
            return
        pix = QPixmap()
        if not pix.loadFromData(raw, "JPG"):
            return
        self.pixmap = pix
        if not self.isVisible() or self.closing_at:
            self.closing_at = 0.0
            if not self.isVisible():
                self.shown_at = time.monotonic()
                self.setWindowOpacity(0.0)
                self.show()
                self.raise_()
            self.timer.start(16)
        self.update()

    def set_pointer(self, p: tuple[float, float] | None) -> None:
        if p != self.pointer:
            self.pointer = p
            self.update()

    def close_soon(self) -> None:
        if not self.isVisible():
            self.deleteLater()
            return
        if not self.closing_at:
            self.closing_at = time.monotonic()
            self.timer.start(16)

    def tick(self) -> None:
        now = time.monotonic()
        if self.closing_at:
            k = (now - self.closing_at) / FADE_S
            if k >= 1:
                self.timer.stop()
                self.hide()
                self.deleteLater()
                return
            self.setWindowOpacity(1 - k)
            return
        age = now - self.shown_at
        self.setWindowOpacity(min(1.0, age / FADE_S))
        if age > TAG_S + 0.6:
            self.timer.stop()
        self.update()

    def paintEvent(self, _e) -> None:  # noqa: N802
        if self.pixmap is None:
            return
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        r = QRectF(self.rect())
        p.drawPixmap(r, self.pixmap, QRectF(self.pixmap.rect()))
        age = time.monotonic() - self.shown_at
        edges = set(self.spec.get("edges") or [])
        lines = {
            "left": (r.topLeft() + QPointF(0.75, 0), r.bottomLeft() + QPointF(0.75, 0)),
            "right": (r.topRight() - QPointF(0.75, 0), r.bottomRight() - QPointF(0.75, 0)),
            "top": (r.topLeft() + QPointF(0, 0.75), r.topRight() + QPointF(0, 0.75)),
            "bottom": (r.bottomLeft() - QPointF(0, 0.75), r.bottomRight() - QPointF(0, 0.75)),
        }
        # Where the mirror ends inside the screen in front: a thin glass edge,
        # flashing in the accent colour as it appears.
        for side in edges & set(lines):
            a, b = lines[side]
            p.setPen(QPen(QColor(0, 0, 0, 60), 3))
            p.drawLine(a, b)
            p.setPen(QPen(QColor(255, 255, 255, 200), 1.5))
            p.drawLine(a, b)
            if age < FLASH_S:
                accent = QColor(ACCENT)
                accent.setAlphaF(1 - age / FLASH_S)
                p.setPen(QPen(accent, 4))
                p.drawLine(a, b)
        if self.pointer is not None:
            self.paint_pointer(p, QPointF(r.x() + self.pointer[0] * r.width(), r.y() + self.pointer[1] * r.height()))
        if age < TAG_S + 0.5:
            self.paint_tag(p, r, 1.0 if age < TAG_S else max(0.0, 1 - (age - TAG_S) / 0.5))
        p.end()

    def paint_pointer(self, p: QPainter, at: QPointF) -> None:
        path = QPainterPath()
        path.moveTo(at + QPointF(*ARROW[0]))
        for x, y in ARROW[1:]:
            path.lineTo(at + QPointF(x, y))
        path.closeSubpath()
        p.setPen(QPen(QColor(0, 0, 0, 70), 3.2))
        p.drawPath(path.translated(0.6, 1.0))
        p.setPen(QPen(QColor(17, 17, 17), 1.3))
        p.setBrush(QColor("white"))
        p.drawPath(path)

    def paint_tag(self, p: QPainter, r: QRectF, alpha: float) -> None:
        text = f"{self.spec.get('label') or 'Behind'} · live"
        font = QFont()
        font.setPixelSize(12)
        font.setWeight(QFont.Weight.DemiBold)
        p.setFont(font)
        m = QFontMetricsF(font)
        w, h = m.horizontalAdvance(text) + 26, m.height() + 10
        if w + 16 > r.width() or h + 16 > r.height():
            return
        tag = QRectF(r.right() - w - 10, r.top() + 10, w, h)
        p.setOpacity(alpha)
        p.setPen(QPen(QColor(255, 255, 255, 110), 1))
        grad = QLinearGradient(tag.topLeft(), tag.bottomLeft())
        grad.setColorAt(0, QColor(60, 60, 70, 175))
        grad.setColorAt(1, QColor(25, 25, 30, 175))
        p.setBrush(grad)
        p.drawRoundedRect(tag, h / 2, h / 2)
        p.setBrush(QColor(ACCENT))
        p.setPen(Qt.PenStyle.NoPen)
        p.drawEllipse(QPointF(tag.left() + 11, tag.center().y()), 3, 3)
        p.setPen(QColor("white"))
        p.drawText(tag.adjusted(18, 0, -6, 0), Qt.AlignmentFlag.AlignCenter, text)
        p.setOpacity(1.0)


def _stdout(line: str) -> None:
    sys.stdout.write(line + "\n")
    sys.stdout.flush()


class MirrorHelper:
    """The helper's mirror commands."""

    def __init__(self, out=_stdout):
        self.out = out
        self.captures: dict[str, Capture] = {}
        self.windows: dict[str, MirrorWindow] = {}

    def handle(self, line: str) -> bool:
        word, _, rest = line.strip().partition(" ")
        if word not in ("mirror", "mirror_image", "mirror_pointer"):
            return False
        try:
            data = json.loads(rest)
        except json.JSONDecodeError:
            return True
        if not isinstance(data, dict):
            return True
        if word == "mirror":
            self.set(data)
        elif word == "mirror_image":
            window = self.windows.get(data.get("id"))
            if window is not None:
                window.image(data)
        else:
            window = self.windows.get(data.get("id"))
            if window is not None:
                ok = isinstance(data.get("x"), (int, float)) and isinstance(data.get("y"), (int, float))
                window.set_pointer((float(data["x"]), float(data["y"])) if ok and not data.get("hide") else None)
        return True

    def set(self, state: dict) -> None:
        captures = {c["id"]: c for c in state.get("captures", []) if isinstance(c, dict) and isinstance(c.get("id"), str)}
        shows = {s["id"]: s for s in state.get("shows", []) if isinstance(s, dict) and isinstance(s.get("id"), str)}
        for sid in list(self.captures):
            if sid not in captures:
                self.captures.pop(sid).stop()
        for sid, spec in captures.items():
            if sid in self.captures:
                self.captures[sid].set(spec)
            else:
                self.captures[sid] = Capture(spec, self.out)
        for sid in list(self.windows):
            if sid not in shows:
                self.windows.pop(sid).close_soon()
        for sid, spec in shows.items():
            window = self.windows.get(sid)
            if window is None:
                window = MirrorWindow(spec)
                if not window.ok:
                    log.warning("mirror: no screen here at %s", spec.get("os"))
                    window.deleteLater()
                    continue
                self.windows[sid] = window
            elif not window.set(spec):
                self.windows.pop(sid).close_soon()
