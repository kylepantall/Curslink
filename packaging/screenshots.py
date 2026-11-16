"""Draw the desktop app's pages to PNGs, in light and dark.

Used by CI (.github/workflows/screenshots.yml) to show the app as it's
drawn on each platform (one Curslink look; the fonts and the Mac's window
buttons are what differ), without each machine to hand::

    python packaging/screenshots.py OUT_DIR

Every Settings section is drawn as well as the main pages.
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

PAGES = ("status", "devices", "layout", "calibrate")


def main(argv: list[str]) -> int:
    out = Path(argv[0] if argv else "screenshots")
    out.mkdir(parents=True, exist_ok=True)
    home = Path(tempfile.mkdtemp(prefix="curslink-shots-"))
    os.environ["CURSLINK_HOME"] = str(home)
    config = home / "curslink.toml"
    import tomlkit

    # The example has [host] and [agent] side by side; keep the host, so the
    # pictures show the host's pages (the layout editor, its devices).
    doc = tomlkit.parse((Path(__file__).resolve().parent.parent / "examples" / "curslink.toml").read_text("utf-8"))
    doc.pop("agent", None)
    config.write_text(tomlkit.dumps(doc), "utf-8")

    from PySide6.QtWidgets import QApplication

    from curslink.gui import theme
    from curslink.gui.app import Curslink
    from curslink.gui.pages.settings import SECTIONS
    from curslink.gui.prefs import Prefs
    from curslink.gui.window import MainWindow

    qt = QApplication.instance() or QApplication(["curslink-screenshots"])
    qt.setStyle("Fusion")  # as the app does
    manager = theme.manager("light")
    manager.apply()  # as the app does, before any window is built
    # No Mica or vibrancy: grab() draws the widgets, not the desktop behind.
    app = Curslink(Prefs(config=str(config), start_linking=False, glass=False))
    win = MainWindow(app)
    app.window = win
    win.resize(1100, 760)
    win.show()
    pages = [*PAGES, *(f"settings/{key}" for key, _t, _d in SECTIONS)]
    for mode in ("light", "dark"):
        manager.set_mode(mode)
        for page in pages:
            win.show_page(page)
            for _ in range(5):
                qt.processEvents()
            path = out / f"{theme.platform_key()}-{mode}-{page.replace('/', '-')}.png"
            win.grab().save(str(path))
            print("saved", path, flush=True)
            if page == "calibrate":  # its gaze settings are below the calibration
                bar = win.pages["calibrate"].verticalScrollBar()
                bar.setValue(bar.maximum())
                qt.processEvents()
                path = path.with_name(path.stem + "-settings.png")
                win.grab().save(str(path))
                bar.setValue(0)
                print("saved", path, flush=True)
    win.close()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
