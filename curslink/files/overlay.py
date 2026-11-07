"""The overlay helper's part in dragging files (Qt only).

Commands on the helper's stdin (see :mod:`curslink.overlay`):

    strips [{"x", "y", "w", "h", "side"}, ...]   host: drop targets along these
    strips off                                   screen sides (OS coordinates)
    shield on | shield off                       host: a drop target over every screen
    card {json} | card hide                      agent: the placeholder that
                                                 follows the pointer
    toast {json}                                 agent: the "Receiving..." popup

Drop targets are windows painted almost (not quite: a fully clear window
lets drags fall through on Windows) transparent. When a drag enters one the
helper prints ``drag {"paths": [...]}`` on stdout (``[]`` for a drag that
isn't of files). A drop on one is always refused, so the app the file came
from does nothing with it.
"""

from __future__ import annotations

import json
import os
import sys
import time

CARD_W, CARD_H = 260, 64
TOAST_W, TOAST_H = 320, 76
ACCENT = "#3fa9ff"


def _say(line: str) -> None:
    try:
        sys.stdout.write(line + "\n")
        sys.stdout.flush()
    except (OSError, ValueError, AttributeError):
        pass


def _flags(click_through: bool):
    from PySide6.QtCore import Qt

    flags = (Qt.WindowType.FramelessWindowHint | Qt.WindowType.Tool | Qt.WindowType.WindowStaysOnTopHint
             | Qt.WindowType.WindowDoesNotAcceptFocus | Qt.WindowType.NoDropShadowWindowHint)
    if click_through:
        flags |= Qt.WindowType.WindowTransparentForInput
    if sys.platform.startswith("linux"):
        flags |= Qt.WindowType.X11BypassWindowManagerHint
    return flags


def _overlay_attrs(w, click_through: bool) -> None:
    from PySide6.QtCore import Qt

    attrs = [Qt.WidgetAttribute.WA_TranslucentBackground, Qt.WidgetAttribute.WA_ShowWithoutActivating,
             Qt.WidgetAttribute.WA_MacAlwaysShowToolWindow]
    if click_through:
        attrs.append(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
    for attr in attrs:
        w.setAttribute(attr, True)
    w.setFocusPolicy(Qt.FocusPolicy.NoFocus)


def local_paths(mime) -> list[str]:
    if mime is None or not mime.hasUrls():
        return []
    # Qt writes Windows paths with forward slashes; hand on the OS's own form.
    return [os.path.normpath(u.toLocalFile()) for u in mime.urls() if u.isLocalFile() and u.toLocalFile()]


def make_catcher(rect):
    """An invisible drop target covering ``rect`` (a QRect, Qt coordinates)."""
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QColor, QPainter
    from PySide6.QtWidgets import QWidget

    class Catcher(QWidget):
        def __init__(self):
            super().__init__(None, _flags(click_through=False))
            _overlay_attrs(self, click_through=False)
            self.setAcceptDrops(True)
            self.setGeometry(rect)
            self.last: list[str] | None = None

        def paintEvent(self, _e) -> None:  # noqa: N802
            p = QPainter(self)
            p.fillRect(self.rect(), QColor(0, 0, 0, 2))
            p.end()

        def _report(self, e) -> None:
            paths = local_paths(e.mimeData())
            if paths != self.last:
                self.last = paths
                _say("drag " + json.dumps({"paths": paths}))
            if paths:
                e.setDropAction(Qt.DropAction.CopyAction)
                e.accept()
            else:
                e.ignore()

        def dragEnterEvent(self, e) -> None:  # noqa: N802
            self._report(e)

        def dragMoveEvent(self, e) -> None:  # noqa: N802
            self._report(e)

        def dragLeaveEvent(self, _e) -> None:  # noqa: N802
            self.last = None

        def dropEvent(self, e) -> None:  # noqa: N802
            # The file went to the other computer: the app it came from must
            # neither move nor copy it here.
            e.setDropAction(Qt.DropAction.IgnoreAction)
            e.ignore()

    w = Catcher()
    w.show()
    return w


def strip_rect(strip: dict, to_logical) -> "object":
    """One side of one screen -> the QRect of a thin strip along it, on the
    Qt screen that holds that screen's centre."""
    from PySide6.QtCore import QPoint, QRect
    from PySide6.QtGui import QGuiApplication

    cx, cy = to_logical(strip["x"] + strip["w"] / 2, strip["y"] + strip["h"] / 2)
    screen = QGuiApplication.screenAt(QPoint(round(cx), round(cy))) or QGuiApplication.primaryScreen()
    g = screen.geometry()
    t = 3
    side = strip.get("side")
    if side == "left":
        return QRect(g.x(), g.y(), t, g.height())
    if side == "right":
        return QRect(g.x() + g.width() - t, g.y(), t, g.height())
    if side == "top":
        return QRect(g.x(), g.y(), g.width(), t)
    return QRect(g.x(), g.y() + g.height() - t, g.width(), t)


def _elide(painter, text: str, width: int) -> str:
    from PySide6.QtCore import Qt

    return painter.fontMetrics().elidedText(text, Qt.TextElideMode.ElideMiddle, width)


def _paint_badge(p, x: int, y: int, ext: str, is_dir: bool) -> None:
    from PySide6.QtCore import QRectF, Qt
    from PySide6.QtGui import QColor, QFont

    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QColor(ACCENT))
    if is_dir:
        p.drawRoundedRect(QRectF(x, y + 8, 40, 30), 5, 5)
        p.drawRoundedRect(QRectF(x, y + 4, 18, 10), 3, 3)
        return
    p.drawRoundedRect(QRectF(x + 4, y, 32, 42), 5, 5)
    f = QFont(p.font())
    f.setBold(True)
    f.setPixelSize(10 if len(ext) > 3 else 11)
    p.setFont(f)
    p.setPen(QColor("white"))
    p.drawText(QRectF(x + 4, y, 32, 42), Qt.AlignmentFlag.AlignCenter, ext or "FILE")


