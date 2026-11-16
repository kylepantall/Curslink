"""Setting up a desk, a step at a time.

Curslink's set-up used to be spread over its pages: pair on Devices, drag
screens on Layout, line up from three buttons there, calibrate on another
page, and switches in Settings. The guide strings those together in the
order they're needed:

1. **This computer**: what it does, and its name (the first-run page).
2. **Devices**: add the others.
3. **Your desk**: where the screens stand (the phone scan, or by hand).
4. **Your seat**: each overlap joined by eye from where you sit
   (curslink.seatalign). Only when a screen stands in front of another.
5. **Looking**: switch by looking at a screen. Optional.
6. **Try it**: move the pointer onto each device.

:func:`steps` says where each stands, from the config and the files beside
it (nothing here is kept twice: a step that's been done elsewhere shows as
done). :class:`GuideBar` is the strip across the top of the window while
the guide is running: the step, what to do, and Continue. :class:`SetupCard`
is the same list on the Status page afterwards, each step with a button to
do it again. Only a host has a guide; an agent just waits to be added.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QFrame, QHBoxLayout, QVBoxLayout, QWidget

from curslink.gui.widgets import Card, button, label, pill, set_pill

log = logging.getLogger(__name__)

ORDER = ("computer", "devices", "desk", "seat", "looking", "try")
OPTIONAL = ("desk", "seat", "looking")  # can be skipped

DONE, TODO, SKIPPED, NOT_NEEDED = "done", "todo", "skipped", "none"


@dataclass
class Step:
    key: str
    title: str  # in the list
    state: str  # DONE / TODO / SKIPPED / NOT_NEEDED
    detail: str  # where it stands, in a few words
    heading: str  # on the bar, while it's the step being done
    text: str  # ...and what to do
    action: str = ""  # the button that does it (starts the scan, opens the step's window)
    again: str = ""  # ...and on the Status card once it's done
    page: str = "status"  # the page it's done on


def _keys(text: str) -> list[str]:
    return [k for k in (text or "").split(",") if k in ORDER]


def overlaps(cfg) -> list[tuple[str, str]]:
    """The pairs of screens that overlap in the saved layout, as
    ``("device/screen" behind, in front)``; none while overlapping is off."""
    try:
        from curslink.config import parse_layout

        layout = parse_layout(cfg.raw())
        if not layout.allow_overlap:
            return []
        return [(f"{b.device_id}/{b.id}", f"{f.device_id}/{f.id}") for b, f in layout.overlaps()]
    except Exception:  # noqa: BLE001 - a layout that doesn't parse has no overlaps to line up
        return []


def joined(cfg, desk: dict | None) -> list[tuple[str, str]]:
    """The overlaps that have been lined up (a saved fit)."""
    out = []
    for t in cfg.fits():
        try:
            out.append((str(t["behind"]), str(t["front"])))
        except (KeyError, TypeError):
            continue
    return out


def steps(app, desk: dict | None = None) -> list[Step]:
    """Every step and where it stands now. ``desk``: the solved desk, if
    it's already been read (else curslink.seat.load)."""
    from curslink import seat

    prefs = app.prefs
    said_done, skipped = _keys(prefs.guide_done), _keys(prefs.guide_skipped)
    configured = app.has_config()
    cfg = app.config() if configured else None
    if desk is None and configured:
        try:
            desk = seat.load()
        except Exception:  # noqa: BLE001
            desk = None
    me = str(cfg.get("host", "device_id", "")) if cfg is not None else ""
    others = [d for d in cfg.devices() if d.get("id") != me] if cfg is not None else []
    names = [str(d.get("name") or d.get("id")) for d in others]

    def state(key: str, done: bool, needed: bool = True) -> str:
        if not needed:
            return NOT_NEEDED
        if done:
            return DONE
        return SKIPPED if key in skipped else TODO

    out = [Step("computer", "This computer", state("computer", configured),
                next((str(d.get("name") or me) for d in (cfg.devices() if cfg is not None else [])
                      if d.get("id") == me), me or "Not set up"),
                "Which computer is this?", "Pick what this computer does and give it a name.",
                again="Set up again…", page="setup")]

    out.append(Step("devices", "Devices", state("devices", bool(others)),
                    ", ".join(names) if names else "None added yet",
                    "Add your other devices",
                    "Open Curslink on each other computer or phone. They show up below by themselves: press Pair "
                    "beside each one. A phone can scan the QR code instead.",
                    again="Add another", page="devices"))

    scanned = desk is not None
    placed = scanned or "desk" in said_done
    how = "Scanned with your phone" if scanned else "Placed by hand" if placed else "Not placed yet"
    out.append(Step("desk", "Your desk", state("desk", placed, needed=bool(others)), how,
                    "Show Curslink where your screens stand",
                    "Scan the desk with your phone's camera: walk it along the desk, two neighbouring screens in "
                    "view at a time is enough, standing wherever is easiest. No phone? Drag the screens into place "
                    "below and press Save layout.",
                    action="Scan with my phone", again="Scan again", page="layout"))

    pairs = overlaps(cfg) if cfg is not None else []
    fitted = set(joined(cfg, desk)) if cfg is not None else set()
    left = [p for p in pairs if p not in fitted]
    by_eye = desk is not None and desk.get("how") == "joined"
    seat_done = bool(pairs) and not left and (by_eye or "seat" in said_done)
    if not pairs:
        seat_detail = "No screen stands in front of another"
    elif seat_done:
        mm = seat.mm_of(desk) if desk is not None else None
        seat_detail = "Lined up from your seat"
        if by_eye and mm:
            try:
                seat_detail += f", {seat.distance(desk, seat.main_screen(desk)) * mm / 10:.0f} cm back"
            except Exception:  # noqa: BLE001
                pass
    else:
        n = len(left) or len(pairs)
        seat_detail = f"{n} overlap{'s' if n != 1 else ''} to line up"
    out.append(Step("seat", "Your seat", state("seat", seat_done, needed=bool(pairs)), seat_detail,
                    "Line up from your seat",
                    "Sit how you normally sit. A red ring appears where one screen stands in front of another: "
                    "nudge with the arrow keys until it closes, corner by corner.",
                    action="Line up from my seat", again="Line up again", page="layout"))

    gaze_on = bool(cfg.get("gaze", "enabled", False)) if cfg is not None else False
    calibrated = False
    if cfg is not None:
        try:
            calibrated = (app.config_path.parent / str(cfg.get("gaze", "calibration", "calibration.json"))).exists()
        except (OSError, TypeError):
            calibrated = False
    looking = calibrated and gaze_on
    out.append(Step("looking", "Switch by looking", state("looking", looking, needed=bool(others)),
                    "On" if looking else "Calibrated, turned off" if calibrated else "Not set up",
                    "Switch by looking? (optional)",
                    "Curslink can move the pointer to the screen you turn to. Look at each screen once when asked. "
                    "Skip it and the pointer still crosses at the screen edges.",
                    action="Start looking", again="Calibrate again", page="calibrate"))

    out.append(Step("try", "Try it", state("try", "try" in said_done, needed=bool(others)),
                    "Tried" if "try" in said_done else "Not tried yet",
                    "Try it",
                    "Move the pointer off the edge of this screen onto each device. Each one ticks itself off as "
                    "the pointer arrives.",
                    page="status"))
    return out


