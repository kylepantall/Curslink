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
from curslink.gui import theme
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


# The drawn keyboard: rows of (key name, width in key units).
_ROWS = [
    [("`", 1), *[(c, 1) for c in "1234567890-="], ("backspace", 2)],
    [("tab", 1.5), *[(c, 1) for c in "qwertyuiop[]"], ("\\", 1.5)],
    [("caps_lock", 1.8), *[(c, 1) for c in "asdfghjkl;'"], ("enter", 2.2)],
    [("shift_l", 2.4), *[(c, 1) for c in "zxcvbnm,./"], ("shift_r", 2.6)],
]
_BOTTOM = {
    "windows": [("ctrl_l", 1.5), ("cmd_l", 1.3), ("alt_l", 1.3), ("space", 6), ("alt_gr", 1.3), ("cmd_r", 1.3),
                ("menu", 1.3), ("ctrl_r", 1.5)],
    "mac": [("fn", 1.1), ("ctrl_l", 1.3), ("alt_l", 1.3), ("cmd_l", 1.6), ("space", 6), ("cmd_r", 1.6), ("alt_r", 1.3)],
    "linux": [("ctrl_l", 1.5), ("cmd_l", 1.3), ("alt_l", 1.3), ("space", 6), ("alt_gr", 1.3), ("menu", 1.3),
              ("ctrl_r", 1.5)],
}
_SHORT = {"Windows key": "Win", "Backspace": "⌫", "Caps Lock": "Caps", "Control": "Ctrl", "Option": "Opt"}


class KeyboardView(QWidget):
    """A keyboard for one system, drawn, with keys lit: the one to press
    (``target``, a family), the last one heard (``hit``: name, arrived right)
    and taught ones."""

    def __init__(self, kind: str, compact: bool = False):
        super().__init__()
        self.kind = kind if kind in _BOTTOM else "windows"
        self.compact = compact
        self.target = ""
        self.hit: tuple[str, bool] | None = None
        self.taught = ""
        self.dimmed = False
        self.setMinimumHeight(150 if compact else 210)

    def rows(self) -> list[list[tuple[str, float]]]:
        return [*_ROWS, _BOTTOM[self.kind]]

    def show_keys(self, target: str = "", hit: tuple[str, bool] | None = None, taught: str = "") -> None:
        self.target, self.hit, self.taught = target, hit, taught
        self.update()

    def caption(self, key: str) -> str:
        if key == "space":
            return ""
        if key == "fn":
            return "fn"
        text = key_label(key, self.kind).replace(" (left)", "").replace(" (right)", "")
        if self.compact:
            return {"AltGr": "Gr", "Menu": "☰", "Enter": "↵", "Tab": "⇥"}.get(text, _SHORT.get(text, text))
        return _SHORT.get(text, text) if len(text) > 8 else text

    def state(self, key: str) -> str:
        fam = family(key) if key != "alt_gr" else "alt"
        if self.target and fam == self.target and not key.endswith("_r") and key != "alt_gr":
            return "target"
        if self.hit is not None and self.hit[0] == key:
            return "good" if self.hit[1] else "warn"
        if self.taught and fam == self.taught:
            return "taught"
        return ""

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt
        from PySide6.QtCore import QRectF
        from PySide6.QtGui import QFont, QPainter, QPen

        from curslink.gui import theme

        t = theme.current()
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        if self.dimmed:
            p.setOpacity(0.45)
        rows = self.rows()
        gap = 4.0 if self.compact else 5.0
        pad = 8.0
        w, h = self.width() - 2 * pad, self.height() - 2 * pad
        key_h = (h - gap * (len(rows) - 1)) / len(rows)
        p.setPen(QPen(t.solid("stroke"), 1))
        p.setBrush(t.solid("card2"))
        p.drawRoundedRect(QRectF(0.5, 0.5, self.width() - 1, self.height() - 1), 10, 10)
        font = QFont(self.font())
        font.setPixelSize(10 if self.compact else 11)
        p.setFont(font)
        colours = {
            "": (t.solid("card"), t.solid("stroke"), t.solid("ink")),
            "target": (t.solid("accent", "card"), t.solid("accent"), t.solid("accent_ink")),
            "good": (t.solid("good_bg"), t.solid("good"), t.solid("good")),
            "warn": (t.solid("warn_bg"), t.solid("warn"), t.solid("warn")),
            "taught": (t.solid("accent"), t.solid("accent"), t.solid("accent_ink")),
        }
        y = pad
        for row in rows:
            units = sum(width for _, width in row)
            unit = (w - gap * (len(row) - 1)) / units
            x = pad
            for key, width in row:
                kw = unit * width
                fill, edge, ink = colours[self.state(key)]
                p.setPen(QPen(edge, 1.5 if self.state(key) else 1))
                p.setBrush(fill)
                p.drawRoundedRect(QRectF(x, y, kw, key_h), 5, 5)
                p.setPen(ink)
                p.drawText(QRectF(x + 2, y, kw - 4, key_h), Qt.AlignmentFlag.AlignCenter, self.caption(key))
                x += kw + gap
            y += key_h + gap
        p.end()


