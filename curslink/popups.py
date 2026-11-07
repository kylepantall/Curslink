"""Carrying Isolink's floating chats from one device to another.

Drag one of Isolink's floating chats (a "popup") over an edge to another
device, or look at the other device while holding it, and the same chat
opens there under the pointer, still held, to be let go wherever you like.
Its window on the device it left closes. This works when both devices run
Isolink linked to the same account and have allowed Curslink in Isolink's
Connected apps (see :mod:`curslink.isolink`).

The host decides. Every device tells it what its Isolink says: the
account's handle (``me``) and the chat being dragged there, if any (agents
send ``isolink {me, windows, dragging}`` frames). When input switches from
a device dragging a chat to one whose Isolink has the same ``me``:

* the new device presses the left button where its pointer is and has its
  Isolink open the chat "held" (an agent does this on ``popup_in {chat,
  open}`` and answers ``popup_result {chat, ok}``; the host does it itself,
  the press standing in for the one its capture swallowed). The real
  button-up, wherever it comes, lets go of it there;
* the old device lets go of its drag where it is (the host injects a
  button-up at its parked pointer; an agent gets the router's usual one)
  and, once the chat opened on the new device, closes it (``popup_close
  {chat}`` to an agent).

A chat dragged on an agent back to the host isn't treated as a file drag:
the router lets go of that agent's button as for any other drag.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Callable

from curslink.isolink import ChatDrag, IsolinkLink
from curslink.protocol import POPUPS_FEATURE
from curslink.router import Router, SwitchEvent

log = logging.getLogger(__name__)


class DeviceIsolink:
    """What one device's Isolink says, as far as the host knows."""

    def __init__(self, me: str | None = None, windows: bool = False, dragging: ChatDrag | None = None):
        self.me = me
        self.windows = windows
        self.dragging = dragging

    @property
    def ready(self) -> bool:
        return self.me is not None and self.windows


