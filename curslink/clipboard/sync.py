"""Sending what was copied to the other devices, and taking theirs.

What was copied travels on the paired link as two frame types (see
:mod:`curslink.protocol`)::

    clip       {id, kind, size, files?}   something new was copied: ``kind``
                                          is "text", "image" (a PNG) or
                                          "files"; ``size`` bytes follow;
                                          ``files`` lists [{path, size}] in
                                          order (relative, "/" separated)
    clip_data  {id, data, end?}           the next bytes (base64, up to 32 KiB
                                          raw); ``end: true`` on the last

The bytes are the text (UTF-8), the PNG, or every file's bytes one after
the other. A newer ``clip`` replaces one still arriving. Frames are only
written as fast as the link drains (``send_bulk``), so the pointer never
waits behind a big copy.

:class:`ClipboardSync` is the same on the host and an agent: each linked
peer (an agent, or the host) has a sender and a receiver. What arrives is
put on this device's clipboard and, on the host, sent on to every other
peer, so the host is the hub.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import logging
import os
import shutil
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Awaitable, Callable

from curslink.files.meta import Dragged, human_size
from curslink.files.transfer import TransferError, plan, safe_parts

log = logging.getLogger(__name__)

KINDS = ("text", "image", "files")
CHUNK = 32 * 1024
DEFAULT_MAX_MB = 25
KEEP_STAGED = 3  # received clips (files, images) kept on disk; older ones are removed

Send = Callable[..., Awaitable[None]]


def new_id() -> str:
    return uuid.uuid4().hex[:12]


def default_staging() -> Path:
    return Path(tempfile.gettempdir()) / "curslink-clipboard"


@dataclass
class Clip:
    """Something copied: text, a PNG, or files."""

    kind: str
    text: str = ""
    image: bytes = b""
    # (source path, relative path, size) of every file, in order.
    files: list[tuple[str, str, int]] = field(default_factory=list)
    # The top-level files and folders (what goes on a clipboard), for "files".
    tops: list[str] = field(default_factory=list)

    @property
    def size(self) -> int:
        if self.kind == "text":
            return len(self.text.encode("utf-8"))
        if self.kind == "image":
            return len(self.image)
        return sum(s for _, _, s in self.files)

    def describe(self) -> str:
        if self.kind == "files":
            n = len(self.tops) or len(self.files)
            return f"{n} file{'s' if n != 1 else ''} ({human_size(self.size)})"
        return f"{self.kind} ({human_size(self.size)})"

    def header(self) -> dict:
        data = {"kind": self.kind, "size": self.size}
        if self.kind == "files":
            data["files"] = [{"path": rel, "size": size} for _, rel, size in self.files]
        return data

    def chunks(self):
        """The bytes to send, CHUNK at a time."""
        if self.kind == "files":
            for src, _, size in self.files:
                left = size
                with open(src, "rb") as f:
                    while left > 0:
                        block = f.read(min(CHUNK, left))
                        if not block:
                            raise TransferError(f"{src} got shorter while it was sent")
                        left -= len(block)
                        yield block
            return
        raw = self.text.encode("utf-8") if self.kind == "text" else self.image
        for i in range(0, len(raw), CHUNK):
            yield raw[i:i + CHUNK]


def clip_from_helper(data: dict, max_bytes: int) -> Clip | None:
    """What the helper says was copied (its ``clip {...}`` line) -> a Clip
    to send, or None (nothing usable, or too big)."""
    kind = data.get("kind")
    clip: Clip | None = None
    if kind == "text" and isinstance(data.get("text"), str) and data["text"]:
        clip = Clip("text", text=data["text"])
    elif kind == "image" and isinstance(data.get("path"), str):
        path = Path(data["path"])
        try:
            if path.stat().st_size <= max_bytes:
                clip = Clip("image", image=path.read_bytes())
            else:
                log.info("clipboard: the copied image is too big to share (%s)", human_size(path.stat().st_size))
        except OSError as exc:
            log.info("clipboard: can't read the copied image: %s", exc)
        finally:
            with contextlib.suppress(OSError):
                path.unlink()
    elif kind == "files" and isinstance(data.get("paths"), list):
        items = []
        for p in data["paths"]:
            if not isinstance(p, str) or not os.path.exists(p):
                continue
            items.append(Dragged(p, os.path.basename(os.path.normpath(p)) or p, 0, os.path.isdir(p)))
        if not items:
            return None
        try:
            files = plan(items)
        except (TransferError, OSError) as exc:
            log.info("clipboard: can't share the copied files: %s", exc)
            return None
        clip = Clip("files", files=files, tops=[i.path for i in items])
    if clip is None:
        return None
    if clip.size > max_bytes:
        log.info("clipboard: %s is too big to share (the limit is %s)", clip.describe(), human_size(max_bytes))
        return None
    return clip


async def send_clip(send: Send, clip: Clip, cid: str | None = None) -> None:
    """Send one clip with ``send(t, **data)`` (a bulk sender)."""
    cid = cid or new_id()
    await send("clip", id=cid, **clip.header())
    pending: str | None = None
    blocks = clip.chunks()
    while True:
        # Files are read off the loop (see ClipboardSync.helper_line).
        block = await asyncio.to_thread(next, blocks, None) if clip.kind == "files" else next(blocks, None)
        if block is None:
            break
        if pending is not None:
            await send("clip_data", id=cid, data=pending)
        pending = base64.b64encode(block).decode("ascii")
    await send("clip_data", id=cid, data=pending or "", end=True)


class _Incoming:
    def __init__(self, cid: str, kind: str, size: int, files: list[tuple[list[str], int]], folder: Path | None):
        self.id = cid
        self.kind = kind
        self.size = size
        self.files = files
        self.folder = folder
        self.got = 0
        self.buf = bytearray()
        self.index = -1
        self.left = 0
        self.out = None

    def close(self) -> None:
        if self.out is not None:
            self.out.close()
            self.out = None


class Receiver:
    """Puts one peer's ``clip`` / ``clip_data`` frames back together."""

    def __init__(self, staging: Path, max_bytes: int, on_clip: Callable[[Clip, str], None]):
        self.staging = staging
        self.max_bytes = max_bytes
        self.on_clip = on_clip
        self.incoming: _Incoming | None = None

    def handle(self, t: str, data: dict) -> None:
        try:
            if t == "clip":
                self._begin(data)
            elif t == "clip_data":
                inc = self.incoming
                if inc is None or data.get("id") != inc.id:
                    return  # one we gave up on, or replaced
                self._data(inc, base64.b64decode(data.get("data") or "", validate=True))
                if data.get("end") is True:
                    self.incoming = None
                    self._end(inc)
        except (ValueError, OSError, TransferError) as exc:
            log.warning("clipboard: dropped what was copied on the other device: %s", exc)
            self.drop()

    def drop(self) -> None:
        inc, self.incoming = self.incoming, None
        if inc is not None:
            inc.close()
            if inc.folder is not None:
                shutil.rmtree(inc.folder, ignore_errors=True)

    def _begin(self, data: dict) -> None:
        self.drop()
        cid, kind, size = data.get("id"), data.get("kind"), data.get("size")
        if kind not in KINDS or not isinstance(size, int) or isinstance(size, bool) or size < 0:
            raise ValueError("bad clip header")
        if size > self.max_bytes:
            log.info("clipboard: what was copied on the other device is too big to take (%s)", human_size(size))
            return
        files: list[tuple[list[str], int]] = []
        folder = None
        if kind in ("files", "image"):
            prune(self.staging, KEEP_STAGED - 1)
            folder = self.staging / f"{int(time.time() * 1000)}-{_safe_id(cid)}"
            folder.mkdir(parents=True)
        if kind == "files":
            raw = data.get("files")
            if not isinstance(raw, list) or not raw:
                raise ValueError("clip of files without files")
            for item in raw:
                if not isinstance(item, dict) or not isinstance(item.get("size"), int) or item["size"] < 0:
                    raise ValueError("bad file in clip")
                files.append((safe_parts(item.get("path")), item["size"]))
            if sum(s for _, s in files) != size:
                raise ValueError("clip file sizes don't add up")
        self.incoming = _Incoming(cid, kind, size, files, folder)
        if kind == "files":
            self._next_file(self.incoming)

    def _next_file(self, inc: _Incoming) -> None:
        """Open the next file to write (and create any empty ones on the way)."""
        inc.close()
        while inc.index + 1 < len(inc.files):
            inc.index += 1
            parts, size = inc.files[inc.index]
            path = inc.folder.joinpath(*parts)
            if not path.resolve().is_relative_to(inc.folder.resolve()):
                raise TransferError("file outside the clip's folder")
            path.parent.mkdir(parents=True, exist_ok=True)
            inc.out = open(path, "wb")
            inc.left = size
            if size > 0:
                return
            inc.close()

    def _data(self, inc: _Incoming, block: bytes) -> None:
        inc.got += len(block)
        if inc.got > inc.size:
            raise ValueError("more bytes than the clip said")
        if inc.kind != "files":
            inc.buf += block
            return
        view = memoryview(block)
        while view:
            if inc.out is None:
                raise ValueError("more bytes than the clip's files")
            n = min(inc.left, len(view))
            inc.out.write(view[:n])
            view = view[n:]
            inc.left -= n
            if inc.left == 0:
                self._next_file(inc)

    def _end(self, inc: _Incoming) -> None:
        inc.close()
        if inc.got != inc.size:
            raise ValueError("the clip ended early")
        if inc.kind == "text":
            clip = Clip("text", text=inc.buf.decode("utf-8"))
        elif inc.kind == "image":
            path = inc.folder / "clipboard.png"
            path.write_bytes(bytes(inc.buf))
            clip = Clip("image", image=bytes(inc.buf), tops=[str(path)])
        else:
            files, tops = [], []
            for parts, size in inc.files:
                files.append((str(inc.folder.joinpath(*parts)), "/".join(parts), size))
                top = str(inc.folder / parts[0])
                if top not in tops:
                    tops.append(top)
            clip = Clip("files", files=files, tops=tops)
        self.on_clip(clip, inc.id)


