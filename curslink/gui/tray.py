"""The tray (Windows, Linux) / menu bar (macOS) icon and its menu."""

from __future__ import annotations

import sys

from PySide6.QtGui import QAction, QGuiApplication, QKeySequence, QPalette
from PySide6.QtWidgets import QMenu, QSystemTrayIcon

from curslink.gui.common import tray_icon
from curslink.gui.service import FAILED, RUNNING, STARTING

MAC = sys.platform == "darwin"


def _title(text: str) -> str:
    """macOS menus use title case."""
    return text.title().replace("A Device", "a Device") if MAC else text


class Tray(QSystemTrayIcon):
    def __init__(self, app):
        super().__init__()
        self.app = app
        self.setToolTip("Curslink")
        menu = QMenu()
        self.heading = QAction("Curslink", menu)
        self.heading.setEnabled(False)
        self.detail = QAction("", menu)
        self.detail.setEnabled(False)
        menu.addAction(self.heading)
        menu.addAction(self.detail)
        menu.addSeparator()
        self.gaze = menu.addAction(_title("Turn gaze off"), self.toggle_gaze)
        self.gaze.setShortcut(QKeySequence("Ctrl+Alt+G"))
        self.home = menu.addAction(_title("Bring input home"), lambda: app.service.send("home"))
        self.home.setShortcut(QKeySequence("Ctrl+Alt+Home"))
        menu.addAction(_title("Pair a device…"), lambda: app.show_window("devices"))
        self.switch = menu.addAction("", app.ask_switch_role)
        menu.addSeparator()
        self.update_action = menu.addAction("", lambda: app.show_window("settings"))
        self.update_action.setVisible(False)
        menu.addAction(_title("Open Curslink") + ("…" if MAC else ""), app.show_window)
        self.run = menu.addAction(_title("Stop linking"), self.toggle_run)
        quit_action = menu.addAction(_title("Quit Curslink"), app.quit)
        quit_action.setShortcut(QKeySequence.StandardKey.Quit)
        self.menu = menu
        self.setContextMenu(menu)
        self.activated.connect(self.on_activated)
        self.messageClicked.connect(app.show_window)

        app.service_changed.connect(lambda _s: self.refresh())
        app.config_changed.connect(self.refresh)
        app.status.changed.connect(self.refresh)
        app.status.switched.connect(lambda _s: self.refresh())
        app.update_found.connect(self.show_update)
        QGuiApplication.instance().paletteChanged.connect(lambda _p: self.refresh())
        self.refresh()

    @staticmethod
    def place() -> str:
        return "menu bar" if MAC else "tray"

    def available(self) -> bool:
        return QSystemTrayIcon.isSystemTrayAvailable()

    def say(self, title: str, text: str) -> None:
        if self.isVisible() and self.supportsMessages():
            self.showMessage(title, text, self.app.window.windowIcon() if self.app.window else tray_icon("", True),
                             6000)

    def on_activated(self, reason) -> None:
        # A left click opens the window on Windows and Linux; macOS shows the menu.
        if MAC:
            return
        if reason in (QSystemTrayIcon.ActivationReason.Trigger, QSystemTrayIcon.ActivationReason.DoubleClick):
            self.app.show_window()

    def toggle_gaze(self) -> None:
        self.app.service.send("gaze toggle")

    def toggle_run(self) -> None:
        if self.app.linking:
            self.app.stop_linking()
        else:
            self.app.start_linking()

    def show_update(self, release) -> None:
        self.update_action.setText(f"Curslink {release.version} is out…")
        self.update_action.setVisible(True)

    def dark_bar(self) -> bool:
        if sys.platform == "win32":
            return True  # Windows 10/11 taskbars default to dark
        window = QGuiApplication.palette().color(QPalette.ColorRole.Window)
        return window.lightness() < 128

    def refresh(self) -> None:
        app, status, state = self.app, self.app.status, self.app.service.state
        role = app.role or "host"
        names = {d.get("id"): d.get("name", d.get("id")) for d in (app.config().devices() if app.has_config() else [])}
        if state == RUNNING:
            icon_state = "paused" if status.gaze_paused else "running"
            self.heading.setText(f"Curslink: linking as {role}")
            if role == "host":
                where = names.get(status.active, status.active)
                gaze = "gaze off" if status.gaze_paused else ("gaze on" if status.gaze else "gaze off")
                self.detail.setText(f"Input is on {where} · {gaze}")
            else:
                self.detail.setText("Connected to the host" if status.host_state == "connected"
                                    else (status.host_detail or "Waiting for the host"))
        elif state == STARTING:
            icon_state = "paused"
            self.heading.setText("Curslink: starting")
            self.detail.setText("")
        else:
            icon_state = "stopped"
            self.heading.setText("Curslink: not linking" if state != FAILED else "Curslink stopped")
            self.detail.setText(app.service.error[:80] if state == FAILED else "")
        self.detail.setVisible(bool(self.detail.text()))
        is_host_running = state == RUNNING and role == "host"
        self.gaze.setVisible(role == "host")
        self.gaze.setEnabled(is_host_running and status.gaze)
        self.gaze.setText(_title("Turn gaze on" if status.gaze_paused else "Turn gaze off"))
        self.home.setVisible(role == "host")
        self.home.setEnabled(is_host_running and status.active != status.device_id)
        self.switch.setText(_title("Use as agent…" if role == "host" else "Use as host…"))
        self.switch.setVisible(app.has_config())
        self.run.setText(_title("Stop linking" if app.linking else "Start linking"))
        self.run.setEnabled(app.has_config())
        self.setIcon(tray_icon(icon_state, self.dark_bar(), template=MAC))
        self.setToolTip(f"{self.heading.text()}\n{self.detail.text()}".strip())
