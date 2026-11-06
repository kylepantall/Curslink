"""The macOS permissions card: shown in setup and at the top of Status while
Curslink is missing a permission it needs (see curslink.macperms)."""

from __future__ import annotations

import logging

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QHBoxLayout, QVBoxLayout

from curslink import macperms
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
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.refresh)
        self.refresh()

    def refresh(self) -> None:
        role = self.app.role
        now = macperms.missing(role) if self.app.has_config() else []
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
            self.timer.start(2000)
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
                         button("Allow…", "primary", lambda _=False, k=key: macperms.request(k)))