def _safe_id(cid) -> str:
    text = "".join(c for c in str(cid) if c.isalnum())[:24]
    return text or "clip"


def prune(staging: Path, keep: int) -> None:
    """Remove all but the newest ``keep`` received clips."""
    try:
        dirs = sorted((p for p in staging.iterdir() if p.is_dir()), key=lambda p: p.name)
    except OSError:
        return
    for old in dirs[:max(0, len(dirs) - keep)]:
        shutil.rmtree(old, ignore_errors=True)


def helper_command(clip: Clip) -> str:
    """The ``clipset`` line that puts a received clip on this device's clipboard."""
    if clip.kind == "text":
        data = {"kind": "text", "text": clip.text}
    elif clip.kind == "image":
        data = {"kind": "image", "path": clip.tops[0]}
    else:
        data = {"kind": "files", "paths": clip.tops}
    return "clipset " + json.dumps(data, ensure_ascii=True, separators=(",", ":"))


class ClipboardSync:
    """This device's clipboard and its links' (see the module docs)."""

    def __init__(self, command: Callable[[str], None], max_bytes: int = DEFAULT_MAX_MB * 1024 * 1024,
                 staging: Path | None = None, names: Callable[[str], str] = str):
        """``command(line)`` sends a line to the overlay helper."""
        self.command = command
        self.max_bytes = max_bytes
        self.staging = staging or default_staging()
        self.names = names
        self.peers: dict[str, Send] = {}
        self.kinds: dict[str, tuple[str, ...]] = {}  # what each peer takes
        self.receivers: dict[str, Receiver] = {}
        self.sending: dict[str, asyncio.Task] = {}

    # ------------------------------------------------------------- peers
    def add_peer(self, peer_id: str, send: Send, kinds: tuple[str, ...] = KINDS) -> None:
        self.remove_peer(peer_id)
        self.peers[peer_id] = send
        self.kinds[peer_id] = kinds
        self.receivers[peer_id] = Receiver(self.staging, self.max_bytes,
                                           lambda clip, cid, p=peer_id: self._received(p, clip))

    def remove_peer(self, peer_id: str) -> None:
        self.peers.pop(peer_id, None)
        self.kinds.pop(peer_id, None)
        receiver = self.receivers.pop(peer_id, None)
        if receiver is not None:
            receiver.drop()
        task = self.sending.pop(peer_id, None)
        if task is not None:
            task.cancel()

    def stop(self) -> None:
        for peer_id in list(self.peers):
            self.remove_peer(peer_id)

    # ----------------------------------------------------------- traffic
    def helper_line(self, line: str) -> None:
        """A ``clip {...}`` line from the helper: something was copied here."""
        try:
            data = json.loads(line.split(" ", 1)[1])
        except (IndexError, ValueError):
            return
        if not isinstance(data, dict):
            return
        if not self.peers:
            if data.get("kind") == "image" and isinstance(data.get("path"), str):
                with contextlib.suppress(OSError):
                    os.unlink(data["path"])
            return
        if data.get("kind") == "files":
            # Off the loop: listing copied files in a protected folder can
            # wait on a macOS permission prompt, and the loop forwards input.
            async def files() -> None:
                self._share(await asyncio.to_thread(clip_from_helper, data, self.max_bytes))

            asyncio.get_running_loop().create_task(files())
            return
        self._share(clip_from_helper(data, self.max_bytes))

    def _share(self, clip: Clip | None) -> None:
        if clip is not None:
            log.info("clipboard: copied %s here; sharing it", clip.describe())
            self._send_all(clip)

    def handle(self, peer_id: str, t: str, data: dict) -> None:
        """A ``clip`` or ``clip_data`` frame from a peer."""
        receiver = self.receivers.get(peer_id)
        if receiver is not None:
            receiver.handle(t, data)

    def _received(self, peer_id: str, clip: Clip) -> None:
        log.info("clipboard: %s copied %s", self.names(peer_id), clip.describe())
        self.command(helper_command(clip))
        self._send_all(clip, but=peer_id)

    def _send_all(self, clip: Clip, but: str | None = None) -> None:
        cid = new_id()
        for peer_id, send in list(self.peers.items()):
            if peer_id == but or clip.kind not in self.kinds.get(peer_id, KINDS):
                continue
            old = self.sending.pop(peer_id, None)
            if old is not None:
                old.cancel()  # a newer copy replaces one still on its way
            task = asyncio.get_running_loop().create_task(self._send(peer_id, send, clip, cid))
            self.sending[peer_id] = task

    async def _send(self, peer_id: str, send: Send, clip: Clip, cid: str) -> None:
        try:
            await send_clip(send, clip, cid)
        except asyncio.CancelledError:
            raise
        except (ConnectionError, OSError, TransferError) as exc:
            log.info("clipboard: couldn't send to %s: %s", self.names(peer_id), exc)
        except Exception:  # pragma: no cover - never take the service down
            log.exception("clipboard: sending to %s", peer_id)
        finally:
            if self.sending.get(peer_id) is asyncio.current_task():
                del self.sending[peer_id]
