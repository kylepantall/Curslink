"""Settings > Network: how each link is doing, and the optimiser's switch.

Fed by the running host or agent's ``network`` events (every couple of
seconds, see :func:`curslink.net.check.network_event`) and ``netcheck``
events (the "Check the network" button)."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QFrame, QHBoxLayout, QVBoxLayout, QWidget

from curslink.gui.widgets import Card, Toggle, button, label, pill, set_pill, text_block

GRADES = {"good": ("Good", "on"), "fair": ("Fair", "wait"), "poor": ("Poor", "bad"), "unknown": ("Measuring", "off")}
PATHS = {"udp": "Direct, pointer by UDP", "tcp": "Direct, TCP only", "relay": "Through the relay"}
KINDS = {"wifi": "Wi-Fi", "wired": "wired", "vpn": "VPN", "other": ""}


def _ms(value) -> str:
    return "–" if value is None else f"{value:.0f} ms"


def describe(link: dict) -> tuple[str, str, list[str]]:
    """(heading, numbers line, extra lines) for one link."""
    name = link.get("name") or ("The host" if link.get("device_id") == "host" else link.get("device_id", "?"))
    heading = f"{name} · {PATHS.get(link.get('path'), 'Connecting')}"
    rtt = link.get("rtt") or {}
    parts = []
    if rtt.get("samples"):
        parts.append(f"Latency {_ms(rtt.get('p50_ms'))} (most {_ms(rtt.get('p95_ms'))})")
        parts.append(f"jitter {_ms(rtt.get('jitter_ms'))}")
    else:
        parts.append("Measuring latency…")
    loss = link.get("loss_pct")
    if loss is not None:
        parts.append(f"loss {loss:.1f}%")
    iface = link.get("interface") or {}
    if iface:
        kind = KINDS.get(iface.get("kind", ""), "")
        parts.append(f"over {iface.get('name', '?')}" + (f" ({kind})" if kind and kind.lower() not in
                                                         iface.get("name", "").lower() else ""))
    extra = []
    reason = (link.get("tuning") or {}).get("reason")
    if link.get("optimise"):
        extra.append(f"Optimising: {reason}." if reason else "Optimising: no changes needed right now.")
    extra += ["• " + tip for tip in link.get("tips") or []]
    return heading, " · ".join(parts), extra


class LinkRow(QFrame):
    def __init__(self):
        super().__init__()
        self.setObjectName("row")
        v = QVBoxLayout(self)
        v.setContentsMargins(18, 10, 18, 10)
        v.setSpacing(3)
        top = QHBoxLayout()
        self.heading = label("")
        self.grade = pill("Measuring", "off")
        top.addWidget(self.heading, 1)
        top.addWidget(self.grade, 0, Qt.AlignmentFlag.AlignVCenter)
        v.addLayout(top)
        self.numbers = label("", "small", wrap=True)
        v.addWidget(self.numbers)
        self.extra = label("", "small", wrap=True)
        v.addWidget(self.extra)

    def show_link(self, link: dict) -> None:
        heading, numbers, extra = describe(link)
        self.heading.setText(heading)
        self.numbers.setText(numbers)
        self.extra.setText("\n".join(extra))
        self.extra.setVisible(bool(extra))
        text, kind = GRADES.get(link.get("grade", "unknown"), GRADES["unknown"])
        set_pill(self.grade, text, kind)


class NetworkCard(Card):
    """The card itself; ``set_optimise`` is wired to the config by the page."""

    def __init__(self, on_optimise, on_check):
        super().__init__("Network", rows=True)
        self.optimise = Toggle("Improve the connection automatically")
        self.optimise.clicked.connect(on_optimise)
        self.add_row(text_block(
            "Improve the connection automatically",
            "Curslink watches each link and tunes it: marks its packets low-latency (Wi-Fi's fast lane), keeps "
            "Wi-Fi awake while the pointer is on that computer (and off background scans on Windows), sends "
            "pointer moves twice when packets go missing, sends fewer, steadier updates on a slow network, "
            "and leaves the relay for a direct link once the other computer shows up on the LAN."),
            self.optimise)
        self._links_holder = QWidget(self)
        self._links = QVBoxLayout(self._links_holder)
        self._links.setContentsMargins(0, 0, 0, 0)
        self._links.setSpacing(0)
        self.rows.addWidget(self._links_holder)
        self.empty = label("Start linking to see how each connection is doing.", "small", wrap=True)
        self.empty.setContentsMargins(18, 10, 18, 10)
        self._links.addWidget(self.empty)
        self.rows_by_id: dict[str, LinkRow] = {}
        self.check_button = button("Check the network", None, on_check)
        self.check_button.setToolTip("Look for the other computers by mDNS and list this computer's networks")
        self.check_text = text_block("Network check", "Checks mDNS (how the computers find each other) "
                                     "and which networks this computer is on.")
        self.add_row(self.check_text, self.check_button)

    def show_network(self, event: dict) -> None:
        links = [l for l in event.get("links") or [] if isinstance(l, dict)]
        seen = set()
        for link in links:
            key = str(link.get("device_id", "?"))
            seen.add(key)
            row = self.rows_by_id.get(key)
            if row is None:
                row = LinkRow()
                self.rows_by_id[key] = row
                self._links.addWidget(row)
            row.show_link(link)
        for key in set(self.rows_by_id) - seen:
            row = self.rows_by_id.pop(key)
            row.setParent(None)
            row.deleteLater()
        self.empty.setVisible(not links)
        if not links:
            self.empty.setText("Nothing linked right now. Once another computer connects, its latency, jitter "
                               "and packet loss show here.")

    def stopped(self) -> None:
        self.show_network({"links": []})
        self.empty.setText("Start linking to see how each connection is doing.")

    def checking(self) -> None:
        self.check_button.setEnabled(False)
        self.check_text.detail_label.setText("Checking… (a few seconds)")
        self.check_text.detail_label.setVisible(True)

    def show_check(self, event: dict) -> None:
        self.check_button.setEnabled(True)
        lines = [str(s) for s in event.get("summary") or []]
        self.check_text.detail_label.setText("\n".join(lines) or "No result.")
        self.check_text.detail_label.setVisible(True)

    def check_unavailable(self) -> None:
        self.check_button.setEnabled(True)
        self.check_text.detail_label.setText("Start linking first: the check runs in Curslink's background service.")
        self.check_text.detail_label.setVisible(True)

