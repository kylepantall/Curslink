"""App mirror: drag an app's window off one computer onto another.

Drag a window by its title bar over an edge that leads to another device
(or carry it there by looking) and it isn't moved off its screen: it's put
back where it was, and the other device shows it, live, in a window of its
own with the app's shape (its aspect ratio, at the size it really is on the
desk). That window has no frame of its own: a Mac app on a PC looks like
the Mac app, title bar, rounded corners and all. The pointer and the
keyboard go through it to the app, where it really is: move into the
mirror and input is on the computer the app runs on, at the same spot in
its window, which is brought to the front there so clicks land in it. Move
out of it and input is back on the device showing it.

The app's own title bar moves the mirror. Pressing it and dragging (which
on the computer it runs on starts moving the real window) is noticed there,
the real window goes back where it was, and the mirror follows the pointer
instead until the button comes up; carried over an edge it goes on to that
device, and carried back to the computer the app runs on it ends. The
mirror's outer few pixels are its own: drag them to make it bigger or
smaller: round its edges is a thin ring of its own (outside the app's
picture) where the pointer is the resize arrows, and near a corner a short
curved handle shows, as the Isolink chat popups have. Near it, a small tag
names the app and has a box (fill the screen) and a close button.

It behaves like the app's own window. The app's full screen button (macOS's
green one, Windows' maximise) or a double click on its title bar makes the
mirror fill the screen it's on, the app sized to that screen's shape, rather
than the real window going full screen where it runs. The pointer drawn in
it is the app's own cursor (its look sent from the computer it runs on),
moved by the mouse here, so it never lags. The real window is kept on its
own screen while it's used, never bigger than it, so every part of the
mirror can be reached. A mirror not in use shows its last picture (a still,
refreshed now and then) rather than streaming, and each mirror has a button
of its own on the taskbar (or the Dock), with the app's icon.

Files dragged into a mirror go to the app the way files dragged across
screens go to another device (:mod:`curslink.files.handoff`): crossing
into the mirror is a crossing to that computer with the button held.

A phone's app (the screen of the phone, as Android's screen capture lets
Curslink see it) is cast by dragging across from the phone with the left
button held: a phone has no windows to drag. A phone shows mirrors of
computers' apps in the same overlay mirror mode draws with.

How it travels. Pictures go as mirror mode's do (:mod:`curslink.mirror`):
the host adds an app's capture (``window``: which window to capture) and
its mirror (``app``: a show that is a window of its own) to each device's
``mirror`` state, and ``mirror_frame`` / ``mirror_ack`` carry frames and
answers, with ids that start ``app:``. Besides those:

* ``app_drag`` (device -> host): a window there is being dragged by its
  title bar, or the drag ended (:class:`WindowDragWatch`);
* ``app_window`` (device -> host): a cast window moved, changed size or
  closed (:class:`CastWatch`);
* ``app_view`` (device -> host): a mirror there was resized, or closed, or
  its tag's box pressed (``full``);
* ``app_window`` with ``full``: the app's full screen button, or a double
  click on its title bar (the press never reaches the app); with ``min``:
  its minimise button (its mirror is minimised); with ``icon``:
  the app's icon, sent on as ``app_icon`` (host -> the device showing it);
* ``app_do`` (host -> device): bring a cast window to the front, put it
  back where it was, resize it, stop watching it.

The router asks :class:`AppMirrorHost` where the pointer goes
(:meth:`AppMirrorHost.hop`), so input moves in and out of mirrors with no
crossing animation.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Callable

log = logging.getLogger(__name__)

PREFIX = "app:"
# The pointer goes into a mirror's app this far inside its edge.
GRIP = 2
# Round a mirror (on the device showing it) is a ring this wide of its own,
# outside the app's picture: the resize arrows, and the corners' handles.
RING = 16
# Coming out of an app, the pointer lands this far outside its mirror (in
# the ring), so it doesn't go straight back in.
LAND_PX = 4
# A press this close outside a mirror resizes it: the pointer doesn't go into
# the app while the button is held.
RESIZE_PX = 20
# Two presses on an app's title bar this close together are a double click.
DOUBLE_S = 0.45
DOUBLE_PX = 6
# A Mac's menu bar: an app's window isn't put under it.
MENU_BAR = {"mac": 25}
# A window counts as dragged by its title bar once it has moved this far
# (OS pixels) with its size unchanged while the left button is held.
MOVE_PX = 6
# How often a held press checks whether its window is moving, and how often
# a cast window's place is checked.
DRAG_POLL_S = 0.06
CAST_POLL_S = 0.3
# A window put back is put back again this much later (see AppMirrorDevice).
RESTORE_AGAIN_S = 0.35
# A cast window minimised where it runs is put back; for this long it may
# still be off screen (macOS's animation), and that isn't its being closed.
MIN_GRACE_S = 2.0
# A mirror is never bigger than this share of the screen showing it, nor
# narrower than MIN_VIEW px.
MAX_VIEW = 0.9
MIN_VIEW = 160
# While the pointer is this close to a mirror (layout px), its tag shows.
NEAR_PX = 40
# A mirror this close to its screen's edge: the pointer doesn't leave the app
# through that side (there's nowhere to go but the gap).
WALL_PX = 48
# A window's own rounded corners on its computer (its OS pixels): the
# mirror is cut the same way, so nothing square shows behind them.
CORNERS = {"mac": 12, "windows": 8}
# Moving a carried mirror sends its new place at most this often.
PLACE_EVERY_S = 1 / 60
# Frames a second: the app the pointer is in (at least, and at most), and
# the other apps' while it is.
IN_USE_FPS = 60
MAX_IN_USE_FPS = 60
IDLE_FPS = 10
# A mirror nobody is using or near: its last picture, refreshed this often.
STILL_FPS = 1
# An app's icon is no bigger than this (base64 PNG): it goes in one frame
# (curslink.protocol.MAX_FRAME_BYTES).
MAX_ICON = 48_000


def _on_desk(rect) -> bool:
    """Not where Windows keeps a minimised window (-32000, -32000)."""
    return rect[0] > -30000 and rect[1] > -30000


def is_app(stream_id) -> bool:
    return isinstance(stream_id, str) and stream_id.startswith(PREFIX)


def _rect(v) -> tuple[float, float, float, float] | None:
    if not (isinstance(v, (list, tuple)) and len(v) == 4):
        return None
    try:
        x, y, w, h = (float(n) for n in v)
    except (TypeError, ValueError):
        return None
    return (x, y, w, h) if w > 0 and h > 0 else None


# ------------------------------------------------------------- the device side
class WindowDragWatch:
    """Notices a window being dragged by its title bar on this computer.

    When the left button goes down, it notes the window under the pointer
    (``probe``, :func:`curslink.appmirror.windows.window_at_cursor`); while
    it's held, it checks where that window is (``rect``) every
    :data:`DRAG_POLL_S`. Moved, the same size: it's being dragged, and
    ``report(window)`` says so, with ``start`` (where it was) and ``at``
    (where on it it was pressed, as fractions). ``report(None)`` when the
    button comes up after that.

    ``submit(fn, done)`` runs ``fn`` off the event loop and hands its answer
    to ``done`` back on it; ``call_later(delay, fn)`` is the loop's."""

    def __init__(self, report: Callable[[dict | None], None], submit, call_later,
                 probe: Callable[[], dict | None] | None = None, rect: Callable[[dict], list | None] | None = None):
        from curslink.appmirror import windows

        self.report = report
        self.submit = submit
        self.call_later = call_later
        self.probe = probe or windows.window_at_cursor
        self.rect = rect or windows.window_rect
        self.window: dict | None = None
        self.dragging = False
        self.held = False
        self._gen = 0

    def on_button(self, button: str, pressed: bool) -> None:
        if button != "left":
            return
        self._gen += 1
        gen = self._gen
        if pressed:
            self.held = True
            self.window = None

            def found(win):
                if gen == self._gen and self.held and win is not None:
                    self.window = dict(win, start=list(win["os"]))
                    self.call_later(DRAG_POLL_S, lambda: self._poll(gen))

            self.submit(self.probe, found)
            return
        self.held = False
        if self.dragging:
            self.dragging = False
            self.report(None)
        self.window = None

    def _poll(self, gen: int) -> None:
        if gen != self._gen or not self.held or self.window is None:
            return

        def got(now):
            if gen != self._gen or not self.held or self.window is None:
                return
            start = self.window["start"]
            if now is None:
                return  # it went away
            moved = abs(now[0] - start[0]) + abs(now[1] - start[1])
            same_size = abs(now[2] - start[2]) < 2 and abs(now[3] - start[3]) < 2
            if same_size and moved >= MOVE_PX:
                self.window["os"] = list(now)
                if not self.dragging:
                    self.dragging = True
                    log.info("app mirror: %s window is being dragged", self.window.get("app") or "an app's")
                    self.report(dict(self.window))
            self.call_later(DRAG_POLL_S, lambda: self._poll(gen))

        win = self.window
        self.submit(lambda: self.rect(win), got)


