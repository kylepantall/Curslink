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
queueing them behind the pointer. A screen behind can be set to stay
mirrored while you use any screen joined to it edge to edge, not only
while you use it (``[layout.mirror_joined]``). Where the pointer on the screen behind is
in the covered part, the device behind draws its real cursor (shape and
all) into the picture it captures (capture ``cursor``), so the mirror casts
it rather than drawing a copy. Only an older agent behind, which can't, gets
the host's ``mirror_pointer`` and the mirror draws an arrow there. A device
in front that takes tiles (capture ``tiles``) gets the covered area at the
screen behind's own pixels, in tiles, only those that changed. An agent showing
a mirror moves its own idle cursor (left wherever input last left it) to
the side of the screen, so it doesn't sit on the mirror like a second
pointer (:class:`CursorPark`).

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

from curslink.layout import JOIN_GAP, Fit, Layout, Screen

log = logging.getLogger(__name__)

DEFAULT_FPS = 30
MAX_FPS = 30
# Frames a capture may have on their way before one is shown; past that the
# newest waits (replacing any older waiting one) for an answer.
IN_FLIGHT = 2
# A mirror's picture ([layout] mirror_quality): tiles as PNG (every pixel as
# it is), or as JPEG at that quality.
QUALITIES = {"lossless": "png", "high": 95, "standard": 80}
# Tiles (see the helper's Capture) a capture may have on their way at once.
TILES_IN_FLIGHT = 16
# The pointer in a mirror is sent at most this often.
POINTER_EVERY_S = 1 / 90
# An agent's idle cursor is moved off a mirror this long after it shows
# (input has just left, and a file drag leaving may still need it there).
PARK_AFTER_S = 0.8


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


