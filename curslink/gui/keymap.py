"""Key mapping dialogs.

* :class:`PairMapDialog` (host): what each key becomes on its way to another
  computer, per pair of systems (``mac>windows``) or for one device.
* :class:`KeyboardCheckDialog` (agent): "My keyboard is set up wrong". It
  listens to the keys the host sends (the running agent reports them instead
  of typing them, see ``keys learn on`` in :mod:`curslink.events`) and works
  out fixes two ways: a guided check that asks for keys this system has, and
  a teach mode where you press a key on the shared keyboard and then the key
  it should be on this computer's own keyboard. Fixes are saved here, in
  ``[agent].modifier_map``.
"""

from __future__ import annotations

import sys

from PySide6.QtCore import Qt
from PySide6.QtGui import QKeyEvent
from PySide6.QtWidgets import (
    QComboBox, QDialog, QDialogButtonBox, QGridLayout, QHBoxLayout, QVBoxLayout, QWidget,
)

from curslink.inputs.keys import (
    KEY_LABELS, OS_KINDS, OS_LABELS, family, guided_steps, key_label, learned_fix, os_kind, pair_key,
    preset_map,
)
from curslink.gui.widgets import button, clear_layout, label

# Qt key -> key name on the wire (pynput's). Ctrl and Meta are filled in by
# qt_key_name: on a Mac Qt calls Cmd "Control" and Control "Meta".
_QT_KEYS = {
    Qt.Key.Key_Escape: "esc", Qt.Key.Key_Tab: "tab", Qt.Key.Key_Backtab: "tab",
    Qt.Key.Key_Backspace: "backspace", Qt.Key.Key_Return: "enter", Qt.Key.Key_Enter: "enter",
    Qt.Key.Key_Insert: "insert", Qt.Key.Key_Delete: "delete", Qt.Key.Key_Pause: "pause",
    Qt.Key.Key_Print: "print_screen", Qt.Key.Key_Home: "home", Qt.Key.Key_End: "end",
    Qt.Key.Key_Left: "left", Qt.Key.Key_Up: "up", Qt.Key.Key_Right: "right", Qt.Key.Key_Down: "down",
    Qt.Key.Key_PageUp: "page_up", Qt.Key.Key_PageDown: "page_down", Qt.Key.Key_Shift: "shift",
    Qt.Key.Key_Alt: "alt", Qt.Key.Key_AltGr: "alt_gr", Qt.Key.Key_CapsLock: "caps_lock",
    Qt.Key.Key_NumLock: "num_lock", Qt.Key.Key_ScrollLock: "scroll_lock", Qt.Key.Key_Menu: "menu",
    Qt.Key.Key_Super_L: "cmd_l", Qt.Key.Key_Super_R: "cmd_r", Qt.Key.Key_Space: "space",
    **{getattr(Qt.Key, f"Key_F{n}"): f"f{n}" for n in range(1, 21)},
}


def qt_key_name(event: QKeyEvent, mac: bool = sys.platform == "darwin") -> str | None:
    """A key pressed on this computer, named the way the host sends keys."""
    key = event.key()
    if key == Qt.Key.Key_Control:
        return "cmd" if mac else "ctrl"
    if key == Qt.Key.Key_Meta:
        return "ctrl" if mac else "cmd"
    if key in _QT_KEYS:
        return _QT_KEYS[key]
    if Qt.Key.Key_A <= key <= Qt.Key.Key_Z or Qt.Key.Key_0 <= key <= Qt.Key.Key_9:
        return chr(key).lower()
    text = event.text()
    if len(text) == 1 and text.isprintable():
        return text.lower()
    vk = event.nativeVirtualKey()
    return f"vk:{vk}" if vk else None


def this_kind() -> str:
    return os_kind(sys.platform)


class KeyPicker(QComboBox):
    """A key: pick a common one or type its name."""

    def __init__(self, name: str = "", kind: str = ""):
        super().__init__()
        self.setEditable(True)
        self.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self.setMinimumWidth(180)
        for key in KEY_LABELS:
            self.addItem(key_label(key, kind), key)
        self.set_key(name, kind)

    def set_key(self, name: str, kind: str = "") -> None:
        i = self.findData(name)
        if i >= 0:
            self.setCurrentIndex(i)
        else:
            self.setEditText(name)

    def key(self) -> str:
        text = self.currentText().strip()
        i = self.findText(text)
        if i >= 0:
            return str(self.itemData(i))
        return text if len(text) == 1 else text.lower().replace(" ", "_")


