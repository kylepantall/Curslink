"""Regenerate Curslink icon exports on macOS with sips, iconutil and Pillow."""

from io import BytesIO
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory

from PIL import Image, ImageDraw

HERE = Path(__file__).resolve().parent
ASSETS = HERE.parents[1] / "curslink" / "assets"
SIZES = (16, 22, 32, 44, 64)
DOTS = {"good": "#2EC27E", "warning": "#E5A50A"}


def rasterize(source, directory):
    destination = directory / f"{source.stem}.png"
    subprocess.run(
        ["sips", "-s", "format", "png", str(source), "--out", str(destination)],
        check=True, stdout=subprocess.DEVNULL,
    )
    with Image.open(destination) as image:
        return image.convert("RGBA")


def square(image, size):
    return image.resize((size, size), Image.Resampling.LANCZOS)


def write_ico(images, target):
    """Write one PNG payload per ICO entry to preserve the small-icon design."""
    import struct

    offset = 6 + 16 * len(images)
    entries, payloads = [], []
    for image in images:
        buffer = BytesIO()
        image.save(buffer, format="PNG")
        payload = buffer.getvalue()
        size = image.width % 256
        entries.append(struct.pack("<BBBBHHII", size, size, 0, 0, 1, 32, len(payload), offset))
        payloads.append(payload)
        offset += len(payload)
    target.write_bytes(struct.pack("<HHH", 0, 1, len(images)) + b"".join(entries + payloads))


def tray_variant(glyph, size, colour):
    canvas = glyph.copy()
    unit = canvas.width / 100
    draw = ImageDraw.Draw(canvas)
    # Radius 20%, with an 8% transparent separation from the glyph.
    draw.ellipse(tuple(round(n * unit) for n in (48, 48, 104, 104)), fill=(0, 0, 0, 0))
    draw.ellipse(tuple(round(n * unit) for n in (56, 56, 96, 96)), fill=colour)
    return square(canvas, size)


def main():
    with TemporaryDirectory() as temporary:
        scratch = Path(temporary)
        master = rasterize(ASSETS / "curslink.svg", scratch)
        small = rasterize(ASSETS / "curslink-small.svg", scratch)
        tray = rasterize(ASSETS / "tray.svg", scratch)
        small.save(ASSETS / "curslink-small.png")
        tray.save(ASSETS / "tray.png")

        iconset = scratch / "curslink.iconset"
        iconset.mkdir()
        for base in (16, 32, 128, 256, 512):
            for scale, suffix in ((1, ""), (2, "@2x")):
                size = base * scale
                source = small if size <= 24 else master
                square(source, size).save(iconset / f"icon_{base}x{base}{suffix}.png")
        subprocess.run(
            ["iconutil", "-c", "icns", str(iconset), "-o", str(HERE / "curslink.icns")],
            check=True,
        )

        tile = master.crop(master.getchannel("A").getbbox())
        windows = []
        linux = HERE / "png"
        linux.mkdir(exist_ok=True)
        for size in (16, 20, 24, 32, 40, 48, 64, 128, 256):
            if size <= 24:
                windows.append(square(small, size))
                continue
            width = round(size * .94)
            frame = Image.new("RGBA", (size, size))
            frame.alpha_composite(square(tile, width), ((size - width) // 2,) * 2)
            windows.append(frame)
        write_ico(windows, HERE / "curslink.ico")

        for size in (16, 22, 24, 32, 48, 64, 128, 256, 512):
            source = small if size <= 24 else tile
            square(source, size).save(linux / f"curslink-{size}.png")

        tray_dir = HERE / "tray"
        tray_dir.mkdir(exist_ok=True)
        for size in SIZES:
            square(tray, size).save(tray_dir / f"template-{size}.png")
            for platform, off in {"windows": ("#8A8A8A", "#9A9A9A"),
                                  "linux": ("#6C6C70", "#919193")}.items():
                for theme, off_colour in zip(("light", "dark"), off):
                    glyph = tray.copy()
                    if theme == "dark":
                        glyph = Image.new("RGBA", tray.size, "white")
                        glyph.putalpha(tray.getchannel("A"))
                    for status, colour in {**DOTS, "off": off_colour}.items():
                        name = f"{platform}-{theme}-{status}-{size}.png"
                        tray_variant(glyph, size, colour).save(tray_dir / name)


if __name__ == "__main__":
    main()
