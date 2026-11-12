"""The desktop app: a window, a tray icon, and the host or agent running behind them.

``curslink gui`` (or opening the installed app) starts it. Closing the
window leaves it running in the tray / menu bar; Quit in the tray menu stops
linking and exits. A second launch just shows the first one's window.
"""

from __future__ import annotations

import argparse
import getpass
import logging
import os
import sys
import tempfile
import time
from pathlib import Path

from PySide6.QtCore import QLockFile, QObject, QTimer, Signal, qInstallMessageHandler, qVersion
from PySide6.QtNetwork import QLocalServer, QLocalSocket
from PySide6.QtWidgets import QApplication, QMessageBox

from curslink import __version__, diagnostics, update
from curslink.config import ConfigError, agent_config, host_config, load_raw
from curslink.configedit import ConfigFile, is_whole, role_of
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
        self.service.event.connect(self._on_look)
        self.service.event.connect(self._on_hint)
        self.calibrating = False  # host: the service gave the camera to calibration
        self.service.event.connect(self._on_calibrating)
        self.service.event.connect(self._on_paired)
        self._look = None
        self._hint = None
        self.release: update.Release | None = None
        self.quitting = False
        self.app_filters: list = []  # app-wide event filters, removed before quitting
        self.window = None
        self.tray = None
        diagnostics.on_crash(lambda path: call_on_main(
            lambda: self.status.add_log("error", f"Something went wrong; a report was saved to {path}")))

    # -- the config file ----------------------------------------------------
    @property
    def config_path(self) -> Path:
        return self.prefs.config_path

    def has_config(self) -> bool:
        """A config to run from. A stray setting left in a deleted config's
        place counts as none, so the app goes back to setup instead of
        failing to link."""
        if not self.config_path.exists():
            return False
        try:
            raw = load_raw(self.config_path)
            return role_of(raw) is None or is_whole(raw)  # no role: check_config explains
        except Exception:
            return True  # there, but broken: check_config says what's wrong

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

    def keep_screen_on(self) -> bool:
        """"Keep my screen on" ([host] / [agent] keep_screen_on, see curslink.keepawake)."""
        try:
            cfg = self.config()
            return bool(cfg.get("agent" if cfg.role == "agent" else "host", "keep_screen_on", False))
        except Exception:
            return False

    def set_keep_screen_on(self, on: bool) -> None:
        """Save it and tell the running service, which takes it without a restart."""
        cfg = self.config()
        table = "agent" if cfg.role == "agent" else "host"
        if bool(cfg.get(table, "keep_screen_on", False)) == on:
            return
        cfg.set(table, "keep_screen_on", on)
        cfg.save()
        self.service.send(f"keep_screen_on {'on' if on else 'off'}")
        self.config_saved(restart=False)

    def ask_switch_role(self) -> None:
        """Confirm, then switch host <-> agent."""
        role = self.role
        if role is None:
            return
        target = "agent" if role == "host" else "host"
        what = ("another computer's keyboard and mouse will control this one" if target == "agent"
                else "this computer's keyboard and mouse will control your other devices, and phones can pair here")
        parent = self.window if self.window is not None and self.window.isVisible() else None
        answer = QMessageBox.question(
            parent, "Curslink",
            f"Use this computer as the {target}?\n\nAs the {target}, {what}. Your {role} settings and "
            f"pairings are kept, so you can switch back at any time.")
        if answer == QMessageBox.StandardButton.Yes:
            self.switch_role()

    def switch_role(self) -> str | None:
        """Host <-> agent, keeping each role's settings for switching back."""
        from curslink import macperms
        from curslink.configedit import switch_role
        from curslink.gui.pages.layout import detect_screens

        if self.role is None:
            return None
        was_linking = self.linking
        if was_linking:
            self.stop_linking()
        try:
            role = switch_role(self.config_path, detect_screens())
        except Exception as exc:  # noqa: BLE001 - shown, not fatal
            log.error("couldn't switch roles: %s", exc)
            self.status.add_log("error", f"Couldn't switch roles: {exc}")
            if was_linking:
                self.start_linking()
            return None
        log.info("this computer is now the %s", role)
        self.status.add_log("info", f"This computer is now the {role}.")
        self.config_saved(restart=False)
        if self.window is not None:
            if macperms.missing(role):
                self.window.show_page("status")
            elif not self.paired_peers("agent" if role == "host" else "host"):
                self.window.show_page("devices")  # nothing to link with in this role yet
            else:
                self.window.show_page("status")
        if was_linking or self.prefs.start_linking:
            self.start_linking()
        return role

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

    def _on_paired(self, event: dict) -> None:
        """The service paired with a device that asked (it started pairing).
        A host puts a new agent in its layout and restarts, so the host lets
        it connect (it turns away devices not in [[devices]])."""
        if event.get("event") != "paired":
            return
        device_id, name = str(event.get("device_id") or ""), str(event.get("name") or "")
        if not device_id:
            return
        log.info("paired with %s (%s, %s), which asked", name, device_id, event.get("role"))
        added = False
        if self.role == "host" and event.get("role") == "agent":
            from curslink.gui.pages.devices import add_to_layout

            try:
                added = add_to_layout(self, device_id, name or device_id, event.get("screens") or None)
            except Exception:
                log.exception("couldn't add %s to the layout", device_id)
        if added:
            log.info("added %s to the layout; restarting so it can connect", device_id)
            if self.tray is not None:
                self.tray.say(f"Paired with {name or device_id}",
                              "It's been added to the right of your screens. Arrange it on the Layout page.")
            self.config_saved()
        else:
            self.config_changed.emit()  # the Devices page lists it

    def _on_look(self, event: dict) -> None:
        """Agent: the host's calibration wants "Look here" on one of our screens."""
        if event.get("event") != "look":
            return
        if self._look is None:
            if event.get("hide"):
                return
            from curslink.gui.overlay import RemoteLookHere

            self._look = RemoteLookHere()
        self._look.apply(event)

    def _on_hint(self, event: dict) -> None:
        """Agent: the host's Layout page marks where its screens overlap ours."""
        if event.get("event") != "hint":
            return
        if self._hint is None:
            if event.get("hide"):
                return
            from curslink.gui.overlaphint import OverlapHint

            self._hint = OverlapHint()
        self._hint.show(event)

    def _on_calibrating(self, event: dict) -> None:
        if event.get("event") == "calibrating":
            self.calibrating = bool(event.get("on"))

    def _on_service_state(self, state: str) -> None:
        if state != RUNNING:
            self.calibrating = False
        if state in (STOPPED, FAILED):
            self.status.stopped()
            if self._look is not None:
                self._look.hide()
            if self._hint is not None:
                self._hint.hide()
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
        # Only the Windows setup restarts Curslink itself (from a script that
        # waits for this copy to exit). Everywhere else this copy must not
        # exec the new version in place: that keeps the same process id, so
        # the new copy finds the single-instance lock "held by a running
        # copy" (itself) and quits at once, which left nothing running after
        # every macOS / Linux update. Install, then restart() instead.
        relaunch = None
        if kind == "windows-setup":
            relaunch = ["gui"] if Path(sys.executable).stem.lower() != "curslinkw" else []

        def run():
            self.service.stop()
            return update.update(release, kind, relaunch=relaunch)

        def done(message):
            log.info("%s", message)
            if sys.platform == "win32":
                # A Windows portable copy unpacks beside this one; nothing to restart into.
                self.quit()
            else:
                self.restart(just_updated=True)

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

    def paired_peers(self, role: str | None = None) -> list:
        try:
            from curslink.config import pairing_config
            from curslink.pairing.store import PairingStore

            store = pairing_config(self.config().raw(), self.config_path.parent).store
            return PairingStore(store).peers(role)
        except Exception:
            return []

    def has_pairings(self) -> bool:
        """Paired with at least one other device (until then, setup isn't done)."""
        return self.has_config() and bool(self.paired_peers())

    def agent_paired(self) -> bool:
        """An agent with a host to link to (so it can run without a window)."""
        return self.role == "agent" and bool(self.paired_peers("host"))

    def tray_placed(self) -> bool:
        """The tray / menu bar icon is really on screen (not just requested)."""
        tray = self.tray
        if tray is None or not tray.available() or not tray.isVisible():
            return False
        # macOS reports where the menu bar item is; some Linux trays report
        # nothing even though the icon is there, so only trust it on macOS.
        # macOS 26 reports a placed item with no height (e.g. 38 x 0), which
        # isEmpty() counts as empty: that kept the Dock icon after closing the
        # window and reopened the window when starting hidden. An item that
        # was never placed has no size at all.
        if sys.platform != "darwin":
            return True
        g = tray.geometry()
        return g.width() > 0 or g.height() > 0

    def check_tray_placed(self) -> None:
        if self.window is not None and not self.window.isVisible() and not self.tray_placed():
            log.warning("the %s icon isn't showing (geometry %s); opening the window instead",
                        self.tray.place() if self.tray else "tray", self.tray.geometry() if self.tray else None)
            self.show_window()

    def hide_to_tray(self, force_note: bool = False) -> None:
        """Close the window and keep running in the tray, saying so once."""
        from curslink import macperms

        if self.window is None or not self.tray_placed() or macperms.missing(self.role):
            return
        self.window.hide()
        from curslink.gui import native

        native.set_dock_icon(False)
        if force_note or not self.prefs.told_about_tray:
            self.prefs.told_about_tray = True
            self.prefs.save()
            self.tray.say("Curslink is running in the background",
                          f"It links by itself. Open it from the {self.tray.place()} icon.")

    def restart(self, just_updated: bool = False) -> None:
        """Quit and open Curslink again (macOS applies some permissions then).

        The new copy starts once this one has exited: started while this
        copy still held the single-instance lock, it would just show this
        (closing) window and quit, leaving nothing running.
        """
        import subprocess

        from curslink.update import SKIP_ENV, app_bundle

        bundle = app_bundle()
        if bundle is not None:
            # Opened as the app, so macOS checks the app's permissions again.
            argv = ["/usr/bin/open", "-n", str(bundle)]
        elif getattr(sys, "frozen", False):
            argv = [sys.executable, "gui"]
        else:
            argv = [sys.executable, "-m", "curslink", "gui"]
        env = dict(os.environ)
        if just_updated:
            env[SKIP_ENV] = "just-updated"
        log.info("restarting Curslink")
        subprocess.Popen(restart_command(argv, os.getpid()), env=env, stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **_detached())
        self.quit()

    def quit(self) -> None:
        if self.quitting:
            # Quitting closes the window, whose closeEvent comes back here
            # (on macOS from inside NSApplication terminate:): don't start
            # a second quit inside the first.
            return
        self.quitting = True
        self.service.stop()
        if self.tray is not None:
            self.tray.hide()
        self.remove_event_filters()
        QApplication.quit()

    def remove_event_filters(self) -> None:
        """Take out every Python event filter before Qt tears down.

        On macOS QApplication.quit() ends in NSApplication terminate:, which
        calls exit() straight away: C++ destructors then delete the widgets
        while Python is still loaded, and each deletion sends events through
        the filters. A Python filter called then reads a half-destroyed
        event and the app crashes on the way out."""
        qt = QApplication.instance()
        for f in self.app_filters:
            if qt is not None:
                qt.removeEventFilter(f)
        self.app_filters.clear()
        if self.window is not None and hasattr(self.window, "remove_event_filters"):
            self.window.remove_event_filters()


