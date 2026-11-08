"""Mirror mode: see what's behind a screen that stands in front of another.

With overlapping screens (``[layout] allow_overlap``), a laptop on the desk
in front of a monitor hides part of it: often the corner with the clock, the
tray and notifications. With ``[layout] mirror = true``, while you use the
screen behind, every screen in front of it shows the part of it that it
covers, live, so it reads as looking through the screen in front. When you
use the screen in front again, its own desktop comes back.

Any overlap takes part: a host's screen behind an agent's or in front of
one, two agents', two screens of one computer, or a phone's (its app shows
and captures with Android's own screen capture, which asks once). The host
works out which streams it wants from the layout and the screen in use, and
tells each device what to capture and what to show (``mirror`` frames, the
whole wanted state each time). The device behind captures just the covered
rectangle and sends JPEG frames (``mirror_frame``), which the host passes on
to the device in front (or draws itself). The device showing a frame answers
``mirror_ack``, which goes back to the device capturing it: it keeps at most
:data:`IN_FLIGHT` frames unanswered, so a slow link drops frames rather than
queueing them behind the pointer. Where the pointer on the screen behind is
in the covered part (``[layout] mirror_pointer``, on by default), the host
sends where (``mirror_pointer``) and the mirror draws it.

On a computer the capturing and drawing happen in the overlay helper process
(:mod:`curslink.mirror.helper`), which already owns the switch animation.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import dataclass
from typing import Callable

from curslink.layout import Fit, Layout, Screen

log = logging.getLogger(__name__)

DEFAULT_FPS = 15
MAX_FPS = 30
# Frames a capture may have on their way before one is shown; past that the
# newest waits (replacing any older waiting one) for an answer.
IN_FLIGHT = 2
# The pointer in a mirror is sent at most this often.
POINTER_EVERY_S = 1 / 90


@dataclass(frozen=True)
class Stream:
    """The part of ``source`` (behind) that ``target`` (in front) covers."""

    source: Screen
    target: Screen
    rect: tuple[int, int, int, int]  # layout pixels, on the screen behind
    # Where that is on the screen in front (layout pixels): ``rect`` unless
    # the overlap is calibrated (curslink.layout.Fit).
    front: tuple[int, int, int, int] | None = None

    @property
    def shown(self) -> tuple[int, int, int, int]:
        return self.front or self.rect

    @property
    def id(self) -> str:
        return f"{self.source.device_id}:{self.source.id}>{self.target.device_id}:{self.target.id}"

    def os_rect(self, screen: Screen) -> list[int]:
        x, y, w, h = self.shown if screen == self.target else self.rect
        ox, oy = screen.to_os(x, y)
        return [ox, oy, w, h]

    def edges(self) -> list[str]:
        """The sides of the mirror that lie inside the screen in front (not
        on its border): where it ends, which the mirror marks."""
        x, y, w, h = self.shown
        t = self.target
        out = []
        if x > t.x:
            out.append("left")
        if x + w < t.right:
            out.append("right")
        if y > t.y:
            out.append("top")
        if y + h < t.bottom:
            out.append("bottom")
        return out

    def pointer(self, x: float, y: float) -> tuple[float, float] | None:
        """Where a pointer at layout (x, y) is in the mirror, 0..1 across and down."""
        rx, ry, w, h = self.rect
        if rx <= x < rx + w and ry <= y < ry + h:
            return ((x - rx) / w, (y - ry) / h)
        return None


def streams_for(layout: Layout, screen: Screen, can: Callable[[str], bool] = lambda device_id: True) -> list[Stream]:
    """The mirrors wanted while ``screen`` is the one in use: one for every
    screen in front of it, on devices that take part (``can``)."""
    if not layout.mirror or not layout.allow_overlap or not can(screen.device_id):
        return []
    return [Stream(screen, front, rect, None if there == rect else there)
            for front, rect, there in layout.covered_parts(screen) if can(front.device_id)]


def nudge(fit: tuple[float, float, float], dx: float, dy: float) -> tuple[float, float, float]:
    """Move the picture on the screen in front by (dx, dy) of the screen
    behind's pixels: the front screen sits that much the other way on it."""
    x, y, k = fit
    return (x - dx, y - dy, k)


