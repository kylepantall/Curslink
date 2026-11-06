"""The macOS permissions card: shown in setup and at the top of Status while
Curslink is missing a permission it needs (see curslink.macperms)."""

from __future__ import annotations

import logging

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QHBoxLayout, QVBoxLayout

from curslink import macperms
from curslink.gui.common import in_background
from curslink.gui.widgets import Card, button, label, pill, spot, text_block

log = logging.getLogger(__name__)


class PermissionsCard(Card):
    """Lists what's missing with an Allow button each, and re-checks every
    couple of seconds, because the answer comes from System Settings."""

    def __init__(self, app):
        super().__init__(rows=True)
        self.setObjectName("accentCard")
        self.app = app
        head = QHBoxLayout()
        head.setContentsMargins(18, 14, 18, 6)
        head.setSpacing(16)
        head.addWidget(spot("not-linking", 96, 72))
        words = QVBoxLayout()
        words.setSpacing(4)
        words.addWidget(label("macOS needs your OK", "h2"))
        self.intro = label("", "muted", wrap=True)
        words.addWidget(self.intro)
        head.addLayout(words, 1)
        self.body.insertLayout(0, head)
        self._missing: list[str] = []
        self._checking = False
        self.asked = False  # pressed Allow at least once
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.check)
        # First answer from this process (instant); after that each check runs
        # in a new process, because macOS keeps a process's first answer.
        self.apply(macperms.missing(app.role) if app.has_config() else [])

    def refresh(self) -> None:
        self.check()

    def check(self) -> None:
        if self._checking or not self.app.has_config():
            return
        if not macperms.needed(self.app.role):
            self.apply([])
            return
        self._checking = True
        role = self.app.role

        def done(now):
            self._checking = False
            self.apply(now)

        def failed(_exc):
            self._checking = False

        in_background(lambda: macperms.missing_fresh(role), done, failed, name="curslink-macperms")

    def apply(self, now: list[str]) -> None:
        if now != self._missing:
            granted = [k for k in self._missing if k not in now]
            self._missing = now
            self.draw()
            if granted:
                log.info("permission granted: %s", ", ".join(granted))
                if self.app.linking:
                    # The service checks permissions when it starts.
                    self.app.service.restart()
        self.setVisible(bool(now))
        if now and not self.timer.isActive():
            self.timer.start(3000)
        elif not now:
            self.timer.stop()

    def draw(self) -> None:
        self.clear_rows()
        n = len(self._missing)
        self.intro.setText(
            f"Curslink needs {'these permissions' if n > 1 else 'this permission'} to work. Press Allow, "
            "switch Curslink on in System Settings, and this card goes away by itself.")
        for key in self._missing:
            perm = macperms.PERMISSIONS[key]
            self.add_row(text_block(perm.title, perm.why), pill("Needed", "wait"),
                         button("Allow…", "primary", lambda _=False, k=key: self.allow(k)))
        self.restart_row = self.add_row(
            text_block("Already allowed?",
                       "macOS sometimes only applies these after Curslink restarts. Still asking? "
                       "Select Curslink in System Settings, remove it with −, and allow it again: "
                       "after an update macOS can keep showing an old switch that no longer applies."),
            button("Restart Curslink", None, self.app.restart))
        self.restart_row.setVisible(self.asked)

    def allow(self, key: str) -> None:
        macperms.request(key)
        self.asked = True
        self.restart_row.setVisible(True)
        self.check()