def by_key(all_steps: list[Step], key: str) -> Step | None:
    return next((s for s in all_steps if s.key == key), None)


def needed(all_steps: list[Step]) -> list[Step]:
    return [s for s in all_steps if s.state != NOT_NEEDED]


def next_key(all_steps: list[Step], after: str | None = None) -> str:
    """The first step still to do (after ``after``, if given); "" when there's none."""
    keys = [s.key for s in needed(all_steps)]
    start = keys.index(after) + 1 if after in keys else 0
    for s in needed(all_steps)[start:]:
        if after is not None or s.state == TODO:
            return s.key
    return ""


def complete(all_steps: list[Step]) -> bool:
    return all(s.state != TODO for s in all_steps)


class Guide:
    """The guide's place: which step it's on (``app.prefs.guide``, "" when
    it isn't running), and going between them."""

    def __init__(self, app):
        self.app = app
        self.reached: set[str] = set()  # "Try it": the devices the pointer has been to

    @property
    def key(self) -> str:
        return self.app.prefs.guide if self.app.prefs.guide in ORDER else ""

    @property
    def active(self) -> bool:
        return bool(self.key) and self.app.role != "agent"

    def _mark(self, field: str, key: str, on: bool) -> None:
        keys = _keys(getattr(self.app.prefs, field))
        if on and key not in keys:
            keys.append(key)
        elif not on and key in keys:
            keys.remove(key)
        setattr(self.app.prefs, field, ",".join(keys))

    def go(self, key: str) -> None:
        """Start (or move to) a step; "" ends the guide."""
        self.app.prefs.guide = key if key in ORDER else ""
        self.app.prefs.save()
        if key == "try":
            self.reached = set()
        window = getattr(self.app, "window", None)
        if window is not None:
            step = by_key(steps(self.app), key) if key else None
            if step is not None:
                window.show_page(step.page)
            window.refresh_guide()

    def start(self) -> None:
        """From the first step still to do."""
        all_steps = steps(self.app)
        self.go(next_key(all_steps) or needed(all_steps)[-1].key)

    def forward(self, skip: bool = False) -> None:
        """Continue (or Skip): this step is done with, on to the next that's needed."""
        key = self.key
        if not key:
            return
        self._mark("guide_skipped", key, skip)
        self._mark("guide_done", key, not skip)
        self.go(next_key(steps(self.app), after=key))

    def back(self) -> None:
        keys = [s.key for s in needed(steps(self.app)) if s.key != "computer"]
        if self.key in keys and keys.index(self.key) > 0:
            self.go(keys[keys.index(self.key) - 1])

    def close(self) -> None:
        self.go("")

    def act(self, key: str | None = None) -> None:
        """The step's own button: start what does it, on its page."""
        key = key or self.key
        window = getattr(self.app, "window", None)
        step = by_key(steps(self.app), key)
        if window is None or step is None:
            return
        window.show_page(step.page)
        page = window.pages.get(step.page)
        try:
            if key == "desk":
                page.start_lineup()
            elif key == "seat":
                cfg = self.app.config()
                pairs = overlaps(cfg)
                fitted = set(joined(cfg, None))
                todo = [p for p in pairs if p not in fitted] or pairs
                if todo:
                    page.tune_fit(*todo[0])
            elif key == "looking":
                page.begin()
            elif key == "devices":
                page.search()
        except Exception:  # noqa: BLE001 - the page is open either way
            log.exception("guide: couldn't start %s", key)

    def switched(self, device_id: str) -> None:
        """The pointer went to a device (for "Try it")."""
        if self.key == "try" and device_id:
            self.reached.add(device_id)