class CastWatch:
    """Where each window being cast is: ``report(id, rect)`` when one moves
    or changes size, ``report(id, None)`` when it's gone."""

    def __init__(self, report: Callable[[str, list | None], None], submit, call_later,
                 rect: Callable[[dict], list | None] | None = None,
                 full: Callable[[dict], bool] | None = None, on_full: Callable[[str], None] = lambda sid: None,
                 unmin: Callable[[dict, list | None], bool] | None = None, clock: Callable[[], float] = time.monotonic,
                 on_min: Callable[[str], None] = lambda sid: None):
        from curslink.appmirror import windows

        self.report = report
        self.submit = submit
        self.call_later = call_later
        self.rect = rect or windows.window_rect
        # full(window): it went full screen (or was maximised) where it runs,
        # and was put back (True); on_full(id) then says so: its mirror fills
        # its screen instead.
        self.full = full
        self.on_full = on_full
        # unmin(window, rect): it was minimised where it runs, and was put
        # back (True). Until it's back (MIN_GRACE_S), where it is isn't said,
        # and its not being on screen isn't its being closed.
        self.unmin = unmin
        self.on_min = on_min
        self.clock = clock
        self.back_by: dict[str, float] = {}
        self.windows: dict[str, dict] = {}
        self.last: dict[str, list] = {}
        self._running = False

    def set(self, windows: dict[str, dict]) -> None:
        self.windows = dict(windows)
        self.last = {i: r for i, r in self.last.items() if i in self.windows}
        self.back_by = {i: t for i, t in self.back_by.items() if i in self.windows}
        for i, win in self.windows.items():
            self.last.setdefault(i, list(win.get("os") or []))
        if self.windows and not self._running:
            self._running = True
            self.call_later(CAST_POLL_S, self._poll)

    def _poll(self) -> None:
        if not self.windows:
            self._running = False
            return
        pending = dict(self.windows)

        def check():
            out = {}
            for i, w in pending.items():
                went_full = False
                if self.full is not None:
                    try:
                        went_full = bool(self.full(w))
                    except Exception:  # pragma: no cover - platform trouble
                        log.debug("app mirror: couldn't check for full screen", exc_info=True)
                r = self.rect(w)
                went_min = False
                if self.unmin is not None:
                    try:
                        went_min = bool(self.unmin(w, r))
                    except Exception:  # pragma: no cover - platform trouble
                        log.debug("app mirror: couldn't check for minimised", exc_info=True)
                out[i] = (r, went_full, went_min)
            return out

        def got(rects):
            now = self.clock()
            for i, (r, went_full, went_min) in rects.items():
                if i not in self.windows:
                    continue
                if went_min:
                    log.info("app mirror: %s was minimised where it runs: put back (its mirror is its window)",
                             self.windows[i].get("app") or "an app")
                    self.back_by[i] = now + MIN_GRACE_S
                    self.on_min(i)  # its mirror is minimised instead
                    continue
                if now < self.back_by.get(i, 0.0) and (r is None or not _on_desk(r)):
                    continue  # on its way back
                if went_full:
                    # Where it is now is on its way back: the next look says.
                    self.on_full(i)
                    continue
                if r is None:
                    self.windows.pop(i, None)
                    self.last.pop(i, None)
                    self.report(i, None)
                elif [round(v) for v in r] != [round(v) for v in self.last.get(i, [])]:
                    self.last[i] = list(r)
                    self.report(i, list(r))
            self.call_later(CAST_POLL_S, self._poll)

        self.submit(check, got)