def grow(fit: tuple[float, float, float], factor: float, behind: tuple[float, float],
         front: tuple[float, float]) -> tuple[float, float, float]:
    """Make the picture ``factor`` times bigger, keeping the middle of the
    covered part where it is on the screen in front."""
    x, y, k = fit
    k2 = min(MAX_FIT_SCALE, max(MIN_FIT_SCALE, k / factor))
    out = []
    for o, size_b, size_f in ((x, behind[0], front[0]), (y, behind[1], front[1])):
        lo, hi = max(0.0, o), min(size_b, o + k * size_f)
        c = (lo + hi) / 2 if hi > lo else o + k * size_f / 2
        u = (c - o) / k  # that point, on the screen in front
        out.append(c - k2 * u)
    return (out[0], out[1], k2)


MIN_FIT_SCALE, MAX_FIT_SCALE = 0.1, 10.0


def current_fit(layout: Layout, behind: Screen, front: Screen) -> tuple[float, float, float]:
    """Where ``front`` stands on ``behind`` now: calibrated, from the screens'
    real sizes, or as laid out."""
    f = layout.fit(behind, front) or layout.sized_fit(behind, front)
    if f is not None:
        return (f.x, f.y, f.scale)
    return (float(front.x - behind.x), float(front.y - behind.y), 1.0)


def apply_fit(layout: Layout, data: dict) -> tuple[float, float, float] | None:
    """Change one overlap's fit in ``layout`` right away, for fine-tuning from
    the Layout page (the ``fit {...}`` command). ``data`` names
    ``"behind"`` and ``"front"`` ("device/screen") and then either ``"x",
    "y", "scale"``; or ``"dx", "dy"`` (move the picture, in the screen
    behind's pixels) and/or ``"grow"`` (a factor); or ``"reset": true`` to
    drop the calibrated one. Returns the fit now in use, or None if ``data``
    doesn't make sense."""
    try:
        behind_ref = tuple(str(data["behind"]).split("/", 1))
        front_ref = tuple(str(data["front"]).split("/", 1))
        if len(behind_ref) != 2 or len(front_ref) != 2:
            return None
        behind, front = layout.screen(*behind_ref), layout.screen(*front_ref)
    except (KeyError, ValueError, TypeError):
        return None
    try:
        if data.get("reset"):
            new = None
        elif "x" in data:
            new = (float(data["x"]), float(data["y"]), float(data["scale"]))
        else:
            new = current_fit(layout, behind, front)
            new = nudge(new, float(data.get("dx", 0)), float(data.get("dy", 0)))
            if "grow" in data:
                new = grow(new, float(data["grow"]), (behind.width, behind.height), (front.width, front.height))
    except (KeyError, TypeError, ValueError):
        return None
    if new is not None and not new[2] > 0:
        return None
    keep = tuple(f for f in layout.fits if (f.behind, f.front) != (behind_ref, front_ref))
    if new is not None:
        keep += (Fit(behind_ref, front_ref, *new),)
    layout.fits = keep
    return current_fit(layout, behind, front)


def screen_os(screen: Screen) -> list[int]:
    """The whole screen as the layout has it, in that computer's OS pixels:
    the helper places ``os`` rectangles as fractions of it, so a layout size
    that isn't the screen's real one (Windows scaling) still lands right."""
    return [screen.os_x, screen.os_y, screen.width, screen.height]