class MapRows(QWidget):
    """Rows of "this key -> that key", each with a remove button."""

    def __init__(self, src_kind: str = "", dst_kind: str = ""):
        super().__init__()
        self.src_kind, self.dst_kind = src_kind, dst_kind
        self.rows: list[tuple[KeyPicker, KeyPicker, QWidget]] = []
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        self.grid = QGridLayout()
        self.grid.setHorizontalSpacing(10)
        outer.addLayout(self.grid)
        self.empty = label("No keys change.", "small")
        outer.addWidget(self.empty)
        add = QHBoxLayout()
        self.add_button = button("Add a key", None, lambda: self.add_row("", ""))
        add.addWidget(self.add_button)
        add.addStretch(1)
        outer.addLayout(add)

    def set_mapping(self, mapping: dict[str, str]) -> None:
        clear_layout(self.grid)
        self.rows = []
        for src, dst in mapping.items():
            self.add_row(src, dst)
        self._relabel()

    def add_row(self, src: str, dst: str) -> None:
        a, b = KeyPicker(src, self.src_kind), KeyPicker(dst, self.dst_kind)
        remove = button("Remove")
        n = len(self.rows)
        self.grid.addWidget(a, n, 0)
        self.grid.addWidget(label("becomes"), n, 1)
        self.grid.addWidget(b, n, 2)
        self.grid.addWidget(remove, n, 3)
        entry = (a, b, remove)
        remove.clicked.connect(lambda: self.remove_row(entry))
        self.rows.append(entry)
        self._relabel()

    def remove_row(self, entry) -> None:
        self.set_mapping({a.key(): b.key() for a, b, r in self.rows if (a, b, r) != entry and a.key()})

    def mapping(self) -> dict[str, str]:
        out = {}
        for a, b, _ in self.rows:
            if a.key() and b.key():
                out[a.key()] = b.key()
        return out

    def _relabel(self) -> None:
        self.empty.setVisible(not self.rows)


