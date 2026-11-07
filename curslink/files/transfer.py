"""Sending dragged files over the paired link, and saving them on arrival.

The files travel on the same TLS-PSK connection as the input, as ordinary
frames (see :mod:`curslink.protocol`), so they are encrypted and
authenticated the same way and need no extra port or pairing::

    file_begin  {id, from, count, total, card}   a drop: ``count`` files,
                                                 ``total`` bytes in all
    file_item   {id, i, path, size}              file ``i`` starts; ``path``
                                                 is relative, "/" separated
    file_data   {id, i, data}                    the next bytes of file ``i``
                                                 (base64, up to 32 KiB raw)
    file_end    {id}                             all sent
    file_cancel {id, reason}                     the sender gave up
    file_result {id, ok, saved?, error?}         agent -> host: saved, or why not
    file_ack    {id, n}                          receiver -> sender: ``n`` bytes
                                                 have arrived (only when
                                                 file_begin had ``ack: true``)

Data frames are written only as fast as the connection drains, so moves
and clicks queued behind them wait milliseconds, not seconds. With acks
the sender also keeps no more than ``Outgoing.window`` bytes in flight, so
the OS's socket buffers and the Wi-Fi queue never fill with file data that
the pointer's moves and the drop itself would wait behind.

The receiver writes into a hidden staging folder next to where the files
end up and only moves them into place once every byte has arrived, so a
cancelled or broken transfer leaves nothing half written behind. Names are
cleaned up (no ``..``, no absolute paths, nothing Windows can't store) and
never overwrite an existing file: ``report.pdf`` becomes ``report (1).pdf``.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import logging
import os
import re
import shutil
import sys
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Awaitable, Callable

from curslink.files.meta import Dragged, card, human_size
from curslink.protocol import Message, encode

log = logging.getLogger(__name__)

CHUNK = 32 * 1024          # base64 of this fits comfortably in one frame
MAX_FILES = 10000          # per drop
MAX_ACTIVE = 4             # transfers being received at once
SPARE_BYTES = 64 * 1024 * 1024  # leave at least this much disk free
ACK_TIMEOUT_S = 30.0       # no file_ack for this long: the receiver is gone

Send = Callable[..., Awaitable[None]]


class TransferError(Exception):
    pass


def new_id() -> str:
    return uuid.uuid4().hex[:12]


# ------------------------------------------------------------------ sending


def plan(items: list[Dragged]) -> list[tuple[str, str, int]]:
    """(source path, relative path, size) for every file to send. A folder
    sends the files inside it, under the folder's own name."""
    out: list[tuple[str, str, int]] = []
    for item in items:
        if not item.is_dir:
            out.append((item.path, item.name, os.stat(item.path).st_size))
            continue
        base = Path(item.path)
        for root, dirs, files in os.walk(item.path):
            dirs.sort()
            for f in sorted(files):
                src = Path(root) / f
                if src.is_symlink() or not src.is_file():
                    continue
                rel = "/".join((item.name, *src.relative_to(base).parts))
                out.append((str(src), rel, src.stat().st_size))
                if len(out) > MAX_FILES:
                    raise TransferError(f"more than {MAX_FILES} files in one drop")
    if len(out) > MAX_FILES:
        raise TransferError(f"more than {MAX_FILES} files in one drop")
    return out


@dataclass
class Outgoing:
    id: str
    device_id: str
    items: list[Dragged]
    sent: int = 0
    total: int = 0
    refused: str = ""        # the receiver said no (file_result ok=false)
    task: asyncio.Task | None = None
    # Bytes allowed in flight (sent but not acknowledged), or None: no acks.
    window: int | None = None
    acked: int = 0
    _moved: asyncio.Event | None = None

    def ack(self, n: object) -> None:
        """file_ack from the receiver."""
        if isinstance(n, int) and not isinstance(n, bool) and n > self.acked:
            self.acked = n
            self._wake()

    def set_window(self, window: int | None) -> None:
        if self.window is not None:  # (acks are asked for at the start, or not at all)
            self.window = window
            self._wake()

    def _wake(self) -> None:
        if self._moved is not None:
            self._moved.set()

    async def room(self, n: int) -> None:
        """Wait until ``n`` more bytes fit in the window."""
        waited = 0.0
        while self.window is not None and self.sent - self.acked + n > self.window and self.sent > self.acked:
            if self.refused:
                raise TransferError(self.refused)
            if waited >= ACK_TIMEOUT_S:
                raise TransferError("the other computer stopped taking the files")
            self._moved = self._moved or asyncio.Event()
            self._moved.clear()
            try:
                await asyncio.wait_for(self._moved.wait(), 0.5)
                waited = 0.0
            except asyncio.TimeoutError:
                waited += 0.5


