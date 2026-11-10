"""macOS: mirror mode's capture as one ScreenCaptureKit stream per screen.

Mirror mode used to take a fresh screenshot of the covered area 30 times a
second (``CGWindowListCreateImage``, plus a list of every window each
time). Since macOS 15 each of those sets a capture up and tears it down
again inside WindowServer. DisplayLink Manager drives its screens by
capturing them too, and that churn on the same screen can freeze a
DisplayLink display until DisplayLink is switched off and on.

A stream is what ScreenCaptureKit is made for, and what DisplayLink's own
capture is: set up once, it hands over a frame only when something on the
screen changed (none while it's still), and several streams of one screen
share it. Curslink's mirrors and guides are left out by the content filter.

A stream lingers for :data:`LINGER_S` after its capture stops, so the
pointer going back and forth across the screen in front doesn't start and
stop one each time.

Everything here is best effort: if ScreenCaptureKit isn't there (macOS
before 12.3, or no pyobjc for it) or a stream fails, :func:`stream_for`
says None and the capture falls back to screenshots.
"""

from __future__ import annotations

import logging
import threading
import time

log = logging.getLogger(__name__)

LINGER_S = 20.0
START_S = 4.0
_KCV_BGRA = 0x42475241  # kCVPixelFormatType_32BGRA

_streams: dict[int, "MacStream"] = {}
_broken: set[int] = set()  # displays a stream failed on: screenshots there from now on
_said_missing = False


def _frameworks():
    """(ScreenCaptureKit, CoreMedia, Quartz, objc), or None."""
    global _said_missing
    try:
        import CoreMedia
        import objc
        import Quartz
        import ScreenCaptureKit
    except ImportError:
        if not _said_missing:
            _said_missing = True
            log.info("mirror: ScreenCaptureKit isn't available; capturing by screenshots")
        return None
    return ScreenCaptureKit, CoreMedia, Quartz, objc


def display_for(quartz, x: float, y: float, w: float, h: float):
    """The display id (CGDirectDisplayID) and its bounds (points, global)
    with the middle of this rectangle on it, or None."""
    cx, cy = x + w / 2, y + h / 2
    err, ids, count = quartz.CGGetActiveDisplayList(16, None, None)
    if err:
        return None
    for did in list(ids)[:count]:
        b = quartz.CGDisplayBounds(did)
        bx, by, bw, bh = b.origin.x, b.origin.y, b.size.width, b.size.height
        if bx <= cx < bx + bw and by <= cy < by + bh:
            return int(did), (bx, by, bw, bh)
    return None


_Output = None


def _output_class(sck, objc):
    """The SCStreamOutput / SCStreamDelegate class (made once)."""
    global _Output
    if _Output is not None:
        return _Output
    from Foundation import NSObject

    protocols = [p for p in (objc.protocolNamed("SCStreamOutput"), objc.protocolNamed("SCStreamDelegate")) if p]

    class CurslinkStreamOutput(NSObject, protocols=protocols):
        def stream_didOutputSampleBuffer_ofType_(self, _stream, buf, kind):  # noqa: N802
            owner = getattr(self, "owner", None)
            if owner is not None and kind == 0:  # SCStreamOutputTypeScreen
                owner._frame(buf)

        def stream_didStopWithError_(self, _stream, error):  # noqa: N802
            owner = getattr(self, "owner", None)
            if owner is not None:
                owner._stopped(error)

    _Output = CurslinkStreamOutput
    return _Output


