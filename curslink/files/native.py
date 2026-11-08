"""Receiving side of a file drag: a real drag of the files, dropped like any other.

As soon as a file drag reaches this device (from the host over an edge, or
from an agent onto the host), the sender starts sending, and this device
starts a real OS drag of the files under the pointer:

* empty placeholders with the real names go in a temp folder,
  ``<temp>/curslink-drops/<id>/``, and the incoming files are written over
  them (:class:`curslink.files.transfer.FileReceiver` ``targets``);
* the overlay helper puts a small invisible window under the pointer, this
  device presses the left button on it, and the helper starts a drag of the
  placeholders' paths from there (``nativedrag`` in
  :mod:`curslink.files.overlay`), showing the file card as the drag image.

Then the pointer moves as usual and the OS's own drag and drop takes over:
let go on the desktop or in a folder and the file manager copies the files
there; let go on an app and it gets their paths in the temp folder. If you
let go before every byte has arrived, the button-up is held back and a
progress popup shows until they have; then the drop happens.

Where a real drag can't be started (no Qt helper, or the platform can't hold
the button-up back), or doesn't start, the card follows the pointer instead
and the files land in Downloads when you let go.
"""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from curslink.files.popups import ReceivePopups
from curslink.files.transfer import FileReceiver, move_into, safe_parts, TransferError

log = logging.getLogger(__name__)

START_TIMEOUT_S = 1.5   # the helper must have started the drag by then
FOLLOW_EVERY_S = 0.08   # how often the window under a held drag is checked
KEEP_DROPS_S = 24 * 3600


def staging_root() -> Path:
    return Path(tempfile.gettempdir()) / "curslink-drops"


def clean_old(root: Path | None = None, max_age_s: float = KEEP_DROPS_S) -> None:
    """Temp folders of drops from more than a day ago: an app given a path
    in one has long since opened it."""
    root = root or staging_root()
    if not root.is_dir():
        return
    cutoff = time.time() - max_age_s
    for d in root.iterdir():
        try:
            if d.stat().st_mtime < cutoff:
                shutil.rmtree(d, ignore_errors=True)
        except OSError:
            pass


@dataclass
class Drop:
    id: str
    staging: Path
    tops: list[str]
    card: dict
    native: bool               # trying a real drag
    phase: str = "starting"    # starting | dragging | fallback | finished
    done: bool = False         # every byte arrived
    released: bool = False     # the user let go
    waited: bool = False       # ...before every byte had arrived
    inject: bool = True        # the button-up is ours to send
    started_at: float = field(default_factory=time.monotonic)