class WindowLog(QObject):
    """Logs every window the app shows, so a window that flashes up can be
    traced in the diagnostics."""

    def eventFilter(self, obj, event) -> bool:  # noqa: N802
        from PySide6.QtCore import QEvent
        from PySide6.QtWidgets import QWidget

        if event.type() == QEvent.Type.Show and isinstance(obj, QWidget) and obj.isWindow():
            log.info("window shown: %s %r at %s", type(obj).__name__, obj.windowTitle(), obj.geometry())
        return False


def restart_command(argv: list[str], pid: int, wait_s: int = 60) -> list[str]:
    """A command that waits (up to ``wait_s``) for process ``pid`` to exit, then runs ``argv``."""
    if sys.platform == "win32":
        import subprocess

        # A batch file, as the Windows update uses: cmd's quoting rules make
        # a one-line wait loop fragile.
        script = Path(tempfile.mkdtemp(prefix="curslink-restart-")) / "restart.cmd"
        script.write_text("\r\n".join([
            "@echo off",
            "set /a n=0",
            ":wait",
            f"if %n% geq {wait_s} goto start",
            f'tasklist /FI "PID eq {pid}" 2>nul | find "{pid}" >nul || goto start',
            "set /a n+=1",
            "ping -n 2 127.0.0.1 >nul",
            "goto wait",
            ":start",
            'start "" ' + subprocess.list2cmdline(argv),
            '(goto) 2>nul & del "%~f0"',
        ]) + "\r\n")
        return ["cmd", "/c", str(script)]
    script = (f'i=0; while kill -0 {pid} 2>/dev/null && [ $i -lt {wait_s * 5} ]; '
              'do sleep 0.2; i=$((i+1)); done; exec "$@"')
    return ["/bin/sh", "-c", script, "curslink", *argv]


