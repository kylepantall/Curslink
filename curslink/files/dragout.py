"""Agent side of a file drag leaving this device for the host.

The host's mouse is driving this device with its left button held (say,
dragging a file in Explorer) and the pointer goes over the edge to the host
(or you look at the host). Instead of letting go of the button here, which
would drop the file wherever the pointer stopped, the host sends
``drag_out`` and this device:

1. covers its screens with the overlay helper's invisible drop target (the
   "shield") and nudges the pointer a pixel, so the OS tells the helper what
   is being dragged (on Windows, if it says nothing, the selection in the
   Explorer window the drag started from is used instead);
2. ends its own drag with Escape and lets go of the button over the
   shield, so nothing is moved or copied here;
3. answers ``drag_out {id, card}`` (or ``{none: true}``).

The host shows the card under its own pointer; letting go there sends
``fetch {id}`` and the files come over as ``file_*`` frames, the same ones
the host uses to send files here (see :mod:`curslink.files.transfer`).
"""

from __future__ import annotations

import asyncio
import json
import logging
import subprocess
import sys
from typing import Awaitable, Callable

from curslink.files.meta import card, describe
from curslink.files.transfer import Outgoing, new_id, send_files

log = logging.getLogger(__name__)

PROBE_WAIT_S = 0.6


class AgentDragOut:
    def __init__(
        self,
        helper: Callable[[str], None],
        send: Callable[..., None],
        send_bulk: Callable[..., Awaitable[None]],
        name: str,
        nudge: Callable[[bool], None] | None = None,
        explorer: Callable[[], list[str]] | None = None,
    ):
        """``helper(line)``: a command for the overlay helper. ``send`` /
        ``send_bulk``: frames to the host. ``nudge(release)`` (blocking):
        move the held drag a pixel and back, and with ``release`` cancel it
        with Escape and let go. ``explorer()`` (blocking): the files selected
        in the window in front, when the helper can't tell."""
        self.helper = helper
        self.send = send
        self.send_bulk = send_bulk
        self.name = name
        self.nudge = nudge or _nudge
        self.explorer = explorer if explorer is not None else (explorer_selection if sys.platform == "win32" else None)
        self.pending: Outgoing | None = None
        self._heard: asyncio.Future | None = None
        self._task: asyncio.Task | None = None
        self._sending: asyncio.Task | None = None

    # ------------------------------------------------------------ helper
    def on_helper(self, line: str) -> None:
        """A line from the overlay helper (on the loop)."""
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
        log.info("drag out: the drop target saw a %s drag", "file" if paths else "non-file")
        if self._heard is not None and not self._heard.done():
            self._heard.set_result(paths)

    # -------------------------------------------------------------- frames
    def start(self) -> None:
        """``drag_out`` from the host: find out what's dragged, end the drag here."""
        self.pending = None
        if self._task is not None and not self._task.done():
            return
        self._task = asyncio.get_running_loop().create_task(self._probe())

    async def _probe(self) -> None:
        loop = asyncio.get_running_loop()
        self._heard = loop.create_future()
        paths: list[str] = []
        try:
            self.helper("shield on")
            await asyncio.sleep(0.06)  # let the helper put it up
            await loop.run_in_executor(None, self.nudge, False)
            try:
                paths = await asyncio.wait_for(asyncio.shield(self._heard), PROBE_WAIT_S)
            except asyncio.TimeoutError:
                log.info("drag out: the drop target heard nothing")
            if not paths and self.explorer is not None:
                paths = await loop.run_in_executor(None, self.explorer)
                if paths:
                    log.info("drag out: using the %d item(s) selected in Explorer", len(paths))
        finally:
            # End the drag here either way: Escape, then let go over the shield.
            await loop.run_in_executor(None, self.nudge, True)
            await asyncio.sleep(0.3)
            self.helper("shield off")
            self._heard = None
        items = await asyncio.to_thread(describe, paths) if paths else []
        if not items:
            log.info("drag out: not a file drag")
            self.send("drag_out", none=True)
            return
        self.pending = Outgoing(new_id(), "host", items)
        log.info("drag out: %s, waiting to be dropped on the host", ", ".join(i.name for i in items[:3]))
        self.send("drag_out", id=self.pending.id, card=card(items))

    def owns(self, tid) -> bool:
        return self.pending is not None and tid == self.pending.id

    def fetch(self, data: dict) -> None:
        out = self.pending
        if out is None or data.get("id") != out.id:
            return
        if data.get("cancel"):
            log.info("drag out: the host didn't take it")
            self.pending = None
            if self._sending is not None:
                self._sending.cancel()
            return
        log.info("drag out: the host wants it; sending")
        self._sending = asyncio.get_running_loop().create_task(self._send(out))

    async def _send(self, out: Outgoing) -> None:
        try:
            await send_files(self.send_bulk, out, self.name)
        except asyncio.CancelledError:
            return
        except Exception as exc:
            log.warning("couldn't send %s to the host: %s", out.items[0].name, out.refused or exc)

    def result(self, data: dict) -> None:
        out = self.pending
        if out is None:
            return
        if data.get("ok") is True:
            log.info("drag out: the host saved %s", ", ".join(map(str, data.get("saved") or [])))
            self.pending = None
        else:
            out.refused = str(data.get("error") or "refused")

    def close(self) -> None:
        """The link dropped."""
        self.pending = None
        if self._sending is not None:
            self._sending.cancel()


def _nudge(release: bool) -> None:
    from pynput import mouse

    from curslink.inputs.pynput_backend import drag_nudge

    x, y = mouse.Controller().position
    drag_nudge(int(x), int(y), release=release)


# Paths of the selected items in the File Explorer window in front.
_EXPLORER_PS = r"""
$fg = [int64]$args[0]
foreach ($w in (New-Object -ComObject Shell.Application).Windows()) {
  try {
    if ([int64]$w.HWND -eq $fg) {
      foreach ($i in $w.Document.SelectedItems()) { [Console]::Out.WriteLine($i.Path) }
    }
  } catch {}
}
"""


def explorer_selection() -> list[str]:
    """Windows: the items selected in the File Explorer window in front,
    which is what a drag from it carries. [] if that isn't Explorer."""
    try:
        import ctypes

        hwnd = ctypes.windll.user32.GetForegroundWindow()
        if not hwnd:
            return []
        out = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", "& {" + _EXPLORER_PS + "} " + str(int(hwnd))],
            capture_output=True, text=True, timeout=4, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        return [line.strip() for line in out.stdout.splitlines() if line.strip()]
    except Exception:
        log.debug("couldn't read Explorer's selection", exc_info=True)
        return []
