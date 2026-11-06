"""Makes every platform's icon files from the masters in curslink/assets.

    pip install PySide6-Essentials pillow
    python packaging/icons/make_icons.py

Masters (drawn on Apple's icon grid: the tile fills the middle 824 of 1024 px):

    curslink/assets/curslink.svg        the icon
    curslink/assets/curslink-small.png  a simpler version for 16-24 px
    curslink/assets/tray.png            black tray / menu bar glyph (used as is)

Writes, next to this script:

    curslink.icns           macOS (16-1024 px, Apple's grid kept as drawn)
    curslink.ico            Windows (16-256 px, tile enlarged to fill the square)
    png/curslink-<N>.png    Linux hicolor sizes (filled like the .ico)

and docs/images/icon.png for the README. The outputs are committed, so builds
don't need to run this; run it again whenever a master changes.
"""

from __future__ import annotations

import io
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
ASSETS = ROOT / "curslink" / "assets"

ICO_SIZES = (16, 24, 32, 48, 64, 128, 256)
ICNS_SIZES = (16, 32, 64, 128, 256, 512, 1024)
PNG_SIZES = (16, 24, 32, 48, 64, 128, 256, 512)
SMALL_MAX = 24
TILE_SPAN = 824 / 1024   # share of the master the tile covers
TILE_FILL = 0.94         # share of the square it covers in .ico / PNGs


def _to_pillow(image):
    from PIL import Image
    from PySide6.QtCore import QBuffer, QIODevice

    buf = QBuffer()
    buf.open(QIODevice.OpenModeFlag.WriteOnly)
    image.save(buf, "PNG")
    return Image.open(io.BytesIO(bytes(buf.data()))).convert("RGBA")


def render(size: int, fill: float | None):
    """The icon at ``size`` px: ``fill`` = the tile's share of the square, or
    None for the master's own margins (macOS)."""
    from PySide6.QtCore import QRectF, Qt
    from PySide6.QtGui import QImage, QPainter
    from PySide6.QtSvg import QSvgRenderer

    span = size if fill is None else size * fill / TILE_SPAN
    image = QImage(size, size, QImage.Format.Format_ARGB32)
    image.fill(Qt.GlobalColor.transparent)
    painter = QPainter(image)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
    target = QRectF((size - span) / 2, (size - span) / 2, span, span)
    if size <= SMALL_MAX and fill is not None:
        painter.drawImage(QRectF(0, 0, size, size), QImage(str(ASSETS / "curslink-small.png")))
    else:
        QSvgRenderer(str(ASSETS / "curslink.svg")).render(painter, target)
    painter.end()
    return _to_pillow(image)


def main() -> int:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtGui import QGuiApplication

    app = QGuiApplication.instance() or QGuiApplication(sys.argv[:1])  # noqa: F841

    (HERE / "png").mkdir(parents=True, exist_ok=True)
    for size in PNG_SIZES:
        render(size, TILE_FILL).save(HERE / "png" / f"curslink-{size}.png")

    ico = [render(s, TILE_FILL) for s in ICO_SIZES]
    ico[-1].save(HERE / "curslink.ico", sizes=[(s, s) for s in ICO_SIZES], append_images=ico[:-1])

    icns = [render(s, None) for s in ICNS_SIZES]
    icns[-1].save(HERE / "curslink.icns", append_images=icns[:-1])

    readme = ROOT / "docs" / "images" / "icon.png"
    readme.parent.mkdir(parents=True, exist_ok=True)
    render(256, TILE_FILL).save(readme)
    print(f"wrote icons to {HERE} and {readme}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
