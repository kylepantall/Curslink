"""The main window: a sidebar of pages and the page itself.

The sidebar follows each platform (see docs/design/desktop-ui-spec.md):
Windows has no brand block and a short accent bar on the selected page;
macOS floats the sidebar on glass under the traffic lights; Linux has a
plain Adwaita-style sidebar. Before this computer is set up, the sidebar
shows just the app's name and version.
"""

from __future__ import annotations

import logging
import sys

from PySide6.QtCore import QEvent, QRectF, QSize, Qt, QTimer
from PySide6.QtGui import QCloseEvent, QColor, QKeySequence, QPainter, QPixmap, QShortcut
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
from curslink.gui import native, theme
from curslink.gui.common import DOT_COLOURS, app_image
from curslink.gui.service import FAILED, RUNNING, STARTING
from curslink.gui.widgets import label, nav_icon, restyle_pills

log = logging.getLogger(__name__)

PAGES = (
    ("status", "Status"),
    ("devices", "Devices"),
    ("layout", "Layout"),
    ("calibrate", "Calibrate"),
    ("settings", "Settings"),
)
HOST_ONLY = ("layout", "calibrate")


class NavButton(QPushButton):
    """A sidebar entry: glyph and title, drawn per platform."""

    def __init__(self, key: str, title: str, sub: bool = False):
        super().__init__(title)
        self.key = key
        # A section under a page (Settings): indented under its title, no glyph.
        self.sub = sub
        # Its sections are listed under it: the selected section is the one
        # highlighted, as in Windows' and macOS's own Settings.
        self.expanded = False
        self.setCheckable(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setObjectName("nav")
        self.setAccessibleName(title)
        self._hover = False

    def sizeHint(self) -> QSize:
        h = theme.current().size["nav_h"]
        return QSize(200, h - 4 if self.sub else h)

    def minimumSizeHint(self) -> QSize:
        return self.sizeHint()

    def enterEvent(self, e) -> None:  # noqa: N802
        self._hover = True
        self.update()
        super().enterEvent(e)

    def leaveEvent(self, e) -> None:  # noqa: N802
        self._hover = False
        self.update()
        super().leaveEvent(e)

    def paintEvent(self, _e) -> None:  # noqa: N802
        th = theme.current()
        t = th.tokens
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = QRectF(self.rect())
        radius = th.size["r_nav"]
        selected = self.isChecked() and not self.expanded
        if selected or self._hover:
            bg = theme.qcolor(t["navsel"])
            if not selected:
                bg.setAlphaF(bg.alphaF() * 0.6)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(bg)
            p.drawRoundedRect(r, radius, radius)
        ink = t["ink"] if self.isEnabled() else t["ink3"]
        icon_colour = t["accent"] if selected and self.isEnabled() else ink
        pad = 12
        if not self.sub:
            icon = nav_icon(self.key, icon_colour, 16).pixmap(16, 16)
            p.drawPixmap(int(pad), int((r.height() - 16) / 2), icon)
        f = self.font()
        f.setPixelSize(th.size["font_px"] - (1 if self.sub else 0))
        f.setWeight(f.Weight.DemiBold if self.isChecked() else f.Weight.Normal)
        p.setFont(f)
        p.setPen(theme.qcolor(ink))
        p.drawText(r.adjusted(pad + 16 + 12, 0, -8, 0), Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
                   self.text())
        p.end()


class Dot(QWidget):
    def __init__(self):
        super().__init__()
        self.setFixedSize(10, 10)
        self.colour = QColor("#888")

    def set(self, colour: str) -> None:
        self.colour = QColor(colour)
        self.update()

    def paintEvent(self, _e) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(self.colour)
        p.drawEllipse(QRectF(1, 1, 8, 8))
        p.end()


def logo(size: int) -> QLabel:
    lab = QLabel()
    px = QPixmap.fromImage(app_image(size * 2))
    px.setDevicePixelRatio(2.0)
    lab.setPixmap(px)
    lab.setFixedSize(size, size)
    lab.setStyleSheet("background: transparent;")
    return lab


SIDE_INSET = 8  # the floating macOS sidebar's gap to the window edge


class MainWindow(QMainWindow):
    def __init__(self, app):
        super().__init__()
        self.app = app
        self.setWindowTitle("Curslink")
        QShortcut(QKeySequence("Ctrl+Shift+D"), self, app.show_console)  # Cmd+Shift+D on a Mac
        self.resize(1080, 720)
        self.setMinimumSize(QSize(760, 520))
        self.glass = False
        self._glass_wanted = False  # one Curslink look: its own solid surfaces, no Mica or vibrancy behind
        if sys.platform == "darwin" and native.can_glass():
            native.prepare(self, glass=False)  # the sidebar runs up under the window's buttons

        from curslink.gui.pages.calibrate import CalibratePage
        from curslink.gui.pages.devices import DevicesPage
        from curslink.gui.pages.layout import LayoutPage
        from curslink.gui.pages.settings import SettingsPage
        from curslink.gui.pages.setup import SetupPage
        from curslink.gui.pages.status import StatusPage

        th = theme.current()
        root = QWidget()
        root.setObjectName("root")
        h = QHBoxLayout(root)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(0)
        self.setCentralWidget(root)

        side = QWidget()
        side.setObjectName("sidebar")
        side.setFixedWidth(th.size["side_w"])
        side.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        sv = QVBoxLayout(side)
        sv.setContentsMargins(10, 8 + th.size["mac_top"], 10, 12)
        sv.setSpacing(4)
        self.side = side

        self.brand = QWidget()
        brand = QHBoxLayout(self.brand)
        brand.setContentsMargins(8, 6, 8, 14)
        brand.setSpacing(10)
        brand.addWidget(logo(34))
        names = QVBoxLayout()
        names.setSpacing(0)
        names.addWidget(label("Curslink", "brand"))
        self.role_label = label("", "brandSub")
        names.addWidget(self.role_label)
        brand.addLayout(names, 1)
        sv.addWidget(self.brand)
        self.brand.setVisible(True)

        self.nav = QButtonGroup(self)
        self.nav.setExclusive(True)
        self.nav_buttons: dict[str, NavButton] = {}
        self.nav_box = QWidget()
        nb = QVBoxLayout(self.nav_box)
        nb.setContentsMargins(0, 0, 0, 0)
        nb.setSpacing(2)
        for key, title in PAGES:
            b = NavButton(key, title)
            b.clicked.connect(lambda _=False, k=key: self.show_page(k))
            self.nav.addButton(b)
            self.nav_buttons[key] = b
            nb.addWidget(b)
            if key == "settings":
                nb.addWidget(self._settings_nav())
        sv.addWidget(self.nav_box)

        # First run: just the name and version, centred.
        self.welcome = QWidget()
        wv = QVBoxLayout(self.welcome)
        wv.setContentsMargins(8, 0, 8, 40)
        wv.setSpacing(4)
        wv.addStretch(1)
        wv.addWidget(logo(96), 0, Qt.AlignmentFlag.AlignHCenter)
        wv.addSpacing(10)
        for text, name in (("Curslink", "welcomeName"), (f"Version {__version__}", "welcomeVersion")):
            w = label(text, name)
            w.setAlignment(Qt.AlignmentFlag.AlignHCenter)
            wv.addWidget(w)
        note = label("Set up this computer to start linking.", "welcomeNote", wrap=True)
        note.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        wv.addSpacing(8)
        wv.addWidget(note)
        wv.addStretch(1)
        sv.addWidget(self.welcome, 1)

        self.spacer = QWidget()
        sv.addWidget(self.spacer, 1)

        self.footer = QFrame()
        fv = QVBoxLayout(self.footer)
        fv.setContentsMargins(10, 8, 10, 4)
        fv.setSpacing(2)
        top = QHBoxLayout()
        top.setSpacing(7)
        self.dot = Dot()
        top.addWidget(self.dot)
        self.state_label = label("", "sideNoteTitle")
        top.addWidget(self.state_label, 1)
        fv.addLayout(top)
        self.version_label = label("", "sideNoteText")
        fv.addWidget(self.version_label)
        sv.addWidget(self.footer)
        # macOS: the sidebar is the window's glass itself, full height from
        # under the traffic lights to the bottom edge, with the body beside it.
        self.floating_side = False
        h.addWidget(side)

        pane = QWidget()
        pane.setObjectName("contentPane")
        pane.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        pv = QVBoxLayout(pane)
        pv.setContentsMargins(th.size["side_w"] + SIDE_INSET if self.floating_side else 0, 0, 0, 0)
        self.stack = QStackedWidget()
        pv.addWidget(self.stack)
        h.addWidget(pane, 1)

        calibrate = CalibratePage(app)
        self.pages = {
            "status": StatusPage(app),
            "devices": DevicesPage(app),
            "layout": LayoutPage(app),
            "calibrate": calibrate,
            "settings": SettingsPage(app, calibrate=calibrate),  # gaze settings go on the Calibrate page
            "setup": SetupPage(app),
        }
        for page in self.pages.values():
            self.stack.addWidget(page)
        if self.floating_side:
            side.setParent(root)
            side.raise_()
            root.installEventFilter(self)
            self.place_side()

        app.service_changed.connect(lambda _s: self.refresh_side())
        app.status.changed.connect(self.refresh_side)
        app.config_changed.connect(self.refresh_side)
        theme.manager().changed.connect(self.theme_changed)
        self.refresh_side()
        self.show_page("status")

    def _settings_nav(self) -> QWidget:
        """The Settings sections, listed under Settings while it's open."""
        from curslink.gui.pages.settings import SECTIONS

        self.section_nav = QButtonGroup(self)
        self.section_nav.setExclusive(True)
        self.section_buttons: dict[str, NavButton] = {}
        self.sections_box = QWidget()
        v = QVBoxLayout(self.sections_box)
        v.setContentsMargins(0, 0, 0, 4)
        v.setSpacing(1)
        for key, title, _description in SECTIONS:
            b = NavButton(key, title, sub=True)
            b.setAccessibleName(f"Settings: {title}")
            b.clicked.connect(lambda _=False, k=key: self.show_page(f"settings/{k}"))
            self.section_nav.addButton(b)
            self.section_buttons[key] = b
            v.addWidget(b)
        self.sections_box.hide()
        return self.sections_box

    def place_side(self) -> None:
        root = self.centralWidget()
        self.side.setGeometry(SIDE_INSET, SIDE_INSET, self.side.width(), max(0, root.height() - 2 * SIDE_INSET))
        self.side.raise_()

    def remove_event_filters(self) -> None:
        """Stop watching the central widget (see Curslink.remove_event_filters)."""
        if self.floating_side:
            root = self.centralWidget()
            if root is not None:
                root.removeEventFilter(self)

    def eventFilter(self, obj, event) -> bool:  # noqa: N802
        if self.floating_side and obj is self.centralWidget() and event.type() == QEvent.Type.Resize:
            self.place_side()
        return super().eventFilter(obj, event)

    # -- pages ----------------------------------------------------------------
    def show_page(self, key: str) -> None:
        """Show a page; ``settings/<section>`` opens one Settings section."""
        key, _, section = key.partition("/")
        configured = self.app.has_config()
        if key != "setup" and not configured:
            key = "setup"
        if key in HOST_ONLY and self.app.role == "agent":
            key = "status"
        page = self.pages[key]
        if key == "settings":
            page.show_section(section or (page.current_section() if self.current_page() == "settings"
                                          else "general"))
        self.stack.setCurrentWidget(page)
        for k, b in self.nav_buttons.items():
            b.setChecked(k == key)
        self.refresh_side()
        if hasattr(page, "shown"):
            page.shown()

    def current_page(self) -> str:
        w = self.stack.currentWidget()
        return next(k for k, p in self.pages.items() if p is w)

    def refresh_side(self) -> None:
        configured = self.app.has_config()
        role = self.app.role
        first_run = not configured or (bool(getattr(self, "pages", None)) and self.current_page() == "setup")
        self.brand.setVisible(not first_run)
        self.nav_box.setVisible(not first_run)
        self.welcome.setVisible(first_run)
        self.spacer.setVisible(not first_run)
        self.footer.setVisible(not first_run)
        for key in HOST_ONLY:
            self.nav_buttons[key].setVisible(role != "agent")
        settings = getattr(self, "pages", {}).get("settings")
        if settings is not None:
            on = self.current_page() == "settings"
            self.sections_box.setVisible(on and not first_run)
            self.nav_buttons["settings"].expanded = on
            self.nav_buttons["settings"].update()
            keys = settings.section_keys(role)
            current = settings.current_section()
            for key, b in self.section_buttons.items():
                b.setVisible(key in keys)
                b.setChecked(key == current)
        if role:
            self.role_label.setText(f"{role.title()} · {self.app.device_name()}")
        self.version_label.setText(f"Curslink {__version__}" + (f" · {role.title()}" if role else ""))
        state = self.app.service.state
        status = self.app.status
        if state == RUNNING:
            if role == "agent":
                if status.host_state == "connected":
                    text, colour = "Linked to the host", DOT_COLOURS["good"]
                else:
                    text, colour = "Waiting for the host", DOT_COLOURS["warn"]
            elif status.gaze_paused:
                text, colour = "Linking, gaze off", DOT_COLOURS["warn"]
            else:
                text, colour = "Linking as host", DOT_COLOURS["good"]
        elif state == STARTING:
            text, colour = "Starting…", DOT_COLOURS["warn"]
        elif state == FAILED:
            text, colour = "Stopped: needs attention", DOT_COLOURS["bad"]
        else:
            text, colour = "Not linking", theme.current().css("ink3")
        self.state_label.setText(text)
        self.dot.set(colour)

    # -- theme and native look ------------------------------------------------
    def theme_changed(self) -> None:
        restyle_pills()
        for b in [*self.nav_buttons.values(), *self.section_buttons.values()]:
            b.update()
        self.refresh_side()
        if self.glass:
            native.apply_glass(self, theme.current().dark)
        else:
            native.set_dark_title_bar(self, theme.current().dark)
        for page in self.pages.values():
            if hasattr(page, "theme_changed"):
                page.theme_changed()
        self.update()

    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
        native.set_dock_icon(True)
        if self._glass_wanted and not self.glass:
            if sys.platform == "win32":
                # Qt sends this before the window reaches the screen, so Mica
                # and the title bar colour are in place for the first frame
                # (applied later, the window flashed black and light first).
                self._apply_glass()
            else:
                QTimer.singleShot(0, self._apply_glass)
        else:
            native.set_dark_title_bar(self, theme.current().dark)

    def _apply_glass(self) -> None:
        self.glass = native.apply_glass(self, theme.current().dark)
        theme.manager().set_glass(self.glass)

    def hideEvent(self, event) -> None:  # noqa: N802
        super().hideEvent(event)
        if self.app.tray_placed():
            native.set_dock_icon(False)

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802
        if self.app.quitting:
            event.accept()  # already on the way out: don't quit a second time
            return
        if self.app.tray is None or not self.app.tray.isVisible():
            self.app.quit()
            event.accept()
            return
        event.ignore()
        self.hide()
        if not self.app.prefs.told_about_tray:
            self.app.prefs.told_about_tray = True
            self.app.prefs.save()
            self.app.tray.say("Curslink is still running",
                              f"It keeps linking in the background. Open it again from the {self.app.tray.place()} icon.")