class PopupHandoff:
    """The host's side: watches switches and carries a held chat across."""

    def __init__(
        self,
        router: Router,
        server,
        link: IsolinkLink,
        inject: Callable[[str], None],
        press_here: Callable[[], None],
        emit: Callable[..., None] = lambda kind, **data: None,
    ):
        """``inject("let_go")`` (blocking) lets go of the host's own drag at
        the parked pointer; ``press_here()`` (blocking) presses the left
        button where the host's pointer is."""
        self.router = router
        self.server = server
        self.link = link
        self.inject = inject
        self.press_here = press_here
        self.emit = emit
        self.devices: dict[str, DeviceIsolink] = {}
        # chat -> device it's waiting to close on, once the new one says it opened.
        self.closing: dict[str, str] = {}
        link.on_change = self._local_changed
        router.listeners.append(self.on_switch)
        # A chat dragged on an agent back here: let go of the button there as
        # usual, rather than ask it about a file drag.
        files_drag_out = router.can_drag_out
        router.can_drag_out = lambda device_id: (not self._dragging_on(device_id)) and files_drag_out(device_id)
        # ...and gaze may carry it to another device mid-drag.
        router.carries_drag = lambda from_device, to_device: self._dragging_on(from_device)

    # ---------------------------------------------------------------- state
    def state(self, device_id: str) -> DeviceIsolink:
        if device_id == self.router.local_id:
            return DeviceIsolink(self.link.me, self.link.windows, self.link.dragging)
        return self.devices.get(device_id) or DeviceIsolink()

    def _dragging_on(self, device_id: str) -> bool:
        return device_id != self.router.local_id and self.state(device_id).dragging is not None

    def _local_changed(self) -> None:
        d = self.link.dragging
        if d is not None:
            log.info("popups: a floating chat is being dragged here")

    def on_isolink(self, device_id: str, data: dict) -> None:
        """An agent's ``isolink`` frame."""
        me = data.get("me") if isinstance(data.get("me"), str) else None
        dragging = ChatDrag.from_json(data.get("dragging"))
        self.devices[device_id] = DeviceIsolink(me, data.get("windows") is True, dragging)
        if dragging is not None:
            log.info("popups: a floating chat is being dragged on %s", device_id)

    def on_disconnect(self, device_id: str) -> None:
        self.devices.pop(device_id, None)
        for chat in [c for c, d in self.closing.items() if d == device_id]:
            self.closing.pop(chat, None)

    # ------------------------------------------------------------ switching
    def on_switch(self, event: SwitchEvent) -> None:
        source, target = event.from_device, event.to_device
        if source == target:
            return
        src = self.state(source)
        drag = src.dragging
        if drag is None or not src.ready:
            return
        dst = self.state(target)
        if not dst.ready:
            why = "Isolink isn't open there" if dst.me is None else "Curslink isn't allowed to move floating chats there"
            if target != self.router.local_id and not self.server.supports(target, POPUPS_FEATURE):
                why = "Curslink there is too old (or [agent.isolink] is off)"
            log.info("popups: not carrying the chat to %s: %s", target, why)
            self.emit("popup", state="stayed", to=target, reason=why)
            return
        if dst.me != src.me:
            log.info("popups: not carrying the chat to %s: its Isolink is a different account", target)
            self.emit("popup", state="stayed", to=target, reason="a different Isolink account")
            return
        log.info("popups: carrying a floating chat from %s to %s", source, target)
        self._forget_drag(source)
        self.closing[drag.chat] = source
        loop = asyncio.get_running_loop()
        if source == self.router.local_id:
            # Let go of it here, at the parked pointer; it closes once it's open there.
            loop.run_in_executor(None, self.inject, "let_go")
        if target == self.router.local_id:
            loop.create_task(self._open_here(drag))
        else:
            # The button the agent presses is ours to let go of now: on a
            # release, or when input moves on.
            self.router.hold_remote_button("left")
            self.server.send(target, "popup_in", chat=drag.chat, open=drag.open)

    def _forget_drag(self, device_id: str) -> None:
        if device_id == self.router.local_id:
            self.link.dragging = None
        elif device_id in self.devices:
            self.devices[device_id].dragging = None

    async def _open_here(self, drag: ChatDrag) -> None:
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, self.press_here)
        ok = await self.link.open_chat(drag.chat, held=True, open_=drag.open)
        self.on_result(self.router.local_id, {"chat": drag.chat, "ok": ok})

    def on_result(self, device_id: str, data: dict) -> None:
        """The new device opened the chat (``popup_result``), or couldn't."""
        chat = data.get("chat")
        if not isinstance(chat, str):
            return
        source = self.closing.pop(chat, None)
        if source is None:
            return
        if data.get("ok") is not True:
            log.info("popups: %s couldn't open the chat; it stays on %s", device_id, source)
            self.emit("popup", state="failed", to=device_id)
            return
        self.emit("popup", state="moved", to=device_id)
        if source == self.router.local_id:
            asyncio.get_running_loop().create_task(self.link.close_chat(chat))
        else:
            self.server.send(source, "popup_close", chat=chat)


class AgentPopups:
    """An agent's side: tells the host what its Isolink says, and opens or
    closes chats the host carries."""

    def __init__(self, link: IsolinkLink, send: Callable[..., None], press: Callable[[], None]):
        """``send(t, **data)``: a frame to the host. ``press()`` (blocking):
        the left button down where the pointer is, held for the host's
        button-up."""
        self.link = link
        self.send = send
        self.press = press
        link.on_change = self.report

    def report(self) -> None:
        d = self.link.dragging
        self.send("isolink", me=self.link.me, windows=self.link.windows,
                  dragging=d.to_json() if d is not None else None)

    def handle(self, t: str, data: dict) -> bool:
        """A frame from the host. True if it was one of ours."""
        if t == "popup_in":
            chat = data.get("chat")
            if isinstance(chat, str):
                asyncio.get_running_loop().create_task(self._open(chat, data.get("open") is True))
            return True
        if t == "popup_close":
            chat = data.get("chat")
            if isinstance(chat, str):
                asyncio.get_running_loop().create_task(self.link.close_chat(chat))
            return True
        return False

    async def _open(self, chat: str, open_: bool) -> None:
        await asyncio.get_running_loop().run_in_executor(None, self.press)
        ok = await self.link.open_chat(chat, held=True, open_=open_)
        self.send("popup_result", chat=chat, ok=ok)