class MacStream:
    """One display's stream of a rectangle of it, at the display's pixels."""

    def __init__(self, display_id: int, bounds):
        self.display_id = display_id
        self.bounds = bounds
        self.lock = threading.Lock()
        self.latest = None  # (seq, QImage)
        self.seq = 0
        self.stream = None
        self.output = None
        self.want: tuple | None = None  # (rect, scale, fps, cursor, unseen)
        self.applied: tuple | None = None
        self.busy = False  # a start or an update is under way
        self.dead = False
        self.users = 0
        self.released_at: float | None = None
        self.made_at = time.monotonic()

    # -- called from the capture (Qt's thread) ---------------------------------
    def acquire(self) -> None:
        self.users += 1
        self.released_at = None

    def stalled(self, now: float | None = None) -> bool:
        """Still not streaming :data:`START_S` after it was asked to: given up."""
        now = time.monotonic() if now is None else now
        if not self.dead and self.applied is None and now - self.made_at > START_S:
            self._fail(f"it didn't start within {START_S:g}s")
        return self.dead

    def configure(self, rect, scale: float, fps: int, cursor: bool, unseen: frozenset[int] | None) -> None:
        want = (tuple(round(v, 1) for v in rect), round(scale, 3), int(fps), bool(cursor), unseen)
        if want == self.want:
            return
        rect_changed = self.want is None or want[:2] != self.want[:2]
        self.want = want
        if rect_changed:
            with self.lock:
                self.latest = None  # an old picture of another area would show in the wrong place
        self._apply()

    def release(self) -> None:
        self.users = max(0, self.users - 1)
        if self.users == 0:
            self.released_at = time.monotonic()

    def frame(self):
        """The newest frame, as (seq, QImage), or None yet."""
        with self.lock:
            return self.latest

    # -- the stream ------------------------------------------------------------
    def _apply(self) -> None:
        if self.busy or self.dead or self.want is None or self.want == self.applied:
            return
        fw = _frameworks()
        if fw is None:
            self._fail("no ScreenCaptureKit")
            return
        sck, cm, quartz, objc = fw
        want = self.want
        self.busy = True

        def got_content(content, error):
            try:
                if content is None:
                    self._fail(f"couldn't list the screens ({error})")
                    return
                display = next((d for d in content.displays() if int(d.displayID()) == self.display_id), None)
                if display is None:
                    self._fail("the screen isn't there to capture")
                    return
                unseen = want[4]
                if unseen is not None:
                    hide = [win for win in content.windows() if int(win.windowID()) in unseen]
                    flt = sck.SCContentFilter.alloc().initWithDisplay_excludingWindows_(display, hide)
                else:
                    import os

                    me = [a for a in content.applications() if int(a.processID()) == os.getpid()]
                    flt = sck.SCContentFilter.alloc().initWithDisplay_excludingApplications_exceptingWindows_(
                        display, me, [])
                cfg = self._config(sck, cm, quartz, want)
                if self.stream is None:
                    self._start(sck, flt, cfg, want)
                else:
                    self._update(flt, cfg, want)
            except Exception as exc:  # pragma: no cover - macOS only
                log.debug("mirror: stream setup failed", exc_info=True)
                self._fail(str(exc))

        try:
            sck.SCShareableContent.getShareableContentExcludingDesktopWindows_onScreenWindowsOnly_completionHandler_(
                False, True, got_content)
        except Exception as exc:  # pragma: no cover - macOS only
            self._fail(str(exc))

    def _config(self, sck, cm, quartz, want):
        (x, y, w, h), scale, fps, cursor, _unseen = want
        bx, by, _bw, _bh = self.bounds
        cfg = sck.SCStreamConfiguration.alloc().init()
        cfg.setSourceRect_(quartz.CGRectMake(x - bx, y - by, w, h))
        cfg.setWidth_(max(2, round(w * scale)))
        cfg.setHeight_(max(2, round(h * scale)))
        cfg.setMinimumFrameInterval_(cm.CMTimeMake(1, max(1, min(120, fps))))
        cfg.setPixelFormat_(_KCV_BGRA)
        cfg.setShowsCursor_(cursor)
        cfg.setQueueDepth_(3)
        return cfg

    def _start(self, sck, flt, cfg, want) -> None:
        output = _output_class(sck, _frameworks()[3]).alloc().init()
        output.owner = self
        stream = sck.SCStream.alloc().initWithFilter_configuration_delegate_(flt, cfg, output)
        ok = stream.addStreamOutput_type_sampleHandlerQueue_error_(output, 0, None, None)
        if isinstance(ok, tuple):
            ok = ok[0]
        if not ok:
            self._fail("couldn't add the stream's output")
            return
        self.stream, self.output = stream, output

        def started(error):
            if error is not None:
                self._fail(f"the stream didn't start ({error})")
                return
            log.info("mirror: streaming display %d with ScreenCaptureKit", self.display_id)
            self._done(want)

        stream.startCaptureWithCompletionHandler_(started)

    def _update(self, flt, cfg, want) -> None:
        stream = self.stream
        left = [2]

        def one(error):
            if error is not None:
                self._fail(f"the stream couldn't change ({error})")
                return
            left[0] -= 1
            if left[0] == 0:
                self._done(want)

        stream.updateContentFilter_completionHandler_(flt, one)
        stream.updateConfiguration_completionHandler_(cfg, one)

    def _done(self, want) -> None:
        self.applied = want
        self.busy = False
        if self.want != self.applied:
            self._apply()  # it changed again meanwhile

    def _fail(self, why: str) -> None:
        if not self.dead:
            log.warning("mirror: ScreenCaptureKit stream of display %d failed: %s; capturing by screenshots",
                        self.display_id, why)
        self.dead = True
        self.busy = False
        _broken.add(self.display_id)
        self.stop()

    def _stopped(self, error) -> None:
        self._fail(f"it stopped ({error})")

    def stop(self) -> None:
        stream, self.stream = self.stream, None
        if self.output is not None:
            self.output.owner = None
        if stream is not None:
            try:
                stream.stopCaptureWithCompletionHandler_(lambda _e: None)
            except Exception:  # pragma: no cover - macOS only
                log.debug("mirror: couldn't stop a stream", exc_info=True)
        if _streams.get(self.display_id) is self:
            del _streams[self.display_id]

    # -- frames (ScreenCaptureKit's queue) -------------------------------------
    def _frame(self, buf) -> None:
        try:
            img = sample_to_qimage(buf)
        except Exception:  # pragma: no cover - macOS only
            log.debug("mirror: couldn't read a stream frame", exc_info=True)
            return
        if img is None:
            return  # idle (nothing changed), blank or suspended
        with self.lock:
            self.seq += 1
            self.latest = (self.seq, img)