class GuideBar(QFrame):
    """The strip across the top of the window while the guide is running."""

    def __init__(self, app, parent=None):
        super().__init__(parent)
        self.app = app
        self.setObjectName("guideBar")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        row = QHBoxLayout(self)
        row.setContentsMargins(32, 14, 24, 14)
        row.setSpacing(14)
        words = QVBoxLayout()
        words.setSpacing(2)
        self.eyebrow = label("", "eyebrow")
        self.heading = label("", "guideTitle")
        self.text = label("", "muted", wrap=True)
        self.ticks = label("", "small", wrap=True)
        for w in (self.eyebrow, self.heading, self.text, self.ticks):
            words.addWidget(w)
        row.addLayout(words, 1)
        buttons = QVBoxLayout()
        buttons.setSpacing(8)
        top = QHBoxLayout()
        top.setSpacing(8)
        self.action = button("", None, lambda: app.guide.act())
        self.next = button("Continue", "primary", lambda: app.guide.forward())
        top.addWidget(self.action)
        top.addWidget(self.next)
        bottom = QHBoxLayout()
        bottom.setSpacing(14)
        bottom.addStretch(1)
        self.back = button("Back", "link", lambda: app.guide.back())
        self.skip = button("Skip this step", "link", lambda: app.guide.forward(skip=True))
        self.leave = button("Finish later", "link", lambda: app.guide.close())
        for b in (self.back, self.skip, self.leave):
            bottom.addWidget(b)
        buttons.addLayout(top)
        buttons.addLayout(bottom)
        row.addLayout(buttons)
        self.hide()

    def refresh(self) -> None:
        guide = self.app.guide
        if not guide.active or not self.app.has_config():
            self.hide()
            return
        all_steps = steps(self.app)
        step = by_key(all_steps, guide.key)
        shown = [s for s in needed(all_steps)]
        if step is None or step not in shown:
            # the step isn't needed any more (the overlap went): on to the next
            nxt = next_key(all_steps)
            if nxt and nxt != guide.key:
                guide.go(nxt)
            else:
                guide.close()
            return
        n = shown.index(step) + 1
        self.eyebrow.setText(f"SET UP YOUR DESK  ·  STEP {n} OF {len(shown)}")
        self.heading.setText(step.heading)
        self.text.setText(step.text)
        last = n == len(shown)
        done = step.state == DONE
        self.action.setVisible(bool(step.action))
        self.action.setText(step.again if done and step.again else step.action)
        # What does the step is the thing to press until it's done; then Continue is.
        self.action.setObjectName("" if done or not step.action else "primary")
        self.next.setObjectName("primary" if done or not step.action else "")
        for b in (self.action, self.next):
            b.style().unpolish(b)
            b.style().polish(b)
        self.next.setText("Finish" if last else "Continue")
        self.skip.setVisible(step.key in OPTIONAL and not done and not last)
        self.back.setVisible(n > 2)
        ticks = ""
        if step.key == "try":
            cfg = self.app.config()
            me = str(cfg.get("host", "device_id", ""))
            parts = []
            for d in cfg.devices():
                if d.get("id") == me:
                    continue
                mark = "✓" if d.get("id") in guide.reached else "○"
                parts.append(f"{mark} {d.get('name') or d.get('id')}")
            ticks = "     ".join(parts)
            if not self.app.linking:
                ticks = "Press Start linking first.     " + ticks
        elif done:
            ticks = f"✓ {step.detail}"
        self.ticks.setText(ticks)
        self.ticks.setVisible(bool(ticks))
        self.show()


