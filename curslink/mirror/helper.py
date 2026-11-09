"""Mirror mode in the overlay helper process: capture covered areas, draw mirrors.

Commands (one line each, from :class:`curslink.mirror.MirrorPeer`)::

    mirror {"captures": [{"id", "screen", "os": [x, y, w, h], "fps", "cursor"?, "tiles"?}],
            "shows": [{"id", "screen", "os": [x, y, w, h], "label", "edges"}]}
    mirror_image {"id", "jpg", "w", "h", "tile"?}
    mirror_pointer {"id", "x", "y"} | {"id", "hide": true}
    mirror_shape {"id", "png", "w", "h", "hx", "hy", "cw"}

A capture with ``cursor`` draws this computer's real cursor (its picture
and hot spot, :mod:`curslink.mirror.cursorshape`) into each frame where it
is, so the mirror casts it: the same look, in the same place, and no copy
drawn on top. ``mirror_pointer`` and ``mirror_shape`` (a drawn pointer and
its look) are only for an older agent behind, which can't.

A capture with ``tiles`` sends the area at this screen's own pixels (never
scaled down), cut into :data:`TILE` px tiles, each a sharp JPEG with
``"tile": [x, y, w, h]`` and only when it changed; the mirror puts them
together and scales the whole once, onto the screen in front. Without it
(an older device in front) a frame is the whole area, made small enough for
one protocol frame.

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
from PySide6.QtGui import (QColor, QCursor, QFont, QFontMetricsF, QGuiApplication, QImage, QLinearGradient,
                           QPainter, QPainterPath, QPen, QPixmap)
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
# Tiles: their side in the screen's own pixels, the JPEG quality they start
# at, and a resend of all of them this often (in case one went missing).
TILE = 256
TILE_QUALITY = 90
TILE_REFRESH_S = 6.0
# The pointer's look is sent while it's this close to the captured area
# (logical pixels), so it's right as soon as it gets there.
CURSOR_NEAR = 48


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


def _qscreen(screen_id: str, whole: tuple[float, float, float, float]):
    """This computer's QScreen for a layout screen: "main" is the primary,
    "screenN" the Nth (as Layout > Detect names them), else by display name,
    else the one with its middle where the OS says (logical or physical)."""
    screens = QGuiApplication.screens()
    if screen_id == "main":
        return QGuiApplication.primaryScreen()
    if screen_id.startswith("screen") and screen_id[6:].isdigit() and 0 < int(screen_id[6:]) <= len(screens):
        return screens[int(screen_id[6:]) - 1]
    for sc in screens:
        if sc.name() == screen_id:
            return sc
    x, y, w, h = whole
    cx, cy = x + w / 2, y + h / 2
    for sc in screens:
        g, r = sc.geometry(), sc.devicePixelRatio() or 1.0
        for k in (1.0, r):
            if g.x() * k <= cx < (g.x() + g.width()) * k and g.y() * k <= cy < (g.y() + g.height()) * k:
                return sc
    return None


def spec_to_logical(spec: dict) -> tuple[float, float, float, float, int] | None:
    """Where a capture's or mirror's ``os`` rectangle is, in Qt's logical
    pixels, and on which screen. With ``screen_os`` (the whole screen as the
    host's layout has it) it goes by fractions of that screen, like the
    pointer and the calibration layers do: Windows' scaling makes a screen's
    size differ between processes, and the layout may have either."""
    whole = spec.get("screen_os")
    if isinstance(whole, list) and len(whole) == 4:
        try:
            ox, oy, ow, oh = (float(v) for v in whole)
            x, y, w, h = (float(v) for v in spec.get("os"))
            screens = QGuiApplication.screens()
            qs = _qscreen(str(spec.get("screen", "")), (ox, oy, ow, oh))
            if qs is not None and ow > 0 and oh > 0 and w > 0 and h > 0:
                g = qs.geometry()
                kx, ky = g.width() / ow, g.height() / oh
                return g.x() + (x - ox) * kx, g.y() + (y - oy) * ky, w * kx, h * ky, screens.index(qs)
        except (TypeError, ValueError):
            pass
    return os_to_logical(_screens(), spec.get("os"), sys.platform == "win32")


def fit_jpeg(image: QImage, quality: int, scale: float, limit: int = MAX_B64) -> tuple[bytes, int, float]:
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
        if len(raw) * 4 / 3 + 4 <= limit:
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
# Whether the last mac_grab already has the cursor in it: macOS can draw it
# as a window of its own (WindowServer's "Cursor", at the cursor level, as
# with a bigger or coloured pointer), which a grab then takes in. Then it's
# cast as it is, and not drawn in a second time.
mac_grab_has_cursor = False
_said_cursor: bool | None = None


def mac_cursor_windows(quartz, infos) -> list[int]:
    """The ids of the windows in ``infos`` that are the cursor itself."""
    level = None
    try:
        level = int(quartz.CGWindowLevelForKey(quartz.kCGCursorWindowLevelKey))
    except Exception:  # pragma: no cover - macOS only
        pass
    out = []
    for i in infos:
        name = str(i.get("kCGWindowName") or "")
        owner = str(i.get("kCGWindowOwnerName") or "")
        layer = i.get("kCGWindowLayer")
        if "kCGWindowNumber" in i and ((level is not None and layer is not None and int(layer) >= level)
                                       or (owner == "WindowServer" and "cursor" in name.lower())):
            out.append(int(i["kCGWindowNumber"]))
    return out


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
        global mac_grab_has_cursor, _said_cursor
        mac_grab_has_cursor = False
        if listed is not None and _listed_ok:
            try:
                import os

                me = os.getpid()
                infos = Quartz.CGWindowListCopyWindowInfo(Quartz.kCGWindowListOptionOnScreenOnly,
                                                          Quartz.kCGNullWindowID) or []
                ids = [int(i["kCGWindowNumber"]) for i in infos
                       if int(i.get("kCGWindowOwnerPID", -1)) != me and "kCGWindowNumber" in i]
                cursor = set(mac_cursor_windows(Quartz, infos)) & set(ids)
                mac_grab_has_cursor = bool(cursor)
                if mac_grab_has_cursor != _said_cursor:
                    _said_cursor = mac_grab_has_cursor
                    log.info("mirror: the capture %s the cursor itself", "shows" if cursor else "doesn't show")
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
        self.guides: GuideOverlay | None = None
        self.area: tuple[float, float, float, float, int] | None = None  # where it captures, logical
        self.cursor_key: str | None = None  # the pointer's look the last frame carried
        self.tiles: dict[tuple[int, int], bytes] = {}  # each tile's hash, as last sent
        self.tiles_size = (0, 0)
        self.tiles_sent = 0.0  # last full send of every tile
        self.cursor_cache: tuple[str, QImage] | None = None
        self.timer = QTimer()
        self.timer.timeout.connect(self.grab)
        self.set(spec)

    def set(self, spec: dict) -> None:
        if spec.get("os") != self.spec.get("os") or spec.get("tiles") != self.spec.get("tiles"):
            self.last_hash = b""
            self.cursor_key = None
            self.tiles = {}
        self.spec = spec
        self.show_guides(bool(spec.get("tune")))
        fps = max(1, min(30, int(spec.get("fps", 30) or 30)))
        self.timer.start(round(1000 / fps))
        QTimer.singleShot(0, self.grab)

    def image(self) -> QImage | None:
        self.has_cursor = False  # the grab has the cursor in it already
        found = spec_to_logical(self.spec)
        if found is None:
            self.why = "no screen of this computer is there"
            return None
        lx, ly, lw, lh, index = found
        self.area = found
        if sys.platform == "darwin" and not mac_screen_recording(ask=True):
            self.why = MAC_NO_RECORDING
            return None
        if sys.platform == "darwin":
            # Through Quartz first: it can leave out this process's own
            # windows (the edge indicator), which would show up twice.
            img = mac_grab(lx, ly, lw, lh)
            if img is not None:
                self.has_cursor = mac_grab_has_cursor
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
        cast = None
        if self.spec.get("cursor") and not self.spec.get("tune") and not getattr(self, "has_cursor", False):
            img, cast = self.draw_cursor(img)
        now = time.monotonic()
        if self.spec.get("tiles"):
            self.send_tiles(img, now)
            return
        small = img.scaled(64, 36, Qt.AspectRatioMode.IgnoreAspectRatio, Qt.TransformationMode.FastTransformation)
        small = small.convertToFormat(QImage.Format.Format_RGB32)
        digest = hashlib.blake2b(bytes(small.constBits())[: small.sizeInBytes()] + repr(cast).encode(),
                                 digest_size=12).digest()
        if digest == self.last_hash and now - self.last_sent < REFRESH_S:
            return
        raw, self.quality, self.scale = fit_jpeg(img, self.quality, self.scale)
        if self.quality < 72 and len(raw) * 4 / 3 < MAX_B64 * 0.6:
            self.quality = min(72, self.quality + 4)  # room again: creep back up
        self.last_hash, self.last_sent = digest, now
        frame = {"id": self.id, "jpg": base64.b64encode(raw).decode("ascii"), "w": img.width(), "h": img.height()}
        self.out("mirror_frame " + json.dumps(frame, separators=(",", ":")))

    def send_tiles(self, img: QImage, now: float) -> None:
        """The tiles of ``img`` that changed (all of them now and then)."""
        w, h = img.width(), img.height()
        if (w, h) != self.tiles_size or now - self.tiles_sent >= TILE_REFRESH_S:
            self.tiles, self.tiles_size, self.tiles_sent = {}, (w, h), now
        if img.format() != QImage.Format.Format_RGB32:
            img = img.convertToFormat(QImage.Format.Format_RGB32)
        for y in range(0, h, TILE):
            for x in range(0, w, TILE):
                part = img.copy(x, y, min(TILE, w - x), min(TILE, h - y))
                digest = hashlib.blake2b(bytes(part.constBits())[: part.sizeInBytes()], digest_size=12).digest()
                if self.tiles.get((x, y)) == digest:
                    continue
                self.tiles[(x, y)] = digest
                raw, _q, _s = fit_jpeg(part, TILE_QUALITY, 1.0)
                frame = {"id": self.id, "jpg": base64.b64encode(raw).decode("ascii"), "w": w, "h": h,
                         "tile": [x, y, part.width(), part.height()]}
                self.out("mirror_frame " + json.dumps(frame, separators=(",", ":")))
        self.last_sent = now

    def draw_cursor(self, img: QImage) -> tuple[QImage, tuple | None]:
        """``img`` with this computer's cursor drawn in where it is (if it's
        over the area and showing), and what was drawn (look and place)."""
        if self.area is None:
            return img, None
        lx, ly, lw, lh, index = self.area
        pos = QCursor.pos()
        if not (lx - CURSOR_NEAR <= pos.x() <= lx + lw + CURSOR_NEAR
                and ly - CURSOR_NEAR <= pos.y() <= ly + lh + CURSOR_NEAR):
            return img, None
        from curslink.mirror import cursorshape

        screens = QGuiApplication.screens()
        dpr = screens[index].devicePixelRatio() if 0 <= index < len(screens) else 1.0
        found = cursorshape.current(dpr)
        if found is None:
            return img, None  # hidden, or its look can't be read
        if self.cursor_cache is None or self.cursor_cache[0] != found["key"]:
            look = QImage()
            if not look.loadFromData(base64.b64decode(found["png"]), "PNG"):
                return img, None
            self.cursor_cache = (found["key"], look)
        look = self.cursor_cache[1]
        k = img.width() / max(1e-6, lw)  # the screen's pixels per logical pixel
        at = QRectF((pos.x() - lx - found["hx"]) * k, (pos.y() - ly - found["hy"]) * k, found["w"] * k, found["h"] * k)
        if img.format() not in (QImage.Format.Format_RGB32, QImage.Format.Format_ARGB32_Premultiplied):
            img = img.convertToFormat(QImage.Format.Format_ARGB32_Premultiplied)
        else:
            img = img.copy()
        p = QPainter(img)
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        p.drawImage(at, look, QRectF(look.rect()))
        p.end()
        return img, (found["key"], round(at.x()), round(at.y()))

    def show_guides(self, on: bool) -> None:
        """While its mirror is being adjusted, the guides across this screen."""
        if on and self.guides is None:
            self.guides = GuideOverlay(self.spec)
            if not self.guides.ok:
                self.guides.deleteLater()
                self.guides = None
        elif on:
            self.guides.set(self.spec)
        elif self.guides is not None:
            self.guides.hide()
            self.guides.deleteLater()
            self.guides = None

    def stop(self) -> None:
        self.timer.stop()
        self.show_guides(False)


# Alignment guides while a mirror is being adjusted: the same lines and ring
# drawn across the screen behind and on the mirror, so you move, stretch and
# zoom it until they carry on unbroken past the screen in front's bezel.
GUIDE_COLORS = {"h": QColor(56, 225, 255), "v": QColor(255, 79, 216), "d": QColor(255, 214, 10),
                "o": QColor(93, 255, 122)}


def guide_shapes(c: QRectF) -> list[tuple[str, QPointF, QPointF]]:
    """The guides for a covered area ``c`` (in the painter's coordinates):
    three lines across and three down through it, and its two diagonals,
    each carried on far past it. ("o", centre, (r, r)) is the ring, crossing
    all four of its sides."""
    import math

    w, h = max(1.0, c.width()), max(1.0, c.height())
    far = 20 * max(w, h) + 10_000
    out = []
    for f in (0.25, 0.5, 0.75):
        y = c.top() + f * h
        out.append(("h", QPointF(c.left() - far, y), QPointF(c.right() + far, y)))
        x = c.left() + f * w
        out.append(("v", QPointF(x, c.top() - far), QPointF(x, c.bottom() + far)))
    n = far / w
    out.append(("d", c.topLeft() - QPointF(w, h) * n, c.bottomRight() + QPointF(w, h) * n))
    out.append(("d", c.topRight() + QPointF(w, -h) * n, c.bottomLeft() - QPointF(w, -h) * n))
    r = (min(w, h) / 2 + math.hypot(w, h) / 2) / 2  # between the inner and outer circles
    out.append(("o", c.center(), QPointF(r, r)))
    return out


def paint_guides(p: QPainter, c: QRectF, width: float = 2.0) -> None:
    p.save()
    p.setBrush(Qt.BrushStyle.NoBrush)
    shapes = guide_shapes(c)
    for halo in (True, False):
        for kind, a, b in shapes:
            p.setPen(QPen(QColor(0, 0, 0, 150), width + 2.5) if halo else QPen(GUIDE_COLORS[kind], width))
            if kind == "o":
                p.drawEllipse(a, b.x(), b.y())
            else:
                p.drawLine(a, b)
    p.restore()


class GuideOverlay(QWidget):
    """The guides over the whole screen behind, while its mirror is being
    adjusted: click-through, and kept out of the capture (the mirror draws
    its own, in the same place on the picture)."""

    def __init__(self, spec: dict):
        flags = (Qt.WindowType.FramelessWindowHint | Qt.WindowType.Tool | Qt.WindowType.WindowStaysOnTopHint
                 | Qt.WindowType.WindowDoesNotAcceptFocus | Qt.WindowType.NoDropShadowWindowHint
                 | Qt.WindowType.WindowTransparentForInput)
        if sys.platform.startswith("linux"):
            flags |= Qt.WindowType.X11BypassWindowManagerHint
        super().__init__(None, flags)
        for attr in (Qt.WidgetAttribute.WA_TranslucentBackground, Qt.WidgetAttribute.WA_ShowWithoutActivating,
                     Qt.WidgetAttribute.WA_TransparentForMouseEvents, Qt.WidgetAttribute.WA_MacAlwaysShowToolWindow):
            self.setAttribute(attr, True)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        from curslink.capturehide import exclude_from_capture

        exclude_from_capture(self)
        self.covered = QRectF()
        self.ok = self.set(spec)

    def set(self, spec: dict) -> bool:
        whole = spec.get("screen_os")
        screen = spec_to_logical({**spec, "os": whole}) if isinstance(whole, list) else None
        at = spec.get("guides") if isinstance(spec.get("guides"), list) else spec.get("os")
        found = spec_to_logical({**spec, "os": at})
        if screen is None or found is None:
            return False
        sx, sy, sw, sh, index = screen
        lx, ly, lw, lh, _ = found
        qs = QGuiApplication.screens()[index]
        if self.screen() is not qs:
            self.setScreen(qs)
        self.setGeometry(QRect(round(sx), round(sy), max(1, round(sw)), max(1, round(sh))))
        self.covered = QRectF(lx - round(sx), ly - round(sy), lw, lh)
        if not self.isVisible():
            self.show()
        self.update()
        return True

    def paintEvent(self, _e) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        paint_guides(p, self.covered)
        p.end()


ARROW = [(0, 0), (0, 17.5), (4.4, 13.4), (7.4, 20.6), (10.4, 19.4), (7.5, 12.4), (13.2, 12.4)]


EDGE_GRAB = 14  # px from a mirror's side that stretch it while adjusting


class MirrorWindow(QWidget):
    def __init__(self, spec: dict, out=None):
        # Adjusting (spec "tune"): it takes the pointer to be dragged, stretched
        # and zoomed, and says so on ``out`` as mirror_tune lines.
        self.tune = bool(spec.get("tune"))
        self.out = out
        flags = (Qt.WindowType.FramelessWindowHint | Qt.WindowType.Tool | Qt.WindowType.WindowStaysOnTopHint
                 | Qt.WindowType.WindowDoesNotAcceptFocus | Qt.WindowType.NoDropShadowWindowHint)
        if not self.tune:
            flags |= Qt.WindowType.WindowTransparentForInput
        if sys.platform.startswith("linux"):
            flags |= Qt.WindowType.X11BypassWindowManagerHint
        super().__init__(None, flags)
        for attr in (Qt.WidgetAttribute.WA_TranslucentBackground, Qt.WidgetAttribute.WA_ShowWithoutActivating,
                     Qt.WidgetAttribute.WA_MacAlwaysShowToolWindow):
            self.setAttribute(attr, True)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, not self.tune)
        self.setMouseTracking(self.tune)
        self._drag: tuple[str, QPointF] | None = None  # (which: "move" or the sides, last global point)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        from curslink.capturehide import exclude_from_capture

        exclude_from_capture(self)  # a mirror never ends up in another one
        self.pixmap: QPixmap | None = None
        # Put together from tiles: the picture, and which tiles have come.
        self.canvas: QImage | None = None
        self.got: dict[tuple[int, int], int] = {}
        self.pointer: tuple[float, float] | None = None
        # The pointer's real look on the computer behind: (picture, its size
        # and hot spot in that computer's logical pixels, captured width).
        self.shape: tuple[QPixmap, float, float, float, float, float] | None = None
        self.spec: dict = {}
        self.shown_at = 0.0
        self.closing_at = 0.0
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.tick)
        self.ok = self.set(spec)

    def set(self, spec: dict) -> bool:
        found = spec_to_logical(spec)
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
        self.update()
        return True

    def image(self, data: dict) -> None:
        try:
            raw = base64.b64decode(data.get("jpg", ""), validate=True)
        except (ValueError, TypeError):
            return
        tile = data.get("tile")
        if tile is not None:
            if not self.add_tile(raw, data, tile):
                return
        else:
            pix = QPixmap()
            if not pix.loadFromData(raw, "JPG"):
                return
            self.pixmap, self.canvas, self.got = pix, None, {}
        if not self.isVisible() or self.closing_at:
            self.closing_at = 0.0
            if not self.isVisible():
                self.shown_at = time.monotonic()
                self.setWindowOpacity(0.0)
                self.show()
                self.raise_()
            self.timer.start(16)
        self.update()

    def add_tile(self, raw: bytes, data: dict, tile) -> bool:
        """One tile into the picture: True once the picture is whole."""
        try:
            w, h = int(data["w"]), int(data["h"])
            x, y, tw, th = (int(v) for v in tile)
        except (KeyError, TypeError, ValueError):
            return False
        if not (0 < w <= 16384 and 0 < h <= 16384 and 0 <= x < w and 0 <= y < h and tw > 0 and th > 0):
            return False
        part = QImage()
        if not part.loadFromData(raw, "JPG"):
            return False
        if self.canvas is None or (self.canvas.width(), self.canvas.height()) != (w, h):
            self.canvas = QImage(w, h, QImage.Format.Format_RGB32)
            self.canvas.fill(QColor(0, 0, 0))
            self.got = {}
        p = QPainter(self.canvas)
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        p.drawImage(QRectF(x, y, tw, th), part, QRectF(part.rect()))
        p.end()
        self.got[(x, y)] = tw * th
        if sum(self.got.values()) < w * h:
            return self.isVisible() and not self.closing_at  # not whole yet: show it once it is
        self.pixmap = None
        return True

    def set_pointer(self, p: tuple[float, float] | None) -> None:
        if p != self.pointer:
            self.pointer = p
            self.update()

    def set_shape(self, data: dict | None) -> None:
        self.shape = parse_shape(data) if data else None
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

    # -- adjusting -----------------------------------------------------------------
    def _sides(self, pos: QPointF) -> str:
        w, h = self.width(), self.height()
        out = ""
        if pos.x() < EDGE_GRAB:
            out += "l"
        elif pos.x() > w - EDGE_GRAB:
            out += "r"
        if pos.y() < EDGE_GRAB:
            out += "t"
        elif pos.y() > h - EDGE_GRAB:
            out += "b"
        return out

    def _tell(self, **data) -> None:
        if self.out is not None:
            self.out("mirror_tune " + json.dumps({"id": self.spec.get("id"), **data}, separators=(",", ":")))

    def mousePressEvent(self, e) -> None:  # noqa: N802
        if not self.tune:
            return
        sides = self._sides(e.position())
        self._drag = (sides or "move", e.globalPosition())

    def mouseMoveEvent(self, e) -> None:  # noqa: N802
        if not self.tune:
            return
        if self._drag is None:
            sides = self._sides(e.position())
            shapes = {"l": Qt.CursorShape.SizeHorCursor, "r": Qt.CursorShape.SizeHorCursor,
                      "t": Qt.CursorShape.SizeVerCursor, "b": Qt.CursorShape.SizeVerCursor,
                      "lt": Qt.CursorShape.SizeFDiagCursor, "rb": Qt.CursorShape.SizeFDiagCursor,
                      "rt": Qt.CursorShape.SizeBDiagCursor, "lb": Qt.CursorShape.SizeBDiagCursor}
            self.setCursor(shapes.get(sides, Qt.CursorShape.OpenHandCursor))
            return
        which, last = self._drag
        now = e.globalPosition()
        dx, dy = now.x() - last.x(), now.y() - last.y()
        if abs(dx) < 1 and abs(dy) < 1:
            return
        self._drag = (which, now)
        w, h = max(1, self.width()), max(1, self.height())
        if which == "move":
            self._tell(mx=round(dx / w, 5), my=round(dy / h, 5))
            return
        # Stretching a side: bigger by how far it moved, about the opposite side.
        factors, ax, ay = [], 0.5, 0.5
        if "r" in which:
            factors.append((w + dx) / w)
            ax = 0.0
        if "l" in which:
            factors.append((w - dx) / w)
            ax = 1.0
        if "b" in which:
            factors.append((h + dy) / h)
            ay = 0.0
        if "t" in which:
            factors.append((h - dy) / h)
            ay = 1.0
        factor = sum(factors) / len(factors)
        if factor > 0:
            self._tell(grow=round(factor, 5), ax=ax, ay=ay)

    def mouseReleaseEvent(self, _e) -> None:  # noqa: N802
        self._drag = None

    def wheelEvent(self, e) -> None:  # noqa: N802
        if not self.tune:
            return
        steps = e.angleDelta().y() / 120 or e.pixelDelta().y() / 40
        if not steps:
            return
        pos = e.position()
        self._tell(grow=round(1.05 ** steps, 5), ax=round(pos.x() / max(1, self.width()), 4),
                   ay=round(pos.y() / max(1, self.height()), 4))

    def paintEvent(self, _e) -> None:  # noqa: N802
        whole = self.canvas is not None and sum(self.got.values()) >= self.canvas.width() * self.canvas.height()
        if self.pixmap is None and not whole:
            return
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        r = QRectF(self.rect())
        if whole:
            # The screen behind's own pixels, scaled once onto this screen's.
            p.drawImage(r, self.canvas, QRectF(self.canvas.rect()))
        else:
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
        if self.tune:
            paint_guides(p, self.guide_rect(r))
            pen = QPen(QColor(ACCENT), 3, Qt.PenStyle.DashLine)
            p.setPen(pen)
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawRect(r.adjusted(1.5, 1.5, -1.5, -1.5))
            self.paint_tag(p, r, 1.0, "Line up the coloured lines · drag to move · scroll to zoom · drag a side to stretch")
        elif age < TAG_S + 0.5:
            self.paint_tag(p, r, 1.0 if age < TAG_S else max(0.0, 1 - (age - TAG_S) / 0.5))
        p.end()

    def guide_rect(self, r: QRectF) -> QRectF:
        """Where the guides' area (spec ``guides``, on the screen behind) is
        on this mirror, which shows spec ``src`` of there."""
        try:
            gx, gy, gw, gh = (float(v) for v in self.spec["guides"])
            x, y, w, h = (float(v) for v in self.spec["src"])
        except (KeyError, TypeError, ValueError):
            return r
        if w <= 0 or h <= 0:
            return r
        kx, ky = r.width() / w, r.height() / h
        return QRectF(r.x() + (gx - x) * kx, r.y() + (gy - y) * ky, gw * kx, gh * ky)

    def paint_pointer(self, p: QPainter, at: QPointF) -> None:
        if self.shape is not None:
            pix, w, h, hx, hy, cw = self.shape
            k = self.width() / cw
            p.drawPixmap(QRectF(at.x() - hx * k, at.y() - hy * k, w * k, h * k), pix, QRectF(pix.rect()))
            return
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

    def paint_tag(self, p: QPainter, r: QRectF, alpha: float, text: str = "") -> None:
        text = text or f"{self.spec.get('label') or 'Behind'} · live"
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


def parse_shape(data: dict) -> tuple[QPixmap, float, float, float, float, float] | None:
    """A ``mirror_shape`` as (picture, w, h, hx, hy, cw), or None if it's not usable."""
    try:
        w, h, hx, hy, cw = (float(data[k]) for k in ("w", "h", "hx", "hy", "cw"))
        raw = base64.b64decode(data.get("png", ""), validate=True)
    except (KeyError, TypeError, ValueError):
        return None
    if min(w, h, cw) <= 0 or max(w, h) > 512:
        return None
    pix = QPixmap()
    if not pix.loadFromData(raw, "PNG"):
        return None
    return pix, w, h, hx, hy, cw


def _stdout(line: str) -> None:
    sys.stdout.write(line + "\n")
    sys.stdout.flush()


class MirrorHelper:
    """The helper's mirror commands."""

    def __init__(self, out=_stdout):
        self.out = out
        self.captures: dict[str, Capture] = {}
        self.windows: dict[str, MirrorWindow] = {}
        self.shapes: dict[str, dict] = {}  # the pointer's look behind each mirror

    def handle(self, line: str) -> bool:
        word, _, rest = line.strip().partition(" ")
        if word not in ("mirror", "mirror_image", "mirror_pointer", "mirror_shape"):
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
        elif word == "mirror_shape":
            sid = data.get("id")
            if isinstance(sid, str):
                self.shapes[sid] = data
                window = self.windows.get(sid)
                if window is not None:
                    window.set_shape(data)
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
        self.shapes = {sid: d for sid, d in self.shapes.items() if sid in shows}
        for sid, spec in shows.items():
            window = self.windows.get(sid)
            old = None
            if window is not None and window.tune != bool(spec.get("tune")):
                # Taking the pointer or not is fixed when a window is made.
                old = self.windows.pop(sid)
                old.close_soon()
                window = None
            if window is None:
                window = MirrorWindow(spec, self.out)
                if not window.ok:
                    log.warning("mirror: no screen here at %s", spec.get("os"))
                    window.deleteLater()
                    continue
                if sid in self.shapes:
                    window.set_shape(self.shapes[sid])
                if old is not None and (old.pixmap is not None or old.canvas is not None):
                    window.pixmap, window.canvas, window.got = old.pixmap, old.canvas, dict(old.got)
                    window.shown_at = time.monotonic() - TAG_S - 1  # no "live" tag again
                    window.show()
                self.windows[sid] = window
            elif not window.set(spec):
                self.windows.pop(sid).close_soon()
