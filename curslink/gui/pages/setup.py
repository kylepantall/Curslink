"""First run (or "set this computer up again"): host or agent, and a name."""

from __future__ import annotations

import shutil

from PySide6.QtCore import QSize
from PySide6.QtWidgets import QButtonGroup, QHBoxLayout, QLineEdit

from curslink.configedit import new_agent_config, new_host_config, slug, this_computer_name
from curslink.gui.widgets import Card, Page, button, label, spot, svg_pixmap


class SetupPage(Page):
    def __init__(self, app):
        super().__init__("Set up Curslink",
                         "Curslink moves one keyboard and mouse between your computers. "
                         "Tell it what this computer does.")
        self.app = app
        self.header.insertWidget(0, spot("welcome", 120, 90))

        role_card = Card("What does this computer do?")
        choices = QHBoxLayout()
        self.roles = QButtonGroup(self)
        self.host_choice = button("Host\nThe keyboard and mouse\nare plugged in here", "choice")
        self.agent_choice = button("Agent\nAnother computer's keyboard\nand mouse control this one", "choice")
        for b, art in ((self.host_choice, "role-host"), (self.agent_choice, "role-agent")):
            b.setIcon(svg_pixmap(f"illustrations/spot/{art}.svg", 72, 54))
            b.setIconSize(QSize(72, 54))
            b.setCheckable(True)
            self.roles.addButton(b)
            choices.addWidget(b, 1)
        self.host_choice.setChecked(True)
        self.roles.buttonClicked.connect(lambda _b: self.refresh())
        role_card.body.addLayout(choices)
        self.add(role_card)

        name_card = Card("Name")
        self.name = QLineEdit(this_computer_name())
        self.name.setAccessibleName("This computer's name")
        self.name.setMaximumWidth(380)
        name_card.body.addWidget(label("The name your other devices see when they pair with this one.", "muted"))
        name_card.body.addWidget(self.name)
        self.add(name_card)

        self.screens_card = Card("Screens")
        self.screens_text = label("", "muted", wrap=True)
        self.screens_card.body.addWidget(self.screens_text)
        self.add(self.screens_card)

        self.replace_note = label("", "warn", wrap=True)
        self.add(self.replace_note)

        row = QHBoxLayout()
        row.addStretch(1)
        self.cancel = button("Cancel", None, lambda: app.show_window("settings"))
        row.addWidget(self.cancel)
        row.addWidget(button("Set up", "primary", self.create))
        self.layout_.addLayout(row)
        self.finish()

    def shown(self) -> None:
        self.refresh()

    def refresh(self) -> None:
        from curslink.gui.pages.layout import detect_screens

        exists = self.app.has_config()
        self.cancel.setVisible(exists)
        self.replace_note.setVisible(exists)
        if exists:
            self.replace_note.setText(
                f"This replaces {self.app.config_path}. The old file is kept beside it as "
                f"{self.app.config_path.name}.bak. Pairings are kept.")
        host = self.host_choice.isChecked()
        self.screens_card.setVisible(host)
        if host:
            screens = detect_screens()
            sizes = ", ".join(f"{s['width']}×{s['height']}" for s in screens) or "none found"
            self.screens_text.setText(
                f"Found {len(screens)} screen{'s' * (len(screens) != 1)} here ({sizes}). After pairing, place your "
                "other devices' screens beside them on the Layout page.")

    def create(self) -> None:
        from curslink.gui.pages.layout import detect_screens

        name = self.name.text().strip() or this_computer_name()
        device_id = slug(name)
        path = self.app.config_path
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            shutil.copy2(path, path.with_name(path.name + ".bak"))
        was_linking = self.app.linking
        if was_linking:
            self.app.stop_linking()
        if self.host_choice.isChecked():
            screens = detect_screens() or [{"id": "main", "x": 0, "y": 0, "width": 1920, "height": 1080}]
            text = new_host_config(device_id, name, screens)
        else:
            text = new_agent_config(device_id, name)
        path.write_text(text, encoding="utf-8")
        self.app.config_saved(restart=False)
        self.app.window.show_page("status" if self.app.role == "host" else "devices")
        if self.app.prefs.start_linking or was_linking:
            self.app.start_linking()