def device_states(streams: list[Stream], names: dict[str, str], fps: int) -> dict[str, dict]:
    """What each device should capture and show for ``streams`` (the payload
    of its ``mirror`` frame). Devices with nothing to do aren't listed."""
    out: dict[str, dict] = {}
    for s in streams:
        src = out.setdefault(s.source.device_id, {"captures": [], "shows": []})
        src["captures"].append({"id": s.id, "screen": s.source.id, "os": s.os_rect(s.source),
                                "screen_os": screen_os(s.source), "fps": fps})
        dst = out.setdefault(s.target.device_id, {"captures": [], "shows": []})
        dst["shows"].append({"id": s.id, "screen": s.target.id, "os": s.os_rect(s.target),
                             "screen_os": screen_os(s.target), "size": list(s.shown[2:]),
                             "label": names.get(s.source.device_id, s.source.device_id), "edges": s.edges()})
    return out


EMPTY = {"captures": [], "shows": []}


class MirrorSource:
    """Frames a device captures, on their way to the device showing them,
    :data:`IN_FLIGHT` at a time. The newest frame that had to wait goes as
    soon as one is answered."""

    def __init__(self, send: Callable[..., None]):
        self.send = send
        self.flying: dict[str, int] = {}
        self.waiting: dict[str, dict] = {}

    def reset(self, ids) -> None:
        """Only these captures are on now."""
        ids = set(ids)
        self.flying = {i: n for i, n in self.flying.items() if i in ids}
        self.waiting = {i: f for i, f in self.waiting.items() if i in ids}
        for i in ids:
            self.flying.setdefault(i, 0)

    def offer(self, frame: dict) -> None:
        sid = frame.get("id")
        if sid not in self.flying:
            return  # a capture that has stopped
        if self.flying[sid] >= IN_FLIGHT:
            self.waiting[sid] = frame
            return
        self.flying[sid] += 1
        self.send("mirror_frame", **frame)

    def ack(self, sid: str) -> None:
        if sid not in self.flying:
            return
        self.flying[sid] = max(0, self.flying[sid] - 1)
        frame = self.waiting.pop(sid, None)
        if frame is not None:
            self.offer(frame)


def helper_frame(line: str) -> dict | None:
    """A ``mirror_frame {json}`` line from the overlay helper, or None."""
    word, _, rest = line.partition(" ")
    if word != "mirror_frame":
        return None
    try:
        frame = json.loads(rest)
    except json.JSONDecodeError:
        return None
    if not isinstance(frame, dict) or not isinstance(frame.get("id"), str) or not isinstance(frame.get("jpg"), str):
        return None
    return frame


class LatestWriter:
    """Hands lines to the helper on a thread of its own, newest per key only:
    a frame is ~60 KB, and the asyncio loop (carrying the pointer) must
    never wait on the helper's pipe. Older frames still waiting are dropped."""

    def __init__(self, write: Callable[[str], None]):
        self.write = write
        self.pending: dict[str, str] = {}
        self.cond = threading.Condition()
        self.closed = False
        threading.Thread(target=self._run, name="curslink-mirror-out", daemon=True).start()

    def put(self, key: str, line: str) -> None:
        with self.cond:
            self.pending[key] = line
            self.cond.notify()

    def _run(self) -> None:
        while True:
            with self.cond:
                while not self.pending and not self.closed:
                    self.cond.wait()
                if self.closed:
                    return
                key = next(iter(self.pending))
                line = self.pending.pop(key)
            try:
                self.write(line)
            except Exception:  # pragma: no cover - the helper went away
                log.debug("mirror: couldn't reach the helper", exc_info=True)

    def close(self) -> None:
        with self.cond:
            self.closed = True
            self.cond.notify()


def _command(word: str, data: dict) -> str:
    return word + " " + json.dumps(data, separators=(",", ":"))


