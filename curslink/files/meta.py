"""What a dragged file looks like on the placeholder: its name, kind and size."""

from __future__ import annotations

import mimetypes
import os
from dataclasses import dataclass
from pathlib import Path

# Walking a huge folder just to show its size isn't worth the wait.
MAX_WALK = 20000

_KINDS = {
    "application/pdf": "PDF document",
    "application/zip": "ZIP archive",
    "application/json": "JSON file",
    "application/msword": "Word document",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "Word document",
    "application/vnd.ms-excel": "Excel spreadsheet",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": "Excel spreadsheet",
    "application/vnd.ms-powerpoint": "PowerPoint presentation",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": "PowerPoint presentation",
    "text/plain": "Text file",
    "text/csv": "CSV file",
    "text/html": "HTML file",
}


def kind_of(name: str, is_dir: bool = False) -> str:
    """A short human description: "PDF document", "PNG image", "Folder"."""
    if is_dir:
        return "Folder"
    ext = Path(name).suffix.lstrip(".")
    mime, _ = mimetypes.guess_type(name, strict=False)
    if mime in _KINDS:
        return _KINDS[mime]
    if mime:
        major = mime.split("/", 1)[0]
        noun = {"image": "image", "video": "video", "audio": "audio", "text": "text file"}.get(major)
        if noun and ext:
            return f"{ext.upper()} {noun}"
    return f"{ext.upper()} file" if ext else "File"


def human_size(n: int) -> str:
    """1536 -> "1.5 KB" (powers of 1000, like Finder and Explorer's details)."""
    if n < 1000:
        return f"{n} byte{'s' if n != 1 else ''}"
    value = float(n)
    for unit in ("KB", "MB", "GB", "TB"):
        value /= 1000.0
        if value < 1000 or unit == "TB":
            return f"{value:.1f} {unit}" if value < 100 else f"{value:.0f} {unit}"
    raise AssertionError("unreachable")


@dataclass(frozen=True)
class Dragged:
    """One thing being dragged: a file or a folder."""

    path: str
    name: str
    size: int
    is_dir: bool

    @property
    def kind(self) -> str:
        return kind_of(self.name, self.is_dir)

    def card(self) -> dict:
        return {"name": self.name, "kind": self.kind, "size": self.size, "dir": self.is_dir}


def folder_size(path: str) -> int:
    total = 0
    seen = 0
    for root, _dirs, files in os.walk(path):
        for f in files:
            seen += 1
            if seen > MAX_WALK:
                return total
            try:
                total += os.lstat(os.path.join(root, f)).st_size
            except OSError:
                pass
    return total


def describe(paths: list[str]) -> list[Dragged]:
    """The dragged paths that exist, as cards. Blocking (stats every file
    of a folder): run it off the event loop."""
    out = []
    for p in paths:
        try:
            st = os.stat(p)
        except OSError:
            continue
        is_dir = os.path.isdir(p)
        name = os.path.basename(os.path.normpath(p)) or p
        out.append(Dragged(p, name, folder_size(p) if is_dir else st.st_size, is_dir))
    return out


def card(items: list[Dragged]) -> dict:
    """The placeholder's contents: the first item, plus how many in all."""
    total = sum(i.size for i in items)
    first = items[0]
    return {
        "name": first.name,
        "kind": first.kind,
        "size": total,
        "size_text": human_size(total),
        "count": len(items),
        "ext": "" if first.is_dir else Path(first.name).suffix.lstrip(".").upper()[:4],
        "dir": first.is_dir,
    }