def streams_for(layout: Layout, screen: Screen, can: Callable[[str], bool] = lambda device_id: True,
                joined: bool = True, gap: int = JOIN_GAP) -> list[Stream]:
    """The mirrors wanted while ``screen`` is the one in use: one for every
    screen in front of it, on devices that take part (``can``). With
    ``joined``, also those of screens set to stay mirrored while a screen
    joined to them is in use (``[layout.mirror_joined]``, see
    :meth:`Layout.mirrored_while`)."""
    if not layout.mirror or not layout.allow_overlap:
        return []
    behind = [screen] + (layout.mirrored_while(screen, gap) if joined else [])
    return [Stream(b, front, rect, None if there == rect else there)
            for b in behind if can(b.device_id)
            for front, rect, there in layout.covered_parts(b) if can(front.device_id)]


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
    "y", "scale"``; or ``"place": [x, y], "corner": [u, v]`` (that corner of
    the screen in front, as fractions of it, clicked there, at its real size);
    or ``"dx", "dy"`` (move the picture, in the screen
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
        elif "place" in data:
            # Lining up from one click: that corner of the screen in front
            # (``corner``, fractions of it) is at ``place`` on the one behind
            # (its pixels); its size, from the two screens' real sizes.
            px, py = (float(v) for v in data["place"][:2])
            u, v = (float(c) for c in data.get("corner", (0, 0))[:2])
            sized = layout.sized_fit(behind, front)
            k = sized.scale if sized is not None else 1.0
            new = (px - u * front.width * k, py - v * front.height * k, k)
        elif "x" in data:
            new = (float(data["x"]), float(data["y"]), float(data["scale"]))
        else:
            new = current_fit(layout, behind, front)
            new = nudge(new, float(data.get("dx", 0)), float(data.get("dy", 0)))
            if "grow" in data and isinstance(data.get("anchor"), (list, tuple)) and len(data["anchor"]) == 2:
                # Bigger about a point of the front screen (its pixels) that stays put.
                x, y, k = new
                k2 = min(MAX_FIT_SCALE, max(MIN_FIT_SCALE, k / float(data["grow"])))
                u, v = float(data["anchor"][0]), float(data["anchor"][1])
                new = (x + (k - k2) * u, y + (k - k2) * v, k2)
            elif "grow" in data:
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


def device_states(streams: list[Stream], names: dict[str, str], fps: int,
                  tuning: tuple[str, str] | None = None, anchors: dict | None = None,
                  cast: Callable[[Stream], bool] | None = None,
                  tiles: Callable[[Stream], bool] | None = None, quality: str = "high") -> dict[str, dict]:
    """What each device should capture and show for ``streams`` (the payload
    of its ``mirror`` frame). Devices with nothing to do aren't listed.

    The overlap being adjusted (``tuning``) gets alignment guides: ``guides``,
    the covered area when adjusting began (kept in ``anchors``, so the lines
    across the screen behind stay put while the mirror moves), in the screen
    behind's OS pixels; its mirror also gets ``src``, what it shows of there.

    ``cast(stream)``: the device behind draws its cursor into the picture
    (capture ``cursor``). ``tiles(stream)``: the device in front takes it in
    tiles at full resolution (capture ``tiles``)."""
    out: dict[str, dict] = {}
    for s in streams:
        src = out.setdefault(s.source.device_id, {"captures": [], "shows": []})
        src["captures"].append({"id": s.id, "screen": s.source.id, "os": s.os_rect(s.source),
                                "screen_os": screen_os(s.source), "fps": fps})
        if cast is not None and cast(s):
            src["captures"][-1]["cursor"] = True
        if tiles is not None and tiles(s):
            src["captures"][-1]["tiles"] = True
            src["captures"][-1]["quality"] = quality
        dst = out.setdefault(s.target.device_id, {"captures": [], "shows": []})
        dst["shows"].append({"id": s.id, "screen": s.target.id, "os": s.os_rect(s.target),
                             "screen_os": screen_os(s.target), "size": list(s.shown[2:]),
                             "label": names.get(s.source.device_id, s.source.device_id), "edges": s.edges()})
        if tuning == (f"{s.source.device_id}/{s.source.id}", f"{s.target.device_id}/{s.target.id}"):
            covered = s.os_rect(s.source)
            at = anchors.setdefault(tuning, covered) if anchors is not None else covered
            # The mirror takes the pointer (drag, stretch and zoom it), and
            # guides go across the screen behind and the mirror alike.
            dst["shows"][-1].update(tune=True, guides=list(at), src=covered)
            src["captures"][-1].update(tune=True, guides=list(at))
    return out


EMPTY = {"captures": [], "shows": []}


class MirrorSource:
    """Frames a device captures, on their way to the device showing them,
    :data:`IN_FLIGHT` at a time (:data:`TILES_IN_FLIGHT` tiles). The newest
    frame that had to wait goes as soon as one is answered; a tile waits
    behind others only for a newer copy of the same tile."""

    def __init__(self, send: Callable[..., None]):
        self.send = send
        self.flying: dict[str, int] = {}
        self.waiting: dict[str, dict] = {}  # id -> {tile key (None: whole) -> frame}

    def reset(self, ids) -> None:
        """Only these captures are on now."""
        ids = set(ids)
        self.flying = {i: n for i, n in self.flying.items() if i in ids}
        self.waiting = {i: f for i, f in self.waiting.items() if i in ids}
        for i in ids:
            self.flying.setdefault(i, 0)

    @staticmethod
    def _key(frame: dict):
        tile = frame.get("tile")
        return tuple(tile[:2]) if isinstance(tile, list) and len(tile) >= 2 else None

    def offer(self, frame: dict) -> None:
        sid = frame.get("id")
        if sid not in self.flying:
            return  # a capture that has stopped
        key = self._key(frame)
        if self.flying[sid] >= (IN_FLIGHT if key is None else TILES_IN_FLIGHT):
            waiting = self.waiting.setdefault(sid, {})
            older = waiting.get(key)
            if key is None:
                waiting.clear()  # a whole frame overtakes everything before it
            if older is not None and "cursor" in older and "cursor" not in frame:
                frame = dict(frame, cursor=older["cursor"])  # the pointer's new look still has to go
            waiting[key] = frame  # a newer copy of a waiting tile keeps its place
            return
        self.flying[sid] += 1
        self.send("mirror_frame", **frame)

    def ack(self, sid: str) -> None:
        if sid not in self.flying:
            return
        self.flying[sid] = max(0, self.flying[sid] - 1)
        waiting = self.waiting.get(sid)
        if waiting:
            frame = waiting.pop(next(iter(waiting)))
            if not waiting:
                self.waiting.pop(sid, None)
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

    def __init__(self, helper: Callable[[str], None], send: Callable[..., None], images: Callable[[str, str], None] | None = None,
                 on_shows: Callable[[list[dict]], None] | None = None):
        self.helper = helper
        self.on_shows = on_shows  # an agent's CursorPark.shows
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
            if self.on_shows is not None:
                self.on_shows(shows)
        elif t == "mirror_frame":
            sid = data.get("id")
            if sid in self.showing:
                if isinstance(data.get("cursor"), dict):
                    # The pointer's look behind (curslink.mirror.cursorshape): on
                    # its own, so a newer frame can't drop it on the way.
                    self.images(f"shape:{sid}", _command("mirror_shape", dict(data["cursor"], id=sid)))
                tile = data.get("tile")
                key = sid if not isinstance(tile, list) else f"{sid}@{tile[:2]}"
                self.images(key, _command("mirror_image", {k: data[k] for k in ("id", "jpg", "w", "h", "tile") if k in data}))
            self.send("mirror_ack", id=sid)  # answered even if dropped, so the capture never stalls
        elif t == "mirror_pointer":
            if data.get("id") in self.showing:
                self.helper(_command("mirror_pointer", data))
        elif t == "mirror_ack":
            self.source.ack(data.get("id"))

    def helper_line(self, line: str) -> bool:
        """A line the helper printed: True if it was mirror mode's."""
        word, _, rest = line.partition(" ")
        if word == "mirror_tune":
            try:
                data = json.loads(rest)
            except json.JSONDecodeError:
                return True
            if isinstance(data, dict) and data.get("id") in self.showing:
                self.send("mirror_tune", **data)
            return True
        frame = helper_frame(line)
        if frame is None:
            return False
        self.source.offer(frame)
        return True

    def stop(self) -> None:
        if self.state is not EMPTY:
            self.handle("mirror", EMPTY)


def park_point(shows: list[dict]) -> tuple[int, int] | None:
    """Where an agent's idle cursor goes while ``shows`` are up: the middle
    of the right edge of the screen the first one is on (in the OS pixels the
    host moves this device's pointer in), so only a pixel of it is seen. Not
    a corner: Windows' bottom ones peek at the desktop or open widgets on
    hover. None while adjusting a mirror (that takes this cursor)."""
    if not shows or any(s.get("tune") for s in shows):
        return None
    whole = shows[0].get("screen_os")
    if not (isinstance(whole, list) and len(whole) == 4):
        return None
    try:
        x, y, w, h = (float(v) for v in whole)
    except (TypeError, ValueError):
        return None
    if w <= 0 or h <= 0:
        return None
    return int(x + w) - 1, int(y + h / 2)


class CursorPark:
    """An agent's own cursor stays where input last left this device. In
    mirror mode a mirror can come up right under it, where it looks like a
    second pointer next to the one the mirror draws. Once each time input is
    away, while a mirror shows, move it to the side of the screen.
    ``session`` is the agent's :class:`curslink.net.client.AgentSession`."""

    def __init__(self, session, call_later: Callable[[float, Callable[[], None]], object],
                 delay: float = PARK_AFTER_S):
        self.session = session
        self.call_later = call_later
        self.delay = delay
        self.point: tuple[int, int] | None = None
        self._parked_at: int | None = None  # session.entered when it was moved
        self._timer = None

    def shows(self, shows: list[dict]) -> None:
        self.point = park_point(shows)
        if self.point is not None and self._timer is None and not self._done():
            self._timer = self.call_later(self.delay, self._fire)

    def _done(self) -> bool:
        cursor = getattr(self.session, "cursor", None)
        if cursor is not None and cursor.hidden:
            return True  # hidden while input is away: nothing to see under the mirror
        return self.session.active or self._parked_at == self.session.entered

    def _fire(self) -> None:
        self._timer = None
        if self.point is None or self._done():
            return
        drag_out = getattr(self.session, "drag_out", None)
        if self.session.buttons or (drag_out is not None and getattr(drag_out, "busy", False)):
            self._timer = self.call_later(self.delay, self._fire)  # a drag is still ending here
            return
        self._parked_at = self.session.entered
        try:
            self.session.sink.move_to(*self.point)
        except Exception:
            log.debug("couldn't move the cursor aside", exc_info=True)


class MirrorHost:
    """The host's side: which streams are on, and passing frames, answers
    and the pointer between devices.

    ``server`` is the :class:`curslink.net.server.HostServer` (``supports``,
    ``send``); ``local`` is this computer's own :class:`MirrorPeer` (None
    without an overlay helper that can do it). Call :meth:`update` whenever
    the pointer moves (the router's ``on_moved``), and :meth:`refresh` when
    an agent connects or goes."""

    def __init__(self, layout: Layout, router, server, local: MirrorPeer | None, fps: int = DEFAULT_FPS,
                 clock: Callable[[], float] = time.monotonic, gap: int = JOIN_GAP, quality: str = "high"):
        self.layout = layout
        self.router = router
        self.server = server
        self.local = local
        self.local_id = router.local_id
        self.fps = max(1, min(MAX_FPS, int(fps)))
        self.quality = quality if quality in QUALITIES else "high"
        self.clock = clock
        self.gap = gap  # screens this far apart still count as joined
        self.names = {d.id: d.name for d in layout.devices.values()}
        self.streams: dict[str, Stream] = {}
        self.sent: dict[str, dict] = {}  # device -> the state it was last sent
        self._screen: tuple | None = None  # (screen mirrored, screen in use) last planned for
        self._pointer: dict[str, tuple[float, float] | None] = {}
        self._pointer_at = 0.0
        self._logged: set[str] = set()
        # Fine-tuning an overlap: mirror the screen behind it, wherever the pointer is.
        self.held: Screen | None = None
        # Told the fit in use after each ``fit`` command (the app's events).
        self.on_fit: Callable[[dict], None] | None = None
        # Adjusting an overlap on the mirror itself: ("behind", "front") keys,
        # and the layout's pointer_through to put back after.
        self.tuning: tuple[str, str] | None = None
        self._through = layout.pointer_through
        self._anchors: dict = {}  # where its guides are (see device_states)

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
        if "tune" in data or data.get("hold") is False:
            self.set_tuning((str(data["behind"]), str(data["front"])) if data.get("tune") else None)
        if "hold" in data:
            device, screen_id = str(data["behind"]).split("/", 1)
            self.hold(self.layout.screen(device, screen_id) if data["hold"] else None)
        else:
            self.refresh()
        if self.on_fit is not None:
            self.on_fit({"behind": data["behind"], "front": data["front"], "x": round(now[0], 1),
                         "y": round(now[1], 1), "scale": round(now[2], 4)})

    def set_tuning(self, pair: tuple[str, str] | None) -> None:
        """Let the mirror of ``pair`` take the pointer, to drag, stretch and
        zoom it (None: done). While it does, the pointer doesn't go through
        to the screen behind, so it can reach the mirror."""
        if pair == self.tuning:
            return
        if self.tuning is None:
            self._through = self.layout.pointer_through
        self.tuning = pair
        self._anchors = {}
        self.layout.pointer_through = False if pair is not None else self._through
        self.sent = {d: {} for d in self.sent}  # send the shows again, with or without "tune"

    def tune(self, data: dict) -> None:
        """A ``mirror_tune`` from the mirror being adjusted: ``{"id", "mx", "my"}``
        (dragged by that fraction of its size) or ``{"id", "grow", "ax", "ay"}``
        (bigger about that point, as fractions of it)."""
        stream = self.streams.get(data.get("id"))
        if stream is None or self.tuning is None:
            return
        behind, front = stream.source, stream.target
        pair = (f"{behind.device_id}/{behind.id}", f"{front.device_id}/{front.id}")
        if pair != self.tuning:
            return
        sx, sy, sw, sh = stream.shown
        k = current_fit(self.layout, behind, front)[2]
        cmd: dict = {"behind": pair[0], "front": pair[1]}
        try:
            if "grow" in data:
                factor = float(data["grow"])
                if not 0.2 <= factor <= 5:
                    return
                cmd["grow"] = factor
                cmd["anchor"] = [sx - front.x + float(data.get("ax", 0.5)) * sw,
                                 sy - front.y + float(data.get("ay", 0.5)) * sh]
            else:
                cmd["dx"] = float(data.get("mx", 0)) * sw * k
                cmd["dy"] = float(data.get("my", 0)) * sh * k
        except (TypeError, ValueError):
            return
        self.fit(cmd)

    def update(self) -> None:
        screen = self.held or self.router.screen
        # Held, the pointer can be elsewhere: whether the picture casts it changes with it.
        key = (screen, self.router.screen)
        if key != self._screen:
            self._screen = key
            self._plan(screen)
        self._send_pointer()

    def _plan(self, screen: Screen) -> None:
        # Held (fine-tuning one overlap), only that screen's mirrors.
        streams = streams_for(self.layout, screen, self.can, joined=self.held is None, gap=self.gap)
        self.streams = {s.id: s for s in streams}
        self._pointer = {sid: p for sid, p in self._pointer.items() if sid in self.streams}
        states = device_states(streams, self.names, self.fps, self.tuning, self._anchors,
                               cast=lambda s: self.router.screen == s.source and self._captures_cursor(s.source.device_id),
                               tiles=lambda s: self._takes_tiles(s.target.device_id), quality=self.quality)
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
        data = {k: frame[k] for k in ("id", "jpg", "w", "h", "cursor", "tile") if k in frame}
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
        elif t == "mirror_tune":
            self.tune(data)

    def local_send(self, t: str, **data) -> None:
        """What this computer's own MirrorPeer sends 'to the host'."""
        if t == "mirror_frame":
            self.on_frame(self.local_id, data)
        elif t == "mirror_ack":
            self.on_ack(self.local_id, data.get("id"))
        elif t == "mirror_tune":
            self.tune(data)

    # -- the pointer -------------------------------------------------------------
    def _captures_cursor(self, device_id: str) -> bool:
        """Its captures show its real cursor (this computer's helper draws it in)."""
        return device_id == self.local_id or self.server.supports(device_id, "mirror_cursor")

    def _takes_tiles(self, device_id: str) -> bool:
        return device_id == self.local_id or self.server.supports(device_id, "mirror_tiles")

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