class AppMirrorDevice:
    """One computer's own part in App mirror (an agent's, or the host's for
    itself): watch for windows dragged by their title bar, keep track of the
    windows it casts, and do what the host asks (``app_do``). ``send(t,
    **data)`` goes to the host. Call :meth:`on_button` with this computer's
    left button (the host's input there), :meth:`on_captures` with each
    ``mirror`` state's captures, :meth:`helper_line` with the helper's lines."""

    def __init__(self, send: Callable[..., None], submit, call_later, ops=None,
                 skip: Callable[[], set[int]] = lambda: set()):
        from curslink.appmirror import windows

        self.send = send
        self.ops = ops or windows
        self.call_later = call_later
        # Our own processes (the overlay helper, whose mirrors are windows too).
        self.skip = skip
        self.drags = WindowDragWatch(self._dragged, submit, call_later, lambda: self.ops.window_at_cursor(self.skip()),
                                     self.ops.window_rect)
        self.casts = CastWatch(self._cast_moved, submit, call_later, self._rect, self._undo_full,
                               lambda sid: self.send("app_window", id=sid, full=True), self._undo_min,
                               on_min=lambda sid: self.send("app_window", id=sid, min=True))
        self.submit = submit
        self.cast: dict[str, dict] = {}
        # Windows hidden here while their app is cast: id -> [window, home, in use].
        self.hidden: dict[str, list] = {}
        # A press on a cast app's title bar: (id, when, x, y), for a double click.
        self._title_at: tuple[str, float, float, float] | None = None
        # The press kept from the app (its full screen button): its release is too.
        self._kept = False
        self.clock = time.monotonic

    def _rect(self, win: dict) -> list | None:
        """Where a cast window is, as far as the host is concerned: a window
        parked out of sight (macOS) is still at home, in use or not: on its
        way home (or back to the corner) it mustn't be reported in the
        corner, or the pointer going into its mirror lands there, falls out,
        goes in again and the mirror traps and jumps."""
        hid = next((h for h in self.hidden.values() if h[0].get("wid") == win.get("wid")), None)
        if hid is None or not getattr(self.ops, "parks", lambda: False)():
            return self.ops.window_rect(win)
        now = self.ops.window_rect(win)
        if now is None:
            return list(hid[1]) if self.ops.window_alive(win) else None
        return [hid[1][0], hid[1][1], now[2], now[3]]

    def on_button(self, button: str, pressed: bool) -> None:
        if self.ops.supported():
            self.drags.on_button(button, pressed)

    def intercept(self, button: str, pressed: bool) -> bool:
        """A press the host sent (before it's made here): True to keep it from
        the app. A cast app's full screen button, or the second press of a
        double click on its title bar, fills the screen its mirror is on
        instead (``app_window {id, full}``); the app never sees it, so the
        real window doesn't go full screen (into a Space of its own on a
        Mac, which its picture can't follow)."""
        if button != "left" or not self.cast or not self.ops.supported():
            return False
        if not pressed:
            kept, self._kept = self._kept, False
            return kept
        hit = getattr(self.ops, "title_hit", None)
        where = getattr(self.ops, "cursor", None)
        at = where() if where is not None else None
        if hit is None or at is None:
            return False
        found = None
        for sid, win in self.cast.items():
            had = self.hidden.get(sid)
            if had is not None and not had[2]:
                continue  # hidden, not in use: the pointer isn't in it
            r = self.ops.window_rect(win)
            if r is not None and r[0] <= at[0] < r[0] + r[2] and r[1] <= at[1] < r[1] + r[3]:
                found = (sid, win)
                break
        if found is None:
            self._title_at = None
            return False
        sid, win = found
        try:
            what = hit(win, at[0], at[1])
        except Exception:  # pragma: no cover - platform trouble
            log.debug("app mirror: couldn't tell what was pressed", exc_info=True)
            return False
        now = self.clock()
        last = self._title_at
        double = (what == "title" and last is not None and last[0] == sid and now - last[1] <= DOUBLE_S
                  and abs(at[0] - last[2]) <= DOUBLE_PX and abs(at[1] - last[3]) <= DOUBLE_PX)
        if what == "min":
            # Its minimise button: its mirror goes to its taskbar button (or
            # the Dock) where it's shown; the real window stays (its picture
            # is what the mirror shows when it comes back).
            self._title_at = None
            self._kept = True
            log.info("app mirror: %s's minimise button: its mirror is minimised", win.get("app") or "an app")
            self.send("app_window", id=sid, min=True)
            return True
        if what == "full" or double:
            self._title_at = None
            self._kept = True
            log.info("app mirror: %s's %s: its mirror fills the screen (or goes back)", win.get("app") or "an app",
                     "full screen button" if what == "full" else "title bar, double clicked")
            self.send("app_window", id=sid, full=True)
            return True
        self._title_at = (sid, now, at[0], at[1]) if what == "title" else None
        return False

    def _undo_full(self, win: dict) -> bool:
        """(Off the event loop.) The window went full screen, or was
        maximised, where it runs (its own button pressed here, or a key):
        put it back, True. Its mirror fills its screen instead."""
        undo = getattr(self.ops, "undo_full", None)
        return bool(undo(win)) if undo is not None else False

    def _undo_min(self, win: dict, rect: list | None) -> bool:
        """(Off the event loop.) The window was minimised where it runs (its
        own button, its taskbar button, a key): put it back, True. Cast, its
        mirror is its window: minimised, the picture is whatever is behind
        where it was (Kyle, 0.4.124: "we could see the boundary because the
        app was minimised"), and input sent to it misses it. Only checked
        when it looks put away (off every screen there, or not showing)."""
        undo = getattr(self.ops, "undo_minimise", None)
        if undo is None:
            return False
        if rect is not None and _on_desk(rect):
            # Where the host's told it is (a parked Mac window: at home). Where
            # it really is: minimised, it's off every screen, or not showing.
            rect = self.ops.window_rect(win)
            if rect is not None and _on_desk(rect):
                return False
        return bool(undo(win))

    def _fit(self, rect: list) -> list:
        """``rect`` moved (and made smaller) to be all on one of this
        computer's screens: the pointer can reach all of a window that is."""
        fit = getattr(self.ops, "fit_on_screen", None)
        if fit is None:
            return list(rect)
        try:
            return list(fit(rect) or rect)
        except Exception:  # pragma: no cover - platform trouble
            log.debug("app mirror: couldn't fit a window on its screen", exc_info=True)
            return list(rect)

    def _dragged(self, window: dict | None) -> None:
        if window is None:
            self.send("app_drag", none=True)
        else:
            self.send("app_drag", window=window)

    def _cast_moved(self, sid: str, rect: list | None) -> None:
        if rect is None:
            self.cast.pop(sid, None)
            self.send("app_window", id=sid, gone=True)
        else:
            self.send("app_window", id=sid, os=[round(v, 1) for v in rect])

    def on_captures(self, captures: list[dict]) -> None:
        windows = {c["id"]: dict(c["window"], os=c.get("os")) for c in captures
                   if is_app(c.get("id")) and isinstance(c.get("window"), dict)}
        self.cast = windows
        self.casts.set(windows)
        hide = {c["id"]: bool(c.get("use")) for c in captures if c["id"] in windows and c.get("hide")}
        for sid in [i for i in self.hidden if i not in hide]:
            win, home, _use = self.hidden.pop(sid)
            self.submit(lambda w=win, h=home: self.ops.show_window(w, h), lambda _r: None)
        for sid, use in hide.items():
            had = self.hidden.get(sid)
            if had is not None and had[2] == use:
                continue
            win = windows[sid]
            home = list(had[1]) if had is not None else list(win["os"])
            self.hidden[sid] = entry = [win, home, use]
            self.submit(lambda w=win, e=entry, u=use: self._hide(w, e, u), lambda _r: None)

    def _hide(self, win: dict, entry: list, use: bool) -> None:
        """(Off the event loop.) Hide a cast window, or bring it home to be
        used: all of it on its screen then, so the pointer reaches all of it
        (part of it off the screen, the pointer stopped at the screen's edge
        however far the mirror went)."""
        if use:
            fitted = self._fit(entry[1])
            if [round(v) for v in fitted] != [round(v) for v in entry[1]]:
                entry[1] = fitted
                if not getattr(self.ops, "parks", lambda: False)():
                    self.ops.move_window(win, fitted)
        self.ops.hide_window(win, entry[1], use)

    def handle(self, data: dict) -> None:
        """``app_do`` from the host."""
        sid, what = data.get("id"), data.get("do")
        win = self.cast.get(sid) or (data.get("window") if isinstance(data.get("window"), dict) else None)
        if win is None:
            return
        if what == "raise":
            self.submit(lambda: self.ops.raise_window(win), lambda _r: None)
        elif what == "restore" and _rect(data.get("os")) is not None:
            rect = list(data["os"])
            self.submit(lambda: self._put(sid, win, rect), lambda _r: None)
            # Again in a moment: the drag ending (a Windows snap at the edge,
            # or the window let go of at the parked pointer) can come after.
            self.call_later(RESTORE_AGAIN_S, lambda: self.submit(lambda: self._put(sid, win, rect),
                                                                 lambda _r: None))
        elif what == "resize" and isinstance(data.get("size"), list) and len(data["size"]) == 2:
            # Its mirror was resized: the app is too, so it lays itself out
            # for the new size rather than being stretched.
            try:
                w, h = (max(80.0, float(v)) for v in data["size"])
            except (TypeError, ValueError):
                return
            had = self.hidden.get(sid)
            if had is not None:
                had[1] = [had[1][0], had[1][1], w, h]
            self.submit(lambda: self._resize(sid, win, w, h), lambda _r: None)
        elif what == "stop":
            self.cast.pop(sid, None)
            self.casts.set(self.cast)

    def _put(self, sid, win: dict, rect: list) -> None:
        """Back where it was (off the event loop): or, hidden, parked again."""
        had = self.hidden.get(sid)
        if had is not None:
            had[1] = list(rect)
            if not had[2] and getattr(self.ops, "parks", lambda: False)():
                self.ops.hide_window(win, list(rect), False)
                return
        self.ops.move_window(win, rect)

    def _resize(self, sid, win: dict, w: float, h: float) -> None:
        had = self.hidden.get(sid)
        if had is not None and not had[2] and getattr(self.ops, "parks", lambda: False)():
            had[1] = self._fit(had[1])
            self.ops.hide_window(win, had[1], False)
            return
        now = self.ops.window_rect(win)
        if now is not None:
            rect = self._fit([now[0], now[1], w, h])
            if had is not None:
                had[1] = list(rect)
            self.ops.move_window(win, rect)

    def helper_line(self, line: str) -> bool:
        """``app_view {...}`` from the helper (a mirror here was resized or
        closed), or ``app_icon {id, png}`` (the icon of an app it captures):
        on to the host."""
        import json

        word, _, rest = line.partition(" ")
        if word not in ("app_view", "app_icon"):
            return False
        try:
            data = json.loads(rest)
        except json.JSONDecodeError:
            return True
        if not isinstance(data, dict) or not is_app(data.get("id")):
            return True
        if word == "app_view":
            self.send("app_view", **data)
        elif isinstance(data.get("png"), str) and 0 < len(data["png"]) <= MAX_ICON:
            self.send("app_window", id=data["id"], icon=data["png"])
        return True


