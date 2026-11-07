"""The "Receiving..." popup for files arriving, on whichever device gets them."""

from __future__ import annotations

import json
import time
from typing import Callable


class ReceivePopups:
    """A :class:`curslink.files.transfer.FileReceiver`'s ``on_event``: shows
    the overlay helper's popup (``helper(line)``) and tells the app
    (``emit("file", ...)``)."""

    def __init__(self, helper: Callable[[str], None], emit: Callable[..., None] = lambda kind, **data: None):
        self.helper = helper
        self.emit = emit
        self.toasts: dict[str, dict] = {}
        self.shown = 0.0
        self.quiet: set[str] = set()   # transfers that show nothing until revealed

    def reveal(self, tid: str) -> None:
        """Start showing a quiet transfer's popup (the drop is waiting for it)."""
        if tid in self.quiet:
            self.quiet.discard(tid)
            if tid in self.toasts:
                self.helper("toast " + json.dumps(self.toasts[tid], separators=(",", ":")))

    def __call__(self, kind: str, info: dict) -> None:
        tid = info.get("id", "")
        if tid in self.quiet:
            if kind == "start":
                self.toasts[tid] = {**info, "state": "receiving", "received": 0}
            elif kind == "progress" and tid in self.toasts:
                self.toasts[tid]["received"] = info.get("received", 0)
            elif kind in ("done", "failed"):
                self.toasts.pop(tid, None)
                if kind == "failed":
                    self.quiet.discard(tid)
                    self.helper("toast " + json.dumps({**info, "state": kind}, separators=(",", ":")))
                self.emit("file", state=kind, **{k: v for k, v in info.items() if k != "card"})
            return
        if kind == "start":
            self.helper("card hide")
            self.toasts[tid] = {**info, "state": "receiving", "received": 0}
        elif kind == "progress":
            toast = self.toasts.get(tid)
            if toast is None:
                return
            toast["received"] = info.get("received", 0)
            now = time.monotonic()
            if now - self.shown < 0.1:
                return  # ten updates a second is plenty
            self.shown = now
        else:
            toast = {**self.toasts.pop(tid, {}), **info, "state": kind}
            self.helper("toast " + json.dumps(toast, separators=(",", ":")))
            self.emit("file", state=kind, **{k: v for k, v in info.items() if k != "card"})
            return
        self.helper("toast " + json.dumps(self.toasts[tid], separators=(",", ":")))
        if kind == "start":
            self.emit("file", state="receiving", **{k: v for k, v in info.items() if k != "card"})