def _paint_panel(p, w: int, h: int) -> None:
    from PySide6.QtCore import QRectF
    from PySide6.QtGui import QColor, QPen

    p.setPen(QPen(QColor(255, 255, 255, 40), 1))
    p.setBrush(QColor(28, 30, 36, 236))
    p.drawRoundedRect(QRectF(0.5, 0.5, w - 1, h - 1), 12, 12)


def card_lines(info: dict) -> tuple[str, str]:
    from curslink.files.meta import human_size

    count = int(info.get("count") or 1)
    size = info.get("size_text") or human_size(int(info.get("size") or 0))
    title = str(info.get("name") or "File")
    if count > 1:
        return f"{title} and {count - 1} more", f"{count} items · {size}"
    return title, f"{info.get('kind') or 'File'} · {size}"


def make_card(info: dict):
    """The placeholder that follows the pointer: file badge, name, kind and size."""
    from PySide6.QtCore import QRectF, Qt, QTimer
    from PySide6.QtGui import QColor, QCursor, QFont, QPainter
    from PySide6.QtWidgets import QWidget

    class Card(QWidget):
        def __init__(self):
            super().__init__(None, _flags(click_through=True))
            _overlay_attrs(self, click_through=True)
            self.resize(CARD_W, CARD_H)
            self.info = info
            self.born = time.monotonic()
            self.timer = QTimer(self)
            self.timer.timeout.connect(self.follow)
            self.timer.start(8)
            self.follow()

        def follow(self) -> None:
            pos = QCursor.pos()
            self.move(pos.x() + 14, pos.y() + 14)
            if time.monotonic() - self.born < 0.3:
                self.update()

        def paintEvent(self, _e) -> None:  # noqa: N802
            # Pops in: a quick scale and fade from the pointer.
            t = min(1.0, (time.monotonic() - self.born) / 0.18)
            ease = 1 - (1 - t) ** 3
            p = QPainter(self)
            p.setRenderHint(QPainter.RenderHint.Antialiasing)
            p.setOpacity(0.25 + 0.75 * ease)
            p.scale(0.85 + 0.15 * ease, 0.85 + 0.15 * ease)
            _paint_panel(p, CARD_W, CARD_H)
            _paint_badge(p, 12, 11, str(self.info.get("ext") or ""), bool(self.info.get("dir")))
            title, detail = card_lines(self.info)
            f = QFont(p.font())
            f.setPixelSize(13)
            f.setBold(True)
            p.setFont(f)
            p.setPen(QColor("white"))
            text_x, text_w = 62, CARD_W - 74
            p.drawText(QRectF(text_x, 12, text_w, 20), Qt.AlignmentFlag.AlignVCenter,
                       _elide(p, title, text_w))
            f.setBold(False)
            f.setPixelSize(12)
            p.setFont(f)
            p.setPen(QColor(255, 255, 255, 170))
            p.drawText(QRectF(text_x, 33, text_w, 18), Qt.AlignmentFlag.AlignVCenter, _elide(p, detail, text_w))
            p.end()

    w = Card()
    w.show()
    return w