# ----------------------------------------------------------------- the host side
@dataclass
class AppSession:
    """One app window cast from ``source`` and shown on ``target``."""

    id: str
    source: str               # device the app runs on
    window: dict              # its window (curslink.appmirror.windows), or {} for a phone's screen
    src_screen: object        # curslink.layout.Screen it's on
    rect: tuple[float, float, float, float]   # where the window is, layout px
    target: object            # curslink.layout.Screen showing it
    view: tuple[float, float, float, float]   # where its mirror is, layout px
    name: str = ""
    key: int = 0              # bumped when the mirror needs the whole picture
    raised: int = 0           # bumped each time the pointer goes into it
    hover: bool = False
    phone: bool = False
    k: float = 1.0            # its mirror's OS pixels per pixel of the window
    dpr: float | None = None  # the pixel ratio of the screen showing it (as its helper says)
    sized: bool = False       # resized where it's shown (so it keeps that size)
    # Filling its screen: (where it was, the app's size then, k then) to go back to.
    full: tuple | None = None
    icon: str = ""            # the app's icon (base64 PNG), once its computer sends it
    min: bool = False         # minimised where it's shown (to its taskbar button, or the Dock)
    started_at: float = field(default_factory=time.monotonic)

    @property
    def label(self) -> str:
        return self.name


