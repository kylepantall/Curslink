"""Draw the desktop app's main pages to PNGs, in light and dark.

Used by CI to show what Qt's native styles look like on each platform::

    python packaging/screenshots.py OUT_DIR
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

PAGES = ("status", "devices", "layout", "calibrate", "settings")


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
    from curslink.gui.prefs import Prefs
    from curslink.gui.window import MainWindow

    qt = QApplication.instance() or QApplication(["curslink-screenshots"])
    style = theme.install_style(qt)
    manager = theme.manager("light")
    manager.apply()  # as the app does, before any window is built
    # No Mica or vibrancy: grab() draws the widgets, not the desktop behind.
    app = Curslink(Prefs(config=str(config), start_linking=False, glass=False))
    win = MainWindow(app)
    app.window = win
    win.resize(1100, 720)
    win.show()
    shots = []
    for mode in ("light", "dark"):
        manager.set_mode(mode)
        for page in PAGES:
            win.show_page(page)
            for _ in range(5):
                qt.processEvents()
            path = out / f"{theme.platform_key()}-{style}-{mode}-{page}.png"
            win.grab().save(str(path))
            shots.append(path)
            print("saved", path, flush=True)
    win.close()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
