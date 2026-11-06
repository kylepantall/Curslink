"""The main window: a sidebar of pages, like the mockup."""

from __future__ import annotations

from PySide6.QtCore import QSize, Qt
from PySide6.QtGui import QCloseEvent, QPixmap
from PySide6.QtWidgets import (
    QButtonGroup,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QPushButton,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from curslink import __version__
from curslink.gui.common import GOOD, WARN, app_image
from curslink.gui.service import FAILED, RUNNING, STARTING
from curslink.gui.widgets import label

PAGES = (
    ("status", "Status"),
    ("devices", "Devices"),
    ("layout", "Layout"),
    ("calibrate", "Calibrate"),
    ("settings", "Settings"),
)


class MainWindow(QMainWindow):
    def __init__(self, app):
        super().__init__()
        self.app = app
        self.setWindowTitle("Curslink")
        self.resize(1120, 760)
        self.setMinimumSize(QSize(760, 520))

        from curslink.gui.pages.calibrate import CalibratePage
        from curslink.gui.pages.devices import DevicesPage
        from curslink.gui.pages.layout import LayoutPage
        from curslink.gui.pages.settings import SettingsPage
        from curslink.gui.pages.setup import SetupPage
        from curslink.gui.pages.status import StatusPage

        root = QWidget()
        h = QHBoxLayout(root)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(0)
        self.setCentralWidget(root)

        side = QWidget()
        side.setObjectName("sidebar")
        side.setFixedWidth(230)
        sv = QVBoxLayout(side)
        sv.setContentsMargins(16, 24, 16, 20)
        sv.setSpacing(20)
        brand = QHBoxLayout()
        brand.setSpacing(12)
        logo = QLabel()
        logo_px = QPixmap.fromImage(app_image(80 * 2))
        logo_px.setDevicePixelRatio(4.0)
        logo.setPixmap(logo_px)
        brand.addWidget(logo)
        names = QVBoxLayout()
        names.setSpacing(0)
        names.addWidget(label("Curslink", "brand"))
        self.role_label = label(__version__, "brandSub")
        names.addWidget(self.role_label)
        brand.addLayout(names, 1)
        sv.addLayout(brand)

        self.nav = QButtonGroup(self)
        self.nav.setExclusive(True)
        self.nav_buttons: dict[str, QPushButton] = {}
        nav_box = QVBoxLayout()
        nav_box.setSpacing(4)
        for key, title in PAGES:
            b = QPushButton(title)
            b.setObjectName("nav")
            b.setCheckable(True)
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            b.clicked.connect(lambda _=False, k=key: self.show_page(k))
            self.nav.addButton(b)
            self.nav_buttons[key] = b
            nav_box.addWidget(b)
        sv.addLayout(nav_box)
        sv.addStretch(1)

        note = QFrame()
        note.setObjectName("sideNote")
        nv = QVBoxLayout(note)
        nv.setContentsMargins(12, 12, 12, 12)
        nv.setSpacing(6)
        self.state_label = label("", "sideNoteTitle")
        nv.addWidget(self.state_label)
        tip = label("Closing this window keeps Curslink running in the tray.", "sideNoteText", wrap=True)
        nv.addWidget(tip)
        sv.addWidget(note)
        h.addWidget(side)

        self.stack = QStackedWidget()
        h.addWidget(self.stack, 1)
        self.pages = {
            "status": StatusPage(app),
            "devices": DevicesPage(app),
            "layout": LayoutPage(app),
            "calibrate": CalibratePage(app),
            "settings": SettingsPage(app),
            "setup": SetupPage(app),
        }
        for page in self.pages.values():
            self.stack.addWidget(page)

        app.service_changed.connect(lambda _s: self.refresh_side())
        app.status.changed.connect(self.refresh_side)
        app.config_changed.connect(self.refresh_side)
        self.refresh_side()
        self.show_page("status")

    def show_page(self, key: str) -> None:
        if key != "setup" and not self.app.has_config():
            key = "setup"
        page = self.pages[key]
        self.stack.setCurrentWidget(page)
        for k, b in self.nav_buttons.items():
            b.setChecked(k == key)
        for b in self.nav_buttons.values():
            b.setEnabled(self.app.has_config())
        if hasattr(page, "shown"):
            page.shown()

    def refresh_side(self) -> None:
        role = self.app.role
        self.role_label.setText(f"{__version__} · {role.title()}" if role else __version__)
        state = self.app.service.state
        if state == RUNNING:
            paused = self.app.status.gaze_paused
            text, colour = ("Linking, gaze paused", WARN) if paused else (f"Linking as {role or 'host'}", GOOD)
        elif state == STARTING:
            text, colour = "Starting…", WARN
        elif state == FAILED:
            text, colour = "Stopped: needs attention", "#E0705B"
        else:
            text, colour = "Not linking", "#8A97A8"
        self.state_label.setText(f'<span style="color:{colour}">●</span>&nbsp; {text}')

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802
        if self.app.quitting or self.app.tray is None or not self.app.tray.isVisible():
            self.app.quit()
            event.accept()
            return
        event.ignore()
        self.hide()
        if not self.app.prefs.told_about_tray:
            self.app.prefs.told_about_tray = True
            self.app.prefs.save()
            self.app.tray.say("Curslink is still running",
                              "It keeps linking in the background. Open it again from this icon.")
