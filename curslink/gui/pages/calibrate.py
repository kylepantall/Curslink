"""Calibrate: look at each screen in turn so gaze switching knows where they are."""

from __future__ import annotations

import logging

from PySide6.QtCore import QPointF, QRectF, QSize, Qt, QTimer
from PySide6.QtGui import QColor, QFont, QPainter, QPen
from PySide6.QtWidgets import QHBoxLayout, QSpinBox, QVBoxLayout, QWidget

from curslink.gui.common import ACCENT_BRIGHT, NAVY, NAVY_2, in_background
from curslink.gui.service import RUNNING, STARTING
from curslink.gui.widgets import Card, Page, button, clear_layout, label

log = logging.getLogger(__name__)

YAW_RANGE, PITCH_RANGE = 50.0, 32.0


class HeadPlot(QWidget):
    """Where your head points (yaw across, pitch up), plus each recorded screen."""

    def __init__(self):
        super().__init__()
        self.current: tuple[float, float] | None = None
        self.points: dict[str, tuple[float, float]] = {}
        self.setMinimumSize(QSize(320, 220))
        self.setAccessibleName("Head direction")

    def sizeHint(self) -> QSize:
        return QSize(420, 260)

    def to_px(self, yaw: float, pitch: float) -> QPointF:
        r = self.rect().adjusted(16, 16, -16, -16)
        x = r.center().x() + max(-1, min(1, yaw / YAW_RANGE)) * r.width() / 2
        y = r.center().y() - max(-1, min(1, pitch / PITCH_RANGE)) * r.height() / 2
        return QPointF(x, y)

    def paintEvent(self, _event) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.fillRect(self.rect(), QColor(NAVY_2))
        p.setPen(QPen(QColor("#4A5D78"), 1))
        c = self.to_px(0, 0)
        p.drawLine(QPointF(16, c.y()), QPointF(self.width() - 16, c.y()))
        p.drawLine(QPointF(c.x(), 16), QPointF(c.x(), self.height() - 16))
        f = QFont()
        f.setPointSizeF(8.5)
        p.setFont(f)
        p.setPen(QColor("#8FA3BC"))
        p.drawText(QRectF(20, 18, 200, 16), "head turned: left ← → right")
        for name, (yaw, pitch) in self.points.items():
            pt = self.to_px(yaw, pitch)
            p.setPen(QPen(QColor("#E8F2FF"), 2))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawRoundedRect(QRectF(pt.x() - 9, pt.y() - 6, 18, 12), 3, 3)
            p.setPen(QColor("#C3D0DF"))
            p.drawText(QRectF(pt.x() - 70, pt.y() + 8, 140, 16), Qt.AlignmentFlag.AlignHCenter, name)
        if self.current is not None:
            pt = self.to_px(*self.current)
            p.setPen(QPen(QColor(ACCENT_BRIGHT), 2))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawEllipse(pt, 14, 14)
            p.setBrush(QColor(ACCENT_BRIGHT))
            p.drawEllipse(pt, 5, 5)
        p.end()