class KeyboardCheckDialog(QDialog):
    """Agent: "My keyboard is set up wrong". A step-by-step window: get
    ready, check my keys (the key to press lights up on a drawn keyboard for
    this system), teach a key (shared keyboard, then this computer's own),
    review and save. Fixes are saved on this computer."""

    STEPS = (("Get ready", "Pointer on this device"), ("Check my keys", ""),
             ("Teach a key", "Optional"), ("Review and save", "Saved on this device"))

    def __init__(self, app, parent=None):
        from PySide6.QtWidgets import QFrame, QStackedWidget

        super().__init__(parent)
        self.app = app
        self.kind = this_kind()
        self.os_name = OS_LABELS.get(self.kind, "computer")
        self.setWindowTitle("Fix my keyboard")
        self.setMinimumSize(760, 520)
        self.fixes: dict[str, str] = dict(app.config().get("agent", "modifier_map", {}) or {})
        self.steps = guided_steps(self.kind)
        self.step = -1           # guided check: which key; -1 = not running
        self.results: list[bool | None] = [None] * len(self.steps)
        self.teach_sent: str | None = None
        self.mode = ""           # "", "check" or "teach"
        self.page = 0

        outer = QHBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        rail = QFrame()
        rail.setObjectName("sidebar")
        rail.setFixedWidth(210)
        self.rail = QVBoxLayout(rail)
        self.rail.setContentsMargins(14, 18, 14, 18)
        self.rail.setSpacing(6)
        self.rail_labels = []
        for i, (title, detail) in enumerate(self.STEPS):
            lab = label("", None, wrap=True)
            lab.setTextFormat(Qt.TextFormat.RichText)
            self.rail_labels.append(lab)
            self.rail.addWidget(lab)
        self.rail.addStretch(1)
        self.listening = label("● Listening to the shared keyboard", "small", wrap=True)
        self.rail.addWidget(self.listening)
        outer.addWidget(rail)

        right = QVBoxLayout()
        right.setContentsMargins(24, 20, 24, 18)
        right.setSpacing(12)
        self.title = label("", "h1", wrap=True)
        right.addWidget(self.title)
        self.lead = label("", "muted", wrap=True)
        right.addWidget(self.lead)
        self.chips = label("", "small", wrap=True)
        self.chips.setTextFormat(Qt.TextFormat.RichText)
        right.addWidget(self.chips)
        self.prompt = label("", "h2", wrap=True)
        right.addWidget(self.prompt)
        self.detail = label("", "small", wrap=True)
        right.addWidget(self.detail)

        self.stack = QStackedWidget()
        self.board = KeyboardView(self.kind)
        ready = KeyboardView(self.kind)
        ready.dimmed = True
        self.stack.addWidget(ready)                        # 0 ready: this system's keyboard, waiting
        self.stack.addWidget(self.board)                   # 1 check
        teach = QWidget()
        two = QHBoxLayout(teach)
        two.setContentsMargins(0, 0, 0, 0)
        two.setSpacing(14)
        self.shared_board = KeyboardView(self.kind, compact=True)
        self.local_board = KeyboardView(self.kind, compact=True)
        for title, board in (("Shared keyboard", self.shared_board), ("This computer's keyboard", self.local_board)):
            col = QVBoxLayout()
            col.setSpacing(6)
            col.addWidget(label(title, "small"))
            col.addWidget(board)
            col.addStretch(1)
            two.addLayout(col)
        self.stack.addWidget(teach)                        # 2 teach
        self.rows = MapRows(self.kind, self.kind)
        review = QWidget()
        review_layout = QVBoxLayout(review)
        review_layout.setContentsMargins(0, 0, 0, 0)
        review_layout.addWidget(self.rows)
        review_layout.addStretch(1)
        self.stack.addWidget(review)                       # 3 review
        right.addWidget(self.stack, 1)
        self.result = label("", None, wrap=True)
        self.result.setTextFormat(Qt.TextFormat.RichText)
        right.addWidget(self.result)

        nav = QHBoxLayout()
        self.back_button = button("Back", None, self.back)
        self.skip_button = button("Skip this key", None, self.skip)
        self.again_button = button("Check again", None, self.start_check)
        self.cancel_button = button("Cancel", None, self.reject)
        self.next_button = button("Start the check", "primary", self.next)
        for b in (self.back_button, self.skip_button, self.again_button):
            nav.addWidget(b)
        nav.addStretch(1)
        nav.addWidget(self.cancel_button)
        nav.addWidget(self.next_button)
        right.addLayout(nav)
        outer.addLayout(right, 1)

        self.rows.set_mapping(self.fixes)
        app.service.event.connect(self.on_event)
        app.service.send("keys learn on")
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.show_page(0)

    # -- pages ------------------------------------------------------------------
    def show_page(self, page: int) -> None:
        self.page = page
        self.mode = {1: "check", 2: "teach"}.get(page, "")
        self.stack.setCurrentIndex(page)
        self.result.setText("")
        running = self.app.service.is_alive()
        self.lead.setVisible(page != 1)
        for i, ((title, detail), lab) in enumerate(zip(self.STEPS, self.rail_labels)):
            detail = detail or f"{len(self.steps)} quick presses"
            mark = "✓" if i < page else str(i + 1)
            weight = "600" if i == page else "400"
            lab.setText(f"<span style='font-weight:{weight}'>{mark}&nbsp;&nbsp;{title}</span><br>"
                        f"<span style='font-size:small'>&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;{detail}</span>")
        self.listening.setVisible(running)
        self.back_button.setVisible(page > 0)
        self.skip_button.setVisible(page == 1)
        self.again_button.setVisible(page == 3)
        self.chips.setVisible(page == 1)
        self.next_button.setText({0: "Start the check", 1: "Skip the rest", 2: "Review and save", 3: "Save"}[page])
        if page == 0:
            self.title.setText("Let's check the keys from your shared keyboard")
            self.lead.setText(f"Use the keyboard plugged into your host, with this {self.os_name} device in control. "
                              "Each key to press lights up on a keyboard, and any that arrives as the wrong key gets "
                              "fixed here. While this window is open the shared keyboard's keys are only listened "
                              "to, not typed.")
            if running:
                self.prompt.setText("Press any key on the shared keyboard to start")
                self.detail.setText("If nothing happens, move the pointer onto this device first.")
            else:
                self.prompt.setText("Start linking first")
                self.detail.setText("Curslink needs to be running and connected to your host to hear its keyboard.")
            self.next_button.setEnabled(running)
        elif page == 1:
            self.title.setText("Check my keys")
            self.lead.setText("")
            self.next_button.setEnabled(True)
            self.show_step()
        elif page == 2:
            self.title.setText("Teach a key")
            self.lead.setText("For a key the check didn't cover. Press it on the shared keyboard, then press the key "
                              "it should be on this computer's own keyboard.")
            self.teach_sent = None
            self.show_teach()
            self.setFocus()
        else:
            self.title.setText("Review and save")
            self.lead.setText(f"These fixes are saved on this {self.os_name} device only, on top of the host's "
                              "mapping. Change or remove any of them here or later in Settings > Keyboard.")
            self.prompt.setText("")
            self.detail.setText("")

    def next(self) -> None:
        if self.page == 3:
            self.save()
        elif self.page == 0:
            self.start_check()
        else:
            self.show_page(self.page + 1)

    def back(self) -> None:
        if self.page > 0:
            self.show_page(self.page - 1)

    # -- check my keys -----------------------------------------------------------
    def start_check(self) -> None:
        self.step = 0
        self.results = [None] * len(self.steps)
        self.show_page(1)

    def show_chips(self) -> None:
        t = theme.current()
        parts = []
        for i, (want, _) in enumerate(self.steps):
            text = key_label(want, self.kind)
            r = self.results[i]
            if i == self.step:
                parts.append(f"<b style='color:{t.css('accent')}'>[{text}]</b>")
            elif r is True:
                parts.append(f"<span style='color:{t.css('good')}'>✓ {text}</span>")
            elif r is False:
                parts.append(f"<span style='color:{t.css('warn')}'>⚠ {text}</span>")
            else:
                parts.append(text)
        self.chips.setText("&nbsp;&nbsp; ".join(parts))

    def show_step(self) -> None:
        self.show_chips()
        if self.step < 0 or self.step >= len(self.steps):
            self.step = -1
            fixed = sum(1 for r in self.results if r is False)
            self.prompt.setText("All done")
            self.detail.setText(f"{fixed} key{'s' if fixed != 1 else ''} fixed. Teach another key, or review and "
                                "save." if fixed else "Every key arrived right. You can still teach a key.")
            self.board.show_keys(hit=self.board.hit)
            self.skip_button.setVisible(False)
            self.next_button.setText("Next: teach a key")
            return
        want, ask = self.steps[self.step]
        self.prompt.setText(ask)
        self.detail.setText(f"Key {self.step + 1} of {len(self.steps)}, on the shared keyboard.")
        self.board.show_keys(target=want, hit=self.board.hit)

    def skip(self) -> None:
        if self.page == 1 and self.step >= 0:
            self.step += 1
            self.board.hit = None
            self.show_step()

    # -- teach a key -------------------------------------------------------------
    def start_teach(self) -> None:
        self.show_page(2)

    def show_teach(self) -> None:
        if self.teach_sent is None:
            self.prompt.setText("1. Press the key on the shared keyboard")
            self.detail.setText("The one that does the wrong thing here.")
            self.local_board.dimmed = True
            self.shared_board.show_keys()
            self.local_board.show_keys()
        else:
            self.prompt.setText(f"2. Now press what {key_label(self.teach_sent, self.kind)} should be, on this "
                                "computer's own keyboard")
            self.detail.setText("")
            self.local_board.dimmed = False
            self.shared_board.show_keys(taught=family(self.teach_sent))
            self.local_board.show_keys()

    # -- keys ------------------------------------------------------------------
    def on_event(self, event: dict) -> None:
        if event.get("event") != "key_learned" or not event.get("pressed"):
            return
        self.heard(str(event.get("key", "")))

    def heard(self, sent: str) -> None:
        """A key pressed on the shared keyboard, as the host sent it."""
        if not sent:
            return
        t = theme.current()
        if self.page == 0:
            self.start_check()
        elif self.mode == "check" and 0 <= self.step < len(self.steps):
            wanted, _ = self.steps[self.step]
            changed = self.add_fix(sent, wanted)
            self.results[self.step] = not changed
            self.board.hit = (sent, not changed)
            if changed:
                self.result.setText(f"<b style='color:{t.css('warn')}'>{key_label(wanted, self.kind)} arrived as "
                                    f"{key_label(family(sent), self.kind)}.</b> It will type {key_label(wanted, self.kind)} "
                                    "from now on.")
            else:
                self.result.setText(f"<b style='color:{t.css('good')}'>✓ {key_label(wanted, self.kind)} arrived "
                                    "right.</b>")
            self.step += 1
            self.show_step()
        elif self.mode == "teach" and self.teach_sent is None:
            self.teach_sent = sent
            self.result.setText("")
            self.show_teach()

    def keyPressEvent(self, event: QKeyEvent) -> None:  # noqa: N802 - Qt
        # The host's keys aren't typed while this is open, so a key here is
        # one pressed on this computer's own keyboard.
        if self.mode == "teach" and self.teach_sent is not None and not event.isAutoRepeat():
            local = qt_key_name(event)
            if local:
                self.taught(local)
            return
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            return  # Enter mustn't press Save or Cancel by accident
        super().keyPressEvent(event)

    def taught(self, local: str) -> None:
        t = theme.current()
        sent, self.teach_sent = self.teach_sent or "", None
        changed = self.add_fix(sent, local)
        self.show_teach()
        self.local_board.show_keys(taught=family(local))
        if changed:
            self.result.setText(f"<b style='color:{t.css('good')}'>✓ Learned: {key_label(family(sent), self.kind)} from the "
                                f"shared keyboard now types {key_label(family(local), self.kind)}.</b> Teach another, or "
                                "review and save.")
        else:
            self.result.setText(f"✓ {key_label(sent, self.kind)} already arrives right.")

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