class PairMapDialog(QDialog):
    """Host: the key mapping for each pair of computers."""

    def __init__(self, config, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Key mappings")
        self.config = config
        raw = config.raw()
        me = raw.get("host", {}).get("device_id")
        self.presets = bool(raw.get("host", {}).get("swap_cmd_ctrl", True))
        self.changes: dict[str, dict[str, str] | None] = {}
        v = QVBoxLayout(self)
        v.addWidget(label("Choose a pair, then the keys that change when you type from the first into the "
                          "second. Pairs you leave alone use the built-in mapping.", "small", wrap=True))
        pick = QHBoxLayout()
        self.src = QComboBox()
        self.dst = QComboBox()
        for kind in OS_KINDS:
            if kind != "android":  # a phone doesn't share its keyboard
                self.src.addItem(OS_LABELS[kind], kind)
            self.dst.addItem(OS_LABELS[kind], kind)
        for d in raw.get("devices", []):
            if d.get("id") and d.get("id") != me:
                self.dst.addItem(f"Only {d.get('name', d['id'])}", f"device:{d['id']}")
        here = this_kind()
        self.src.setCurrentIndex(max(0, self.src.findData(here)))
        self.dst.setCurrentIndex(max(0, self.dst.findData("windows" if here == "mac" else "mac")))
        pick.addWidget(label("From"))
        pick.addWidget(self.src)
        pick.addWidget(label("to"))
        pick.addWidget(self.dst)
        pick.addStretch(1)
        v.addLayout(pick)
        self.note = label("", "small", wrap=True)
        v.addWidget(self.note)
        self.rows = MapRows()
        v.addWidget(self.rows)
        actions = QHBoxLayout()
        self.reset_button = button("Use the built-in mapping", None, self.reset)
        actions.addWidget(self.reset_button)
        actions.addStretch(1)
        v.addLayout(actions)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.save)
        buttons.rejected.connect(self.reject)
        v.addWidget(buttons)
        self._pair = self.pair()
        self.src.currentIndexChanged.connect(self.switch_pair)
        self.dst.currentIndexChanged.connect(self.switch_pair)
        self.show_pair()

    def pair(self) -> str:
        dst = str(self.dst.currentData())
        return dst if dst.startswith("device:") else pair_key(str(self.src.currentData()), dst)

    def stored(self, name: str) -> dict[str, str] | None:
        return self.changes[name] if name in self.changes else self.config.keymap(name)

    def built_in(self) -> dict[str, str]:
        dst = str(self.dst.currentData())
        if dst.startswith("device:"):
            return {}
        return preset_map(str(self.src.currentData()), dst) if self.presets else {}

    def keep_edits(self) -> None:
        """Remember the rows of the pair on screen before showing another."""
        mapping = self.rows.mapping()
        current = self.stored(self._pair)
        if current is not None or mapping != self._shown:
            self.changes[self._pair] = mapping

    def switch_pair(self) -> None:
        self.keep_edits()
        self._pair = self.pair()
        self.show_pair()

    def show_pair(self) -> None:
        dst = str(self.dst.currentData())
        self.src.setEnabled(not dst.startswith("device:"))
        src_kind = str(self.src.currentData())
        self.rows.src_kind, self.rows.dst_kind = src_kind, ("" if dst.startswith("device:") else dst)
        custom = self.stored(self._pair)
        mapping = custom if custom is not None else self.built_in()
        self._shown = dict(mapping)
        self.rows.set_mapping(mapping)
        if dst.startswith("device:"):
            self.note.setText("Keys for this device only: they replace the mapping for its system.")
        elif custom is not None:
            self.note.setText("Your own mapping for this pair.")
        elif mapping:
            self.note.setText("The built-in mapping: Cmd and Ctrl trade places, so shortcuts stay under the "
                              "same finger. Change it here, or turn it off in Settings.")
        else:
            self.note.setText("The built-in mapping changes no keys for this pair.")
        self.reset_button.setEnabled(custom is not None)

    def reset(self) -> None:
        self.changes[self._pair] = None
        self.show_pair()

    def save(self) -> None:
        self.keep_edits()
        for name, mapping in self.changes.items():
            self.config.set_keymap(name, mapping)
        self.accept()