def toast_lines(info: dict) -> tuple[str, str, float | None]:
    """(title, detail, progress 0..1 or None) for the popup."""
    from curslink.files.meta import human_size

    state = info.get("state")
    sender = info.get("from") or "the other computer"
    count = int(info.get("count") or 1)
    what = (info.get("card") or {}).get("name") if count == 1 else f"{count} files"
    what = what or f"{count} file{'s' if count != 1 else ''}"
    total = int(info.get("total") or 0)
    if state == "done" and info.get("dropped"):
        return f"Dropped {what}", human_size(total), 1.0
    if state == "done":
        folder = info.get("folder") or ""
        name = folder.rstrip("/\\").replace("\\", "/").rsplit("/", 1)[-1] or folder
        return f"Saved {what}", f"In {name} · {human_size(total)}", 1.0
    if state == "failed":
        return f"Couldn't receive {what}", str(info.get("error") or ""), None
    got = int(info.get("received") or 0)
    frac = got / total if total else 0.0
    return f"Receiving {what}", f"From {sender} · {human_size(got)} of {human_size(total)}", frac


def make_toast(info: dict):
    """The popup at the bottom of the screen under the pointer."""
    from PySide6.QtCore import QRectF, Qt, QTimer
    from PySide6.QtGui import QColor, QCursor, QFont, QGuiApplication, QPainter

    from PySide6.QtWidgets import QWidget

    class Toast(QWidget):
        def __init__(self):
            super().__init__(None, _flags(click_through=True))
            _overlay_attrs(self, click_through=True)
            self.resize(TOAST_W, TOAST_H)
            self.info = info
            screen = QGuiApplication.screenAt(QCursor.pos()) or QGuiApplication.primaryScreen()
            g = screen.availableGeometry()
            self.move(g.x() + (g.width() - TOAST_W) // 2, g.y() + g.height() - TOAST_H - 28)
            self.closer = QTimer(self)
            self.closer.setSingleShot(True)
            self.closer.timeout.connect(self.close)
            self.set(info)

        def set(self, info: dict) -> None:
            self.info = info
            if info.get("state") in ("done", "failed"):
                self.closer.start(4000 if info.get("state") == "done" else 6000)
            self.update()

        def paintEvent(self, _e) -> None:  # noqa: N802
            title, detail, frac = toast_lines(self.info)
            p = QPainter(self)
            p.setRenderHint(QPainter.RenderHint.Antialiasing)
            _paint_panel(p, TOAST_W, TOAST_H)
            card = self.info.get("card") or {}
            _paint_badge(p, 14, 14, str(card.get("ext") or ""), bool(card.get("dir")))
            f = QFont(p.font())
            f.setPixelSize(13)
            f.setBold(True)
            p.setFont(f)
            p.setPen(QColor("white"))
            text_x, text_w = 66, TOAST_W - 80
            p.drawText(QRectF(text_x, 12, text_w, 20), Qt.AlignmentFlag.AlignVCenter, _elide(p, title, text_w))
            f.setBold(False)
            f.setPixelSize(12)
            p.setFont(f)
            p.setPen(QColor(255, 255, 255, 170))
            p.drawText(QRectF(text_x, 32, text_w, 18), Qt.AlignmentFlag.AlignVCenter, _elide(p, detail, text_w))
            if frac is not None:
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(QColor(255, 255, 255, 40))
                p.drawRoundedRect(QRectF(text_x, 56, text_w, 5), 2.5, 2.5)
                p.setBrush(QColor(ACCENT))
                p.drawRoundedRect(QRectF(text_x, 56, max(5.0, text_w * frac), 5), 2.5, 2.5)
            p.end()

    w = Toast()
    w.show()
    return w


def card_pixmap(info: dict):
    """The file card as a drag image."""
    w = make_card(info)
    w.born -= 1.0  # fully popped in
    w.hide()
    pm = w.grab()
    w.close()
    return pm


def make_drag_source(tid: str, paths: list[str], info: dict):
    """A small invisible window that follows the pointer; when the left
    button goes down on it (pressed for us by the host or agent), it starts
    a real drag of ``paths`` with the card as the image. Reports
    ``nativedrag-ready``, ``nativedrag-started`` and ``nativedrag-ended
    <action>`` on stdout."""
    from PySide6.QtCore import QMimeData, QPoint, Qt, QTimer, QUrl
    from PySide6.QtGui import QColor, QCursor, QDrag, QPainter
    from PySide6.QtWidgets import QWidget

    size = 48

    class Source(QWidget):
        def __init__(self):
            super().__init__(None, _flags(click_through=False))
            _overlay_attrs(self, click_through=False)
            self.resize(size, size)
            self.dragging = False
            self.timer = QTimer(self)
            self.timer.timeout.connect(self.follow)
            self.timer.start(8)
            self.follow()

        def follow(self) -> None:
            pos = QCursor.pos()
            self.move(pos.x() - size // 2, pos.y() - size // 2)

        def paintEvent(self, _e) -> None:  # noqa: N802
            p = QPainter(self)
            p.fillRect(self.rect(), QColor(0, 0, 0, 2))
            p.end()

        def mousePressEvent(self, e) -> None:  # noqa: N802
            if self.dragging or e.button() != Qt.MouseButton.LeftButton:
                return
            self.dragging = True
            self.timer.stop()
            mime = QMimeData()
            mime.setUrls([QUrl.fromLocalFile(p) for p in paths])
            drag = QDrag(self)
            drag.setMimeData(mime)
            pm = card_pixmap(info)
            drag.setPixmap(pm)
            drag.setHotSpot(QPoint(-14, -14))
            _say(f"nativedrag-started {tid}")
            action = drag.exec(Qt.DropAction.CopyAction | Qt.DropAction.MoveAction, Qt.DropAction.CopyAction)
            name = {Qt.DropAction.CopyAction: "copy", Qt.DropAction.MoveAction: "move",
                    Qt.DropAction.LinkAction: "link"}.get(action, "none")
            _say(f"nativedrag-ended {tid} {name}")
            self.close()

    w = Source()
    w.show()
    w.raise_()
    from PySide6.QtGui import QGuiApplication

    if sys.platform == "darwin" and QGuiApplication.platformName() == "cocoa":
        # No press needed: AppKit starts the drag from this window straight
        # away and follows the real button, which is already held down.
        def start() -> None:
            w.dragging = True
            w.timer.stop()
            try:
                started = mac_begin_drag(w, paths, card_pixmap(info),
                                         lambda op: (_say(f"nativedrag-ended {tid} {op}"), w.close()))
            except Exception as exc:  # pragma: no cover - macOS only
                _say(f"log the Mac drag didn't start: {type(exc).__name__}: {exc}")
                started = False
            if started:
                _say(f"nativedrag-started {tid}")
            else:
                w.dragging = False
                w.timer.start(8)
                _say(f"nativedrag-ready {tid}")  # fall back to a press

        QTimer.singleShot(0, start)
    else:
        _say(f"nativedrag-ready {tid}")
    return w


_MAC_SOURCE = None
_MAC_DONE: dict = {}   # the source's ObjC pointer -> done callback


def _mac_source_class():
    """An NSDraggingSource that hands the drag's outcome to a callback."""
    global _MAC_SOURCE
    if _MAC_SOURCE is None:
        import objc
        from Foundation import NSObject

        class CurslinkDragSource(NSObject, protocols=[objc.protocolNamed("NSDraggingSource")]):
            def draggingSession_sourceOperationMaskForDraggingContext_(self, session, context):
                return 1  # NSDragOperationCopy: the files stay in the temp folder

            def draggingSession_endedAtPoint_operation_(self, session, point, operation):
                cb = _MAC_DONE.pop(objc.pyobjc_id(self), None)
                if cb is not None:
                    cb("copy" if operation else "none")

        _MAC_SOURCE = CurslinkDragSource
    return _MAC_SOURCE


def mac_begin_drag(widget, paths: list[str], pixmap, done) -> bool:
    """macOS: start an AppKit drag of ``paths`` from ``widget``'s window,
    with ``pixmap`` as the image, without a mouse-down of its own (the
    button is held already: the drag came from another computer). ``done(op)``
    when it ends. False if it couldn't start."""
    import objc
    from AppKit import NSDraggingItem, NSEvent, NSImage, NSURL
    from Foundation import NSData, NSProcessInfo
    from PySide6.QtCore import QBuffer, QByteArray, QIODevice

    view = objc.objc_object(c_void_p=int(widget.winId()))
    window = view.window()
    if window is None:
        return False
    buf = QByteArray()
    io = QBuffer(buf)
    io.open(QIODevice.OpenModeFlag.WriteOnly)
    pixmap.save(io, "PNG")
    raw = bytes(buf.data())
    image = NSImage.alloc().initWithData_(NSData.dataWithBytes_length_(raw, len(raw)))
    ratio = pixmap.devicePixelRatio() or 1.0
    iw, ih = pixmap.width() / ratio, pixmap.height() / ratio
    at = window.mouseLocationOutsideOfEventStream()
    local = view.convertPoint_fromView_(at, None)
    flipped = view.isFlipped()
    oy = local.y + 14 if flipped else local.y - 14 - ih
    items = []
    for i, p in enumerate(paths):
        item = NSDraggingItem.alloc().initWithPasteboardWriter_(NSURL.fileURLWithPath_(p))
        item.setDraggingFrame_contents_(((local.x + 14 + 6 * i, oy), (iw, ih)), image if i == 0 else None)
        items.append(item)
    event = NSEvent.mouseEventWithType_location_modifierFlags_timestamp_windowNumber_context_eventNumber_clickCount_pressure_(
        1, at, 0, NSProcessInfo.processInfo().systemUptime(), window.windowNumber(), None, 0, 1, 1.0)  # 1: left down
    if event is None:
        return False
    source = _mac_source_class().alloc().init()
    _MAC_DONE[objc.pyobjc_id(source)] = done
    widget._mac_source = source  # keep it alive for the drag
    session = view.beginDraggingSessionWithItems_event_source_(items, event, source)
    return session is not None


class FilesOverlay:
    """Handles the helper's file commands; keeps its windows alive."""

    def __init__(self, to_logical):
        self.to_logical = to_logical
        self.strips: list = []
        self.shield: list = []
        self.card = None
        self.toast = None
        self.source = None

    def handle(self, line: str) -> bool:
        word, _, rest = line.strip().partition(" ")
        rest = rest.strip()
        if word == "strips":
            self._close(self.strips)
            if rest != "off":
                try:
                    strips = json.loads(rest)
                except ValueError:
                    return True
                from PySide6.QtCore import QRect

                rects = []
                for s in strips if isinstance(strips, list) else []:
                    if isinstance(s, dict):
                        r = strip_rect(s, self.to_logical)
                        if r not in rects:
                            rects.append(r)
                self.strips = [make_catcher(QRect(r)) for r in rects]
                _say("log drop targets on " + ", ".join(
                    f"{r.x()},{r.y()} {r.width()}x{r.height()}" for r in rects))
        elif word == "shield":
            self._close(self.shield)
            if rest == "on":
                from PySide6.QtGui import QGuiApplication

                self.shield = [make_catcher(sc.geometry()) for sc in QGuiApplication.screens()]
        elif word == "card":
            if self.card is not None:
                self.card.close()
                self.card = None
            if rest != "hide":
                info = _json(rest)
                if info is not None:
                    self.card = make_card(info)
        elif word == "nativedrag":
            if self.source is not None and not self.source.dragging:
                self.source.close()
            self.source = None
            if rest != "cancel":
                info = _json(rest)
                if info is not None and isinstance(info.get("paths"), list):
                    self.source = make_drag_source(str(info.get("id")), [str(p) for p in info["paths"]],
                                                   info.get("card") or {})
        elif word == "toast":
            info = _json(rest)
            if info is None:
                return True
            if self.toast is not None and self.toast.isVisible() and self.toast.info.get("id") == info.get("id"):
                self.toast.set(info)
            else:
                if self.toast is not None:
                    self.toast.close()
                self.toast = make_toast(info)
        else:
            return False
        return True

    @staticmethod
    def _close(windows: list) -> None:
        for w in windows:
            w.close()
        windows.clear()


def _json(text: str) -> dict | None:
    try:
        data = json.loads(text)
    except ValueError:
        return None
    return data if isinstance(data, dict) else None