def sample_to_qimage(buf):
    """A complete ScreenCaptureKit frame as a QImage (a copy), else None."""
    fw = _frameworks()
    if fw is None:
        return None
    sck, cm, quartz, _objc = fw
    atts = cm.CMSampleBufferGetSampleAttachmentsArray(buf, False)
    if not atts:
        return None
    status = atts[0].get(sck.SCStreamFrameInfoStatus)
    if status is None or int(status) != 0:  # SCFrameStatusComplete
        return None
    pix = cm.CMSampleBufferGetImageBuffer(buf)
    if pix is None:
        return None
    return pixel_buffer_to_qimage(quartz, pix)


def pixel_buffer_to_qimage(quartz, pix):
    """A 32-bit BGRA CVPixelBuffer as a QImage (a copy), or None."""
    from PySide6.QtGui import QImage

    quartz.CVPixelBufferLockBaseAddress(pix, 1)  # kCVPixelBufferLock_ReadOnly
    try:
        w, h = int(quartz.CVPixelBufferGetWidth(pix)), int(quartz.CVPixelBufferGetHeight(pix))
        bpr = int(quartz.CVPixelBufferGetBytesPerRow(pix))
        base = quartz.CVPixelBufferGetBaseAddress(pix)
        if base is None or w <= 0 or h <= 0:
            return None
        raw = base.as_buffer(bpr * h) if hasattr(base, "as_buffer") else memoryview(base)
        data = bytes(raw[: bpr * h])
    finally:
        quartz.CVPixelBufferUnlockBaseAddress(pix, 1)
    # BGRA in memory is Qt's (little-endian) ARGB32.
    return QImage(data, w, h, bpr, QImage.Format.Format_ARGB32_Premultiplied).copy()


def stream_for(x: float, y: float, w: float, h: float) -> MacStream | None:
    """The stream of the display this rectangle (points) is on, made if
    need be, or None to capture by screenshots."""
    fw = _frameworks()
    if fw is None:
        return None
    found = display_for(fw[2], x, y, w, h)
    if found is None:
        return None
    did, bounds = found
    if did in _broken:
        return None
    s = _streams.get(did)
    if s is None or s.dead:
        s = _streams[did] = MacStream(did, bounds)
    s.bounds = bounds
    return s


def reap(now: float | None = None) -> None:
    """Stop the streams no capture has used for :data:`LINGER_S`."""
    now = time.monotonic() if now is None else now
    for s in list(_streams.values()):
        if s.users == 0 and s.released_at is not None and now - s.released_at >= LINGER_S:
            s.stop()
