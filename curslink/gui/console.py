"""The debug console: every log line and status event, live, one JSON object
per line (the same shape as the ``.jsonl`` logs and the diagnostics file, see
:func:`curslink.diagnostics.record_dict`), so a copied chunk can be pasted
into a bug report and parsed.

:class:`DebugFeed` collects from the start: the app's own log records and
everything the host or agent service sends. :class:`DebugConsole` shows it,
filtered with the same terms as ``curslink diag show``.
"""

from __future__ import annotations

import collections
import logging
import time
from pathlib import Path

from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtGui import QFont, QFontDatabase, QGuiApplication
from PySide6.QtWidgets import (QCheckBox, QComboBox, QFileDialog, QHBoxLayout, QLabel, QLineEdit, QPlainTextEdit,
                               QPushButton, QVBoxLayout, QWidget)

from curslink import diagnostics

KEEP = 5000
# Sent every couple of seconds: shown only with "Network samples" ticked.
NOISY = {"network", "netstats"}
LEVEL_CHOICES = [("Everything", None), ("Info and up", "info"), ("Warnings and errors", "warning")]


class DebugFeed(QObject):
    """The last :data:`KEEP` records, from the app and its service."""

    added = Signal(dict)
    _incoming = Signal(dict)  # from any thread; handled on the Qt thread

    def __init__(self, parent: QObject | None = None):
        super().__init__(parent)
        self.records: collections.deque[dict] = collections.deque(maxlen=KEEP)
        self._incoming.connect(self._add, Qt.ConnectionType.QueuedConnection)
        self.handler = _FeedHandler(self)
        logging.getLogger().addHandler(self.handler)

    def close(self) -> None:
        logging.getLogger().removeHandler(self.handler)

    def add(self, rec: dict) -> None:
        """Safe from any thread."""
        self._incoming.emit(rec)

    def _add(self, rec: dict) -> None:
        self.records.append(rec)
        self.added.emit(rec)

    def service_event(self, event: dict, role: str | None = None) -> None:
        """One event from the service: a log line arrives as its record, any
        other event as an ``event`` record."""
        kind = event.get("event")
        if kind == "log":
            rec = event.get("rec")
            if not isinstance(rec, dict):  # an older service
                rec = {"type": "log", "ts": diagnostics.utc_stamp(time.time()), "proc": role,
                       "lvl": event.get("level", "info"), "sub": diagnostics.subsystem(str(event.get("logger", ""))),
                       "msg": event.get("message", "")}
        else:
            rec = {"type": "event", "ts": diagnostics.utc_stamp(time.time()), "device": diagnostics.device_name(),
                   "role": role, "proc": role, "event": kind,
                   "fields": {k: v for k, v in event.items() if k != "event"}}
        self._add(rec)


class _FeedHandler(logging.Handler):
    def __init__(self, feed: DebugFeed):
        super().__init__(logging.DEBUG if diagnostics.debug_wanted() else logging.INFO)
        self.feed = feed

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.feed.add(diagnostics.record_dict(record))
        except Exception:  # pragma: no cover - logging mustn't break the app
            pass


def shown(rec: dict, terms: list[str], level: str | None, noisy: bool) -> bool:
    if not noisy and rec.get("type") == "event" and rec.get("event") in NOISY:
        return False
    return diagnostics.matches(rec, terms, level)