class KeyboardCheckDialog(QDialog):
    """Agent: find and fix keys that arrive wrong from the host's keyboard."""

    def __init__(self, app, parent=None):
        super().__init__(parent)
        self.app = app
        self.kind = this_kind()
        self.setWindowTitle("Fix my keyboard")
        self.setMinimumWidth(520)
        self.fixes: dict[str, str] = dict(app.config().get("agent", "modifier_map", {}) or {})
        self.steps = guided_steps(self.kind)
        self.step = -1           # guided check: which step; -1 = not running
        self.teach_sent: str | None = None
        self.mode = ""           # "", "check" or "teach"
        v = QVBoxLayout(self)
        v.addWidget(label(
            f"Use the keyboard shared from your other computer, with this {OS_LABELS.get(self.kind, 'computer')} "
            "in control (move the pointer here first). While this is open its keys are only listened to, "
            "not typed.", "small", wrap=True))
        self.prompt = label("", "h2", wrap=True)
        v.addWidget(self.prompt)
        self.detail = label("", "small", wrap=True)
        v.addWidget(self.detail)
        go = QHBoxLayout()
        self.check_button = button("Check my keys", "primary", self.start_check)
        self.teach_button = button("Teach one key", None, self.start_teach)
        self.skip_button = button("Skip", None, self.skip)
        go.addWidget(self.check_button)
        go.addWidget(self.teach_button)
        go.addWidget(self.skip_button)
        go.addStretch(1)
        v.addLayout(go)
        v.addWidget(label("Fixes saved on this computer", "small"))
        self.rows = MapRows(dst_kind=self.kind)
        v.addWidget(self.rows)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.save)
        buttons.rejected.connect(self.reject)
        v.addWidget(buttons)
        self.rows.set_mapping(self.fixes)
        self.set_mode("")
        app.service.event.connect(self.on_event)
        app.service.send("keys learn on")
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

    # -- modes ----------------------------------------------------------------
    def set_mode(self, mode: str) -> None:
        self.mode = mode
        self.skip_button.setVisible(mode == "check")
        running = self.app.service.is_alive()
        self.check_button.setEnabled(running)
        self.teach_button.setEnabled(running)
        if not running:
            self.prompt.setText("Start linking first")
            self.detail.setText("Curslink needs to be running and connected to your host to hear its keyboard.")
        elif mode == "":
            self.prompt.setText("What's wrong?")
            self.detail.setText("Check my keys asks for a few keys this computer has and fixes any that arrive "
                                "as something else. Teach one key: press it on the shared keyboard, then the "
                                "key it should be on this computer's own keyboard.")

    def start_check(self) -> None:
        self.mode, self.step = "check", 0
        self.set_mode("check")
        self.show_step()

    def show_step(self) -> None:
        if self.step >= len(self.steps):
            self.step = -1
            self.set_mode("")
            self.prompt.setText("All done")
            self.detail.setText("Save to keep the fixes below.")
            return
        _, ask = self.steps[self.step]
        self.prompt.setText(ask)
        self.detail.setText(f"Step {self.step + 1} of {len(self.steps)}, on the shared keyboard.")

    def skip(self) -> None:
        if self.mode == "check":
            self.step += 1
            self.show_step()

    def start_teach(self) -> None:
        self.teach_sent = None
        self.set_mode("teach")
        self.prompt.setText("Press the key on the shared keyboard")
        self.detail.setText("The one that does the wrong thing here.")
        self.setFocus()

    # -- keys -----------------------------------------------------------------
    def on_event(self, event: dict) -> None:
        if event.get("event") != "key_learned" or not event.get("pressed"):
            return
        self.heard(str(event.get("key", "")))

    def heard(self, sent: str) -> None:
        """A key pressed on the shared keyboard, as the host sent it."""
        if not sent:
            return
        if self.mode == "check" and 0 <= self.step < len(self.steps):
            wanted, _ = self.steps[self.step]
            self.add_fix(sent, wanted)
            self.step += 1
            self.show_step()
        elif self.mode == "teach" and self.teach_sent is None:
            self.teach_sent = sent
            self.prompt.setText("Now press the key it should be, on this computer's own keyboard")
            self.detail.setText(f"The shared keyboard sent {key_label(sent, self.kind)}.")

    def keyPressEvent(self, event: QKeyEvent) -> None:  # noqa: N802 - Qt
        # The host's keys aren't typed while this is open, so a key here is
        # one pressed on this computer's own keyboard.
        if self.mode == "teach" and self.teach_sent is not None and not event.isAutoRepeat():
            local = qt_key_name(event)
            if local:
                self.taught(local)
            return
        if event.key() == Qt.Key.Key_Escape and self.mode:
            self.set_mode("")
            return
        super().keyPressEvent(event)

    def taught(self, local: str) -> None:
        sent, self.teach_sent = self.teach_sent or "", None
        changed = self.add_fix(sent, local)
        self.set_mode("")
        self.prompt.setText("Got it" if changed else "That key already arrives right")
        self.detail.setText(f"{key_label(sent, self.kind)} from the shared keyboard now types "
                            f"{key_label(local, self.kind)} here. Save to keep it." if changed else "")

    def add_fix(self, sent: str, wanted: str) -> bool:
        fix = learned_fix(sent, wanted)
        mapping = self.rows.mapping()
        if fix is None:
            # Arrives right: drop an old fix for it that the check just proved wrong.
            if mapping.pop(family(sent), None) is not None or mapping.pop(sent, None) is not None:
                self.rows.set_mapping(mapping)
            return False
        mapping[fix[0]] = fix[1]
        self.rows.set_mapping(mapping)
        return True

    # -- done -----------------------------------------------------------------
    def save(self) -> None:
        cfg = self.app.config()
        cfg.set_modifier_map(self.rows.mapping())
        cfg.save()
        self.accept()
        self.app.config_saved(restart=True)

    def done(self, result: int) -> None:  # noqa: D401 - Qt
        try:
            self.app.service.event.disconnect(self.on_event)
        except (RuntimeError, TypeError):
            pass
        if self.app.service.is_alive():
            self.app.service.send("keys learn off")
        super().done(result)
