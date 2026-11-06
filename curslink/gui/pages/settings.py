"""Settings: the background behaviour, switching, and updates."""

from __future__ import annotations

import sys
import time
from pathlib import Path

from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import QButtonGroup, QFileDialog, QGridLayout, QHBoxLayout, QLineEdit, QSlider, QVBoxLayout

from curslink import __version__, diagnostics
from curslink.autostart import Autostart
from curslink.gui import native, theme
from curslink.gui.camerapicker import CameraPicker
from curslink.gui.widgets import Card, Page, Segmented, Toggle, button, label, spot, text_block


class SettingsPage(Page):
    def __init__(self, app):
        super().__init__("Settings")
        self.app = app
        self.autostart = Autostart()
        self._loading = False

        # -- this computer -------------------------------------------------------
        me = Card("This computer")
        self.role_text = label("", "muted", wrap=True)
        me.body.addWidget(self.role_text)
        name_row = QHBoxLayout()
        self.name = QLineEdit()
        self.name.setAccessibleName("Name other devices see")
        self.name.setMaximumWidth(360)
        self.name.editingFinished.connect(self.save_name)
        name_row.addWidget(label("Name other devices see", "small"))
        name_row.addWidget(self.name)
        name_row.addStretch(1)
        me.body.addLayout(name_row)
        again = QHBoxLayout()
        again.addWidget(button("Set this computer up again…", None, lambda: app.show_window("setup")))
        again.addStretch(1)
        me.body.addLayout(again)
        self.add(me)

        # -- appearance -----------------------------------------------------------
        look = Card("Appearance", rows=True)
        self.theme_choice = Segmented([("system", "System"), ("light", "Light"), ("dark", "Dark")])
        self.theme_choice.picked.connect(self.set_theme)
        look.add_row(text_block("Theme", "System follows your computer's light or dark setting."), self.theme_choice)
        self.glass = Toggle("Transparency effects")
        self.glass.clicked.connect(lambda on: self.set_pref("glass", on))
        effect = {"win": "Mica", "mac": "the glass sidebar"}.get(theme.current().platform, "")
        if effect and native.can_glass():
            look.add_row(text_block("Transparency effects", f"Use {effect} behind the window. Applies next time "
                                    "Curslink opens."), self.glass)
        self.add(look)

        # -- background ---------------------------------------------------------
        bg = Card("Running in the background", rows=True)
        self.login = Toggle("Start Curslink when I sign in")
        self.login.clicked.connect(self.set_login)
        self.minimized = Toggle("Start minimised to the tray")
        self.minimized.clicked.connect(lambda on: self.set_pref("start_minimized", on))
        self.start_linking = Toggle("Start linking when Curslink opens")
        self.start_linking.clicked.connect(lambda on: self.set_pref("start_linking", on))
        tray_word = "menu bar" if sys.platform == "darwin" else "tray"
        for toggle, title, detail in (
            (self.login, "Start Curslink when I sign in", f"Opens in the {tray_word}, ready to link, each time you sign in."),
            (self.minimized, f"Start minimised to the {tray_word}", f"Skip the window on launch; open it from the {tray_word} icon."),
            (self.start_linking, "Start linking when Curslink opens", "Run straight away instead of waiting for Start."),
        ):
            bg.add_row(text_block(title, detail), toggle)
        self.add(bg)

        # -- switching (host) ---------------------------------------------------
        self.switching = Card("Switching")
        grid = QGridLayout()
        grid.setHorizontalSpacing(24)
        grid.setVerticalSpacing(10)
        grid.addWidget(label("Animation where the pointer lands", "small"), 0, 0)
        effects = QHBoxLayout()
        self.effect_group = QButtonGroup(self)
        for key, text in (("pop", "Pop"), ("blink", "Blink"), ("none", "None")):
            b = button(text, "seg")
            b.setCheckable(True)
            b.setProperty("effect", key)
            self.effect_group.addButton(b)
            effects.addWidget(b)
        effects.addStretch(1)
        self.effect_group.buttonClicked.connect(lambda b: self.set_config("host", "effect", b.property("effect")))
        grid.addLayout(effects, 1, 0)

        grid.addWidget(label("Camera", "small"), 0, 1)
        self.camera = CameraPicker()
        self.camera.picked.connect(self.set_camera)
        grid.addWidget(self.camera, 1, 1)

        self.dwell_label = label("", "small")
        grid.addWidget(self.dwell_label, 2, 0)
        self.dwell = QSlider(Qt.Orientation.Horizontal)
        self.dwell.setRange(150, 1000)
        self.dwell.setSingleStep(50)
        self.dwell.setPageStep(50)
        self.dwell.setAccessibleName("How long to look before switching")
        self.dwell.valueChanged.connect(lambda v: self.dwell_label.setText(f"Look for {v} ms before switching"))
        self.dwell.sliderReleased.connect(lambda: self.set_config("gaze", "dwell_ms", self.dwell.value()))
        grid.addWidget(self.dwell, 3, 0)

        hot = QVBoxLayout()
        hot.addWidget(label("Hotkeys", "small"))
        self.hotkeys = label("", wrap=True)
        hot.addWidget(self.hotkeys)
        grid.addLayout(hot, 2, 1, 2, 1)
        self.switching.body.addLayout(grid)
        self.add(self.switching)

        # -- agent ----------------------------------------------------------------
        self.agent_card = Card("Switching", rows=True)
        self.animations = Toggle("Play the switch animation here")
        self.animations.clicked.connect(lambda on: self.set_config("agent", "animations", on))
        self.agent_card.add_row(text_block("Play the switch animation here",
                                           "Show the pop where the pointer lands on this screen."), self.animations)
        self.add(self.agent_card)

        # -- updates --------------------------------------------------------------
        upd = Card("Updates", rows=True)
        self.update_art = spot("update", 96, 72)
        self.update_art.hide()
        self.upd_check = Toggle("Check for updates")
        self.upd_check.clicked.connect(lambda on: self.set_config("update", "check", on, restart=False))
        self.upd_auto = Toggle("Install updates automatically")
        self.upd_auto.clicked.connect(lambda on: self.set_config("update", "auto", on, restart=False))
        self.upd_pre = Toggle("Include pre-releases")
        self.upd_pre.clicked.connect(lambda on: self.set_config("update", "prerelease", on, restart=False))
        upd.add_row(text_block("Check for updates", "Look for a newer release when Curslink opens, and daily."),
                    self.upd_check)
        upd.add_row(text_block("Install updates automatically", "Download, verify and restart into the new version."),
                    self.upd_auto)
        upd.add_row(text_block("Include pre-releases", "Try release candidates before everyone else."),
                    self.upd_pre)
        self.update_text = label(f"You have Curslink {__version__}.", "small")
        self.check_button = button("Check now", None, self.check_now)
        self.install_button = button("Install update", "primary", self.install)
        self.install_button.hide()
        row = upd.add_row(self.update_art, self.update_text, self.install_button, self.check_button,
                          stretch_first=False)
        row.layout().setStretch(1, 1)
        self.add(upd)

        # -- files ------------------------------------------------------------------
        files = Card("Files")
        self.config_text = label("", "mono", wrap=True)
        files.body.addWidget(self.config_text)
        row = QHBoxLayout()
        row.addWidget(button("Open the config folder", None, self.open_folder))
        row.addStretch(1)
        files.body.addLayout(row)
        files.body.addWidget(label("The command line reads the same file: curslink host -c <that file>.", "small"))
        self.add(files)

        # -- diagnostics ------------------------------------------------------------
        diag = Card("Diagnostics")
        self.log_text = label("", "mono", wrap=True)
        diag.body.addWidget(self.log_text)
        self.crash_text = label("", "small", wrap=True)
        diag.body.addWidget(self.crash_text)
        row = QHBoxLayout()
        row.addWidget(button("Open the logs folder", None, self.open_logs))
        row.addWidget(button("Save a diagnostics file…", None, self.save_bundle))
        row.addStretch(1)
        diag.body.addLayout(row)
        diag.body.addWidget(label("Send the diagnostics file with a bug report. It has the logs and crash reports, "
                                  "not your pairing keys.", "small", wrap=True))
        self.add(diag)
        self.finish()

        app.config_changed.connect(self.load)
        app.update_found.connect(self.show_release)
        self.load()

    def shown(self) -> None:
        if not self.camera.loaded:
            self.camera.refresh()
        self.load()
        self.load_diagnostics()

    def load_diagnostics(self) -> None:
        self.log_text.setText(str(diagnostics.current_log_file() or diagnostics.log_dir()))
        crashes = diagnostics.crash_reports()
        if crashes:
            when = time.strftime("%d %b %Y %H:%M", time.localtime(crashes[0].stat().st_mtime))
            self.crash_text.setText(f"{len(crashes)} crash report{'s' * (len(crashes) != 1)}, the latest from {when}.")
        else:
            self.crash_text.setText("No crash reports.")

    def load(self) -> None:
        if not self.app.has_config():
            return
        self._loading = True
        try:
            cfg = self.app.config()
            raw = cfg.raw()
            role = cfg.role
            self.role_text.setText(
                "Host: the keyboard and mouse are plugged in here." if role == "host"
                else "Agent: another computer's keyboard and mouse control this one.")
            if role == "host":
                me = raw.get("host", {}).get("device_id")
                name = next((d.get("name", me) for d in raw.get("devices", []) if d.get("id") == me), me)
            else:
                name = raw.get("agent", {}).get("name", raw.get("agent", {}).get("device_id", ""))
            if not self.name.hasFocus():
                self.name.setText(name or "")
            self.login.setChecked(self.autostart.is_enabled())
            self.minimized.setChecked(self.app.prefs.start_minimized)
            self.start_linking.setChecked(self.app.prefs.start_linking)
            self.switching.setVisible(role == "host")
            self.agent_card.setVisible(role == "agent")
            host = raw.get("host", {})
            gaze = raw.get("gaze", {})
            effect = host.get("effect", "pop")
            for b in self.effect_group.buttons():
                b.setChecked(b.property("effect") == effect)
            self.camera.set_current(int(gaze.get("camera", 0) or 0), str(gaze.get("camera_name", "") or ""))
            self.theme_choice.set_value(self.app.prefs.theme)
            self.glass.setChecked(self.app.prefs.glass)
            self.dwell.setValue(int(gaze.get("dwell_ms", 350)))
            self.dwell_label.setText(f"Look for {self.dwell.value()} ms before switching")
            self.hotkeys.setText(f"Bring input home: {host.get('return_hotkey', 'ctrl+alt+home')}\n"
                                 f"Pause gaze: {host.get('gaze_toggle_hotkey', 'ctrl+alt+g')}")
            self.animations.setChecked(bool(raw.get("agent", {}).get("animations", True)))
            u = raw.get("update", {})
            self.upd_check.setChecked(bool(u.get("check", True)))
            self.upd_auto.setChecked(bool(u.get("auto", True)))
            self.upd_pre.setChecked(bool(u.get("prerelease", False)))
            self.config_text.setText(str(self.app.config_path))
        finally:
            self._loading = False

    # -- saving -----------------------------------------------------------------
    def set_pref(self, key: str, value) -> None:
        setattr(self.app.prefs, key, value)
        self.app.prefs.save()

    def set_theme(self, mode: str) -> None:
        self.set_pref("theme", mode)
        theme.manager().set_mode(mode)

    def set_camera(self, index: int, name: str) -> None:
        if self._loading:
            return
        cfg = self.app.config()
        cfg.set("gaze", "camera", index)
        cfg.set("gaze", "camera_name", name)
        cfg.save()
        self.app.config_saved()

    def set_login(self, on: bool) -> None:
        try:
            self.autostart.set_enabled(on)
        except OSError as exc:
            self.login.setChecked(not on)
            self.update_text.setText(f"Couldn't change start at sign-in: {exc}")

    def set_config(self, table: str, key: str, value, restart: bool = True) -> None:
        if self._loading:
            return
        cfg = self.app.config()
        if cfg.get(table, key) == value:
            return
        cfg.set(table, key, value)
        cfg.save()
        self.app.config_saved(restart=restart)

    def save_name(self) -> None:
        name = self.name.text().strip()
        if not name or self._loading:
            return
        cfg = self.app.config()
        if cfg.role == "host":
            me = cfg.get("host", "device_id")
            for d in cfg.doc.get("devices", []):
                if d.get("id") == me and d.get("name") != name:
                    d["name"] = name
                    cfg.save()
                    self.app.config_saved()
        elif cfg.get("agent", "name") != name:
            cfg.set("agent", "name", name)
            cfg.save()
            self.app.config_saved()

    # -- updates ----------------------------------------------------------------
    def check_now(self) -> None:
        self.update_text.setText("Checking…")

        def result(release, error):
            if error is not None:
                self.update_text.setText(f"Couldn't check: {error}")
            elif release is None:
                self.update_text.setText(f"Curslink {__version__} is the newest release.")

        self.app.check_for_update(quiet=False, on_result=result)

    def show_release(self, release) -> None:
        self.update_text.setText(f"Curslink {release.version} is out (you have {__version__}).")
        self.update_art.show()
        self.install_button.show()

    def install(self) -> None:
        self.update_text.setText("Downloading the update…")
        self.install_button.setEnabled(False)

        def failed(exc):
            self.update_text.setText(f"Update failed: {exc}")
            self.install_button.setEnabled(True)

        self.app.install_update(on_error=failed)

    def open_logs(self) -> None:
        folder = diagnostics.log_dir()
        folder.mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(folder)))

    def save_bundle(self) -> None:
        start = str(Path.home() / "Desktop" / diagnostics.bundle_name())
        path, _ = QFileDialog.getSaveFileName(self, "Save diagnostics", start, "Zip files (*.zip)")
        if not path:
            return
        try:
            diagnostics.write_bundle(Path(path))
        except OSError as exc:
            self.crash_text.setText(f"Couldn't save it: {exc}")
            return
        self.crash_text.setText(f"Saved {path}")

    def open_folder(self) -> None:
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.app.config_path.parent)))