class CalibratePage(Page):
    def __init__(self, app):
        super().__init__(
            "Calibrate gaze",
            "Look at the centre of each screen in turn so Curslink learns where it sits relative to your head. "
            "Linking pauses while the camera is in use here.",
        )
        self.app = app
        self.calibrator = None
        self.targets: list = []
        self.step = 0
        self.results: dict[str, tuple[float, float] | None] = {}
        self.was_linking = False

        self.agent_note = label("Gaze runs on the host, the computer with the camera and the keyboard and mouse. "
                                "Calibrate there.", "warn", wrap=True)
        self.add(self.agent_note)

        self.body = QWidget()
        row = QHBoxLayout(self.body)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(16)

        cam = Card()
        cam.setObjectName("darkCard")
        cam.setStyleSheet(f"QFrame#darkCard {{ background: {NAVY}; }}")
        self.plot = HeadPlot()
        cam.body.addWidget(self.plot, 1)
        self.readout = label("Camera off", "mono")
        self.readout.setStyleSheet("color: #C3D0DF;")
        cam.body.addWidget(self.readout)
        row.addWidget(cam, 1)

        steps = Card()
        self.step_title = label("", "small")
        self.prompt = label("", "h2", wrap=True)
        self.prompt.setStyleSheet("font-size: 20px; font-weight: 700;")
        self.prompt_detail = label("", "muted", wrap=True)
        for w in (self.step_title, self.prompt, self.prompt_detail):
            steps.body.addWidget(w)
        cam_row = QHBoxLayout()
        cam_row.addWidget(label("Camera", "small"))
        self.camera = QSpinBox()
        self.camera.setRange(0, 9)
        self.camera.setAccessibleName("Camera number")
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
        self.list_box = QVBoxLayout()
        self.list_box.setSpacing(6)
        steps.body.addLayout(self.list_box)
        steps.body.addStretch(1)
        row.addWidget(steps, 1)
        self.add(self.body)
        self.finish()

        self.timer = QTimer(self)
        self.timer.timeout.connect(self.tick)
        self.state = "idle"
        self.show_state()
        app.config_changed.connect(self.load_targets)

    def shown(self) -> None:
        if self.state == "idle":
            self.load_targets()

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
        self.camera.setValue(int(cfg.get("gaze", "camera", 0) or 0))
        names = {d.get("id"): d.get("name", d.get("id")) for d in cfg.devices()}
        self.targets = []
        for d in cfg.devices():
            for i, s in enumerate(d.get("screens", [])):
                sid = str(s.get("id", f"screen{i}"))
                self.targets.append((Target(d.get("id"), sid), f"{names[d.get('id')]} / {sid}"))
        self.results = {}
        self.plot.points = {}
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
        if self.was_linking:
            self.app.stop_linking()  # the host holds the camera
        cfg = self.app.config()
        if int(cfg.get("gaze", "camera", 0) or 0) != self.camera.value():
            cfg.set("gaze", "camera", self.camera.value())
            cfg.save()
        self.step = 0
        self.results = {}
        self.plot.points = {}
        self.state = "warming"
        self.show_state()

        def warm():
            cal = Calibrator(self.camera.value())
            cal.start()
            if not cal.wait_for_face(20):
                cal.stop()
                raise RuntimeError("No face found. Check the camera is free and you're facing it in good light.")
            return cal

        in_background(warm, self.warmed, self.failed)

    def warmed(self, cal) -> None:
        if self.state != "warming":  # cancelled meanwhile
            cal.stop()
            return
        self.calibrator = cal
        self.state = "ready"
        self.timer.start(80)
        self.show_state()

    def record(self) -> None:
        target, name = self.targets[self.step]
        self.state = "recording"
        self.show_state()
        cal = self.calibrator
        in_background(lambda: cal.record(target, 1.5), lambda centre: self.recorded(name, centre), self.failed)

    def recorded(self, name: str, centre) -> None:
        self.results[name] = centre
        if centre is not None:
            self.plot.points[name] = centre
        self.next_step()

    def skip(self) -> None:
        if self.state == "ready":
            self.results[self.targets[self.step][1]] = None
            self.next_step()

    def next_step(self) -> None:
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
        if self.calibrator is not None:
            cal, self.calibrator = self.calibrator, None
            in_background(cal.stop)

    def resume_linking(self, force: bool = False) -> None:
        if self.was_linking or (force and self.app.prefs.start_linking):
            QTimer.singleShot(800, self.app.start_linking)
        self.was_linking = False

    def tick(self) -> None:
        cal = self.calibrator
        if cal is None or cal.latest is None:
            return
        yaw, pitch = cal.latest
        self.plot.current = (yaw, pitch)
        self.plot.update()
        self.readout.setText(f"yaw {yaw:+.1f}°   pitch {pitch:+.1f}°   camera {self.camera.value()}")

    # -- drawing --------------------------------------------------------------
    def show_state(self) -> None:
        s, n = self.state, len(self.targets)
        busy = s in ("warming", "ready", "recording")
        self.camera_row.setVisible(not busy)
        self.cancel_button.setVisible(busy)
        self.secondary.setVisible(s == "ready")
        self.primary.setEnabled(s not in ("warming", "recording"))
        if s == "idle":
            self.step_title.setText(f"{n} screen{'s' * (n != 1)} to calibrate")
            self.prompt.setText("Sit how you normally do, facing the camera")
            self.prompt_detail.setText("You'll look at the centre of each screen and press Record. "
                                       "It takes about a minute.")
            self.primary.setText("Start")
            self.readout.setText("Camera off")
            self.plot.current = None
        elif s == "warming":
            self.step_title.setText("Starting the camera")
            self.prompt.setText("Looking for your face…")
            self.prompt_detail.setText("The first run downloads the face model (a few MB).")
            self.primary.setText("Record")
        elif s in ("ready", "recording"):
            name = self.targets[self.step][1]
            self.step_title.setText(f"Step {self.step + 1} of {n}")
            self.prompt.setText(f"Look at the centre of {name}")
            self.prompt_detail.setText("Keep looking for a moment…" if s == "recording"
                                       else "Then press Record and keep looking for a moment.")
            self.primary.setText("Recording…" if s == "recording" else "Record")
        elif s == "done":
            self.step_title.setText("Done")
            self.prompt.setText("Calibration saved")
            self.prompt_detail.setText(self.message + " Gaze switching is on.")
            self.primary.setText("Calibrate again")
        elif s == "error":
            self.step_title.setText("Calibration stopped")
            self.prompt.setText("That didn't work")
            self.prompt_detail.setText(self.message)
            self.primary.setText("Try again")

        clear_layout(self.list_box)
        for i, (_target, name) in enumerate(self.targets):
            if name in self.results:
                c = self.results[name]
                status, colour = (f"yaw {c[0]:+.1f}, pitch {c[1]:+.1f}", "#1A6B48") if c else ("skipped", "#8A5200")
            elif busy and i == self.step:
                status, colour = "now", "#12599E"
            else:
                status, colour = "next", "#556173"
            row = QWidget()
            row.setStyleSheet("background: #F3F5F8; border-radius: 8px;")
            h = QHBoxLayout(row)
            h.setContentsMargins(12, 8, 12, 8)
            t = label(name)
            t.setStyleSheet("font-weight: 600; background: transparent;")
            h.addWidget(t, 1)
            v = label(status, "mono")
            v.setStyleSheet(f"color: {colour}; background: transparent;")
            h.addWidget(v)
            self.list_box.addWidget(row)
        self.plot.update()