class MirrorPeer:
    """One device's part in mirror mode, on top of its overlay helper:
    capture what the host asks for, show what it sends. ``send(t, **data)``
    sends a frame to the host (for an agent) or hands it to the host's own
    :class:`MirrorHost` (for the host). Runs on the asyncio loop; feed it the
    helper's lines with :meth:`helper_line` from there."""

    def __init__(self, helper: Callable[[str], None], send: Callable[..., None], images: Callable[[str, str], None] | None = None):
        self.helper = helper
        self.send = send
        # (stream id, line): frames to show, off the loop (see LatestWriter).
        self.images = images or (lambda sid, line: helper(line))
        self.source = MirrorSource(send)
        self.state = EMPTY
        self.showing: set[str] = set()

    def handle(self, t: str, data: dict) -> None:
        if t == "mirror":
            captures = [c for c in data.get("captures", []) if isinstance(c, dict) and isinstance(c.get("id"), str)]
            shows = [s for s in data.get("shows", []) if isinstance(s, dict) and isinstance(s.get("id"), str)]
            self.state = {"captures": captures, "shows": shows}
            self.source.reset(c["id"] for c in captures)
            self.showing = {s["id"] for s in shows}
            self.helper(_command("mirror", self.state))
        elif t == "mirror_frame":
            sid = data.get("id")
            if sid in self.showing:
                self.images(sid, _command("mirror_image", {k: data[k] for k in ("id", "jpg", "w", "h") if k in data}))
            self.send("mirror_ack", id=sid)  # answered even if dropped, so the capture never stalls
        elif t == "mirror_pointer":
            if data.get("id") in self.showing:
                self.helper(_command("mirror_pointer", data))
        elif t == "mirror_ack":
            self.source.ack(data.get("id"))

    def helper_line(self, line: str) -> bool:
        """A line the helper printed: True if it was mirror mode's."""
        frame = helper_frame(line)
        if frame is None:
            return False
        self.source.offer(frame)
        return True

    def stop(self) -> None:
        if self.state is not EMPTY:
            self.handle("mirror", EMPTY)


