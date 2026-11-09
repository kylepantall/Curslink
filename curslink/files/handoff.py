"""Carrying a file drag from the host to another device, and dropping it there.

How a drag is noticed. On macOS, from the system's drag pasteboard
(:mod:`curslink.files.macdrag`): when input leaves with the button held,
the host checks whether something put files on it since the button went
down. Elsewhere no OS says "a file is being dragged" to a program that
isn't under the pointer, so:

* Edge: while the left button is held and the pointer moves, the overlay
  helper puts thin, invisible drop targets ("strips") along the host's
  screen edges that lead to another device. A file dragged to the edge
  passes over one, and the OS tells the helper what's being dragged
  (``drag`` lines on its stdout, read by :meth:`DragHandoff.on_helper`).
* Gaze (or any switch that isn't over an edge): input leaves with the
  button still held, so the helper covers the host's screens with an
  invisible drop target (the "shield") and the host nudges the parked
  pointer, which makes the OS report the drag to it.

Either way, once input is on another device with a file drag held, that
device shows a placeholder (name, kind, size) following its pointer.
Letting go there sends the files over the link
(:mod:`curslink.files.transfer`) and ends the drag on the host, where the
shield, still under the parked pointer, takes the drop and refuses it, so
the source app neither moves nor copies anything locally. Coming back to
the host with the button still held just carries on the drag there.

The other way, a file dragged on an agent over the edge to the host (or
carried there by a look): the router doesn't let go of that agent's left
button but sends ``drag_out``; the agent finds out what's dragged and ends
its drag (:mod:`curslink.files.dragout`) and answers with a card, which
follows the host's pointer until the button comes up, when the host sends
``fetch`` and the agent sends the files here.

A held drag can go on through any number of devices before it's let go
of: from the Mac over a PC to a phone, say, or from the PC through the Mac
to the phone. Each device it passes shows the card while the pointer is
there and takes it down when it moves on; only the one it's let go of on
gets the files. No file bytes move until then. A drag handed over by one
agent and let go of on another goes agent -> host -> agent: the host
fetches it from the first and passes each frame on to the second as it
comes (:meth:`DragHandoff.on_files`), never keeping more than a frame.

All of this runs on the host's event loop.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from dataclasses import dataclass
from typing import Callable

from curslink.files.meta import Dragged, card, describe, human_size
from curslink.files.transfer import Outgoing, new_id, off_loop_watched, send_files
from curslink.layout import Layout
from curslink.files.native import NativeDrop
from curslink.files.popups import ReceivePopups
from curslink.files.transfer import FileReceiver
from curslink.protocol import DROP_FEATURE, FILE_ACK_FEATURE, FILES_FEATURE, FILES_OUT_FEATURE
from curslink.router import Router, SwitchEvent
from curslink.files.dragdebug import trace

log = logging.getLogger(__name__)

DRAG_START_PX = 8        # a press that moves this far is a drag
PROBE_WAIT_S = 0.6       # how long the shield waits to hear of a drag
SHIELD_LINGER_S = 1.0    # after a drop, before taking the shield down
# File bytes in flight to an agent (sent, not yet acknowledged) once a drag
# is dropped there. Nothing is sent while it's only carried: it may yet go
# on to another device.
DROPPED_WINDOW = 512 * 1024
# Passing an agent's files on to another: if that one takes no more for this
# long, give up (the first agent's frames wait meanwhile).
RELAY_STALL_S = 5.0


@dataclass
class Relay:
    """Files an agent handed over, let go of on another agent: each frame
    from ``source`` goes on to ``target`` with ``send``."""
    id: str
    source: str
    target: str
    send: Callable
    names: str = ""


def edge_strips(layout: Layout, device_id: str, gap: int) -> list[dict]:
    """The sides of this device's screens that lead to another device, as
    ``{"x", "y", "w", "h", "side"}`` in OS coordinates (the screen's own
    rectangle plus which side)."""
    out = []
    for s in layout.screens(device_id):
        sides = {
            "left": ((-1, 0), [(s.x, s.y + s.height * k / 8) for k in range(1, 8)]),
            "right": ((1, 0), [(s.right - 1, s.y + s.height * k / 8) for k in range(1, 8)]),
            "top": ((0, -1), [(s.x + s.width * k / 8, s.y) for k in range(1, 8)]),
            "bottom": ((0, 1), [(s.x + s.width * k / 8, s.bottom - 1) for k in range(1, 8)]),
        }
        for side, ((ex, ey), points) in sides.items():
            if any(layout.find_crossing(device_id, x, y, ex, ey, gap) for x, y in points):
                out.append({"x": s.os_x, "y": s.os_y, "w": s.width, "h": s.height, "side": side})
    return out


class DragHandoff:
    def __init__(
        self,
        router: Router,
        server,
        helper: Callable[[str], None],
        inject: Callable[[str], None],
        local_name: str,
        emit: Callable[..., None] = lambda kind, **data: None,
        drag_source: Callable[[], tuple[int, list[str]] | None] | None = None,
        native_press: Callable[[], None] | None = None,
        release_held: Callable[[], None] = lambda: None,
        follow=None,
    ):
        """``helper(line)`` sends a command to the overlay helper;
        ``inject("nudge" | "release")`` (blocking, run off the loop) moves the
        host's parked pointer a pixel and back, and for "release" lets go of
        the left button too, so the host's OS sees it although input is grabbed.
        ``drag_source()`` (macOS) gives the drag pasteboard's (change count,
        file paths); with it, no drop-target windows are used to find drags.
        ``native_press()`` presses the left button here to start a real drag of
        files arriving from an agent (None: no real drags here, they go to
        Downloads); ``release_held()`` lets go of a button-up the capture held
        back while they arrived (see curslink.files.native)."""
        self.router = router
        self.server = server
        self.helper = helper
        self.inject = inject
        self.local_name = local_name
        self.emit = emit
        self.drag_source = drag_source
        self.strips = [] if drag_source else edge_strips(router.layout, router.local_id,
                                                         router.settings.gap_tolerance_px)
        self._pb_count: int | None = None     # drag pasteboard's count when the button went down
        self.left_down = False
        self.down_at: tuple[float, float] | None = None
        self.strips_up = False
        self.shield_up = False
        self.paths: list[str] | None = None   # what the OS says is being dragged
        self.items: list[Dragged] = []
        self.carrying: str | None = None      # device showing the placeholder
        self.outgoing: dict[str, Outgoing] = {}
        self._gen = 0                         # bumps on every new press: stale work drops out
        router.listeners.append(self.on_switch)
        # Files dragged from an agent, carried here or on to another agent.
        self.awaiting_out: str | None = None  # asked this agent what it's dragging
        self.released_early = False           # ...and the button came up before it said
        self.remote: dict | None = None       # {"device", "id", "card"}: handed over, not dropped yet
        self.remote_at: str | None = None     # the device it's on now (this one: local_id)
        self.expecting: set[str] = set()      # fetched transfer ids, coming here
        self.relays: dict[str, Relay] = {}    # fetched transfer ids, going on to another agent
        popups = ReceivePopups(helper, emit)
        self.receiver = FileReceiver(None, popups)
        self.native = NativeDrop(helper, self.receiver, popups, press=native_press, release=release_held,
                                 cancel=lambda: inject("release"), follow=follow)
        self.carry_id: str | None = None      # the transfer id the carrying device knows the drag by
        router.can_drag_out = lambda device_id: server.supports(device_id, FILES_OUT_FEATURE)
        router.takes_drag = lambda device_id: server.supports(device_id, FILES_FEATURE)
        router.drag_out = self.ask_drag_out

    # --------------------------------------------------------- local input
    def on_button(self, button: str, pressed: bool) -> bool:
        """Called before the router. True: the event was a drop, don't forward it."""
        if button != "left":
            return False
        if self.remote is not None or self.awaiting_out is not None:
            if pressed:
                self.cancel_incoming()
                return False
            if self.remote is not None:
                self.drop_remote()
            else:
                self.released_early = True
            return True
        if pressed:
            if self.router.is_local:
                self._reset()
                self.left_down = True
                self.down_at = None
                if self.drag_source is not None:
                    pb = self.drag_source()
                    self._pb_count = pb[0] if pb else None
            return False
        was_down, self.left_down = self.left_down, False
        if self.carrying and not self.router.is_local and was_down:
            self.drop()
            return True
        self._clear_local()
        return False

    def on_pointer(self, ox: float, oy: float) -> None:
        if not self.left_down or not self.router.is_local:
            return
        if self.down_at is None:
            self.down_at = (ox, oy)
            return
        if not self.strips_up and self.strips and (
                abs(ox - self.down_at[0]) + abs(oy - self.down_at[1]) >= DRAG_START_PX):
            self.strips_up = True
            log.info("drag: watching %d screen edge(s) for a file", len(self.strips))
            self.helper("strips " + json.dumps(self.strips, separators=(",", ":")))

    # ------------------------------------------------------ helper reports
    def on_helper(self, line: str) -> None:
        """A line from the overlay helper: ``drag {"paths": [...]}`` when a
        drag passes over one of its drop targets, ``log ...`` for the log."""
        if line.startswith("log "):
            log.info("overlay helper: %s", line[4:])
            return
        if self.native.on_helper(line):
            return
        if not line.startswith("drag "):
            return
        try:
            data = json.loads(line[5:])
        except ValueError:
            return
        paths = [p for p in data.get("paths", []) if isinstance(p, str)] if isinstance(data, dict) else []
        log.info("drag: a %s drag reached a drop target", "file" if paths else "non-file")
        trace("host-target", files=bool(paths))
        if not self.left_down:
            return
        if not paths:
            self.paths = []
            return
        if paths == self.paths:
            return
        self.paths = paths
        gen = self._gen
        asyncio.get_running_loop().create_task(self._describe(gen, paths))

    async def _describe(self, gen: int, paths: list[str]) -> None:
        items = await off_loop_watched(paths[0] if paths else "", self._slow_read, describe, paths)
        if gen != self._gen:
            return
        if not items:
            log.info("drag: none of the dragged paths could be read")
            return
        self.items = items
        log.info("dragging %s", ", ".join(i.name for i in items[:3]) + ("..." if len(items) > 3 else ""))
        if self.left_down and not self.router.is_local and self.carrying is None:
            self.carry(self.router.active_device)

    # ----------------------------------------------------------- switching
    def on_switch(self, event: SwitchEvent) -> None:
        if (self.remote is not None or self.awaiting_out is not None) and event.to_device != self.remote_at:
            # An agent's drag carried on to another device: the card goes with it.
            if self.remote is not None:
                self._hide_remote()
            self.remote_at = event.to_device
            if self.remote is not None:
                self._show_remote()
        if event.to_device == self.router.local_id:
            if self.carrying is not None:
                log.info("drag came back to this computer")
            self._cancel_carry()
            self.carrying = None
            self.set_shield(False)
            self.strips_up = False  # shown again if the drag moves on
            return
        if not self.left_down:
            return
        if self.strips_up:
            self.helper("strips off")
            self.strips_up = False
        if self.carrying is not None and self.carrying != event.to_device:
            self._cancel_carry()
            self.carrying = None  # the old device took its card down on "leave"
        if self.items:
            self.carry(event.to_device)
        elif self.drag_source is not None:
            asyncio.get_running_loop().create_task(self._check_pasteboard(self._gen))
        elif self.paths is None and event.reason != "edge":
            self.probe()

    async def _check_pasteboard(self, gen: int) -> None:
        """macOS: did a drag that started after the button went down put files
        on the drag pasteboard? Asked twice, in case the app is slow to write."""
        for attempt in range(2):
            pb = await asyncio.to_thread(self.drag_source)
            if gen != self._gen or not self.left_down:
                return
            if pb is None:
                log.info("drag: the drag pasteboard can't be read")
                return
            count, paths = pb
            if count != self._pb_count and paths:
                log.info("drag: %d file(s) on the drag pasteboard", len(paths))
                self.paths = paths
                await self._describe(gen, paths)
                return
            if attempt == 0:
                await asyncio.sleep(0.15)
        log.info("drag: input left with the button held, but no file is being dragged")

    def probe(self) -> None:
        """Input left with the button held, but not over an edge strip: is
        it a file drag? Put the shield up and nudge the pointer to find out."""
        self.set_shield(True)
        gen = self._gen
        loop = asyncio.get_running_loop()

        async def run() -> None:
            await asyncio.sleep(0.05)  # let the helper show the shield first
            await loop.run_in_executor(None, self.inject, "nudge")
            await asyncio.sleep(PROBE_WAIT_S)
            if gen == self._gen and not self.items and self.carrying is None:
                self.set_shield(False)

        loop.create_task(run())

    def carry(self, device_id: str) -> None:
        if not self.server.supports(device_id, FILES_FEATURE):
            log.warning("%s can't take dropped files (update Curslink there, or turn on [agent.files])",
                     device_id)
            return
        self.carrying = device_id
        log.info("drag: carrying %s to %s", ", ".join(i.name for i in self.items[:3]), device_id)
        trace("carry", to=device_id)
        self.set_shield(True)  # the host's own drag rests on it until the drop
        if not self.server.supports(device_id, DROP_FEATURE):
            self.server.send(device_id, "drag", card=card(self.items))  # older agent: sent on the drop
            return
        # Only the card for now: the files go when it's let go of here, and
        # not at all if it goes on to another device.
        self.carry_id = new_id()
        self.server.send(device_id, "drag", card=card(self.items), id=self.carry_id)

    def _cancel_carry(self) -> None:
        tid, self.carry_id = self.carry_id, None
        if tid is None or self.carrying is None:
            return
        log.info("drag: passed over %s without being dropped", self.carrying)
        trace("not-dropped", on=self.carrying)
        self.server.send(self.carrying, "drag", hide=True, id=tid)

    # ---------------------------------------------------------------- drop
    def drop(self) -> None:
        device_id, items = self.carrying, self.items
        self.carrying = None
        loop = asyncio.get_running_loop()
        tid, self.carry_id = self.carry_id, None
        trace("dropped-on", on=device_id)
        if tid is not None:
            log.info("drag: dropped on %s; sending", device_id)
            self.server.send(device_id, "drag", drop=True, id=tid)
            out = Outgoing(tid, device_id, items)
            if self.server.supports(device_id, FILE_ACK_FEATURE):
                out.window = DROPPED_WINDOW
            self.outgoing[out.id] = out
            out.task = loop.create_task(self._send(out))
        else:
            log.info("drag: dropped on %s; sending", device_id)
            self.server.send(device_id, "drag", hide=True)
            out = Outgoing(new_id(), device_id, items)
            self.outgoing[out.id] = out
            out.task = loop.create_task(self._send(out))
        loop.create_task(self._end_host_drag(self._gen))
        self._clear_local(keep_shield=True)

    def _slow_read(self, path: str) -> None:
        """Reading a dragged file is taking a while: on a Mac, most likely
        a prompt asking to let Curslink read the folder it's in. It can only
        be answered here, so bring the pointer home."""
        folder = os.path.basename(os.path.dirname(path)) or path
        log.warning("drag: reading %s is slow; this computer may be asking to let Curslink read %s, "
                    "so input comes back here to answer it", os.path.basename(path), folder)
        trace("slow-read", path=repr(path))
        if not self.router.is_local:
            self.router.go_home("hotkey")

    async def _send(self, out: Outgoing) -> None:
        out.on_slow = self._slow_read
        names = ", ".join(i.name for i in out.items[:3])
        self.emit("file", state="sending", id=out.id, to=out.device_id, names=names)
        try:
            await send_files(self.server.bulk_sender(out.device_id), out, self.local_name)
            log.info("sent %s (%s) to %s", names, human_size(out.total), out.device_id)
        except asyncio.CancelledError:
            return
        except Exception as exc:
            self.outgoing.pop(out.id, None)
            log.warning("couldn't send %s to %s: %s", names, out.device_id, out.refused or exc)
            self.emit("file", state="failed", id=out.id, to=out.device_id, error=out.refused or str(exc))

    def on_result(self, device_id: str, data: dict) -> None:
        relay = self.relays.get(str(data.get("id")))
        if relay is not None and relay.target == device_id:
            # Saved (or not) on the agent it was let go of on: tell the one it came from.
            self.relays.pop(relay.id, None)
            self.server.send(relay.source, "file_result", **data)
            if data.get("ok") is True:
                log.info("passed %s on from %s to %s", relay.names or "the files", relay.source, device_id)
                self.emit("file", state="sent", id=relay.id, to=device_id, saved=data.get("saved"),
                          folder=data.get("folder"))
            else:
                self.emit("file", state="failed", id=relay.id, to=device_id, error=str(data.get("error") or ""))
            return
        out = self.outgoing.get(str(data.get("id")))
        if out is None or out.device_id != device_id:
            return
        if data.get("ok") is True:
            self.outgoing.pop(out.id, None)
            self.emit("file", state="sent", id=out.id, to=device_id, saved=data.get("saved"),
                      folder=data.get("folder"))
        else:
            out.refused = str(data.get("error") or "refused")

    async def _end_host_drag(self, gen: int) -> None:
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, self.inject, "release")
        await asyncio.sleep(SHIELD_LINGER_S)
        if gen == self._gen and self.carrying is None:
            self.set_shield(False)

    # ------------------------------------------- from an agent, here or onwards
    @property
    def incoming(self) -> dict | None:
        """An agent's drag on this computer now, not dropped yet."""
        return self.remote if self.remote_at == self.router.local_id else None

    def ask_drag_out(self, device_id: str) -> None:
        """Router: input is leaving ``device_id`` with its left button held,
        for this computer or another agent that takes files. Ask it whether
        that's a file drag (and to end it there)."""
        log.info("drag: input left %s with the button held; asking what it's dragging", device_id)
        trace("came-back", from_=device_id)
        self.cancel_incoming()
        # on_switch, straight after this, says where it went.
        self.awaiting_out, self.released_early, self.remote_at = device_id, False, None
        self.server.send(device_id, "drag_out")

    def on_drag_out(self, device_id: str, data: dict) -> None:
        """The agent's answer: ``{id, card}`` or ``{none: true}``."""
        if device_id != self.awaiting_out:
            return
        self.awaiting_out = None
        tid, info = data.get("id"), data.get("card")
        if not isinstance(tid, str) or not isinstance(info, dict):
            log.info("drag: %s wasn't dragging a file", device_id)
            self.released_early = False
            self.remote_at = None
            return
        self.remote = {"device": device_id, "id": tid, "card": info}
        if self.remote_at is None:
            self.remote_at = self.router.active_device
        log.info("drag: %s is dragging %s, now on %s", device_id, info.get("name"), self.remote_at)
        self._show_remote()
        if self.released_early:
            self.released_early = False
            self.drop_remote()

    def _show_remote(self) -> None:
        rem, at = self.remote, self.remote_at
        if rem is None or at is None:
            return
        if at == self.router.local_id:
            self.native.begin(rem["id"], rem["card"])
        elif not self.server.supports(at, FILES_FEATURE):
            log.info("drag: %s can't take dropped files; carrying %s on", at, rem["card"].get("name"))
        elif self.server.supports(at, DROP_FEATURE):
            self.server.send(at, "drag", card=rem["card"], id=rem["id"])
        else:
            self.server.send(at, "drag", card=rem["card"])

    def _hide_remote(self) -> None:
        rem, at = self.remote, self.remote_at
        if rem is None or at is None:
            return
        trace("carried-past", on=at)
        if at == self.router.local_id:
            log.info("drag: carried on past this computer; not dropped here")
            self.native.cancel(rem["id"])
        elif self.server.supports(at, DROP_FEATURE):
            self.server.send(at, "drag", hide=True, id=rem["id"])
        elif self.server.supports(at, FILES_FEATURE):
            self.server.send(at, "drag", hide=True)

    def drop_remote(self) -> None:
        """Let go of an agent's drag: its files go to wherever it is now."""
        rem, at = self.remote, self.remote_at
        self.remote, self.remote_at = None, None
        if rem is None:
            return
        source, tid = rem["device"], rem["id"]
        trace("dropped-on", on=at, from_=source)
        if at == self.router.local_id:
            log.info("drag: dropped here; fetching it from %s", source)
            self.expecting.add(tid)
            self.native.let_go(tid, inject=False)  # the real button-up is on its way (or held)
            self.server.send(source, "fetch", id=tid)
            return
        if at is None or not self.server.supports(at, FILES_FEATURE) or not self.server.is_connected(source):
            log.info("drag: let go on %s, which can't take it; %s keeps its files", at, source)
            self.server.send(source, "fetch", id=tid, cancel=True)
            return
        log.info("drag: dropped on %s; passing it on from %s", at, source)
        if self.server.supports(at, DROP_FEATURE):
            self.server.send(at, "drag", drop=True, id=tid)
        else:
            self.server.send(at, "drag", hide=True)
        relay = Relay(tid, source, at, self.server.bulk_sender(at), str(rem["card"].get("name") or ""))
        self.relays[tid] = relay
        self.emit("file", state="sending", id=tid, to=at, names=relay.names)
        self.server.send(source, "fetch", id=tid)

    def cancel_incoming(self) -> None:
        """Forget an agent's drag that wasn't let go of (a new press, a new drag)."""
        if self.remote is not None:
            self._hide_remote()
            self.server.send(self.remote["device"], "fetch", id=self.remote["id"], cancel=True)
        self.remote, self.remote_at, self.awaiting_out, self.released_early = None, None, None, False

    def on_disconnect(self, device_id: str) -> None:
        """An agent went away: a drag from it, or files passing through to or from it, stop."""
        if self.awaiting_out == device_id:
            self.awaiting_out, self.released_early = None, False
        if self.remote is not None and self.remote["device"] == device_id:
            log.info("drag: %s went away; its drag is over", device_id)
            if self.remote_at != device_id:
                self._hide_remote()
            self.remote, self.remote_at = None, None
        for relay in [r for r in self.relays.values() if device_id in (r.source, r.target)]:
            self._relay_failed(relay, f"{device_id} disconnected")

    def on_files(self, device_id: str, msg):
        """file_* frames from an agent: only for transfers this computer asked
        for. Frames passing through to another agent come back as a
        coroutine, which the connection awaits before reading the next one
        (so the first agent never sends faster than the second takes)."""
        tid = msg.get("id")
        if msg.t == "file_ack":
            out = self.outgoing.get(tid)
            if out is not None and out.device_id == device_id:
                out.ack(msg.get("n"))
            return None
        relay = self.relays.get(tid)
        if relay is not None and relay.source == device_id and msg.t != "file_result":
            return self._pass_on(relay, msg)
        if tid not in self.expecting:
            return None
        if msg.t in ("file_end", "file_cancel"):
            self.expecting.discard(tid)
        reply = self.receiver.handle(msg)
        if reply is not None and b'"ok":false' in reply:
            self.expecting.discard(tid)
        return reply

    async def _pass_on(self, relay: Relay, msg) -> None:
        data = dict(msg.data)
        if msg.t == "file_begin":
            data.pop("ack", None)  # the source sends as fast as the target takes: no acks needed
        try:
            await asyncio.wait_for(relay.send(msg.t, **data), RELAY_STALL_S)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._relay_failed(relay, str(exc) or type(exc).__name__)
            return
        if msg.t == "file_cancel":
            self.relays.pop(relay.id, None)
            self.emit("file", state="failed", id=relay.id, to=relay.target, error=str(msg.get("reason") or ""))

    def _relay_failed(self, relay: Relay, why: str) -> None:
        if self.relays.pop(relay.id, None) is None:
            return
        log.warning("couldn't pass %s on from %s to %s: %s", relay.names or "the files", relay.source,
                    relay.target, why)
        self.server.send(relay.source, "fetch", id=relay.id, cancel=True)
        self.server.send(relay.target, "file_cancel", id=relay.id, reason=why[:200])
        self.emit("file", state="failed", id=relay.id, to=relay.target, error=why)

    # --------------------------------------------------------------- state
    def set_shield(self, on: bool) -> None:
        if on != self.shield_up:
            self.shield_up = on
            self.helper("shield on" if on else "shield off")

    def _clear_local(self, keep_shield: bool = False) -> None:
        if self.strips_up:
            self.helper("strips off")
            self.strips_up = False
        if self.carrying is not None:
            self.server.send(self.carrying, "drag", hide=True)
            self.carrying = None
        if not keep_shield:
            self.set_shield(False)
        self.paths = None
        self.items = []
        self.down_at = None

    def _reset(self) -> None:
        self._gen += 1
        self._clear_local()