class DebugConsole(QWidget):
    """A window of JSON lines with a filter."""

    def __init__(self, feed: DebugFeed, save_bundle=None, parent: QWidget | None = None):
        super().__init__(parent, Qt.WindowType.Window)
        self.feed = feed
        self.save_bundle = save_bundle
        self.setWindowTitle("Curslink debug console")
        self.resize(980, 560)

        self.filter = QLineEdit()
        self.filter.setPlaceholderText("Filter: words, key=value (sub=net, lvl=warn, fields.to=laptop), -word to hide")
        self.filter.setClearButtonEnabled(True)
        self.level = QComboBox()
        for text, _ in LEVEL_CHOICES:
            self.level.addItem(text)
        self.level.setCurrentIndex(1)
        self.noisy = QCheckBox("Network samples")
        self.pause = QCheckBox("Pause")
        top = QHBoxLayout()
        top.addWidget(self.filter, 1)
        top.addWidget(self.level)
        top.addWidget(self.noisy)
        top.addWidget(self.pause)

        self.view = QPlainTextEdit()
        self.view.setReadOnly(True)
        self.view.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self.view.setMaximumBlockCount(KEEP)
        mono = QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont)
        mono.setStyleHint(QFont.StyleHint.Monospace)
        self.view.setFont(mono)

        self.count = QLabel("")
        copy = QPushButton("Copy shown")
        copy.clicked.connect(self.copy_shown)
        save = QPushButton("Save diagnostics file…")
        save.clicked.connect(self._save)
        save.setVisible(save_bundle is not None)
        clear = QPushButton("Clear")
        clear.clicked.connect(self.clear)
        bottom = QHBoxLayout()
        bottom.addWidget(self.count, 1)
        bottom.addWidget(clear)
        bottom.addWidget(copy)
        bottom.addWidget(save)

        layout = QVBoxLayout(self)
        layout.addLayout(top)
        layout.addWidget(self.view, 1)
        layout.addLayout(bottom)

        self.filter.textChanged.connect(self.refill)
        self.level.currentIndexChanged.connect(self.refill)
        self.noisy.toggled.connect(self.refill)
        self.pause.toggled.connect(lambda paused: None if paused else self.refill())
        feed.added.connect(self._on_added)
        self._cleared_ts = ""  # records up to this time were cleared from view
        self._total = 0
        self.refill()

    # -- filtering -------------------------------------------------------------
    def _terms(self) -> list[str]:
        return self.filter.text().split()

    def _level(self) -> str | None:
        return LEVEL_CHOICES[self.level.currentIndex()][1]

    def _passes(self, rec: dict) -> bool:
        return shown(rec, self._terms(), self._level(), self.noisy.isChecked())

    def visible_records(self) -> list[dict]:
        return [r for r in self.feed.records if r.get("ts", "") > self._cleared_ts and self._passes(r)]

    def refill(self) -> None:
        records = self.visible_records()
        self.view.setPlainText("\n".join(diagnostics.dumps(r) for r in records))
        self._total = len(records)
        self._show_count()
        self.view.verticalScrollBar().setValue(self.view.verticalScrollBar().maximum())

    def _on_added(self, rec: dict) -> None:
        if self.pause.isChecked() or not self.isVisible() or not self._passes(rec):
            return
        bar = self.view.verticalScrollBar()
        at_end = bar.value() >= bar.maximum() - 4
        self.view.appendPlainText(diagnostics.dumps(rec))
        self._total = min(self._total + 1, KEEP)
        self._show_count()
        if at_end:
            bar.setValue(bar.maximum())

    def _show_count(self) -> None:
        self.count.setText(f"{self._total} line{'s' * (self._total != 1)} shown of {len(self.feed.records)}")

    def showEvent(self, event) -> None:  # noqa: N802 - Qt
        super().showEvent(event)
        self.refill()

    # -- buttons ---------------------------------------------------------------
    def copy_shown(self) -> None:
        QGuiApplication.clipboard().setText(self.view.toPlainText() + "\n")

    def clear(self) -> None:
        self._cleared_ts = diagnostics.utc_stamp(time.time())
        self.refill()

    def _save(self) -> None:
        start = str(Path.home() / "Desktop" / diagnostics.bundle_name())
        path, _ = QFileDialog.getSaveFileName(self, "Save diagnostics", start, "JSON lines (*.jsonl)")
        if path and self.save_bundle is not None:
            self.save_bundle(Path(path))
