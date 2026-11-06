"""The desktop app: a window, a tray icon, and the host or agent running behind them.

``curslink gui`` (or opening the installed app) starts it. Closing the
window leaves it running in the tray / menu bar; Quit in the tray menu stops
linking and exits. A second launch just shows the first one's window.
"""

from __future__ import annotations

import argparse
import getpass
import logging
import sys
from pathlib import Path

from PySide6.QtCore import QObject, QTimer, Signal, qInstallMessageHandler, qVersion
from PySide6.QtNetwork import QLocalServer, QLocalSocket
from PySide6.QtWidgets import QApplication, QMessageBox

from curslink import __version__, diagnostics, update
from curslink.config import ConfigError, agent_config, host_config, load_raw
from curslink.configedit import ConfigFile
from curslink.gui.common import Status, app_icon, call_on_main, in_background, main_thread
from curslink.gui.prefs import Prefs
from curslink.gui.service import FAILED, RUNNING, STARTING, STOPPED, Service

log = logging.getLogger(__name__)


def instance_name() -> str:
    try:
        user = getpass.getuser()
    except Exception:  # no user name in some sandboxes
        user = "user"
    return f"curslink-desktop-{user}"


class Curslink(QObject):
    """Everything the pages share: settings, the config file, the service, the status."""

    config_changed = Signal()
    service_changed = Signal(str)
    update_found = Signal(object)  # update.Release

    def __init__(self, prefs: Prefs):
        super().__init__()
        self.prefs = prefs
        self.status = Status(self)
        self.service = Service(self)
        self.service.event.connect(self.status.apply)
        self.service.output.connect(lambda line: self.status.add_log("output", line))
        self.service.state_changed.connect(self._on_service_state)
        self.release: update.Release | None = None
        self.quitting = False
        self.window = None
        self.tray = None
        diagnostics.on_crash(lambda path: call_on_main(
            lambda: self.status.add_log("error", f"Something went wrong; a report was saved to {path}")))

    # -- the config file ----------------------------------------------------
    @property
    def config_path(self) -> Path:
        return self.prefs.config_path

    def has_config(self) -> bool:
        return self.config_path.exists()

    def config(self) -> ConfigFile:
        return ConfigFile(self.config_path)

    def device_name(self) -> str:
        """This computer's name, as other devices see it."""
        try:
            cfg = self.config()
            raw = cfg.raw()
            if cfg.role == "host":
                me = raw.get("host", {}).get("device_id")
                return next((d.get("name", me) for d in raw.get("devices", []) if d.get("id") == me), me) or ""
            agent = raw.get("agent", {})
            return agent.get("name", agent.get("device_id", ""))
        except Exception:
            return ""

    @property
    def role(self) -> str | None:
        if not self.has_config():
            return None
        try:
            return self.config().role
        except Exception:
            return None

    def check_config(self) -> str | None:
        """What's wrong with the config, in words, or None if it loads."""
        try:
            raw = load_raw(self.config_path)
            role = ConfigFile(self.config_path).role
            if role == "host":
                host_config(raw, base_dir=self.config_path.parent)
            elif role == "agent":
                agent_config(raw, base_dir=self.config_path.parent)
            else:
                return "The config needs a [host] or an [agent] table (but not both)."
        except ConfigError as exc:
            return str(exc)
        return None

    def config_saved(self, restart: bool = True) -> None:
        """Call after writing the config: tells the pages and applies it."""
        self.config_changed.emit()
        if restart and self.service.is_alive():
            self.service.restart()

    # -- the service ----------------------------------------------------------
    def start_linking(self) -> None:
        role = self.role
        if role is None:
            return
        problem = self.check_config()
        if problem:
            self.service.error = problem
            self.service._set_state(FAILED)  # noqa: SLF001 - reported like a failed start
            return
        self.service.start(role, self.config_path)

    def stop_linking(self) -> None:
        self.service.stop()

    @property
    def linking(self) -> bool:
        return self.service.state in (STARTING, RUNNING)

    def _on_service_state(self, state: str) -> None:
        if state in (STOPPED, FAILED):
            self.status.stopped()
        self.service_changed.emit(state)

    # -- updates --------------------------------------------------------------
    def check_for_update(self, quiet: bool = True, on_result=None) -> None:
        settings = update.UpdateSettings()
        if self.has_config():
            try:
                settings = update.update_settings(self.config().raw())
            except ValueError:
                pass
        if quiet and not settings.check:
            return

        def done(release):
            self.release = release
            if release is not None:
                self.update_found.emit(release)
                if quiet and settings.auto and update.install_kind() is not None:
                    log.info("installing Curslink %s", release.version)
                    if self.tray is not None:
                        self.tray.say(f"Updating to Curslink {release.version}",
                                      "Curslink restarts by itself when it's done.")
                    self.install_update()
            if on_result:
                on_result(release, None)

        def failed(exc):
            log.info("update check failed: %s", exc)
            if on_result:
                on_result(None, exc)

        in_background(lambda: update.check(settings, timeout=8.0), done, failed)

    def install_update(self, on_error=None) -> None:
        release, kind = self.release, update.install_kind()
        if release is None:
            return
        if kind is None:
            if on_error:
                on_error(update.UpdateError(
                    'this copy runs from source; update it with pip install -U "curslink[gaze,gui]"'))
            return
        relaunch = ["gui"] if Path(sys.executable).stem.lower() != "curslinkw" else []

        def run():
            self.service.stop()
            return update.update(release, kind, relaunch=relaunch)

        def done(_message):
            self.quit()

        in_background(run, done, on_error)

    # -- lifetime -------------------------------------------------------------
    def show_window(self, page: str | None = None) -> None:
        if self.window is None:
            return
        if page:
            self.window.show_page(page)
        self.window.showNormal()
        self.window.raise_()
        self.window.activateWindow()

    def agent_paired(self) -> bool:
        """An agent with a host to link to (so it can run without a window)."""
        if self.role != "agent":
            return False
        try:
            from curslink.config import pairing_config
            from curslink.pairing.store import PairingStore

            store = pairing_config(self.config().raw(), self.config_path.parent).store
            return bool(PairingStore(store).peers("host"))
        except Exception:
            return False

    def hide_to_tray(self, force_note: bool = False) -> None:
        """Close the window and keep running in the tray, saying so once."""
        if self.window is None or self.tray is None or not self.tray.available():
            return
        self.window.hide()
        from curslink.gui import native

        native.set_dock_icon(False)
        if force_note or not self.prefs.told_about_tray:
            self.prefs.told_about_tray = True
            self.prefs.save()
            self.tray.say("Curslink is running in the background",
                          f"It links by itself. Open it from the {self.tray.place()} icon.")

    def quit(self) -> None:
        self.quitting = True
        self.service.stop()
        if self.tray is not None:
            self.tray.hide()
        QApplication.quit()