class SetupCard(Card):
    """Status page: the steps and where each stands, with a button to do it (again)."""

    resume = Signal()

    def __init__(self, app):
        super().__init__("Your setup", rows=True)
        self.app = app
        self.step_rows: dict[str, tuple] = {}
        for key in ORDER[1:]:
            words = QVBoxLayout()
            words.setSpacing(2)
            title, detail = label(""), label("", "small", wrap=True)
            words.addWidget(title)
            words.addWidget(detail)
            holder = QWidget()
            holder.setLayout(words)
            words.setContentsMargins(0, 0, 0, 0)
            side = QWidget()
            sr = QHBoxLayout(side)
            sr.setContentsMargins(0, 0, 0, 0)
            sr.setSpacing(10)
            state = pill("", "off")
            again = button("", None, lambda _=False, k=key: self.do(k))
            sr.addWidget(state)
            sr.addWidget(again)
            row = self.add_row(holder, side)
            self.step_rows[key] = (row, title, detail, state, again)
        self.resume_button = button("Continue setup", "primary", lambda: app.guide.start())
        self.resume_text = label("A few steps are left.", "muted")
        self.resume_row = self.add_row(self.resume_text, self.resume_button)

    def do(self, key: str) -> None:
        guide = self.app.guide
        if key == "try":
            guide.go("try")
        else:
            guide.act(key)

    def refresh(self) -> None:
        if self.app.role == "agent" or not self.app.has_config():
            self.hide()
            return
        all_steps = steps(self.app)
        words = {DONE: ("Done", "on"), TODO: ("To do", "wait"), SKIPPED: ("Skipped", "off")}
        for key, (row, title, detail, state, again) in self.step_rows.items():
            step = by_key(all_steps, key)
            if row is not None:
                row.setVisible(step is not None and step.state != NOT_NEEDED)
            if step is None or step.state == NOT_NEEDED:
                continue
            title.setText(step.title)
            detail.setText(step.detail)
            set_pill(state, *words[step.state])
            text = (step.again or step.action) if step.state == DONE else (step.action or step.again or "Try now")
            again.setText(text if key != "try" else "Try now")
        left = sum(1 for s in all_steps if s.state == TODO)
        self.resume_text.setText(f"{left} step{'s' if left != 1 else ''} left.")
        self.resume_row.setVisible(left > 0 and not self.app.guide.active)
        self.show()