class NativeDrop:
    def __init__(
        self,
        helper: Callable[[str], None],
        receiver: FileReceiver,
        popups: ReceivePopups,
        press: Callable[[], None] | None,
        release: Callable[[], None],
        cancel: Callable[[], None],
        folder: Path | None = None,
        root: Path | None = None,
        follow=None,
    ):
        """``press()``: the left button down here, under the pointer (None:
        no real drags on this device). ``release()``: let it go (a drop).
        ``cancel()``: end a drag here without dropping (Escape, then let go).
        All blocking and quick. ``folder``: where files go when there's no
        real drag (Downloads by default). ``follow``: an object whose
        ``tick(now)`` runs every FOLLOW_EVERY_S while a real drag is held and
        ``reset()`` when one starts (the Mac brings the window under the
        pointer forward; see curslink.files.macfocus)."""
        self.helper = helper
        self.receiver = receiver
        self.popups = popups
        self.press = press
        self._release = release
        self._cancel = cancel
        self.folder = Path(folder) if folder else receiver.folder
        self.root = root or staging_root()
        self.drops: dict[str, Drop] = {}
        self.follow = follow
        self._following: asyncio.TimerHandle | None = None
        receiver_event = receiver.on_event

        def on_event(kind: str, info: dict) -> None:
            receiver_event(kind, info)
            if kind == "done":
                self._arrived(info.get("id", ""))
            elif kind == "failed":
                self._failed(info.get("id", ""))

        receiver.on_event = on_event

    # ------------------------------------------------------------ lifecycle
    def begin(self, tid: str, card: dict) -> None:
        """A file drag arrived here: lay out its files and start a real drag."""
        self.end_all()
        names = [i for i in card.get("items") or [] if isinstance(i, dict)] or \
            [{"name": card.get("name") or "file", "dir": bool(card.get("dir"))}]
        staging = self.root / tid
        tops = []
        try:
            staging.mkdir(parents=True, exist_ok=False)
            for item in names:
                name = safe_parts(str(item.get("name") or ""))[0] if "/" not in str(item.get("name")) else ""
                if not name or name in tops:
                    continue
                path = staging / name
                path.mkdir() if item.get("dir") else path.touch()
                tops.append(name)
        except (OSError, TransferError) as exc:
            log.warning("drop: couldn't set up %s: %s", staging, exc)
        self.receiver.targets[tid] = staging
        self.popups.quiet.add(tid)
        native = self.press is not None and bool(tops)
        drop = Drop(tid, staging, tops, card, native)
        self.drops[tid] = drop
        if native:
            paths = [str(staging / t) for t in tops]
            log.info("drop: starting a real drag of %s", ", ".join(tops[:3]))
            # The card follows the pointer at once; the real drag's image
            # takes over from it when that starts.
            self.helper("card " + json.dumps(card, separators=(",", ":")))
            self.helper("nativedrag " + json.dumps({"id": tid, "paths": paths, "card": card}, separators=(",", ":")))
            asyncio.get_running_loop().call_later(START_TIMEOUT_S, self._check_started, tid)
        else:
            self._fallback(drop)

    def _check_started(self, tid: str) -> None:
        drop = self.drops.get(tid)
        if drop is not None and drop.phase == "starting":
            log.warning("drop: the real drag didn't start; the files will go to %s", self.folder)
            self.helper("nativedrag cancel")
            self._fallback(drop)

    def _fallback(self, drop: Drop) -> None:
        drop.phase = "fallback"
        self.helper("card " + json.dumps(drop.card, separators=(",", ":")))

    def on_helper(self, line: str) -> bool:
        """Lines from the overlay helper about the real drag. True if it was one."""
        word, _, rest = line.partition(" ")
        tid = rest.split(" ", 1)[0]
        drop = self.drops.get(tid)
        if word == "nativedrag-ready":
            if drop is not None and drop.phase == "starting" and self.press is not None:
                log.info("drop: pressing the button to start the real drag")
                self._bg(self.press)
            return True
        if word == "nativedrag-started":
            if drop is not None and drop.phase == "starting":
                drop.phase = "dragging"
                self._start_following()
                if not rest.endswith(" card"):  # (a Mac's drag keeps the card as its image)
                    self.helper("card hide")
                log.info("drop: real drag started in %d ms", (time.monotonic() - drop.started_at) * 1000)
            return True
        if word == "nativedrag-ended":
            action = rest.split(" ", 1)[1] if " " in rest else "?"
            log.info("drop: the real drag ended (%s)", action)
            if drop is not None and drop.phase == "dragging":
                drop.phase = "finished"
                self.drops.pop(tid, None)
                self.helper("card hide")
            return True
        return False

    def _start_following(self) -> None:
        if self.follow is None or self._following is not None:
            return
        self.follow.reset()
        try:
            self._following = asyncio.get_running_loop().call_soon(self._follow)
        except RuntimeError:
            pass

    def _follow(self) -> None:
        self._following = None
        if not any(d.phase == "dragging" for d in self.drops.values()):
            return
        loop = asyncio.get_running_loop()
        self.follow.tick(loop.time())
        self._following = loop.call_later(FOLLOW_EVERY_S, self._follow)

    def _bg(self, fn: Callable[[], None]) -> None:
        """Input injection blocks for a moment: off the loop."""
        try:
            asyncio.get_running_loop().run_in_executor(None, fn)
        except RuntimeError:  # no loop (tests calling directly)
            fn()

    def release(self) -> None:
        self._bg(self._release)

    def cancel_drag(self) -> None:
        self._bg(self._cancel)

    # ------------------------------------------------------------- the drop
    def holding(self) -> bool:
        """Should a button-up be held back now (a real drag whose files
        haven't all arrived)? Read from input threads."""
        return any(d.phase == "dragging" and not d.done for d in list(self.drops.values()))

    def let_go(self, tid: str | None = None, inject: bool = True) -> None:
        """The user let go. ``inject``: this device's button-up is ours to
        send (an agent), not a real one already on its way (the host)."""
        drop = self.drops.get(tid) if tid else self._current()
        if drop is None:
            return
        drop.released = True
        if drop.phase == "starting":
            # The real drag never got going: let go of the button we may have
            # pressed, and fall back to saving the files.
            log.info("drop: let go before the real drag started")
            self.helper("nativedrag cancel")
            if inject and drop.native:
                self.release()
            self._fallback(drop)
        if drop.done:
            self._finish(drop, inject)
        else:
            log.info("drop: waiting for the files before dropping")
            drop.waited, drop.inject = True, inject
            self.popups.reveal(drop.id)

    def _current(self) -> Drop | None:
        return next(reversed(self.drops.values()), None)

    def _arrived(self, tid: str) -> None:
        drop = self.drops.get(tid)
        if drop is None:
            return
        drop.done = True
        if drop.released:
            self._finish(drop, drop.inject)

    def _finish(self, drop: Drop, inject: bool) -> None:
        self.popups.quiet.discard(drop.id)
        if drop.phase == "dragging":
            if inject or drop.waited:
                self.release()  # the OS drops the files where the pointer is
            if drop.waited:
                self.helper("toast " + json.dumps({"id": drop.id, "state": "done", "count": len(drop.tops),
                                               "total": drop.card.get("size", 0), "card": drop.card,
                                                   "dropped": True}, separators=(",", ":")))
            return
        self.drops.pop(drop.id, None)
        self.helper("card hide")
        saved = move_into(drop.staging, drop.tops, self.folder)
        shutil.rmtree(drop.staging, ignore_errors=True)
        log.info("drop: saved %s in %s", ", ".join(p.name for p in saved), self.folder)
        self.helper("toast " + json.dumps({"id": drop.id, "state": "done", "count": len(saved),
                                           "total": drop.card.get("size", 0), "card": drop.card,
                                           "folder": str(self.folder)}, separators=(",", ":")))

    def _failed(self, tid: str) -> None:
        drop = self.drops.pop(tid, None)
        if drop is None:
            return
        self._end(drop)

    def cancel(self, tid: str | None = None) -> None:
        """The drag went elsewhere (or the link dropped): no drop here.
        Without ``tid``, every drop not let go of yet."""
        if tid is None:
            for t in [t for t, d in self.drops.items() if not d.released]:
                self.cancel(t)
            return
        drop = self.drops.pop(tid, None)
        if drop is not None:
            log.info("drop: cancelled")
            self._end(drop)

    def _end(self, drop: Drop) -> None:
        self.receiver.targets.pop(drop.id, None)
        self.popups.quiet.discard(drop.id)
        if drop.phase in ("starting", "dragging") and drop.native:
            self.helper("nativedrag cancel")
            self.helper("card hide")
            if drop.phase == "dragging":
                self.cancel_drag()
        else:
            self.helper("card hide")
        shutil.rmtree(drop.staging, ignore_errors=True)

    def end_all(self) -> None:
        """A new drag replaces any that wasn't let go of."""
        self.cancel()