async def send_files(send: Send, out: Outgoing, from_name: str) -> None:
    """Send ``out.items`` with ``send(t, **data)``, which waits while the
    connection is busy. Raises if the link goes away or the agent refuses."""
    entries = await asyncio.to_thread(plan, out.items)
    if not entries:
        raise TransferError("nothing to send (empty folders only?)")
    out.total = sum(size for _src, _rel, size in entries)
    extra = {"ack": True} if out.window is not None else {}
    await send("file_begin", id=out.id, count=len(entries), total=out.total, card=card(out.items),
               **{"from": from_name}, **extra)
    try:
        for i, (src, rel, size) in enumerate(entries):
            if out.refused:
                raise TransferError(out.refused)
            await send("file_item", id=out.id, i=i, path=rel, size=size)
            with open(src, "rb") as fh:
                left = size
                while left > 0:
                    chunk = await asyncio.to_thread(fh.read, min(CHUNK, left))
                    if not chunk:
                        raise TransferError(f"{rel} got shorter while it was being sent")
                    await out.room(len(chunk))
                    await send("file_data", id=out.id, i=i, data=base64.b64encode(chunk).decode("ascii"))
                    left -= len(chunk)
                    out.sent += len(chunk)
                    if out.refused:
                        raise TransferError(out.refused)
        await send("file_end", id=out.id)
    except BaseException as exc:
        if not out.refused:
            with contextlib.suppress(Exception):
                await send("file_cancel", id=out.id, reason=str(exc) or type(exc).__name__)
        raise


# ---------------------------------------------------------------- receiving

_BAD_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_WIN_RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}


def safe_parts(path: str) -> list[str]:
    """A sender's relative path -> safe path components, or raise."""
    if not isinstance(path, str) or not path or path.startswith("/") or "\\" in path:
        raise TransferError(f"bad path {path!r}")
    parts = []
    for raw in path.split("/"):
        if raw in ("", ".", ".."):
            raise TransferError(f"bad path {path!r}")
        part = _BAD_CHARS.sub("_", raw).rstrip(" .") or "_"
        if part.split(".", 1)[0].upper() in _WIN_RESERVED:
            part = "_" + part
        while len(part.encode("utf-8")) > 240:
            part = part[:-1]
        parts.append(part)
    if len(parts) > 64:
        raise TransferError("path too deep")
    return parts


def unique(folder: Path, name: str) -> Path:
    """``folder/name``, or ``folder/name (1).ext`` etc. if that is taken."""
    target = folder / name
    if not target.exists():
        return target
    stem, dot, ext = name.partition(".") if not name.startswith(".") else (name, "", "")
    for n in range(1, 10000):
        target = folder / f"{stem} ({n}){dot}{ext}"
        if not target.exists():
            return target
    raise TransferError(f"too many copies of {name} already")


def move_into(staging: Path, tops: list[str], folder: Path) -> list[Path]:
    """Move ``tops`` from ``staging`` into ``folder``, never overwriting."""
    folder.mkdir(parents=True, exist_ok=True)
    saved = []
    for top in tops:
        if not (staging / top).exists():
            continue
        target = unique(folder, top)
        shutil.move(str(staging / top), target)
        saved.append(target)
    return saved


def default_folder() -> Path:
    downloads = Path.home() / "Downloads"
    return downloads if downloads.is_dir() else Path.home()


@dataclass
class Incoming:
    id: str
    sender: str
    count: int
    total: int
    staging: Path
    received: int = 0
    index: int = -1
    size: int = 0          # of the current file
    written: int = 0       # of the current file
    fh: object = None
    tops: list[str] = field(default_factory=list)  # top-level names, in order
    keep: bool = False     # staging is a native drop's folder: the files stay there
    ack: bool = False      # the sender wants file_ack as bytes arrive