def _already_running(name: str) -> bool:
    """Ask a running copy to show its window; True if there was one."""
    socket = QLocalSocket()
    socket.connectToServer(name)
    if not socket.waitForConnected(500):
        return False
    socket.write(b"show\n")
    socket.flush()
    socket.waitForBytesWritten(500)
    socket.disconnectFromServer()
    return True


def _listen(name: str, app: Curslink) -> QLocalServer:
    server = QLocalServer(app)
    QLocalServer.removeServer(name)  # a stale socket from a crash
    server.listen(name)

    def on_connection():
        conn = server.nextPendingConnection()
        if conn is not None:
            conn.readyRead.connect(lambda: (conn.readAll(), app.show_window()))

    server.newConnection.connect(on_connection)
    return server


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="curslink gui", description="Open the Curslink app.")
    parser.add_argument("--minimized", action="store_true", help="start in the tray without opening the window")
    parser.add_argument("-c", "--config", help="use this curslink.toml (remembered for next time)")
    parser.add_argument("-v", "--verbose", action="store_true", help="log at debug level")
    args, qt_args = parser.parse_known_args(argv if argv is not None else [])

    qt = QApplication.instance() or QApplication([sys.argv[0], *qt_args])
    qt.setApplicationName("Curslink")
    qt.setApplicationDisplayName("Curslink")
    qt.setApplicationVersion(__version__)
    qt.setOrganizationDomain("kylepantall.github.io")
    qt.setDesktopFileName("curslink")
    qt.setWindowIcon(app_icon())
    qt.setStyle("Fusion")
    qt.setQuitOnLastWindowClosed(False)

    name = instance_name()
    if _already_running(name):
        return 0

    # Only the copy that stays running owns the log files.
    diagnostics.setup("gui", verbose=args.verbose)
    qInstallMessageHandler(diagnostics.qt_message_handler)
    main_thread()  # created on the Qt thread before any worker needs it
    log.info("Qt %s, platform plugin %s", qVersion(), qt.platformName())

    prefs = Prefs.load()
    if args.config:
        prefs.config = str(Path(args.config).expanduser().resolve())
        prefs.save()
    from curslink.gui import native, theme

    theme.manager(prefs.theme).apply()
    app = Curslink(prefs)
    server = _listen(name, app)  # noqa: F841 - kept alive by its parent

    from curslink.gui.tray import Tray
    from curslink.gui.window import MainWindow

    app.window = MainWindow(app)
    app.tray = Tray(app)
    tray_ok = app.tray.available()
    if sys.platform == "darwin":
        # Clicking the Dock icon re-activates the app: show the window, so
        # an icon in the Dock always leads somewhere.
        def on_state(state) -> None:
            from PySide6.QtCore import Qt

            if (state == Qt.ApplicationState.ApplicationActive and not app.window.isVisible()
                    and native.dock_icon_visible()):
                log.info("Dock icon clicked with the window hidden; showing it")
                app.show_window()

        qt.applicationStateChanged.connect(on_state)
    if tray_ok:
        app.tray.show()

    if not app.has_config():
        app.show_window("setup")
    else:
        if prefs.start_linking:
            app.start_linking()
        # Agents have nothing to look at once paired, so they start hidden.
        hidden = args.minimized or prefs.start_minimized or app.agent_paired()
        log.info("starting %s (minimized flag %s, start minimised %s, %s, tray %s)",
                 "hidden in the tray" if hidden and tray_ok else "with the window open", args.minimized,
                 prefs.start_minimized, app.role, "available" if tray_ok else "unavailable")
        if not hidden or not tray_ok:
            app.show_window()
        else:
            native.set_dock_icon(False)
            if not prefs.told_about_tray:
                prefs.told_about_tray = True
                prefs.save()
                QTimer.singleShot(1200, lambda: app.tray.say(
                    "Curslink is running in the background",
                    f"It links by itself. Open it from the {app.tray.place()} icon."))
    if not tray_ok and args.minimized:
        QMessageBox.information(
            app.window, "Curslink",
            "There's no system tray here, so Curslink stays open in a window. "
            "Minimise it to keep it out of the way.",
        )
    crashed = diagnostics.last_native_crash()
    if crashed is not None and tray_ok:
        QTimer.singleShot(1500, lambda: app.tray.say(
            "Curslink closed unexpectedly last time",
            "A crash report was saved. Settings > Diagnostics has it."))
    QTimer.singleShot(4000, app.check_for_update)
    daily = QTimer(app)
    daily.timeout.connect(app.check_for_update)
    daily.start(24 * 3600 * 1000)

    code = qt.exec()
    log.info("quitting (exit code %s)", code)
    app.service.stop()
    return code
