"""Calibrate: look at each screen in turn so gaze switching knows where they are."""

from __future__ import annotations

import json
import logging
import time

from PySide6.QtCore import QTimer, Qt
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import QHBoxLayout, QProgressBar, QVBoxLayout, QWidget

from curslink.gui.camerapicker import CameraPicker
from curslink.gui.common import call_on_main, in_background
from curslink.gui.overlay import LookHere
from curslink.gui.screenmap import ScreenMap, screens_from_config
from curslink.gui.service import RUNNING, STARTING
from curslink.gui.widgets import Card, Page, button, label, pill, set_spot, spot

log = logging.getLogger(__name__)

# How close (degrees) your head has to point to a recorded screen for the map
# to say you're looking at it.
LOOKING_DEG = 12.0


class CalibratePage(Page):
    def __init__(self, app):
        super().__init__(
            "Calibrate gaze",
            "Look at the centre of each screen in turn so Curslink learns where it sits relative to your head. "
            "Gaze switching pauses while the camera is in use here.",
        )
        self.app = app
        self.calibrator = None
        self.targets: list = []
        self.step = 0
        self.results: dict[str, tuple[float, float] | None] = {}
        self.was_linking = False
        # Linking stayed up (gaze off) so paired computers show "Look here" too.
        self.kept_linking = False
        self._look_sent: str | None = None

        self.agent_note = label("Gaze runs on the host, the computer with the camera and the keyboard and mouse. "
                                "Calibrate there.", "warn", wrap=True)
        self.add(self.agent_note)

        self.body = QWidget()
        col = QVBoxLayout(self.body)
        col.setContentsMargins(0, 0, 0, 0)
        col.setSpacing(16)

        # The layout, drawn: which screens are done, which to look at, and
        # which one you're looking at now. No angles, just the picture.
        mapcard = Card()
        self.map = ScreenMap()
        mapcard.body.addWidget(self.map, 1)
        self.readout = label("Camera off", "muted")
        mapcard.body.addWidget(self.readout)
        col.addWidget(mapcard)

        row = QHBoxLayout()
        row.setSpacing(16)
        steps = Card()
        top = QHBoxLayout()
        top.setSpacing(16)
        self.art = spot("calibrate", 120, 90)
        top.addWidget(self.art)
        words = QVBoxLayout()
        words.setSpacing(4)
        self.step_title = label("", "eyebrow")
        self.prompt = label("", "prompt", wrap=True)
        self.prompt_detail = label("", "muted", wrap=True)
        for w in (self.step_title, self.prompt, self.prompt_detail):
            words.addWidget(w)
        top.addLayout(words, 1)
        steps.body.addLayout(top)
        # The first run downloads the face model: show how far it's got.
        self.progress = QProgressBar()
        self.progress.setTextVisible(False)
        self.progress.setFixedHeight(6)
        self.progress.setAccessibleName("Download progress")
        self.progress_text = label("", "small")
        steps.body.addWidget(self.progress)
        steps.body.addWidget(self.progress_text)
        self.progress.hide()
        self.progress_text.hide()
        cam_row = QHBoxLayout()
        cam_row.addWidget(label("Camera", "small"))
        self.camera = CameraPicker()
        cam_row.addWidget(self.camera)
        cam_row.addStretch(1)
        self.camera_row = QWidget()
        self.camera_row.setLayout(cam_row)
        steps.body.addWidget(self.camera_row)
        buttons = QHBoxLayout()
        self.primary = button("Start", "primary", self.primary_clicked)
        self.secondary = button("Skip this screen", None, self.skip)
        self.cancel_button = button("Cancel", None, self.cancel)
        for b in (self.primary, self.secondary, self.cancel_button):
            buttons.addWidget(b)
        buttons.addStretch(1)
        steps.body.addLayout(buttons)
        steps.body.addStretch(1)
        row.addWidget(steps, 3)

        self.list_card = Card("Screens", rows=True)
        row.addWidget(self.list_card, 2)
        col.addLayout(row)
        self.add(self.body)
        self.finish()
        self.overlay = LookHere(self.overlay_record, self.cancel, self.skip)
        self.looking: str | None = None
        # The card on another computer has no buttons: you press Space here.
        for key, fn in ((Qt.Key.Key_Space, self.record), (Qt.Key.Key_Escape, self.escape)):
            shortcut = QShortcut(QKeySequence(key), self)
            shortcut.setContext(Qt.ShortcutContext.WindowShortcut)
            shortcut.activated.connect(fn)

        self.timer = QTimer(self)
        self.timer.timeout.connect(self.tick)
        # While linking, the host has the camera: light up where it says you look.
        self.live_timer = QTimer(self)
        self.live_timer.timeout.connect(self.live_tick)
        self.live_timer.start(400)
        self.state = "idle"
        self.show_state()
        app.config_changed.connect(self.load_targets)

    def shown(self) -> None:
        if not self.camera.loaded:
            self.camera.refresh()
        if self.state == "idle":
            self.load_targets()

    def theme_changed(self) -> None:
        self.show_state()

    # -- setup ----------------------------------------------------------------
    def load_targets(self) -> None:
        if not self.app.has_config() or self.state != "idle":
            return
        from curslink.gaze.zones import Target

        is_host = self.app.role == "host"
        self.agent_note.setVisible(not is_host)
        self.body.setVisible(is_host)
        if not is_host:
            return
        cfg = self.app.config()
        self.camera.set_current(int(cfg.get("gaze", "camera", 0) or 0), str(cfg.get("gaze", "camera_name", "") or ""))
        self.map.set_screens(screens_from_config(cfg))
        names = {d.get("id"): d.get("name", d.get("id")) for d in cfg.devices()}
        self.targets = []
        for d in cfg.devices():
            for i, s in enumerate(d.get("screens", [])):
                sid = str(s.get("id", f"screen{i}"))
                self.targets.append((Target(d.get("id"), sid), f"{names[d.get('id')]} / {sid}"))
        self.results = {}
        self.show_state()

    # -- the flow -------------------------------------------------------------
    def primary_clicked(self) -> None:
        if self.state in ("idle", "done", "error"):
            self.begin()
        elif self.state == "ready":
            self.record()

    def begin(self) -> None:
        from curslink.app import Calibrator

        self.load_targets()
        if not self.targets:
            return
        self.was_linking = self.app.service.state in (RUNNING, STARTING)
        # Running: keep linking so the other computers can show "Look here",
        # and ask the host to hand over the camera. Still starting: stop it.
        self.kept_linking = self.app.service.state == RUNNING
        self._look_sent = None
        if self.kept_linking:
            self.app.calibrating = False
            self.app.service.send("calibrate on")
        elif self.was_linking:
            self.app.stop_linking()  # the host holds the camera
        cfg = self.app.config()
        index, name = self.camera.current()
        if (int(cfg.get("gaze", "camera", 0) or 0), str(cfg.get("gaze", "camera_name", "") or "")) != (index, name):
            cfg.set("gaze", "camera", index)
            if name:
                cfg.set("gaze", "camera_name", name)
            cfg.save()
        self.step = 0
        self.results = {}
        self.retry = None
        self.state = "warming"
        self.show_state()

        kept = self.kept_linking

        def warm():
            from curslink.gaze.tracker import default_model_path, ensure_model

            if not default_model_path().exists():
                call_on_main(lambda: self.show_download(0, 0))
                ensure_model(progress=lambda done, total: call_on_main(lambda: self.show_download(done, total)))
            call_on_main(self.show_starting)
            deadline = time.monotonic() + 8.0
            while kept and not self.app.calibrating and time.monotonic() < deadline:
                time.sleep(0.05)  # the host is letting go of the camera
            from curslink import macperms

            if not macperms.ensure_camera():
                raise RuntimeError("Curslink isn't allowed to use the camera. Allow it in System Settings › "
                                   "Privacy & Security › Camera, then try again.")
            cal = Calibrator(index)
            cal.start()
            if not cal.wait_for_face(20):
                cal.stop()
                why = getattr(cal.tracker, "error", None)
                if why:
                    raise RuntimeError(f"The camera stopped: {why}. Settings > Diagnostics has the full log.")
                raise RuntimeError("No face found. Check the camera is free and you're facing it in good light.")
            return cal

        in_background(warm, self.warmed, self.failed)

    def show_download(self, done: int, total: int) -> None:
        if self.state != "warming":
            return
        self.prompt.setText("Downloading the face model…")
        self.prompt_detail.setText("Only the first time. Curslink keeps it for next time.")
        mb = 1024 * 1024
        if total:
            self.progress.setRange(0, total)
            self.progress.setValue(done)
            self.progress_text.setText(f"{done / mb:.1f} of {total / mb:.1f} MB · {done * 100 // total}%")
        else:
            self.progress.setRange(0, 0)  # unknown size: a moving bar
            self.progress_text.setText(f"{done / mb:.1f} MB")
        self.progress.show()
        self.progress_text.show()

    def show_starting(self) -> None:
        if self.state != "warming":
            return
        self.prompt.setText("Looking for your face…")
        self.prompt_detail.setText("Starting the camera. Sit facing it in good light.")
        self.progress.setRange(0, 0)
        self.progress_text.setText("Starting the camera")
        self.progress.show()
        self.progress_text.show()

    def warmed(self, cal) -> None:
        if self.state != "warming":  # cancelled meanwhile
            cal.stop()
            return
        self.calibrator = cal
        self.state = "ready"
        self.timer.start(80)
        self.show_state()

    def record(self) -> None:
        if self.state != "ready":
            return
        target, name = self.targets[self.step]
        self.state = "recording"
        self.show_state()
        cal = self.calibrator
        in_background(lambda: cal.record(target, 1.5), lambda centre: self.recorded(name, centre), self.failed)

    def overlay_record(self) -> None:
        self.record()

    def recorded(self, name: str, centre) -> None:
        if self.state != "recording":  # cancelled meanwhile
            return
        if centre is None:
            # The camera barely saw the face (usually a screen far off to the
            # side): stay on this screen so it can be tried again, rather than
            # quietly marking it skipped.
            self.state = "ready"
            self.retry = name
            self.show_state()
            return
        self.results[name] = centre
        self.next_step()

    def skip(self) -> None:
        if self.state == "ready":
            self.results[self.targets[self.step][1]] = None
            self.next_step()

    def next_step(self) -> None:
        self.retry = None
        self.step += 1
        if self.step < len(self.targets):
            self.state = "ready"
            self.show_state()
            return
        self.finish_calibration()

    def finish_calibration(self) -> None:
        from curslink.app import close_pairs
        from curslink.gaze.zones import GazeSettings

        cal = self.calibrator
        self.stop_camera()
        if not cal or not cal.calibration.centroids():
            self.state, self.message = "error", "No screens were recorded, so nothing was saved."
            self.show_state()
            self.resume_linking()
            return
        cfg = self.app.config()
        path = self.app.config_path.parent / str(cfg.get("gaze", "calibration", "calibration.json"))
        cal.calibration.save(path)
        cfg.set("gaze", "enabled", True)
        cfg.save()
        margin = float(cfg.get("gaze", "switch_margin_deg", GazeSettings().switch_margin_deg))
        close = close_pairs(cal.calibration, margin)
        if close:
            a, b, gap = close[0]
            self.message = (f"Saved, but {a.key} and {b.key} are only {gap:.1f}° apart, so gaze may mix them up. "
                            "Edges still work between them.")
        else:
            self.message = f"All {len(cal.calibration.centroids())} screens are far enough apart to tell apart."
        self.state = "done"
        self.show_state()
        self.app.config_changed.emit()
        self.resume_linking(force=True)

    def failed(self, exc) -> None:
        log.warning("calibration stopped: %s", exc, exc_info=exc if not isinstance(exc, RuntimeError) else None)
        self.stop_camera()
        self.state, self.message = "error", str(exc)
        self.show_state()
        self.resume_linking()

    def cancel(self) -> None:
        self.stop_camera()
        self.state = "idle"
        self.show_state()
        self.resume_linking()

    def stop_camera(self) -> None:
        self.timer.stop()
        self.overlay.hide()
        self.looking = None
        if self.calibrator is not None:
            cal, self.calibrator = self.calibrator, None
            in_background(cal.stop)

    def escape(self) -> None:
        if self.state in ("warming", "ready", "recording"):
            self.cancel()

    def resume_linking(self, force: bool = False) -> None:
        if self.kept_linking:
            # Take the cards down, then restart linking: that brings gaze back
            # with whatever calibration was just saved.
            self.send_look(None)
            self.kept_linking = self.was_linking = False
            self._look_sent = None
            self.app.calibrating = False
            QTimer.singleShot(300, self.app.service.restart)
            return
        if self.was_linking or (force and self.app.prefs.start_linking):
            QTimer.singleShot(800, self.app.start_linking)
        self.was_linking = False

    def tick(self) -> None:
        cal = self.calibrator
        if cal is None or cal.latest is None:
            return
        looking = self.nearest(cal.latest)
        if looking != self.looking:
            self.looking = looking
            self.draw_map()
            names = {t.key: n for t, n in self.targets}
            self.readout.setText(f"You're looking at {names.get(looking, looking)}" if looking
                                 else "Face found. Recorded screens light up as you look at them.")

    def nearest(self, angles) -> str | None:
        """The recorded screen your head points closest to, if near enough."""
        cal = self.calibrator
        if cal is None:
            return None
        best, best_d = None, LOOKING_DEG
        for target, (yaw, pitch) in cal.calibration.centroids().items():
            d = ((angles[0] - yaw) ** 2 + (angles[1] - pitch) ** 2) ** 0.5
            if d < best_d:
                best, best_d = target.key, d
        return best

    def linking_camera(self) -> bool:
        """The linking service is running gaze, so it has the camera."""
        st = self.app.status
        return self.app.role == "host" and st.running and st.gaze

    def idle_readout(self) -> str:
        if not self.linking_camera():
            return "Camera off"
        st = self.app.status
        if st.gaze_paused:
            return "Camera on · gaze switching paused"
        names = {t.key: n for t, n in self.targets}
        looking = st.looking()
        return f"Camera on · you're looking at {names.get(looking, looking)}" if looking else \
            "Camera on · no face in view"

    def live_tick(self) -> None:
        if self.calibrator is not None or self.state not in ("idle", "done", "error"):
            return
        looking = self.app.status.looking() if self.linking_camera() else ""
        looking = looking or None
        text = self.idle_readout()
        if looking != self.looking:
            self.looking = looking
            self.draw_map()
        if self.readout.text() != text:
            self.readout.setText(text)

    # -- drawing --------------------------------------------------------------
    def draw_map(self) -> None:
        busy = self.state in ("ready", "recording")
        states: dict[str, set[str]] = {}
        for i, (target, name) in enumerate(self.targets):
            st = set()
            if name in self.results:
                st.add("done" if self.results[name] is not None else "skipped")
            if busy and i == self.step:
                st.add("target")
            if target.key == self.looking:
                st.add("looking")
            states[target.key] = st
        self.map.set_states(states)

    def send_look(self, payload: dict | None) -> None:
        """Show "Look here" on another computer (or take it down everywhere)."""
        if not self.kept_linking:
            return
        text = json.dumps(payload if payload is not None else {"hide": True}, separators=(",", ":"))
        if text == self._look_sent:
            return
        self._look_sent = text
        self.app.service.send("look " + text)

    def screen_os_rect(self, device_id: str, screen_id: str) -> list[int] | None:
        try:
            for d in self.app.config().devices():
                if d.get("id") != device_id:
                    continue
                for i, s in enumerate(d.get("screens", [])):
                    if str(s.get("id", f"screen{i}")) == screen_id:
                        return [int(s.get("os_x", 0)), int(s.get("os_y", 0)),
                                int(s.get("width", 0)), int(s.get("height", 0))]
        except Exception:
            log.debug("couldn't read the screen's position", exc_info=True)
        return None

    def update_overlay(self) -> None:
        if self.state not in ("ready", "recording") or self.app.role != "host":
            self.overlay.hide()
            self.send_look(None)
            return
        target, name = self.targets[self.step]
        try:
            me = self.app.config().get("host", "device_id")
        except Exception:
            me = None
        step = f"Step {self.step + 1} of {len(self.targets)} · {name.split(' / ')[0]}"
        done = {i for i, (_t, n) in enumerate(self.targets) if n in self.results}
        if target.device_id == me:
            self.send_look(None)
            self.overlay.show(target.screen_id, step, recording=self.state == "recording",
                              total=len(self.targets), current=self.step, done=done)
        else:
            # Another computer's screen: that computer shows the card (if it's
            # linked), and the map and the prompt here show it too.
            self.overlay.hide()
            self.send_look({
                "device": target.device_id, "screen": target.screen_id, "step": step,
                "recording": self.state == "recording", "total": len(self.targets),
                "current": self.step, "done": sorted(done), "host": self.app.device_name(),
                "os": self.screen_os_rect(target.device_id, target.screen_id),
            })

    def show_state(self) -> None:
        s, n = self.state, len(self.targets)
        busy = s in ("warming", "ready", "recording")
        if s != "warming":
            self.progress.hide()
            self.progress_text.hide()
        self.camera_row.setVisible(not busy)
        self.cancel_button.setVisible(busy)
        self.secondary.setVisible(s == "ready")
        self.primary.setEnabled(s not in ("warming", "recording"))
        art = "calibrate"
        if s == "idle":
            self.step_title.setText(f"{n} screen{'s' * (n != 1)} to calibrate".upper())
            self.prompt.setText("Sit how you normally do, facing the camera")
            self.prompt_detail.setText("A \"Look here\" card shows on each screen in turn. Look at it and press Space. "
                                       "It takes about a minute.")
            self.primary.setText("Start")
            self.readout.setText(self.idle_readout())
        elif s == "warming":
            self.step_title.setText("STARTING THE CAMERA")
            self.prompt.setText("Getting ready…")
            self.prompt_detail.setText("Starting the camera.")
            self.primary.setText("Record")
            self.readout.setText("Starting the camera…")
        elif s in ("ready", "recording"):
            name = self.targets[self.step][1]
            self.step_title.setText(f"STEP {self.step + 1} OF {n}")
            self.prompt.setText(f"Look at {name}")
            if s == "recording":
                detail = "Keep looking for a moment…"
            elif getattr(self, "retry", None) == name:
                detail = ("The camera couldn't see your face while you looked there. Turn your head a little "
                          "less (move your eyes the rest of the way) and press Record again, or Skip it.")
            else:
                detail = "Then press Record (or Space on the Look here card)."
            self.prompt_detail.setText(detail)
            self.primary.setText("Recording…" if s == "recording" else "Record")
        elif s == "done":
            art = "calibrated"
            self.step_title.setText("DONE")
            self.prompt.setText("Calibration saved")
            self.prompt_detail.setText(self.message + " Gaze switching is on.")
            self.primary.setText("Calibrate again")
            self.readout.setText(self.idle_readout())
        elif s == "error":
            msg = self.message.lower()
            art = "no-face" if "no face" in msg else "camera-off"
            self.step_title.setText("CALIBRATION STOPPED")
            self.prompt.setText("That didn't work")
            self.prompt_detail.setText(self.message)
            self.primary.setText("Try again")
            self.readout.setText(self.idle_readout())
        set_spot(self.art, art, 120, 90)

        self.list_card.clear_rows()
        for i, (_target, name) in enumerate(self.targets):
            if name in self.results:
                p = pill("Recorded", "on") if self.results[name] is not None else pill("Skipped", "wait")
            elif busy and i == self.step:
                p = pill("Look here", "here")
            else:
                p = pill("Next", "off")
            self.list_card.add_row(label(name), p)
        if not self.targets:
            self.list_card.add_row(label("Add screens on the Layout page first.", "small"))
        self.draw_map()
        self.update_overlay()
