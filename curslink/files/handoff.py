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

All of this runs on the host's event loop.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Callable

from curslink.files.meta import Dragged, card, describe, human_size
from curslink.files.transfer import Outgoing, new_id, send_files
from curslink.layout import Layout
from curslink.protocol import FILES_FEATURE
from curslink.router import Router, SwitchEvent

log = logging.getLogger(__name__)

DRAG_START_PX = 8        # a press that moves this far is a drag
PROBE_WAIT_S = 0.6       # how long the shield waits to hear of a drag
SHIELD_LINGER_S = 1.0    # after a drop, before taking the shield down


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
    ):
        """``helper(line)`` sends a command to the overlay helper;
        ``inject("nudge" | "release")`` (blocking, run off the loop) moves the
        host's parked pointer a pixel and back, and for "release" lets go of
        the left button too, so the host's OS sees it although input is grabbed.
        ``drag_source()`` (macOS) gives the drag pasteboard's (change count,
        file paths); with it, no drop-target windows are used to find drags."""
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

    # --------------------------------------------------------- local input
    def on_button(self, button: str, pressed: bool) -> bool:
        """Called before the router. True: the event was a drop, don't forward it."""
        if button != "left":
            return False
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
        if not line.startswith("drag "):
            return
        try:
            data = json.loads(line[5:])
        except ValueError:
            return
        paths = [p for p in data.get("paths", []) if isinstance(p, str)] if isinstance(data, dict) else []
        log.info("drag: a %s drag reached a drop target", "file" if paths else "non-file")
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
        items = await asyncio.to_thread(describe, paths)
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
        if event.to_device == self.router.local_id:
            if self.carrying is not None:
                log.info("drag came back to this computer")
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
        self.set_shield(True)  # the host's own drag rests on it until the drop
        self.server.send(device_id, "drag", card=card(self.items))

    # ---------------------------------------------------------------- drop
    def drop(self) -> None:
        device_id, items = self.carrying, self.items
        self.carrying = None
        log.info("drag: dropped on %s; sending", device_id)
        self.server.send(device_id, "drag", hide=True)
        out = Outgoing(new_id(), device_id, items)
        self.outgoing[out.id] = out
        loop = asyncio.get_running_loop()
        out.task = loop.create_task(self._send(out))
        loop.create_task(self._end_host_drag(self._gen))
        self._clear_local(keep_shield=True)

    async def _send(self, out: Outgoing) -> None:
        names = ", ".join(i.name for i in out.items[:3])
        self.emit("file", state="sending", id=out.id, to=out.device_id, names=names)
        try:
            await send_files(self.server.bulk_sender(out.device_id), out, self.local_name)
            log.info("sent %s (%s) to %s", names, human_size(out.total), out.device_id)
        except Exception as exc:
            self.outgoing.pop(out.id, None)
            log.warning("couldn't send %s to %s: %s", names, out.device_id, out.refused or exc)
            self.emit("file", state="failed", id=out.id, to=out.device_id, error=out.refused or str(exc))

    def on_result(self, device_id: str, data: dict) -> None:
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