class MirrorHost:
    """The host's side: which streams are on, and passing frames, answers
    and the pointer between devices.

    ``server`` is the :class:`curslink.net.server.HostServer` (``supports``,
    ``send``); ``local`` is this computer's own :class:`MirrorPeer` (None
    without an overlay helper that can do it). Call :meth:`update` whenever
    the pointer moves (the router's ``on_moved``), and :meth:`refresh` when
    an agent connects or goes."""

    def __init__(self, layout: Layout, router, server, local: MirrorPeer | None, fps: int = DEFAULT_FPS,
                 clock: Callable[[], float] = time.monotonic):
        self.layout = layout
        self.router = router
        self.server = server
        self.local = local
        self.local_id = router.local_id
        self.fps = max(1, min(MAX_FPS, int(fps)))
        self.clock = clock
        self.names = {d.id: d.name for d in layout.devices.values()}
        self.streams: dict[str, Stream] = {}
        self.sent: dict[str, dict] = {}  # device -> the state it was last sent
        self._screen: Screen | None = None
        self._pointer: dict[str, tuple[float, float] | None] = {}
        self._pointer_at = 0.0
        self._logged: set[str] = set()
        # Fine-tuning an overlap: mirror the screen behind it, wherever the pointer is.
        self.held: Screen | None = None
        # Told the fit in use after each ``fit`` command (the app's events).
        self.on_fit: Callable[[dict], None] | None = None

    def can(self, device_id: str) -> bool:
        if device_id == self.local_id:
            return self.local is not None
        return self.server.supports(device_id, "mirror")

    # -- which streams ---------------------------------------------------------
    def refresh(self) -> None:
        self._screen = None
        self.update()

    def hold(self, screen: Screen | None) -> None:
        """Mirror ``screen`` as if it were in use (None: back to the pointer's)."""
        self.held = screen
        self.refresh()

    def fit(self, data: dict) -> None:
        """The ``fit {...}`` command (see :func:`apply_fit`); ``"hold": true``
        keeps the screen behind mirrored while fine-tuning, ``"hold": false``
        lets go."""
        now = apply_fit(self.layout, data)
        if now is None:
            return
        if "hold" in data:
            device, screen_id = str(data["behind"]).split("/", 1)
            self.hold(self.layout.screen(device, screen_id) if data["hold"] else None)
        else:
            self.refresh()
        if self.on_fit is not None:
            self.on_fit({"behind": data["behind"], "front": data["front"], "x": round(now[0], 1),
                         "y": round(now[1], 1), "scale": round(now[2], 4)})

    def update(self) -> None:
        screen = self.held or self.router.screen
        if screen != self._screen:
            self._screen = screen
            self._plan(screen)
        self._send_pointer()

    def _plan(self, screen: Screen) -> None:
        streams = streams_for(self.layout, screen, self.can)
        self.streams = {s.id: s for s in streams}
        self._pointer = {sid: p for sid, p in self._pointer.items() if sid in self.streams}
        states = device_states(streams, self.names, self.fps)
        for device in set(self.sent) | set(states):
            state = states.get(device, EMPTY)
            if self.sent.get(device, EMPTY) == state:
                continue
            if device != self.local_id and not self.server.supports(device, "mirror"):
                self.sent.pop(device, None)  # gone: it starts with nothing when it's back
                continue
            self.sent[device] = state
            self._to(device, "mirror", **state)
        if set(self.streams) != self._logged:
            self._logged = set(self.streams)
            log.info("mirror: %s", ", ".join(f"{s.id} at {s.os_rect(s.source)}" for s in streams) or "nothing to mirror now")

    def _to(self, device_id: str, t: str, **data) -> None:
        if device_id == self.local_id:
            if self.local is not None:
                self.local.handle(t, data)
        else:
            self.server.send(device_id, t, **data)

    # -- frames and answers ----------------------------------------------------
    def on_frame(self, device_id: str, frame: dict) -> None:
        """A captured frame, from ``device_id`` (an agent, or this computer)."""
        stream = self.streams.get(frame.get("id"))
        if stream is None or stream.source.device_id != device_id:
            if device_id != self.local_id:
                self.server.send(device_id, "mirror_ack", id=frame.get("id"))
            return
        data = {k: frame[k] for k in ("id", "jpg", "w", "h") if k in frame}
        self._to(stream.target.device_id, "mirror_frame", **data)

    def on_ack(self, device_id: str, sid) -> None:
        """The device showing a stream showed (or dropped) one of its frames."""
        stream = self.streams.get(sid)
        if stream is None or stream.target.device_id != device_id:
            return
        self._to(stream.source.device_id, "mirror_ack", id=sid)

    def on_message(self, device_id: str, t: str, data: dict) -> None:
        """HostServer.on_mirror."""
        if t == "mirror_frame":
            self.on_frame(device_id, data)
        elif t == "mirror_ack":
            self.on_ack(device_id, data.get("id"))

    def local_send(self, t: str, **data) -> None:
        """What this computer's own MirrorPeer sends 'to the host'."""
        if t == "mirror_frame":
            self.on_frame(self.local_id, data)
        elif t == "mirror_ack":
            self.on_ack(self.local_id, data.get("id"))

    # -- the pointer -------------------------------------------------------------
    def _captures_cursor(self, device_id: str) -> bool:
        return device_id != self.local_id and self.server.supports(device_id, "mirror_cursor")

    def _send_pointer(self) -> None:
        if not self.streams:
            return
        now = self.clock()
        x, y = self.router.x, self.router.y
        for sid, stream in self.streams.items():
            p = stream.pointer(x, y) if self.router.screen == stream.source else None
            if p is not None and self._captures_cursor(stream.source.device_id):
                p = None  # it's in the picture already (a phone draws its pointer on its screen)
            if p is not None:
                p = (round(p[0], 4), round(p[1], 4))
            last = self._pointer.get(sid)
            if p == last:
                continue
            if p is not None and last is not None and now - self._pointer_at < POINTER_EVERY_S:
                continue  # the next move sends it
            self._pointer[sid] = p
            self._pointer_at = now
            if p is None:
                self._to(stream.target.device_id, "mirror_pointer", id=sid, hide=True)
            else:
                self._to(stream.target.device_id, "mirror_pointer", id=sid, x=p[0], y=p[1])

    def stop(self) -> None:
        for device in list(self.sent):
            if self.sent[device] != EMPTY:
                self._to(device, "mirror", **EMPTY)
        self.sent = {}
        self.streams = {}