def _detached() -> dict:
    import subprocess

    if sys.platform == "win32":
        return {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
                | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x200)}
    return {"start_new_session": True}


def _take_instance_lock(name: str) -> QLockFile | None:
    """The lock only one copy of the app holds, or None if another has it.

    Checking for a running copy's socket alone let two copies start: one
    launched while the other was still starting (before it listened) found
    nobody, and on Windows a second server can listen on the same pipe name.
    A lock file left by a copy that crashed is noticed as stale and taken.
    """
    lock = QLockFile(str(Path(tempfile.gettempdir()) / f"{name}.lock"))
    lock.setStaleLockTime(0)  # stale only if its process has gone, however old
    return lock if lock.tryLock(0) else None


def _show_running_copy(name: str, wait_s: float = 10.0) -> bool:
    """Ask the copy holding the lock to show its window, waiting while it
    starts up and begins listening. True if it answered."""
    if sys.platform == "win32":
        try:
            import ctypes

            ctypes.windll.user32.AllowSetForegroundWindow(-1)  # ASFW_ANY: let it come to the front
        except Exception:
            pass
    deadline = time.monotonic() + wait_s
    while True:
        if _already_running(name):
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.25)


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
        # Being connected to is the request: on Windows the other copy can
        # write and hang up before readyRead fires, and then it never did.
        conn = server.nextPendingConnection()
        while conn is not None:
            conn.disconnected.connect(conn.deleteLater)
            conn.readyRead.connect(conn.readAll)
            app.show_window()
            conn = server.nextPendingConnection()

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
    qt.setOrganizationDomain("orimovo.github.io")
    qt.setDesktopFileName("curslink")
    if not (sys.platform == "darwin" and getattr(sys, "frozen", False)):
        # The packaged macOS app has its bundle's icon, drawn to Apple's
        # icon shape; Qt's would replace it in the Dock once a window opens.
        qt.setWindowIcon(app_icon())
    qt.setStyle("Fusion")
    qt.setQuitOnLastWindowClosed(False)

    name = instance_name()
    instance_lock = _take_instance_lock(name)
    if instance_lock is None:
        # Another copy is running (or starting): bring its window forward instead.
        _show_running_copy(name)
        return 0

    # Only the copy that stays running owns the log files.
    diagnostics.setup("gui", verbose=args.verbose)
    qInstallMessageHandler(diagnostics.qt_message_handler)
    main_thread()  # created on the Qt thread before any worker needs it
    window_log = WindowLog(qt)
    qt.installEventFilter(window_log)
    log.info("Qt %s, platform plugin %s", qVersion(), qt.platformName())

    from curslink.autostart import Autostart

    try:
        Autostart().refresh()  # an update can move the executable (macOS: into Curslink.app)
    except Exception:
        log.warning("couldn't update start-at-sign-in", exc_info=True)

    prefs = Prefs.load()
    if args.config:
        prefs.config = str(Path(args.config).expanduser().resolve())
        prefs.save()
    from curslink.gui import native, theme

    theme.manager(prefs.theme).apply()
    app = Curslink(prefs)
    app.app_filters.append(window_log)
    server = _listen(name, app)  # noqa: F841 - kept alive by its parent
    app.instance_lock = instance_lock  # held until this copy exits

    from curslink.gui.tray import Tray
    from curslink.gui.window import MainWindow

    log.info("building the window")
    app.window = MainWindow(app)
    app.tray = Tray(app)
    tray_ok = app.tray.available()
    log.info("system tray %s", "available" if tray_ok else "not available")
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
        log.info("tray icon shown: visible %s, at %s", app.tray.isVisible(), app.tray.geometry())

    if not app.has_config():
        app.show_window("setup")
    else:
        if prefs.start_linking:
            app.start_linking()
        # Agents have nothing to look at once paired, so they start hidden.
        # Nothing paired yet means setup isn't finished: always show the window.
        hidden = app.has_pairings() and (args.minimized or prefs.start_minimized or app.agent_paired())
        log.info("starting %s (minimized flag %s, start minimised %s, %s, paired %s, tray %s)",
                 "hidden in the tray" if hidden and tray_ok else "with the window open", args.minimized,
                 prefs.start_minimized, app.role, app.has_pairings(), "available" if tray_ok else "unavailable")
        if not hidden or not tray_ok:
            app.show_window()
        else:
            native.set_dock_icon(False)
            # If the tray / menu bar icon never actually appeared, a hidden app
            # can't be reached at all: open the window after all.
            QTimer.singleShot(2500, app.check_tray_placed)
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
    from curslink import macperms

    lacking = macperms.missing(app.role) if app.has_config() else []
    if lacking:
        # Even when it would start hidden: nothing works until they're granted.
        log.warning("macOS permissions missing: %s", ", ".join(lacking))
        app.show_window("status")
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
