"""Status: where the keyboard and mouse are, what's connected, what just happened."""

from __future__ import annotations

import time

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QGridLayout, QHBoxLayout, QPlainTextEdit, QVBoxLayout, QWidget

from curslink.gui.common import friendly_reason
from curslink.gui.service import FAILED, RUNNING, STARTING
from curslink.gui.screenmap import ScreenMap, screens_from_config
from curslink.gui.widgets import Card, Page, Toggle, button, label, pill, set_spot, spot, text_block


class StatusPage(Page):
    def __init__(self, app):
        super().__init__("Status", " ")
        self.app = app
        self.run_button = button("Stop linking", None, self.toggle_run)
        self.header.addWidget(self.run_button)

        self.error = label("", "error", wrap=True)
        self.error.hide()
        self.add(self.error)

        # macOS: Accessibility / Input Monitoring, asked for here until granted.
        from curslink.gui.permissions import PermissionsCard

        self.permissions = PermissionsCard(app)
        self.add(self.permissions)

        # Where input is (host) / the link to the host (agent).
        self.where = Card()
        top = QHBoxLayout()
        top.setSpacing(18)
        # Host: a small map of the screens with the pointer's screen lit.
        # Agent: a picture of the link's state.
        self.mini = ScreenMap(compact=True)
        top.addWidget(self.mini)
        self.art = spot("waiting-for-host", 120, 90)
        top.addWidget(self.art)
        words = QVBoxLayout()
        words.setSpacing(4)
        self.eyebrow = label("KEYBOARD AND MOUSE ARE ON", "eyebrow")
        self.big = label("", "big")
        self.detail = label("", "muted", wrap=True)
        for w in (self.eyebrow, self.big, self.detail):
            words.addWidget(w)
        top.addLayout(words, 1)
        self.home_button = button("Bring input home", None, lambda: app.service.send("home"))
        self.home_button.setToolTip("Ctrl+Alt+Home")
        top.addWidget(self.home_button)
        self.where.body.addLayout(top)
        self.add(self.where)

        # Switching toggles (host only).
        self.switching = QWidget()
        grid = QGridLayout(self.switching)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setSpacing(16)
        self.gaze_toggle = Toggle("Follow my gaze")
        self.gaze_toggle.clicked.connect(self.gaze_clicked)
        self.gaze_text = text_block("Follow my gaze", "")
        self.edge_toggle = Toggle("Screen edges")
        self.edge_toggle.clicked.connect(self.edge_clicked)
        for col, (text, toggle) in enumerate(((self.gaze_text, self.gaze_toggle), (
                text_block("Screen edges", "Push the pointer off an edge to cross to the device next to it."),
                self.edge_toggle))):
            card = Card()
            row = QHBoxLayout()
            row.addWidget(text, 1)
            row.addWidget(toggle)
            card.body.addLayout(row)
            grid.addWidget(card, 0, col)
        self.add(self.switching)

        self.devices = Card("Devices", rows=True)
        self.add(self.devices)
        self.switches = Card("Recent switches", rows=True)
        self.add(self.switches)

        self.activity = Card("Activity")
        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setMinimumHeight(140)
        self.log_view.setMaximumBlockCount(300)
        self.activity.body.addWidget(self.log_view)
        self.add(self.activity)
        self.finish()

        self._logged = -1
        app.status.changed.connect(self.refresh)
        app.service_changed.connect(lambda _s: self.refresh())
        app.config_changed.connect(self.refresh)
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.refresh_log)
        self.timer.start(1000)
        self.refresh()

    def shown(self) -> None:
        self.permissions.refresh()
        self.refresh()

    # -- actions --------------------------------------------------------------
    def toggle_run(self) -> None:
        if self.app.linking:
            self.app.stop_linking()
        else:
            self.app.start_linking()

    def gaze_clicked(self, on: bool) -> None:
        status = self.app.status
        if self.app.service.state == RUNNING and (status.gaze or status.cameras):
            # Something is following your gaze (this camera or an agent's): pause or resume it.
            self.app.service.send("gaze resume" if on else "gaze pause")
            return
        # Gaze isn't running: switch it on in the config (or send them to calibrate).
        cfg = self.app.config()
        cal = self.app.config_path.parent / str(cfg.get("gaze", "calibration", "calibration.json"))
        if on and not cal.exists() and not status.cameras:
            # Nothing else to work with: an uncalibrated camera alone can't
            # tell which device you look at. With an agent's camera it can.
            self.gaze_toggle.setChecked(False)
            self.app.show_window("calibrate")
            return
        cfg.set("gaze", "enabled", on)
        cfg.save()
        self.app.config_saved()

    def edge_clicked(self, on: bool) -> None:
        cfg = self.app.config()
        cfg.set("host", "edge_switching", on)
        cfg.save()
        self.app.config_saved()

    # -- drawing --------------------------------------------------------------
    def refresh(self) -> None:
        app, status, state = self.app, self.app.status, self.app.service.state
        role = app.role or "host"
        self.description.setText(
            "This computer's keyboard and mouse follow you to your other devices."
            if role == "host" else
            "Curslink runs in the background here. Your host's keyboard and mouse arrive when you look at this screen."
        )
        self.run_button.setText("Stop linking" if app.linking else "Start linking")
        self.run_button.setObjectName("" if app.linking else "primary")
        self.run_button.style().unpolish(self.run_button)
        self.run_button.style().polish(self.run_button)

        if state == FAILED and app.service.error:
            self.error.setText(f"Curslink stopped: {app.service.error}")
            self.error.show()
        else:
            self.error.hide()

        names = self.device_names()
        self.mini.setVisible(role == "host")
        self.art.setVisible(role != "host")
        if role == "host":
            try:
                self.mini.set_screens(screens_from_config(app.config()))
            except Exception:
                self.mini.set_screens([])
            if state == RUNNING and status.active:
                states = {f"{status.active}/{status.screen}": {"active"}}
                looking = status.looking()
                if looking:
                    states.setdefault(looking, set()).add("looking")
                self.mini.set_states(states)
            else:
                self.mini.set_states({})
            self.eyebrow.setText("KEYBOARD AND MOUSE ARE ON")
            if state == RUNNING:
                self.big.setText(names.get(status.active, status.active or "This computer"))
                here = status.active == status.device_id
                self.detail.setText(f'Screen "{status.screen}"' + (", this computer" if here else ""))
                self.home_button.setEnabled(not here)
            else:
                self.big.setText("Starting…" if state == STARTING else "Not linking")
                self.detail.setText("Start linking to share this keyboard and mouse with your other devices.")
                self.home_button.setEnabled(False)
            self.home_button.show()
        else:
            self.eyebrow.setText("LINK TO THE HOST")
            self.home_button.hide()
            if state != RUNNING:
                set_spot(self.art, "not-linking", 120, 90)
                self.big.setText("Starting…" if state == STARTING else "Not linking")
                self.detail.setText("Start linking so the host can reach this computer.")
            elif status.host_state == "connected":
                set_spot(self.art, "paired", 120, 90)
                self.big.setText("Connected")
                self.detail.setText(f"Through {status.host_detail}. Look at this screen to use it.")
            elif status.host_state == "disconnected":
                set_spot(self.art, "connection-lost", 120, 90)
                self.big.setText("Connection lost")
                self.detail.setText(status.host_detail or "Trying to reach the host again…")
            else:
                set_spot(self.art, "waiting-for-host", 120, 90)
                self.big.setText("Waiting for the host")
                self.detail.setText(status.host_detail or "Looking for the host…")

        self.switching.setVisible(role == "host")
        if role == "host":
            cfg = app.config().raw() if app.has_config() else {}
            gaze_on_in_config = bool(cfg.get("gaze", {}).get("enabled", False))
            cameras = [names.get(d, d) for d in status.cameras] if state == RUNNING else []
            if state == RUNNING and status.gaze:
                self.gaze_toggle.setChecked(not status.gaze_paused)
                hint = "Switch when you look at another screen. Ctrl+Alt+G pauses it."
            elif cameras:
                # This camera is off, but an agent's own camera still switches.
                self.gaze_toggle.setChecked(not status.gaze_paused)
                hint = (f"Using {', '.join(cameras)}'s camera. Calibrate to use this "
                        "computer's camera too.")
            else:
                self.gaze_toggle.setChecked(gaze_on_in_config)
                hint = ("Switch when you look at another screen. Calibrate first."
                        if not gaze_on_in_config else "Switch when you look at another screen.")
            if state == RUNNING and status.gaze and cameras:
                hint = f"Using this camera and {', '.join(cameras)}'s. Ctrl+Alt+G pauses it."
            self.gaze_text.detail_label.setText(hint)
            self.gaze_text.detail_label.show()
            self.edge_toggle.setChecked(bool(cfg.get("host", {}).get("edge_switching", True)))

        self.refresh_devices(names)
        self.refresh_switches(names)

    def device_names(self) -> dict[str, str]:
        names = {}
        try:
            for d in self.app.config().devices():
                names[d.get("id")] = d.get("name", d.get("id"))
        except Exception:
            pass
        for a in self.app.status.agents.values():
            if a.name:
                names[a.device_id] = a.name
        return names

    def refresh_devices(self, names: dict[str, str]) -> None:
        card, status, app = self.devices, self.app.status, self.app
        card.clear_rows()
        running = app.service.state == RUNNING
        if (app.role or "host") == "host":
            try:
                devices = app.config().devices()
                me = app.config().get("host", "device_id")
            except Exception:
                devices, me = [], None
            for d in devices:
                did = d.get("id")
                screens = len(d.get("screens", []))
                agent = status.agents.get(did)
                detail = f"{'Host' if did == me else 'Agent'} · {screens} screen{'s' * (screens != 1)}"
                if agent and agent.platform:
                    detail += f" · {agent.platform}"
                if did == me:
                    p = pill("This computer", "here")
                elif running and agent and agent.connected:
                    p = pill("Connected", "on")
                else:
                    p = pill("Offline", "off")
                card.add_row(text_block(names.get(did, did), detail), p)
            for agent in status.agents.values():
                if agent.device_id not in {d.get("id") for d in devices}:
                    card.add_row(text_block(agent.name or agent.device_id, "Not in the layout yet"),
                                 pill("Connected" if agent.connected else "Offline", "wait"))
        else:
            from curslink.pairing.store import PairingStore

            try:
                hosts = PairingStore(self._store_path()).peers("host")
            except Exception:
                hosts = []
            for h in hosts:
                connected = running and status.host_state == "connected"
                card.add_row(text_block(h.name, "Host"), pill("Connected" if connected else "Offline",
                                                             "on" if connected else "off"))
            if not hosts:
                card.add_row(text_block("Not paired with a host yet", "Pair on the Devices page."),
                             pill("Waiting", "wait"))

    def _store_path(self):
        from curslink.config import pairing_config

        return pairing_config(self.app.config().raw(), self.app.config_path.parent).store

    def refresh_switches(self, names: dict[str, str]) -> None:
        card = self.switches
        card.clear_rows()
        switches = list(self.app.status.switches)[:8]
        card.setVisible((self.app.role or "host") == "host")
        if not switches:
            card.add_row(label("Nothing yet. Switches show up here as they happen.", "small"))
            return
        for s in switches:
            when = label(time.strftime("%H:%M:%S", time.localtime(s.at)), "mono")
            what = label(f"{names.get(s.source, s.source)} → {names.get(s.target, s.target)} / {s.screen}")
            why = pill(friendly_reason(s.reason), "here")
            card.add_row(what, when, why)

    def refresh_log(self) -> None:
        log = self.app.status.log
        if self.app.status.log_seq == self._logged:
            return
        lines = [f"{time.strftime('%H:%M:%S', time.localtime(t))}  {msg}" for t, _lvl, msg in list(log)[-120:]]
        self.log_view.setPlainText("\n".join(lines))
        self.log_view.verticalScrollBar().setValue(self.log_view.verticalScrollBar().maximum())
        self._logged = self.app.status.log_seq