class AppMirrorHost:
    """The host's side of App mirror: which apps are cast where, and where
    the pointer goes in and out of their mirrors.

    ``router`` is the :class:`curslink.router.Router`, ``server`` the
    :class:`curslink.net.server.HostServer`; ``local`` this computer's own
    :class:`AppMirrorDevice` (None when it can't take part);
    ``changed()`` is called whenever what devices capture or show changes
    (the mirror host sends everyone's ``mirror`` state again);
    ``release_local()`` ends a window drag of this computer's own mouse
    (Escape, and let go) when it leaves for another device."""

    def __init__(self, layout, router, server, local: AppMirrorDevice | None = None,
                 changed: Callable[[], None] = lambda: None, release_local: Callable[[], None] = lambda: None,
                 clock: Callable[[], float] = time.monotonic, can_show_local: bool = True, local_peer=None):
        self.layout = layout
        self.router = router
        self.server = server
        self.local = local
        self.local_id = router.local_id
        self.changed = changed
        self.release_local = release_local
        self.clock = clock
        self.can_show_local = can_show_local
        # This computer's curslink.mirror.MirrorPeer: what it shows (an app's icon).
        self.local_peer = local_peer
        self.sessions: dict[str, AppSession] = {}
        # A mirror being resized (a press just outside it): the pointer doesn't
        # go into its app until the button comes up.
        self.resizing: str | None = None
        self.dragging: dict[str, dict] = {}   # device -> the window being dragged there
        self.through: str | None = None       # the session the pointer is in, on its source
        # (session id, grab across, grab down): a mirror carried by the pointer.
        self.carrying: tuple[str, float, float] | None = None
        self._placed_at = 0.0
        self._n = 0
        self._phone_press: str | None = None  # a phone the left button went down on
        self.names = {d.id: d.name for d in layout.devices.values()}
        self.hide = True  # hide a cast app's window on its own computer
        self.dprs: dict[tuple[str, str], float] = {}  # (device, screen) -> its pixel ratio, as helpers say
        # (device, screen) -> the part of it apps fill (layout px): all but the
        # menu bar and the Dock, or the taskbar, as the helper showing a mirror there says.
        self.works: dict[tuple[str, str], tuple[float, float, float, float]] = {}

    # -- who can ------------------------------------------------------------------
    def is_phone(self, device_id: str) -> bool:
        conn = getattr(self.server, "connections", {}).get(device_id)
        return conn is not None and "android" in str(getattr(conn, "platform", "")).lower()

    def can_cast(self, device_id: str) -> bool:
        from curslink.protocol import APP_MIRROR_FEATURE, MIRROR_FEATURE

        if device_id == self.local_id:
            return self.local is not None and self.local.ops.supported()
        if self.server.supports(device_id, APP_MIRROR_FEATURE):
            return True
        return self.is_phone(device_id) and self.server.supports(device_id, MIRROR_FEATURE)

    def platform(self, device_id: str) -> str:
        """"mac", "windows", "android" or "" (others)."""
        import sys

        if device_id == self.local_id:
            text = sys.platform
        else:
            conn = getattr(self.server, "connections", {}).get(device_id)
            text = str(getattr(conn, "platform", "")) if conn is not None else ""
        text = text.lower()
        if "android" in text:
            return "android"
        if "darwin" in text or "mac" in text:
            return "mac"
        if "win" in text:
            return "windows"
        return ""

    def native(self, device_id: str) -> bool:
        """Does ``device_id`` take app_do "resize" and app_icon? (An older
        agent drops the link on a frame it doesn't know.)"""
        from curslink.protocol import APP_NATIVE_FEATURE

        return device_id == self.local_id or self.server.supports(device_id, APP_NATIVE_FEATURE)

    def can_show(self, device_id: str) -> bool:
        from curslink.protocol import APP_MIRROR_FEATURE, MIRROR_FEATURE

        if device_id == self.local_id:
            return self.can_show_local
        if self.server.supports(device_id, APP_MIRROR_FEATURE):
            return True
        return self.is_phone(device_id) and self.server.supports(device_id, MIRROR_FEATURE)

    # -- the router's hooks ---------------------------------------------------------
    def window_drag(self, device_id: str) -> bool:
        """Is a window being dragged on ``device_id`` (so a held crossing
        from there casts it, rather than being a file drag)?"""
        return device_id in self.dragging

    def on_button(self, button: str, pressed: bool) -> None:
        """Every button the host's mouse presses, wherever input is."""
        if button != "left":
            return
        device = self.router.active_device
        if device == self.local_id and self.local is not None:
            self.local.on_button(button, pressed)
        if pressed and self.through is None and self.carrying is None:
            self.resizing = self._ring_at(self.router.screen, self.router.x, self.router.y)
        elif not pressed:
            self.resizing = None
        if pressed:
            if device != self.local_id and self.is_phone(device) and self.can_cast(device) and self.through is None:
                self._phone_press = device
                self.dragging[device] = {"phone": True, "at": self._phone_at()}
            return
        if self._phone_press is not None:
            self.dragging.pop(self._phone_press, None)
            self._phone_press = None
        if self.carrying is not None:
            sid = self.carrying[0]
            self.carrying = None
            s = self.sessions.get(sid)
            if s is not None:
                log.info("app mirror: %s put down on %s/%s", s.name, s.target.device_id, s.target.id)
            self.moved()  # the pointer is near it (and its tag shows) as it's let go of
            self.changed()

    def _ring_at(self, screen, x: float, y: float) -> str | None:
        """The mirror whose ring (just outside it, where it's resized) is at
        (x, y) on ``screen``, if any."""
        for s in reversed(list(self.sessions.values())):
            if s.target != screen or s.full is not None or s.min:
                continue
            vx, vy, vw, vh = s.view
            near = vx - RESIZE_PX <= x < vx + vw + RESIZE_PX and vy - RESIZE_PX <= y < vy + vh + RESIZE_PX
            inside = vx + GRIP <= x < vx + vw - GRIP and vy + GRIP <= y < vy + vh - GRIP
            if near and not inside:
                return s.id
        return None

    def inside(self, device_id: str, x: float, y: float):
        """The screen the app the pointer is in is on, while (x, y) on
        ``device_id`` is in its window (None otherwise): the pointer goes
        there, wherever the screens' edges are."""
        s = self.sessions.get(self.through) if self.through else None
        if s is None or s.source != device_id:
            return None
        rx, ry, rw, rh = s.rect
        return s.src_screen if rx <= x < rx + rw and ry <= y < ry + rh else None

    def _phone_at(self) -> list[float]:
        s = self.router.screen
        return [round((self.router.x - s.x) / max(1, s.width), 4), round((self.router.y - s.y) / max(1, s.height), 4)]

    def on_drag(self, device_id: str, data: dict) -> None:
        """``app_drag`` from a device (or this computer's own watch)."""
        window = data.get("window")
        if not isinstance(window, dict) or _rect(window.get("os")) is None:
            self.dragging.pop(device_id, None)
            return
        self.dragging[device_id] = window
        # The app's own title bar, pressed in its mirror: the mirror moves.
        if self.through is not None:
            s = self.sessions.get(self.through)
            if s is not None and s.source == device_id and s.window.get("wid") == window.get("wid"):
                self._carry_from_inside(s, window)

    def scale(self, device_id: str) -> tuple[float, float]:
        """Pointer motion on ``device_id`` per host mouse pixel: through a
        mirror, the app's pixels per mirror pixel."""
        s = self.sessions.get(self.through) if self.through else None
        if s is None or s.source != device_id:
            return (1.0, 1.0)
        return (s.rect[2] / max(1.0, s.view[2]), s.rect[3] / max(1.0, s.view[3]))

    def in_use(self):
        """The screen showing the mirror the pointer is in (None: not in
        one). Mirror mode treats that screen as the one in use, so going in
        and out of an app's mirror doesn't switch the boundary mirror off
        and on."""
        s = self.sessions.get(self.through) if self.through else None
        return s.target if s is not None and self.router.active_device == s.source else None

    def holds(self, device_id: str) -> bool:
        """Looking at ``device_id`` shouldn't take the pointer: it's showing
        the mirror the pointer is in."""
        s = self.sessions.get(self.through) if self.through else None
        return s is not None and s.target.device_id == device_id

    def hop(self, device_id: str, screen, x: float, y: float):
        """The pointer would be at (x, y) on ``device_id`` (``screen``, or None
        off its screens): ``(screen, x, y)`` where it really goes instead
        (into a mirror's app, or out of it), or None."""
        if self.carrying is not None:
            return None
        if self.through is not None:
            s = self.sessions.get(self.through)
            if s is None or s.source != device_id:
                self.through = None
                return None
            rx, ry, rw, rh = s.rect
            if rx <= x < rx + rw and ry <= y < ry + rh:
                return None
            u, v = (x - rx) / rw, (y - ry) / rh
            vx, vy, vw, vh = s.view
            t = s.target
            # A side of the mirror against its screen's edge (or the menu bar)
            # is the app's edge, as a window's would be: the pointer stops
            # there, in the app, rather than falling out onto the sliver
            # beside it and in again (the title bar and tabs are up there).
            # Unless that edge leads on (to another screen, or another
            # device): then out through it, as from a full screen app.
            # Filling the screen, the gaps left are the menu bar and the Dock
            # (or the taskbar): those are always reached, as from any app.
            wall = 1 if s.full is not None else WALL_PX
            walled = ((u < 0 and vx - t.x <= wall) or (u >= 1 and t.right - (vx + vw) <= wall)
                      or (v < 0 and vy - t.y <= wall) or (v >= 1 and t.bottom - (vy + vh) <= wall))
            if walled:
                beyond = self._beyond(t, vx + u * vw, vy + v * vh, u, v)
                if beyond is not None:
                    self.through = None
                    self._hover(s, False)
                    return beyond
                cx, cy = min(max(x, rx), rx + rw - 1), min(max(y, ry), ry + rh - 1)
                here = self.layout.screen_at(cx, cy, s.source) or s.src_screen
                return (here, cx, cy) if here is s.src_screen else (here, *here.clamp(cx, cy))
            # Out just past the mirror's edge, into its ring (where it's
            # resized, and its corner's handle shows): moving on leaves it,
            # moving back goes in again.
            tx = vx - LAND_PX if u < 0 else vx + vw - 1 + LAND_PX if u >= 1 else min(max(vx + u * vw, vx), vx + vw - 1)
            ty = vy - LAND_PX if v < 0 else vy + vh - 1 + LAND_PX if v >= 1 else min(max(vy + v * vh, vy), vy + vh - 1)
            tx, ty = s.target.clamp(tx, ty)
            self.through = None
            self._hover(s, True)
            return (s.target, tx, ty)
        if screen is None:
            return None
        for s in reversed(list(self.sessions.values())):
            if s.target != screen or not self.router.available(s.source) or s.id == self.resizing or s.min:
                continue
            vx, vy, vw, vh = s.view
            if not (vx + GRIP <= x < vx + vw - GRIP and vy + GRIP <= y < vy + vh - GRIP):
                continue
            u, v = (x - vx) / vw, (y - vy) / vh
            rx, ry, rw, rh = s.rect
            sx, sy = rx + u * rw, ry + v * rh
            here = self.layout.screen_at(sx, sy, s.source) or s.src_screen
            self.through = s.id
            s.raised += 1
            self._hover(s, True, notify=False)
            self._do(s, "raise")
            self.changed()  # the mirror comes to the front there
            return (here, *here.clamp(sx, sy))
        return None

    def _beyond(self, t, px: float, py: float, u: float, v: float):
        """Past the edge of screen ``t`` the mirror is against, heading out of
        it at (px, py): ``(screen, x, y)`` where the pointer goes on to (one
        of that device's other screens, or another device's), or None (the
        edge leads nowhere: it's a wall)."""
        dx = -1 if u < 0 else 1 if u >= 1 else 0
        dy = -1 if v < 0 else 1 if v >= 1 else 0
        ex, ey = t.clamp(px, py)
        nx, ny = ex + dx * 3, ey + dy * 3
        here = self.layout.screen_at(nx, ny, t.device_id)
        if here is not None and here != t:
            return (here, *here.clamp(nx, ny))
        settings = getattr(self.router, "settings", None)
        if settings is not None and not getattr(settings, "edge_switching", True):
            return None
        try:
            crossing = self.layout.find_crossing(t.device_id, ex, ey, dx * 3, dy * 3,
                                                 getattr(settings, "gap_tolerance_px", 0))
        except Exception:  # pragma: no cover - a layout without crossings
            return None
        if crossing is None or not self.router.available(crossing.screen.device_id):
            return None
        return (crossing.screen, crossing.x, crossing.y)

    def moved(self) -> None:
        """After every move of the pointer (the router's on_moved)."""
        r = self.router
        if self.carrying is not None:
            sid, u, v = self.carrying
            s = self.sessions.get(sid)
            if s is None or r.screen.device_id != s.target.device_id:
                return
            if s.target != r.screen:
                s.target = r.screen  # carried onto another screen of the same device
                self._rescale(s)
            now = self.clock()
            if now - self._placed_at < PLACE_EVERY_S:
                return
            self._placed_at = now
            vw, vh = s.view[2], s.view[3]
            s.view = self._clamped(s.target, r.x - u * vw, r.y - v * vh, vw, vh)
            self.changed()
            return
        for s in self.sessions.values():
            if self.through == s.id:
                continue
            vx, vy, vw, vh = s.view
            near = (not s.min and r.screen == s.target and vx - NEAR_PX <= r.x < vx + vw + NEAR_PX
                    and vy - NEAR_PX <= r.y < vy + vh + NEAR_PX)
            self._hover(s, near)

    def _hover(self, s: AppSession, on: bool, notify: bool = True) -> None:
        if s.hover != on:
            s.hover = on
            if notify:
                self.changed()

    def on_switch(self, event) -> None:
        """A router listener: input moved to another device."""
        if event.reason == "app":
            return
        self.through = None
        if self.carrying is not None:
            self._carried_over(event)
            return
        window = self.dragging.get(event.from_device)
        if window is None or event.reason not in ("edge", "gaze"):
            return
        if not (self.can_cast(event.from_device) and self.can_show(event.to_device)):
            log.info("app mirror: %s can't show %s's apps (update Curslink there?)", event.to_device,
                     event.from_device)
            return
        self.dragging.pop(event.from_device, None)
        self.start(event.from_device, window, event.screen)

    def _carried_over(self, event) -> None:
        sid = self.carrying[0]
        s = self.sessions.get(sid)
        if s is None:
            self.carrying = None
            return
        if event.to_device == s.source:
            log.info("app mirror: %s carried back home: stopped", s.name)
            self.carrying = None
            self.stop(sid)
            return
        if not self.can_show(event.to_device):
            return
        if s.full is not None:
            self.set_full(s, False)
        old = s.target
        s.target = event.screen
        s.dpr = self.dprs.get((s.target.device_id, s.target.id))
        k = 1 / self._scale(old, s.target) if s.phone else self._k(s.source, s.window, s.target) / s.k
        u, v = self.carrying[1], self.carrying[2]
        vw, vh = self._fitted(s.target, s.view[2] * k, s.view[3] * k)
        s.k = vw / max(1.0, s.rect[2])
        s.view = self._clamped(s.target, self.router.x - u * vw, self.router.y - v * vh, vw, vh)
        s.key += 1
        log.info("app mirror: %s carried on to %s", s.name, event.to_device)
        self._send_icon(s)
        self.changed()

    # -- sessions ----------------------------------------------------------------------
    def start(self, source: str, window: dict, target) -> AppSession | None:
        """Cast ``window`` (on ``source``) to the screen ``target``, where the
        pointer just arrived, carrying it there until the button comes up."""
        phone = bool(window.get("phone"))
        if phone:
            src_screen = next(iter(self.layout.screens(source)), None)
            if src_screen is None:
                return None
            rect = (float(src_screen.x), float(src_screen.y), float(src_screen.width), float(src_screen.height))
            name = self.names.get(source, source)
            window = {}
        else:
            ox, oy, w, h = _rect(window.get("start") or window.get("os")) or _rect(window.get("os"))
            src_screen = self._screen_os(source, ox + w / 2, oy + min(h / 2, 20))
            if src_screen is None:
                return None
            lx, ly = src_screen.from_os(ox, oy)
            rect = (lx, ly, w, h)
            app = str(window.get("app") or "App")
            name = f"{app} · {self.names.get(source, source)}"
        k = 1 / self._scale(src_screen, target) if phone else self._k(source, window, target)
        vw, vh = self._fitted(target, rect[2] * k, rect[3] * k)
        at = window.get("at") if not phone else None
        if not phone:
            u, v = (float(at[0]), float(at[1])) if isinstance(at, list) and len(at) == 2 else (0.5, 0.02)
        else:
            u, v = 0.5, 0.0
        view = self._clamped(target, self.router.x - u * vw, self.router.y - v * vh, vw, vh)
        self._n += 1
        sid = f"{PREFIX}{source}:{window.get('wid', 'screen')}:{self._n}"
        s = AppSession(sid, source, {k2: window[k2] for k2 in ("wid", "pid", "app", "title", "scale") if k2 in window},
                       src_screen, rect, target, view, name, phone=phone, k=vw / max(1.0, rect[2]))
        self.sessions[sid] = s
        self.carrying = (sid, u, v)
        log.info("app mirror: casting %s from %s to %s/%s (%.0fx%.0f shown at %.0fx%.0f)", name, source,
                 target.device_id, target.id, rect[2], rect[3], vw, vh)
        if not phone:
            if source == self.local_id:
                self.release_local()
            # The real window goes back where it was before it was dragged.
            self._do(s, "restore", os=[round(v2, 1) for v2 in (window.get("start") or window["os"])])
        self.changed()
        return s

    def _carry_from_inside(self, s: AppSession, window: dict) -> None:
        """The app's title bar was pressed and dragged in its mirror: put the
        real window back, and let the pointer carry the mirror instead."""
        r = self.router
        if s.full is not None:
            self.set_full(s, False)
        rx, ry, rw, rh = s.rect
        u = min(1.0, max(0.0, (r.x - rx) / rw))
        v = min(1.0, max(0.0, (r.y - ry) / rh))
        vx, vy, vw, vh = s.view
        tx, ty = s.target.clamp(vx + u * vw, vy + v * vh)
        self.through = None
        self.dragging.pop(s.source, None)
        if s.source == self.local_id:
            self.release_local()
        self._do(s, "restore", os=[round(v2, 1) for v2 in (window.get("start") or window["os"])])
        self.carrying = (s.id, u, v)
        r.switch_to(s.target, tx, ty, "app")
        log.info("app mirror: moving %s's mirror by its title bar", s.name)
        self.changed()

    def stop(self, sid: str) -> None:
        s = self.sessions.pop(sid, None)
        if s is None:
            return
        if self.through == sid:
            self.through = None
            if self.router.active_device == s.source:
                vx, vy, vw, vh = s.view
                self.router.switch_to(s.target, *s.target.clamp(vx + vw / 2, vy + vh / 2), "app")
        if self.carrying is not None and self.carrying[0] == sid:
            self.carrying = None
        if not s.phone:
            self._do(s, "stop")
        log.info("app mirror: stopped casting %s", s.name)
        self.changed()

    def stop_all(self) -> None:
        for sid in list(self.sessions):
            self.stop(sid)

    def on_disconnect(self, device_id: str) -> None:
        self.dragging.pop(device_id, None)
        for sid, s in list(self.sessions.items()):
            if device_id in (s.source, s.target.device_id):
                self.stop(sid)

    def on_message(self, device_id: str, t: str, data: dict) -> None:
        """app_drag, app_window and app_view from a device."""
        if t == "app_drag":
            self.on_drag(device_id, data)
        elif t == "app_window":
            self.on_window(device_id, data)
        elif t == "app_view":
            self.on_view(device_id, data)

    def on_window(self, device_id: str, data: dict) -> None:
        s = self.sessions.get(data.get("id"))
        if s is None or s.source != device_id:
            return
        if data.get("gone"):
            log.info("app mirror: %s closed", s.name)
            self.stop(s.id)
            return
        if isinstance(data.get("icon"), str) and 0 < len(data["icon"]) <= MAX_ICON:
            s.icon = data["icon"]
            self._send_icon(s)
        if data.get("full"):
            # Its own full screen button (or its title bar double clicked).
            self.set_full(s, s.full is None)
            return
        if data.get("min"):
            # Its own minimise button (or it was minimised where it runs, and
            # put back there): its mirror goes to its taskbar button, or the Dock.
            self.set_min(s, True)
            return
        r = _rect(data.get("os"))
        if r is None:
            return
        ox, oy, w, h = r
        if self.hide and self.layout.screen_at_os(device_id, ox + w / 2, oy + min(h / 2, 20)) is None:
            # Off every screen there: parked out of sight while it's cast (an
            # older Curslink there says so), not moved. It's still where it was.
            ox, oy = s.src_screen.to_os(s.rect[0], s.rect[1])
        screen = self._screen_os(device_id, ox + w / 2, oy + min(h / 2, 20)) or s.src_screen
        lx, ly = screen.from_os(ox, oy)
        resized = abs(w - s.rect[2]) >= 1 or abs(h - s.rect[3]) >= 1
        s.src_screen, s.rect = screen, (lx, ly, w, h)
        if resized and s.full is not None:
            # Filling its screen: it stays filling it, the picture fitted in.
            s.k = s.view[2] / max(1.0, w)
            s.key += 1
        elif resized:
            # The mirror keeps its scale, and its top-left corner (as big as
            # it was made, if it was resized by hand).
            vw, vh = self._fitted(s.target, w * s.k, h * s.k, 1.0 if s.sized else MAX_VIEW)
            s.view = self._clamped(s.target, s.view[0], s.view[1], vw, vh)
            s.key += 1
        self.changed()

    def on_view(self, device_id: str, data: dict) -> None:
        s = self.sessions.get(data.get("id"))
        if s is None or s.target.device_id != device_id:
            return
        if data.get("closed"):
            self.stop(s.id)
            return
        if data.get("full"):
            # Its tag's box: fill the screen, or back.
            self.set_full(s, s.full is None)
            return
        work = _rect(data.get("work"))
        if work is not None:
            self._got_work(s, device_id, work)
        if isinstance(data.get("min"), bool):
            self.set_min(s, data["min"], tell=False)
        if isinstance(data.get("dpr"), (int, float)) and 0.5 <= data["dpr"] <= 4:
            self._got_dpr(s, float(data["dpr"]))
        r = _rect(data.get("os"))
        if r is None or (self.carrying is not None and self.carrying[0] == s.id) or s.full is not None:
            return
        ox, oy, w, h = r
        screen = self._screen_os(device_id, ox + w / 2, oy + h / 2) or s.target
        lx, ly = screen.from_os(ox, oy)
        resized = abs(w - s.view[2]) >= 2 or abs(h - s.view[3]) >= 2
        s.target, s.view = screen, (lx, ly, w, h)
        if resized:
            s.sized = True
            if s.phone or not self.native(s.source):
                # A phone's screen can't change (nor can an older agent's
                # window, from here): the picture scales.
                s.k = w / max(1.0, s.rect[2])
            else:
                # The app itself takes the new size (it lays itself out
                # again, sharp); its mirror follows when it has. Never bigger
                # than its own screen: there, all of it can be reached.
                aw, ah = self._app_size(s, w / s.k, h / s.k)
                s.k = w / max(1.0, aw)
                self._do(s, "resize", size=[round(aw), round(ah)])
        self.changed()

    def _app_size(self, s: AppSession, w: float, h: float) -> tuple[float, float]:
        """``w`` x ``h`` (the app's own pixels), made smaller, keeping its
        shape, to fit on the screen it's on (below a Mac's menu bar)."""
        src = s.src_screen
        room_w = float(src.width)
        room_h = float(src.height) - MENU_BAR.get(self.platform(s.source), 0)
        k = min(1.0, room_w / max(1.0, w), room_h / max(1.0, h))
        return (max(80.0, w * k), max(60.0, h * k))

    def set_full(self, s: AppSession, on: bool) -> None:
        """Make ``s``'s mirror fill its screen (all of it, like a full screen
        app), the app sized to that screen's shape; or put it back as it was."""
        if s.phone or (s.full is not None) == bool(on):
            return
        t = s.target
        if on:
            s.full = (s.view, (s.rect[2], s.rect[3]), s.k)
            aw, ah = self._fill(s)
            log.info("app mirror: %s fills %s/%s at %s (the app at %.0fx%.0f)", s.name, t.device_id, t.id,
                     [round(v) for v in s.view], aw, ah)
        else:
            view, size, k = s.full
            s.full = None
            s.view = self._clamped(t, *view)
            s.k = k
            if self.native(s.source):
                self._do(s, "resize", size=[round(size[0]), round(size[1])])
            log.info("app mirror: %s back to its own size", s.name)
        s.key += 1
        self.changed()

    def _fill(self, s: AppSession) -> tuple[float, float]:
        """Size a full mirror: all of the screen apps fill there (the menu
        bar and the Dock, or the taskbar, stay, as for its own apps' full
        windows), the app sized to that shape."""
        t = s.target
        s.view = self.work_area(t)
        k = self._k(s.source, s.window, t)
        aw, ah = self._app_size(s, s.view[2] / k, s.view[3] / k)
        s.k = min(s.view[2] / max(1.0, aw), s.view[3] / max(1.0, ah))
        if self.native(s.source):
            self._do(s, "resize", size=[round(aw), round(ah)])
        return aw, ah

    def work_area(self, t) -> tuple[float, float, float, float]:
        """The part of screen ``t`` an app fills (layout px): all of it but
        its menu bar and Dock, or its taskbar, as its helper said; until it
        says, all but a Mac's menu bar."""
        got = self.works.get((t.device_id, t.id))
        if got is not None:
            return got
        top = MENU_BAR.get(self.platform(t.device_id), 0)
        return (float(t.x), float(t.y + top), float(t.width), float(t.height - top))

    def _got_work(self, s: AppSession, device_id: str, os_rect) -> None:
        ox, oy, w, h = os_rect
        screen = self._screen_os(device_id, ox + w / 2, oy + h / 2) or s.target
        if w < 100 or h < 100:
            return
        lx, ly = screen.from_os(ox, oy)
        lx, ly = max(lx, screen.x), max(ly, screen.y)
        w, h = min(w, screen.right - lx), min(h, screen.bottom - ly)
        area = (float(lx), float(ly), float(w), float(h))
        key = (screen.device_id, screen.id)
        if self.works.get(key) == area:
            return
        self.works[key] = area
        if s.full is not None and s.target == screen and s.view != area:
            self._fill(s)  # filling it already (sized before its helper said): to the area
            s.key += 1
            self.changed()

    def set_min(self, s: AppSession, on: bool, tell: bool = True) -> None:
        """Minimise ``s``'s mirror (to its taskbar button, or the Dock: it
        stays on that screen, put away), or bring it back. ``tell``: the
        device showing it is told (it wasn't the one that did it)."""
        on = bool(on)
        if s.min == on:
            return
        s.min = on
        log.info("app mirror: %s %s", s.name, "minimised" if on else "back from minimised")
        if on:
            s.hover = False
            if self.carrying is not None and self.carrying[0] == s.id:
                self.carrying = None
            if self.through == s.id:
                # The pointer was in the app (its minimise button): it stays
                # where it is on the screen showing it, the mirror gone.
                self.through = None
                if self.router.active_device == s.source:
                    rx, ry, rw, rh = s.rect
                    u = min(1.0, max(0.0, (self.router.x - rx) / max(1.0, rw)))
                    v = min(1.0, max(0.0, (self.router.y - ry) / max(1.0, rh)))
                    vx, vy, vw, vh = s.view
                    self.router.switch_to(s.target, *s.target.clamp(vx + u * vw, vy + v * vh), "app")
        else:
            s.key += 1  # its whole picture again
        self.changed()

    def _rescale(self, s: AppSession) -> None:
        """``s``'s mirror moved onto another screen of the same device: at
        that screen's scaling (a Windows screen's), as a window moved there is."""
        s.dpr = self.dprs.get((s.target.device_id, s.target.id), s.dpr)
        if s.phone or s.sized or s.full is not None:
            return
        k = self._k(s.source, s.window, s.target)
        if abs(k - s.k) > 0.01:
            vw, vh = self._fitted(s.target, s.rect[2] * k, s.rect[3] * k)
            s.view = self._clamped(s.target, s.view[0], s.view[1], vw, vh)
            s.k = vw / max(1.0, s.rect[2])
            if self.carrying is not None and self.carrying[0] == s.id:
                u, v = self.carrying[1], self.carrying[2]
                s.view = self._clamped(s.target, self.router.x - u * vw, self.router.y - v * vh, vw, vh)
            s.key += 1

    def _send_icon(self, s: AppSession) -> None:
        """The app's icon to the device showing its mirror (its taskbar
        button, or the Dock)."""
        if not s.icon:
            return
        target = s.target.device_id
        if target == self.local_id:
            if self.local_peer is not None:
                self.local_peer.handle("app_icon", {"id": s.id, "png": s.icon})
        elif self.native(target):
            self.server.send(target, "app_icon", id=s.id, png=s.icon)

    def _got_dpr(self, s: AppSession, dpr: float) -> None:
        """The helper showing ``s`` said its screen's pixel ratio: a Windows
        screen's scaling sets how big the app is shown there."""
        self.dprs[(s.target.device_id, s.target.id)] = dpr
        if s.dpr == dpr:
            return
        s.dpr = dpr
        if s.phone or s.sized or self.platform(s.target.device_id) != "windows":
            self.changed()
            return
        k = self._k(s.source, s.window, s.target)
        if abs(k - s.k) > 0.01:
            vx, vy, vw, vh = s.view
            nw, nh = self._fitted(s.target, s.rect[2] * k, s.rect[3] * k)
            s.view = self._clamped(s.target, vx + (vw - nw) / 2, vy + (vh - nh) / 2, nw, nh)
            s.k = nw / max(1.0, s.rect[2])
            s.key += 1
        self.changed()

    # -- what each device captures and shows ---------------------------------------------
    def route(self, sid) -> tuple[str, str] | None:
        """(the device capturing, the device showing) for a stream id."""
        s = self.sessions.get(sid)
        return (s.source, s.target.device_id) if s is not None else None

    def states(self, fps: Callable[[str, str], int], quality: Callable[[str, str], str],
               tiles: Callable[[str], bool]) -> dict[str, dict]:
        """Captures and shows to add to each device's ``mirror`` state."""
        from curslink.mirror import screen_os

        out: dict[str, dict] = {}
        for s in self.sessions.values():
            target = s.target.device_id
            src = out.setdefault(s.source, {"captures": [], "shows": []})
            if s.phone:
                os_rect = [s.src_screen.os_x, s.src_screen.os_y, s.src_screen.width, s.src_screen.height]
            else:
                ox, oy = s.src_screen.to_os(s.rect[0], s.rect[1])
                os_rect = [ox, oy, round(s.rect[2]), round(s.rect[3])]
            in_use = self.through == s.id
            carried = self.carrying is not None and self.carrying[0] == s.id
            rate = fps(s.source, target)
            # The app in use goes first: as many frames as the link takes;
            # one the pointer is near, fewer while another is used; the rest
            # show their last picture (a still, refreshed now and then): no
            # stream to flicker, and nothing to keep up.
            if in_use:
                rate = min(MAX_IN_USE_FPS, max(IN_USE_FPS, rate))
            elif s.hover or carried:
                rate = min(rate, IDLE_FPS) if self.through is not None else rate
            else:
                rate = min(rate, STILL_FPS)
            capture = {"id": s.id, "screen": s.src_screen.id, "os": os_rect, "screen_os": screen_os(s.src_screen),
                       "fps": rate, "key": s.key}
            if not s.phone:
                # The pointer isn't in the picture: the mirror draws it where
                # the host says (no waiting for a frame), in the look sent.
                capture.update(window=dict(s.window), pointer=True, use=in_use, hide=self.hide)
                # No more pixels than the mirror has (the app is drawn at its
                # own resolution, then sent at the screen's).
                px = (s.dpr or 2.0) if self.platform(target) == "mac" else 1.0
                capture["out"] = [max(16, round(s.view[2] * px)), max(16, round(s.view[3] * px))]
            if tiles(target):
                capture.update(tiles=True, quality=quality(s.source, target))
            src["captures"].append(capture)
            dst = out.setdefault(target, {"captures": [], "shows": []})
            vx, vy = s.target.to_os(s.view[0], s.view[1])
            dst["shows"].append({"id": s.id, "app": True, "screen": s.target.id,
                                 "os": [vx, vy, round(s.view[2]), round(s.view[3])],
                                 "screen_os": screen_os(s.target), "size": [round(s.view[2]), round(s.view[3])],
                                 "ratio": round(s.rect[2] / s.rect[3], 5), "label": s.name, "edges": [],
                                 "corner": 0 if s.full is not None else self._corner(s),
                                 "min": s.min,
                                 "hover": s.hover or self.through == s.id, "raise": s.raised,
                                 "carried": carried, "full": s.full is not None,
                                 "app_name": str(s.window.get("app") or s.name)})
        return out

    def _corner(self, s: AppSession) -> float:
        """How round its mirror's corners are (its screen's px): the app's
        own (scaled with it), never less than the windows' of the computer
        showing it (a PC app's 8 px shown at half size on a Mac looked square)."""
        own = CORNERS.get(self.platform(s.source), 0) * s.view[2] / max(1.0, s.rect[2])
        if s.phone:
            return round(own, 1)  # a phone's whole screen: square, as it's captured
        here = CORNERS.get(self.platform(s.target.device_id), 0)
        return round(max(own, here), 1)

    # -- geometry --------------------------------------------------------------------------
    def _screen_os(self, device_id: str, ox: float, oy: float):
        found = self.layout.screen_at_os(device_id, ox, oy)
        if found is not None:
            return found
        best = None
        for sc in self.layout.screens(device_id):
            cx = min(max(ox, sc.os_x), sc.os_x + sc.width)
            cy = min(max(oy, sc.os_y), sc.os_y + sc.height)
            d = (cx - ox) ** 2 + (cy - oy) ** 2
            if best is None or d < best[0]:
                best = (d, sc)
        return best[1] if best else None

    def _k(self, source: str, window: dict, target) -> float:
        """The mirror's OS pixels per pixel of the window: the app is shown at
        the size it is in its own logical pixels, as the screen showing it
        counts them (its own scaling), like a window moved to another
        monitor; so it's sharp and its text the size of that screen's own."""
        src = float(window.get("scale") or 1.0) if self.platform(source) == "windows" else 1.0
        dst = 1.0
        if self.platform(target.device_id) == "windows":
            dst = self.dprs.get((target.device_id, target.id)) or 1.0
        k = dst / max(0.25, src)
        return k if 0.2 <= k <= 5 else 1.0

    def pointers(self) -> dict[str, tuple[str, tuple[float, float] | None]]:
        """Where the pointer is in each app's mirror: id -> (device showing
        it, (x, y) as fractions of it, or None when it isn't in it)."""
        out = {}
        r = self.router
        for s in self.sessions.values():
            p = None
            if s.id == self.through and not s.phone and r.active_device == s.source:
                rx, ry, rw, rh = s.rect
                p = ((r.x - rx) / max(1.0, rw), (r.y - ry) / max(1.0, rh))
                if not (-0.01 <= p[0] <= 1.01 and -0.01 <= p[1] <= 1.01):
                    p = None
            out[s.id] = (s.target.device_id, p)
        return out

    def _scale(self, src, dst) -> float:
        """``src``'s pixels per ``dst`` pixel on the desk, from the two
        screens' real sizes (1 when either isn't known): a mirror is shown at
        the size the app really is."""
        physical = getattr(self.layout, "physical", {})
        a, b = physical.get((src.device_id, src.id)), physical.get((dst.device_id, dst.id))
        if not a or not b or not a[0] or not b[0]:
            return 1.0
        k = (src.width / a[0]) / (dst.width / b[0])
        return k if 0.2 <= k <= 5 else 1.0

    @staticmethod
    def _fitted(screen, w: float, h: float, share: float = MAX_VIEW) -> tuple[float, float]:
        """``w`` x ``h``, made smaller (keeping its shape) to fit the screen
        (``share`` of it)."""
        k = min(1.0, share * screen.width / max(1.0, w), share * screen.height / max(1.0, h))
        w, h = w * k, h * k
        if w < MIN_VIEW:
            h, w = h * MIN_VIEW / max(1.0, w), MIN_VIEW
        return round(w), round(h)

    @staticmethod
    def _clamped(screen, x: float, y: float, w: float, h: float) -> tuple[float, float, float, float]:
        x = min(max(x, screen.x), screen.right - w) if w <= screen.width else screen.x
        y = min(max(y, screen.y), screen.bottom - h) if h <= screen.height else screen.y
        return (round(x), round(y), w, h)

    def _do(self, s: AppSession, what: str, **data) -> None:
        if s.phone:
            return
        if s.source == self.local_id:
            if self.local is not None:
                self.local.handle({"id": s.id, "do": what, "window": s.window, **data})
        else:
            self.server.send(s.source, "app_do", id=s.id, do=what, window=s.window, **data)