class FileReceiver:
    """Agent side: applies ``file_*`` frames. ``on_event(kind, info)`` hears
    "start", "progress", "done" and "failed" for the popup and the app."""

    def __init__(self, folder: Path | None = None, on_event: Callable[[str, dict], None] = lambda k, i: None):
        self.folder = Path(folder) if folder else default_folder()
        self.on_event = on_event
        self.active: dict[str, Incoming] = {}
        # Transfer id -> the folder to write it into and leave it in (a native
        # drop's staging folder; see curslink.files.native), instead of the
        # hidden staging folder whose contents move into ``folder``.
        self.targets: dict[str, Path] = {}

    def handle(self, msg: Message) -> bytes | None:
        tid = msg.get("id")
        if not isinstance(tid, str) or not re.fullmatch(r"[0-9A-Za-z_-]{1,40}", tid):
            return None
        try:
            if msg.t == "file_begin":
                self._begin(tid, msg)
            elif msg.t == "file_cancel":
                self._fail(tid, f"the other computer stopped: {msg.get('reason', '')}".rstrip(": "), reply=False)
            elif tid not in self.active:
                return None  # already failed or finished; ignore the rest of it
            elif msg.t == "file_item":
                self._item(self.active[tid], msg)
            elif msg.t == "file_data":
                inc = self.active[tid]
                self._data(inc, msg)
                if inc.ack:
                    return encode("file_ack", id=tid, n=inc.received)
            elif msg.t == "file_end":
                return self._end(self.active[tid])
        except (TransferError, OSError, ValueError) as exc:
            log.warning("receiving files failed: %s", exc)
            return self._fail(tid, str(exc))
        return None

    # -------------------------------------------------------------- frames
    def _begin(self, tid: str, msg: Message) -> None:
        if tid in self.active:
            raise TransferError("duplicate transfer id")
        if len(self.active) >= MAX_ACTIVE:
            raise TransferError("too many transfers at once")
        count, total = msg.get("count"), msg.get("total")
        if not isinstance(count, int) or not isinstance(total, int) or count < 1 or count > MAX_FILES or total < 0:
            raise TransferError("bad file_begin")
        keep = tid in self.targets
        if keep:
            staging = self.targets[tid]
            staging.mkdir(parents=True, exist_ok=True)
        else:
            self.folder.mkdir(parents=True, exist_ok=True)
            staging = self.folder / f".curslink-{tid}.partial"
        free = shutil.disk_usage(staging if keep else self.folder).free
        if total + SPARE_BYTES > free:
            raise TransferError(f"not enough space: {human_size(total)} needed, {human_size(free)} free")
        if not keep:
            staging.mkdir()
            _hide(staging)
        sender = str(msg.get("from") or "the other computer")[:80]
        self.active[tid] = Incoming(tid, sender, count, total, staging, keep=keep, ack=msg.get("ack") is True)
        info = {"id": tid, "from": sender, "count": count, "total": total}
        if isinstance(msg.get("card"), dict):
            info["card"] = msg.get("card")
        log.info("receiving %d file(s), %s, from %s", count, human_size(total), sender)
        self.on_event("start", info)

    def _item(self, inc: Incoming, msg: Message) -> None:
        i, size = msg.get("i"), msg.get("size")
        if i != inc.index + 1 or not isinstance(size, int) or size < 0 or i >= inc.count:
            raise TransferError("files arrived out of order")
        self._close_current(inc)
        parts = safe_parts(msg.get("path"))
        if parts[0] not in inc.tops:
            inc.tops.append(parts[0])
        target = inc.staging.joinpath(*parts)
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists() and not (inc.keep and target.is_file() and target.stat().st_size == 0):
            raise TransferError(f"{'/'.join(parts)} sent twice")  # (a native drop's empty placeholder is fine)
        inc.fh = open(target, "wb")
        inc.index, inc.size, inc.written = i, size, 0

    def _data(self, inc: Incoming, msg: Message) -> None:
        if msg.get("i") != inc.index or inc.fh is None:
            raise TransferError("data for a file that isn't open")
        chunk = base64.b64decode(msg.get("data") or "", validate=True)
        if inc.written + len(chunk) > inc.size:
            raise TransferError("more data than the file's size")
        inc.fh.write(chunk)
        inc.written += len(chunk)
        inc.received += len(chunk)
        if inc.received > inc.total:
            raise TransferError("more data than announced")
        self.on_event("progress", {"id": inc.id, "received": inc.received, "total": inc.total})

    def _end(self, inc: Incoming) -> bytes:
        self._close_current(inc)
        if inc.index != inc.count - 1 or inc.received != inc.total:
            raise TransferError("the transfer ended early")
        if inc.keep:
            folder = inc.staging
            saved = [inc.staging / top for top in inc.tops]
            self.targets.pop(inc.id, None)
        else:
            folder = self.folder
            saved = move_into(inc.staging, inc.tops, self.folder)
            shutil.rmtree(inc.staging, ignore_errors=True)
        del self.active[inc.id]
        log.info("received %d file(s) from %s into %s", inc.count, inc.sender, folder)
        self.on_event("done", {"id": inc.id, "from": inc.sender, "count": inc.count, "total": inc.total,
                               "folder": str(folder), "saved": [str(p) for p in saved]})
        return encode("file_result", id=inc.id, ok=True, saved=[p.name for p in saved][:50],
                      folder=str(folder))

    # ------------------------------------------------------------- helpers
    def _close_current(self, inc: Incoming) -> None:
        if inc.fh is not None:
            inc.fh.close()
            inc.fh = None
            if inc.written != inc.size:
                raise TransferError("a file arrived incomplete")

    def _fail(self, tid: str, why: str, reply: bool = True) -> bytes | None:
        self.targets.pop(tid, None)
        inc = self.active.pop(tid, None)
        if inc is not None:
            if inc.fh is not None:
                with contextlib.suppress(OSError):
                    inc.fh.close()
            shutil.rmtree(inc.staging, ignore_errors=True)
            self.on_event("failed", {"id": tid, "from": inc.sender, "error": why})
        return encode("file_result", id=tid, ok=False, error=why[:300]) if reply else None

    def close_all(self) -> None:
        """The link dropped: nothing more is coming for any transfer."""
        for tid in list(self.active):
            self._fail(tid, "the connection dropped", reply=False)


def _hide(path: Path) -> None:
    if sys.platform == "win32":
        with contextlib.suppress(Exception):
            import ctypes

            ctypes.windll.kernel32.SetFileAttributesW(str(path), 0x2)  # FILE_ATTRIBUTE_HIDDEN
