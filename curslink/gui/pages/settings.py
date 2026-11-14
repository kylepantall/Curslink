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
from curslink.gui.netpanel import NetworkCard
from curslink.gui.widgets import Card, Page, Segmented, Toggle, seg_track, button, label, spot, text_block


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
        self.switch_button = button("Switch to agent…", None, app.ask_switch_role)
        self.switch_button.setAccessibleName("Switch this computer between host and agent")
        again.addWidget(self.switch_button)
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
        track, seg = seg_track()
        for key, text in (("pop", "Halo"), ("blink", "Blink"), ("none", "None")):
            b = button(text, "seg")
            b.setCheckable(True)
            b.setProperty("effect", key)
            self.effect_group.addButton(b)
            seg.addWidget(b)
        effects.addWidget(track)
        self.preview_button = button("Preview", None, self.preview_effect)
        self.preview_button.setToolTip("Play the animation here, the way it looks when the pointer switches")
        effects.addSpacing(8)
        effects.addWidget(self.preview_button)
        effects.addStretch(1)
        self.effect_group.buttonClicked.connect(self.pick_effect)
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
        self.edge_hint = Toggle("Show where an edge leads")
        self.edge_hint.clicked.connect(lambda on: self.set_config("host", "edge_indicator", on))
        hint_row = QHBoxLayout()
        hint_row.addWidget(text_block(
            "Show where an edge leads",
            "Near an edge that leads to another device, the edge glows and a label shows its name. The edge "
            "it's heading for glows on that device too."), 1)
        hint_row.addWidget(self.edge_hint, 0, Qt.AlignmentFlag.AlignVCenter)
        self.switching.body.addSpacing(6)
        self.switching.body.addLayout(hint_row)
        self.add(self.switching)

        # -- keyboard (host) ------------------------------------------------------
        self.keyboard_card = Card("Keyboard", rows=True)
        title = "Use each computer's shortcuts"
        self.swap_keys = Toggle(title)
        self.swap_keys.clicked.connect(lambda on: self.set_config("host", "swap_cmd_ctrl", on))
        if sys.platform == "darwin":
            detail = ("Automatic. On Windows and Linux, Cmd works as Ctrl, so Cmd+C copies and Cmd+V pastes the way "
                      "they do here. On a Mac every key works as usual.")
        else:
            detail = ("Automatic. On a Mac, the keys work where a Mac keyboard has them: Alt beside the space bar "
                      "is Cmd, so Alt+C copies, Alt+V pastes and Alt+Tab switches apps, and the Windows key is "
                      "Option. On Windows and Linux every key works as usual.")
        self.keyboard_card.add_row(text_block(title, detail), self.swap_keys)
        self.pair_maps_button = button("Customise…", None, self.edit_pair_maps)
        self.pair_maps_text = text_block("Change a single key", "Only needed if a key does the wrong thing.")
        self.keyboard_card.add_row(self.pair_maps_text, self.pair_maps_button)
        self.add(self.keyboard_card)

        # -- overlapping screens (host) -------------------------------------------
        self.mirror_card = Card("Overlapping screens", rows=True)
        self.mirror = Toggle("Mirror the covered area")
        self.mirror.clicked.connect(lambda on: self.set_config("layout", "mirror", on))
        self.mirror_card.add_row(text_block(
            "Mirror the covered area",
            "Where a screen stands in front of another, like a laptop in front of a monitor's corner, it "
            "shows the part it covers while you use the screen behind, so you can see the clock, "
            "notifications and windows there. Works with any overlap, phones included."), self.mirror)
        self.mirror_pointer = Toggle("Keep the pointer on the screen in use")
        self.mirror_pointer.clicked.connect(lambda on: self.set_config("layout", "mirror_pointer", on))
        self.mirror_pointer_row = self.mirror_card.add_row(text_block(
            "Keep the pointer on the screen in use",
            "The pointer can go into the covered part of the screen behind and is drawn in the mirror, so "
            "you can click there. It moves to the screen in front once it leaves the covered part."),
            self.mirror_pointer)
        self.mirror_quality = Segmented([("auto", "Auto"), ("standard", "Standard"), ("high", "High"),
                                         ("lossless", "Lossless")])
        self.mirror_quality.picked.connect(lambda value: self.set_config("layout", "mirror_quality", value))
        self.mirror_quality_row = self.mirror_card.add_row(text_block(
            "Mirror quality",
            "Lossless sends every pixel exactly as the screen behind shows it; High is very close and lighter "
            "on Wi-Fi; Standard is lightest. Auto is lossless while the network is quick, High when it's busy "
            "and Standard when it's slow. Only what changed on the screen behind is sent. The mirror can't be sharper than the screen behind: where that "
            "screen has fewer pixels per inch than the one in front, it's scaled up once to fit."),
            self.mirror_quality)
        self.mirror_fps = Segmented([("auto", "Auto"), ("30", "30"), ("60", "60"), ("120", "120")])
        self.mirror_fps.picked.connect(
            lambda value: self.set_config("layout", "mirror_fps", value if value == "auto" else int(value)))
        self.mirror_fps_row = self.mirror_card.add_row(text_block(
            "Mirror frame rate",
            "Frames a second the mirror updates at, never more than the screen behind refreshes, so 120 "
            "needs a 120 Hz screen. Auto goes up to 120 while the network is quick and drops to 30 or 15 "
            "when it's busy or slow."),
            self.mirror_fps)
        # Rows for screens behind others: keep mirroring while on a screen joined to it.
        self.joined_rows: list = []
        self.joined_toggles: dict[tuple[str, str | None], Toggle] = {}
        self.mirror_note = label("Turn on Allow overlap on the Layout page and put one screen in front of "
                                 "another to use this.", "small", wrap=True)
        self.mirror_card.body.addWidget(self.mirror_note)
        self.add(self.mirror_card)
        # -- keyboard (agent) -----------------------------------------------------
        self.agent_keys_card = Card("Keyboard", rows=True)
        self.fix_keys_button = button("Fix a key…", None, self.check_keyboard)
        self.fix_keys_text = text_block(
            "Keys from the shared keyboard",
            "They follow this computer's shortcuts automatically. If one does the wrong thing, fix it here.")
        self.agent_keys_card.add_row(self.fix_keys_text, self.fix_keys_button)
        self.add(self.agent_keys_card)

        # -- per-monitor jumps (host) ---------------------------------------------
        self.jump_card = Card("Jump to the monitor you look at", rows=True)
        self.jump_all_on = button("Turn all on", None, lambda: self.set_all_jumps(True))
        self.jump_all_off = button("Turn all off", None, lambda: self.set_all_jumps(False))
        self.jump_card.add_row(
            text_block("Monitors", "On: looking at that monitor puts the pointer on it, even one of the "
                       "computer you're already using. Off: looking at it only picks its computer, and the "
                       "pointer comes back where you left it there."),
            self.jump_all_on, self.jump_all_off)
        self.jump_toggles: dict[str, Toggle] = {}
        self.jump_rows = []
        self.add(self.jump_card)

        # -- agent ----------------------------------------------------------------
        self.agent_card = Card("Switching", rows=True)
        self.animations = Toggle("Play the switch animation here")
        self.animations.clicked.connect(lambda on: self.set_config("agent", "animations", on))
        self.agent_card.add_row(text_block("Play the switch animation here",
                                           "Show the pop where the pointer lands on this screen."), self.animations)
        self.agent_camera = Toggle("Use this computer's camera")
        self.agent_camera.clicked.connect(self.set_agent_camera)
        self.agent_card.add_row(text_block(
            "Use this computer's camera",
            "Switch here when this camera sees you facing the screen, on its own or together "
            "with the host's camera. No calibration needed."), self.agent_camera)
        self.agent_camera_picker = CameraPicker()
        self.agent_camera_picker.picked.connect(self.set_agent_camera_device)
        self.agent_camera_row = self.agent_card.add_row(
            text_block("Camera", "The one that faces you when you use this screen."), self.agent_camera_picker)
        # Windows: the input helper service (curslink.winhelper).
        self.helper = None
        if sys.platform == "win32":
            self.helper = Toggle("Control admin apps and UAC prompts")
            self.helper.clicked.connect(self.set_helper)
            self.helper_block = text_block(
                "Control admin apps and UAC prompts",
                "Windows keeps other programs out of apps running as administrator, UAC prompts and the "
                "lock screen. This installs a small Windows service that can reach them (it asks for admin "
                "once); turning it off removes it. Anything running as you could use it to answer a UAC "
                "prompt too, so leave it off if you don't need it.")
            self.agent_card.add_row(self.helper_block, self.helper)
        self.add(self.agent_card)

        # -- clipboard (both) -------------------------------------------------------
        self.clipboard_card = Card("Clipboard", rows=True)
        self.clipboard = Toggle("Share the clipboard")
        self.clipboard.clicked.connect(lambda on: self.set_config(self._role_table(), "clipboard", on))
        self.clipboard_card.add_row(text_block(
            "Share the clipboard",
            "Copy text, a picture or files on one computer and paste them on another. Passwords copied from "
            "a password manager stay where they are. Copies bigger than 25 MB aren't shared."), self.clipboard)
        self.add(self.clipboard_card)

        # -- screen (both) ----------------------------------------------------------
        self.screen_card = Card("Screen", rows=True)
        self.keep_screen = Toggle("Keep my screen on")
        self.keep_screen.clicked.connect(lambda on: None if self._loading else self.app.set_keep_screen_on(on))
        self.screen_card.add_row(text_block(
            "Keep my screen on",
            "While Curslink is linked to another device, this screen doesn't dim, lock or go to sleep, "
            "so it's ready when the pointer comes over. It sleeps as usual once the link drops."),
            self.keep_screen)
        self.add(self.screen_card)

        # -- network (both) ---------------------------------------------------------
        self.network = NetworkCard(lambda on: self.set_config("network", "optimise", on), self.check_network)
        self.add(self.network)

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
        app.service.event.connect(self._on_service_event)
        app.service.state_changed.connect(self._on_service_state)
        self.load()

    def shown(self) -> None:
        for picker in (self.camera, self.agent_camera_picker):
            if not picker.loaded:
                picker.refresh()
        self.load()
        self.refresh_helper()
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
            self.switch_button.setText("Switch to agent…" if role == "host" else "Switch to host…")
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
            self.jump_card.setVisible(role == "host")
            self.keyboard_card.setVisible(role == "host")
            self.mirror_card.setVisible(role == "host")
            layout_raw = raw.get("layout", {}) if isinstance(raw.get("layout", {}), dict) else {}
            overlap = bool(layout_raw.get("allow_overlap", False))
            self.mirror.setChecked(bool(layout_raw.get("mirror", False)))
            self.mirror.setEnabled(overlap)
            self.mirror_pointer.setChecked(bool(layout_raw.get("mirror_pointer", True)))
            self.mirror_pointer_row.setVisible(overlap and self.mirror.isChecked())
            self.mirror_quality.set_value(str(layout_raw.get("mirror_quality", "auto")))
            self.mirror_quality_row.setVisible(overlap and self.mirror.isChecked())
            fps = layout_raw.get("mirror_fps", "auto")
            self.mirror_fps.set_value(str(fps) if str(fps) in ("30", "60", "120") else "auto")
            self.mirror_fps_row.setVisible(overlap and self.mirror.isChecked())
            self.mirror_note.setVisible(not overlap)
            self.load_joined(raw, overlap and self.mirror.isChecked() and role == "host")
            self.agent_keys_card.setVisible(role == "agent")
            if role == "host":
                custom = len(raw.get("keymap", {}) or {})
                self.pair_maps_text.detail_label.setText(
                    "Only needed if a key does the wrong thing." + (" You have changed some keys." if custom else ""))
            else:
                fixes = len(raw.get("agent", {}).get("modifier_map", {}) or {})
                self.fix_keys_text.detail_label.setText(
                    "They follow this computer's shortcuts automatically. If one does the wrong thing, fix it here."
                    + (f" {fixes} key{'s' if fixes != 1 else ''} fixed on this computer." if fixes else ""))
            if role == "host":
                self.load_jumps(raw)
            self.agent_card.setVisible(role == "agent")
            host = raw.get("host", {})
            gaze = raw.get("gaze", {})
            effect = host.get("effect", "pop")
            self.swap_keys.setChecked(bool(host.get("swap_cmd_ctrl", True)))
            self.edge_hint.setChecked(bool(host.get("edge_indicator", True)))
            for b in self.effect_group.buttons():
                b.setChecked(b.property("effect") == effect)
            self.camera.set_current(int(gaze.get("camera", 0) or 0), str(gaze.get("camera_name", "") or ""))
            self.theme_choice.set_value(self.app.prefs.theme)
            self.glass.setChecked(self.app.prefs.glass)
            self.dwell.setValue(int(gaze.get("dwell_ms", 250)))
            self.dwell_label.setText(f"Look for {self.dwell.value()} ms before switching")
            self.hotkeys.setText(f"Bring input home: {host.get('return_hotkey', 'ctrl+alt+home')}\n"
                                 f"Gaze on/off: {host.get('gaze_toggle_hotkey', 'ctrl+alt+g')}")
            self.animations.setChecked(bool(raw.get("agent", {}).get("animations", True)))
            agent_gaze = raw.get("agent", {}).get("gaze", {})
            agent_gaze = agent_gaze if isinstance(agent_gaze, dict) else {}
            self.agent_camera.setChecked(bool(agent_gaze.get("enabled", False)))
            self.agent_camera_row.setVisible(self.agent_camera.isChecked())
            self.agent_camera_picker.set_current(int(agent_gaze.get("camera", 0) or 0),
                                                 str(agent_gaze.get("camera_name", "") or ""))
            role_table = raw.get(self._role_table(), {})
            self.clipboard.setChecked(bool(role_table.get("clipboard", True)) if isinstance(role_table, dict) else True)
            self.keep_screen.setChecked(bool(role_table.get("keep_screen_on", False))
                                        if isinstance(role_table, dict) else False)
            net = raw.get("network", {})
            self.network.optimise.setChecked(bool(net.get("optimise", True)) if isinstance(net, dict) else True)
            u = raw.get("update", {})
            self.upd_check.setChecked(bool(u.get("check", True)))
            self.upd_auto.setChecked(bool(u.get("auto", True)))
            self.upd_pre.setChecked(bool(u.get("prerelease", False)))
            self.config_text.setText(str(self.app.config_path))
        finally:
            self._loading = False

    # -- mirroring on joined screens ---------------------------------------------
    @staticmethod
    def joined_choices(raw: dict) -> list[tuple[str, str, list[tuple[str, str]]]]:
        """Screens behind others with screens joined to them, edge to edge:
        ``("device/screen", its name, [("device/screen", name), ...])``."""
        from curslink.config import ConfigError, parse_layout

        try:
            layout = parse_layout(raw)
        except ConfigError:
            return []
        layout.allow_overlap = True  # (only shown with it on)
        names = dict(SettingsPage.monitors(raw))
        out = []
        for screen in layout.screens():
            if not layout.covering(screen):
                continue
            joined = [f"{s.device_id}/{s.id}" for s in layout.joined(screen)]
            if joined:
                key = f"{screen.device_id}/{screen.id}"
                out.append((key, names.get(key, key), [(k, names.get(k, k)) for k in joined]))
        return out

    @staticmethod
    def joined_on(raw: dict, behind: str, joined: list[str]) -> set[str]:
        """Of ``joined``, the screens that keep ``behind`` mirrored."""
        table = raw.get("layout", {}).get("mirror_joined", {})
        which = table.get(behind) if isinstance(table, dict) else None
        if which == "all":
            return set(joined)
        return {k for k in which if k in joined} if isinstance(which, list) else set()

    def load_joined(self, raw: dict, show: bool) -> None:
        choices = self.joined_choices(raw) if show else []
        wanted = [(b, None) for b, _, js in choices if js] + [(b, j) for b, _, js in choices if len(js) > 1
                                                               for j, _ in js]
        if sorted(wanted, key=str) != sorted(self.joined_toggles, key=str):
            for row in self.joined_rows:
                row.setParent(None)
                row.deleteLater()
            self.joined_rows, self.joined_toggles = [], {}
            for behind, name, joined in choices:
                title = f"Keep mirroring {name} on joined screens"
                toggle = Toggle(title)
                toggle.clicked.connect(lambda on, b=behind: self.set_joined(b, None, on))
                self.joined_toggles[(behind, None)] = toggle
                listed = ", ".join(n for _, n in joined)
                self.joined_rows.append(self.mirror_card.add_row(text_block(
                    title,
                    f"Its mirror stays on while the pointer is on a screen joined to it ({listed}), not only "
                    "while it's on this one. The mirror still shows just the part that's covered."), toggle))
                if len(joined) > 1:
                    for key, jname in joined:
                        sub = Toggle(f"While on {jname}")
                        sub.clicked.connect(lambda on, b=behind, k=key: self.set_joined(b, k, on))
                        self.joined_toggles[(behind, key)] = sub
                        lab = label(f"While on {jname}", "small")
                        lab.setContentsMargins(24, 0, 0, 0)
                        self.joined_rows.append(self.mirror_card.add_row(lab, sub))
        for behind, _, joined in choices:
            keys = [k for k, _ in joined]
            on = self.joined_on(raw, behind, keys)
            self.joined_toggles[(behind, None)].setChecked(bool(on))
            for key in keys:
                if (behind, key) in self.joined_toggles:
                    self.joined_toggles[(behind, key)].setChecked(key in on)
                    self.joined_toggles[(behind, key)].setEnabled(bool(on))

    def set_joined(self, behind: str, key: str | None, on: bool) -> None:
        """Turn keeping ``behind`` mirrored on or off: on all its joined
        screens (``key`` None), or on one of them."""
        if self._loading:
            return
        cfg = self.app.config()
        raw = cfg.raw()
        joined = next((js for b, _, js in self.joined_choices(raw) if b == behind), [])
        keys = [k for k, _ in joined]
        if key is None:
            now = set(keys) if on else set()
        else:
            now = self.joined_on(raw, behind, keys)
            (now.add if on else now.discard)(key)
        table = dict(raw.get("layout", {}).get("mirror_joined", {}) or {})
        if not now:
            table.pop(behind, None)
        else:
            table[behind] = "all" if now == set(keys) else [k for k in keys if k in now]
        cfg.set_mirror_joined(table)
        cfg.save()
        self.app.config_saved()

    # -- per-monitor jumps -------------------------------------------------------
    @staticmethod
    def monitors(raw: dict) -> list[tuple[str, str]]:
        """Every monitor in the layout: ``("device/screen", "Name / screen · WxH")``."""
        out = []
        for d in raw.get("devices", []):
            name = d.get("name", d.get("id"))
            for i, sc in enumerate(d.get("screens", [])):
                sid = str(sc.get("id", f"screen{i}"))
                size = f" · {sc.get('width')}×{sc.get('height')}" if "width" in sc and "height" in sc else ""
                out.append((f"{d.get('id')}/{sid}", f"{name} / {sid}{size}"))
        return out

    @staticmethod
    def jumps_on(raw: dict) -> set[str]:
        gaze = raw.get("gaze", {})
        everything = {key for key, _ in SettingsPage.monitors(raw)}
        if gaze.get("within_device", False):
            return everything  # the older all-or-nothing switch
        listed = gaze.get("jump_screens", [])
        return {k for k in listed if isinstance(k, str)} & everything if isinstance(listed, list) else set()

    def load_jumps(self, raw: dict) -> None:
        monitors = self.monitors(raw)
        on = self.jumps_on(raw)
        if [k for k, _ in monitors] != list(self.jump_toggles):
            for row in self.jump_rows:
                row.setParent(None)
                row.deleteLater()
            self.jump_rows, self.jump_toggles = [], {}
            for key, text in monitors:
                toggle = Toggle(f"Jump to {text}")
                toggle.clicked.connect(lambda checked, k=key: self.set_jump(k, checked))
                self.jump_toggles[key] = toggle
                self.jump_rows.append(self.jump_card.add_row(label(text), toggle))
        for key, toggle in self.jump_toggles.items():
            toggle.setChecked(key in on)
        self.jump_all_on.setEnabled(len(on) < len(monitors))
        self.jump_all_off.setEnabled(bool(on))

    def set_jump(self, key: str, on: bool) -> None:
        cfg = self.app.config()
        keys = self.jumps_on(cfg.raw())
        (keys.add if on else keys.discard)(key)
        self.save_jumps(keys)

    def set_all_jumps(self, on: bool) -> None:
        self.save_jumps({k for k, _ in self.monitors(self.app.config().raw())} if on else set())

    def save_jumps(self, keys: set[str]) -> None:
        if self._loading:
            return
        cfg = self.app.config()
        order = [k for k, _ in self.monitors(cfg.raw())]
        cfg.set("gaze", "jump_screens", [k for k in order if k in keys])
        cfg.remove("gaze", "within_device")  # jump_screens says it all now
        cfg.save()
        self.app.config_saved()

    # -- saving -----------------------------------------------------------------
    def set_pref(self, key: str, value) -> None:
        setattr(self.app.prefs, key, value)
        self.app.prefs.save()

    def set_theme(self, mode: str) -> None:
        self.set_pref("theme", mode)
        theme.manager().set_mode(mode)

    def pick_effect(self, b) -> None:
        self.set_config("host", "effect", b.property("effect"))
        self.preview_effect()

    def preview_effect(self) -> None:
        """Play the chosen switch animation over the middle of this window."""
        b = self.effect_group.checkedButton()
        effect = b.property("effect") if b else "pop"
        self.preview_button.setEnabled(effect != "none")
        if effect == "none":
            return
        if getattr(self, "_overlay", None) is None:
            from curslink.overlay import OverlayProcess

            self._overlay = OverlayProcess()
        c = self.window().frameGeometry().center()
        self._overlay.show(effect, c.x(), c.y())

    def set_camera(self, index: int, name: str) -> None:
        if self._loading:
            return
        cfg = self.app.config()
        cfg.set("gaze", "camera", index)
        cfg.set("gaze", "camera_name", name)
        cfg.save()
        self.app.config_saved()

    def set_agent_camera(self, on: bool) -> None:
        self.agent_camera_row.setVisible(on)
        self.set_config("agent.gaze", "enabled", on)

    def set_agent_camera_device(self, index: int, name: str) -> None:
        if self._loading:
            return
        cfg = self.app.config()
        cfg.set("agent.gaze", "camera", index)
        cfg.set("agent.gaze", "camera_name", name)
        cfg.save()
        self.app.config_saved()

    # -- input helper (Windows) ----------------------------------------------------
    HELPER_STATES = {
        "running": "On: the pointer and keyboard work in admin apps, UAC prompts and the lock screen.",
        "stopped": "Installed, but the service isn't running. Turn it off and on again to repair it.",
    }

    def refresh_helper(self, note: str = "") -> None:
        if self.helper is None:
            return
        from curslink.gui.common import in_background
        from curslink.winhelper.install import status

        def show(state: str) -> None:
            self.helper.setChecked(state != "missing")
            self.helper.setEnabled(True)
            detail = note or self.HELPER_STATES.get(state, "")
            self.helper_block.detail_label.setText(detail or self.helper_detail)

        if not hasattr(self, "helper_detail"):
            self.helper_detail = self.helper_block.detail_label.text()
        in_background(status, show, lambda exc: show("missing"))

    def set_helper(self, on: bool) -> None:
        from curslink.gui.common import in_background
        from curslink.winhelper.install import ERROR_CANCELLED, run_elevated

        self.helper.setEnabled(False)
        self.helper_block.detail_label.setText("Waiting for the admin prompt…")
        verb = "on" if on else "off"

        def done(code: int) -> None:
            if code == 0:
                self.refresh_helper()
            elif code == ERROR_CANCELLED:
                self.refresh_helper("Nothing changed: the admin prompt was declined.")
            else:
                self.refresh_helper(f"Couldn't turn it {verb} (error {code}). The logs have the details.")

        in_background(lambda: run_elevated("install" if on else "uninstall"), done,
                      lambda exc: self.refresh_helper(f"Couldn't turn it {verb}: {exc}"))

    def set_login(self, on: bool) -> None:
        try:
            self.autostart.set_enabled(on)
        except OSError as exc:
            self.login.setChecked(not on)
            self.update_text.setText(f"Couldn't change start at sign-in: {exc}")

    def edit_pair_maps(self) -> None:
        from curslink.gui.keymap import PairMapDialog

        cfg = self.app.config()
        dialog = PairMapDialog(cfg, self.window())
        if dialog.exec():
            cfg.save()
            self.app.config_saved(restart=True)

    def check_keyboard(self) -> None:
        from curslink.gui.keymap import KeyboardCheckDialog

        KeyboardCheckDialog(self.app, self.window()).exec()

    # -- network --------------------------------------------------------------
    def _on_service_event(self, event: dict) -> None:
        kind = event.get("event")
        if kind == "network":
            self.network.show_network(event)
        elif kind == "netcheck":
            self.network.show_check(event)

    def _on_service_state(self, state: str) -> None:
        if state != "running":
            self.network.stopped()

    def check_network(self) -> None:
        if not self.app.service.is_alive():
            self.network.check_unavailable()
            return
        self.network.checking()
        self.app.service.send("netcheck")

    def _role_table(self) -> str:
        """The config table of this computer's role: "host" or "agent"."""
        try:
            return "agent" if self.app.config().role == "agent" else "host"
        except Exception:
            return "host"

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
