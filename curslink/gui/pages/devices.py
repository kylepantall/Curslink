"""Devices: who this computer is paired with, and pairing new ones."""

from __future__ import annotations

import asyncio
import threading
import time

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QComboBox, QHBoxLayout, QLabel, QLineEdit, QMessageBox, QVBoxLayout

from curslink.config import agent_config, host_config
from curslink.gui.common import call_on_main, in_background, main_thread
from curslink.gui.widgets import Card, EmptyState, Page, button, label, pill, spot, text_block


class Pairing:
    """One outgoing pairing attempt, on its own thread and event loop."""

    def __init__(self, me, address: str, port: int, on_code, on_finished, on_failed):
        self.me, self.address, self.port = me, address, port
        # Called on the Qt thread: on_code(code, peer_name), on_finished(PairingResult),
        # on_failed(message). No QObject lives here, so nothing Qt is freed off-thread.
        self.on_code, self.on_finished, self.on_failed = on_code, on_finished, on_failed
        main_thread()  # made here, on the Qt thread, before the worker needs it
        self._loop: asyncio.AbstractEventLoop | None = None
        self._task: asyncio.Task | None = None
        self.cancelled = False

    def start(self) -> None:
        threading.Thread(target=self._run, name="curslink-pairing", daemon=True).start()

    def cancel(self) -> None:
        self.cancelled = True
        loop, task = self._loop, self._task
        if loop and task:
            try:
                loop.call_soon_threadsafe(task.cancel)
            except RuntimeError:
                pass

    def _fail(self, message: str) -> None:
        call_on_main(lambda: self.on_failed(message))

    def _run(self) -> None:
        from curslink.pairing.handshake import PairingError, initiate

        async def go():
            reader, writer = await asyncio.wait_for(asyncio.open_connection(self.address, self.port), 5)
            try:
                return await initiate(reader, writer, self.me,
                                      lambda code, peer: call_on_main(lambda: self.on_code(code, peer.name)))
            finally:
                writer.close()

        async def main():
            self._task = asyncio.current_task()
            return await go()

        loop = asyncio.new_event_loop()
        self._loop = loop
        try:
            result = loop.run_until_complete(main())
        except asyncio.CancelledError:
            self._fail("")
        except PairingError as exc:
            self._fail(f"Pairing failed: {exc}")
        except (OSError, asyncio.TimeoutError) as exc:
            self._fail(f"Couldn't reach {self.address}: {exc or 'timed out'}")
        except Exception as exc:  # a bug, but don't take the app down
            self._fail(f"Pairing failed: {exc}")
        else:
            call_on_main(lambda: self.on_finished(result))
        finally:
            loop.close()


class QrSession:
    """The listener behind one pairing QR code, on its own thread and event loop."""

    def __init__(self, me, control_port: int, relay, on_ready, on_paired, on_failed):
        self.me, self.control_port, self.relay = me, control_port, relay
        # Called on the Qt thread: on_ready(url), on_paired(PairResult, address), on_failed(message).
        self.on_ready, self.on_paired, self.on_failed = on_ready, on_paired, on_failed
        main_thread()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._stop: asyncio.Event | None = None

    def start(self) -> None:
        threading.Thread(target=self._run, name="curslink-qr-pairing", daemon=True).start()

    def cancel(self) -> None:
        loop, stop = self._loop, self._stop
        if loop and stop:
            try:
                loop.call_soon_threadsafe(stop.set)
            except RuntimeError:
                pass

    def _run(self) -> None:
        from curslink.pairing.discovery import local_addresses, rank_addresses
        from curslink.pairing.qr import QrPairing

        async def main():
            self._stop = asyncio.Event()
            done = asyncio.get_running_loop().create_future()

            def paired(result, address):
                if not done.done():
                    done.set_result((result, address))

            qr = QrPairing(self.me, paired)
            await qr.start()
            try:
                addresses = list(rank_addresses(local_addresses()))
                relay = self.relay
                call_on_main(lambda: self.on_ready(qr.url(addresses, self.control_port,
                                                          relay.address if relay else "", relay.key if relay else "")))
                stopped = asyncio.ensure_future(self._stop.wait())
                await asyncio.wait([done, stopped], timeout=qr.lifetime_s, return_when=asyncio.FIRST_COMPLETED)
                stopped.cancel()
                return done.result() if done.done() else None
            finally:
                await qr.close()

        loop = asyncio.new_event_loop()
        self._loop = loop
        try:
            outcome = loop.run_until_complete(main())
        except OSError as exc:
            call_on_main(lambda: self.on_failed(f"Can't take pairing requests here: {exc}"))
        except Exception as exc:  # a bug, but don't take the app down
            call_on_main(lambda: self.on_failed(f"Pairing failed: {exc}"))
        else:
            if outcome is None:
                call_on_main(lambda: self.on_failed(""))
            else:
                call_on_main(lambda: self.on_paired(*outcome))
        finally:
            loop.close()


def qr_pixmap(text: str, scale: int = 6):
    import io

    import segno
    from PySide6.QtGui import QPixmap

    buffer = io.BytesIO()
    segno.make(text, error="m").save(buffer, kind="png", scale=scale, border=2, dark="#000000", light="#ffffff")
    pixmap = QPixmap()
    pixmap.loadFromData(buffer.getvalue(), "PNG")
    return pixmap


EDGES = (("Right", "right"), ("Left", "left"), ("Above", "top"), ("Below", "bottom"))


PHONE_TEXT = ("Install Curslink on your Android phone, then scan a code from here to pair it. Afterwards, push "
              "the pointer past the edge you choose for the phone and it wakes up with your mouse and keyboard on it.")


def add_to_layout(app, device_id: str, name: str, paired_screens=None) -> bool:
    """Host: put a newly paired device in the layout, so the host lets it
    connect ([[devices]]) and the pointer has an edge to reach it by. False
    if it's there already. The caller saves and restarts the service.

    Its screens are what it reported when it connected, else what it said
    while pairing (a phone does), else a placeholder screen."""
    cfg = app.config()
    if cfg.has_device(device_id):
        return False
    agent = app.status.agents.get(device_id)
    reported = (agent.screens if agent else []) or paired_screens or []
    screens = [
        {"id": s.get("name") or f"screen{i + 1}", "x": s["x"], "y": s["y"],
         "width": s["width"], "height": s["height"]}
        for i, s in enumerate(reported)
        if isinstance(s, dict) and all(isinstance(s.get(k), (int, float)) and not isinstance(s.get(k), bool)
                                       for k in ("x", "y", "width", "height"))
    ]
    cfg.add_device(device_id, name, screens or None)
    if [sc["id"] for sc in screens] == ["phone"]:
        # The whole right side leads to it, across every monitor.
        cfg.place_beside(device_id, cfg.get("host", "device_id"), "right", whole_side=True)
    cfg.save()
    return True


class DevicesPage(Page):
    def __init__(self, app):
        super().__init__("Devices", "Pair once and each device gets its own key. Nothing secret goes in a config file.")
        self.app = app
        self.pairing: Pairing | None = None
        self.qr: QrSession | None = None
        self.found = []

        self.code_card = Card()
        self.code_card.setObjectName("accentCard")
        row = QHBoxLayout()
        row.setSpacing(18)
        row.addWidget(spot("pairing", 120, 90))
        words = QVBoxLayout()
        self.code_intro = label("", wrap=True)
        self.code = label("", "code")
        self.code_hint = label("Waiting for it to be entered. Check the code matches before you confirm.", wrap=True)
        for w in (self.code_intro, self.code, self.code_hint):
            words.addWidget(w)
        row.addLayout(words, 1)
        row.addWidget(button("Cancel", None, self.cancel_pairing))
        self.code_card.body.addLayout(row)
        self.code_card.hide()
        self.add(self.code_card)

        self.message = label("", "warn", wrap=True)
        self.message.hide()
        self.add(self.message)

        self.firewall = Card(rows=True)
        self.firewall.setObjectName("accentCard")
        self.firewall.add_row(
            text_block("Windows Firewall is hiding this PC",
                       "Other computers can't find or pair with Curslink here until it's allowed through. "
                       "Windows will ask for permission."),
            button("Allow on my network", "primary", self.allow_firewall))
        self.firewall.hide()
        self.add(self.firewall)

        self.this = Card("This computer", rows=True)
        self.add(self.this)

        # Hosts only: a phone with the Curslink app scans this to pair.
        self.phone = Card("Pair a phone", rows=True)
        self.phone_row = QHBoxLayout()
        self.phone_row.setContentsMargins(20, 10, 20, 14)
        self.phone_row.setSpacing(18)
        self.qr_image = QLabel()
        self.qr_image.setAccessibleName("Pairing QR code")
        self.qr_image.hide()
        self.phone_row.addWidget(self.qr_image)
        words = QVBoxLayout()
        self.phone_text = label(PHONE_TEXT, wrap=True)
        words.addWidget(self.phone_text)
        buttons = QHBoxLayout()
        self.qr_button = button("Show QR code", "primary", self.show_qr)
        self.qr_cancel = button("Cancel", None, self.cancel_qr)
        self.qr_cancel.hide()
        buttons.addWidget(self.qr_button)
        buttons.addWidget(self.qr_cancel)
        buttons.addStretch(1)
        words.addLayout(buttons)
        words.addStretch(1)
        self.phone_row.addLayout(words, 1)
        self.phone.body.addLayout(self.phone_row)
        self.add(self.phone)

        self.paired = Card("Paired with this computer", rows=True)
        self.add(self.paired)

        self.nearby = Card(rows=True)
        head = QHBoxLayout()
        head.setContentsMargins(20, 10, 20, 10)
        head.addWidget(label("Nearby on this network", "h2"), 1)
        self.search_state = label("", "small")
        head.addWidget(self.search_state)
        head.addWidget(button("Search again", None, self.search))
        self.nearby.body.insertLayout(0, head)
        manual = QHBoxLayout()
        manual.setContentsMargins(20, 14, 20, 8)
        self.address = QLineEdit()
        self.address.setPlaceholderText("192.168.1.40 or my-laptop.local")
        self.address.setAccessibleName("Address to pair with")
        manual.addWidget(label("Not listed? Pair by address", "small"))
        manual.addWidget(self.address, 1)
        manual.addWidget(button("Pair", None, self.pair_by_address))
        self.nearby.body.addLayout(manual)
        self.add(self.nearby)
        self.finish()

        app.config_changed.connect(self.refresh)
        self.refresh()

    # -- who am I -------------------------------------------------------------
    def me(self):
        from curslink.pairing.handshake import DeviceInfo

        raw = self.app.config().raw()
        role = self.app.role
        base = self.app.config_path.parent
        if role == "host":
            cfg = host_config(raw, base_dir=base)
            return DeviceInfo(cfg.device_id, cfg.name, "host"), cfg.pairing
        cfg = agent_config(raw, base_dir=base)
        return DeviceInfo(cfg.device_id, cfg.name, "agent"), cfg.pairing

    def store(self):
        from curslink.pairing.store import PairingStore

        return PairingStore(self.me()[1].store)

    # -- lists --------------------------------------------------------------
    def shown(self) -> None:
        self.refresh()
        self.check_firewall()
        if not self.found and self.pairing is None:
            self.search()

    def draw_this(self) -> None:
        from curslink.pairing.discovery import local_addresses, local_hostname

        self.this.clear_rows()
        try:
            me, pairing = self.me()
        except Exception:
            return
        port = getattr(pairing, "port", None)
        ips = ", ".join(local_addresses()) or "no network address"
        self.this.add_row(
            text_block(me.name, f"{me.role.title()} · {local_hostname()} · id {me.device_id}"),
            label(ips + (f" · port {port}" if port else ""), "mono"))

    def check_firewall(self) -> None:
        from curslink import winfirewall

        if not winfirewall.WINDOWS or getattr(self, "_fw_checking", False):
            return
        self._fw_checking = True

        def done(state):
            self._fw_checking = False
            self.firewall.setVisible(state == "blocked")

        def failed(_exc):
            self._fw_checking = False

        in_background(winfirewall.status, done, failed, name="curslink-firewall")

    def allow_firewall(self) -> None:
        from curslink import winfirewall

        if winfirewall.allow():
            self.say("Allowed. Give it a few seconds, then press Search again on the other computer.", ok=True)
            QTimer.singleShot(4000, self.check_firewall)
            if self.app.linking:
                self.app.service.restart()
        else:
            self.say("Windows didn't make the change. Press Allow again and choose Yes when Windows asks.")

    def refresh(self) -> None:
        if not self.app.has_config():
            return
        self.draw_this()
        self.paired.clear_rows()
        try:
            store = self.store()
            peers = store.peers()
        except Exception as exc:
            self.paired.add_row(label(f"Can't read the pairing store: {exc}", "small"))
            return
        if not peers:
            other = "an agent" if self.app.role == "host" else "your host"
            self.paired.rows.addWidget(EmptyState(
                "devices-empty", "Nothing paired yet",
                f"Open Curslink on {other}, on the same network, and pair with it below."))
        connected = {a.device_id for a in self.app.status.agents.values() if a.connected}
        paused = {a.device_id for a in self.app.status.agents.values() if a.paused and not a.connected}
        if self.app.status.host_state == "connected":
            connected |= {p.device_id for p in peers if p.role == "host"}
        self.draw_phone(peers)
        for p in peers:
            t = time.localtime(p.paired_at)
            when = f"{t.tm_mday} {time.strftime('%b %Y', t)}" if p.paired_at else "earlier"
            on = p.device_id in connected
            widgets = [
                text_block(p.name, f"{p.role.title()} · id {p.device_id} · paired {when}"),
                pill("Connected" if on else ("Paused" if p.device_id in paused else "Offline"), "on" if on else "off"),
            ]
            edge = self.edge_picker(p) if self.app.role == "host" and p.role == "agent" else None
            if edge is not None:
                widgets.insert(1, edge)
            widgets.append(button("Unpair", "danger", lambda _=False, peer=p: self.unpair(peer)))
            self.paired.add_row(*widgets)
        self.draw_found()

    def draw_found(self) -> None:
        self.nearby.clear_rows()
        try:
            me, _ = self.me()
            paired = {p.device_id for p in self.store().peers()}
        except Exception:
            return
        want = [f for f in self.found if f.role != me.role]
        for f in want:
            already = f.device_id in paired
            self.nearby.add_row(
                text_block(f.name, " · ".join(x for x in (f.role.title(), f.host,
                                                          "paired" if already else "not paired") if x)),
                label(f.address, "mono"),
                button("Pair again" if already else "Pair", None if already else "primary",
                       lambda _=False, found=f: self.pair_with(found.address, found.pairing_port, found.name)),
            )
        if not want:
            import sys

            other = "agents" if me.role == "host" else "hosts"
            hint = "Open Curslink on the other computer, on the same network."
            if sys.platform == "darwin":
                hint += (" On a Mac, check Curslink is on in System Settings › Privacy & Security › "
                         "Local Network.")
            hint += " Or pair by address below: each computer shows its address under This computer."
            self.nearby.rows.addWidget(EmptyState("searching", f"Looking for {other}…", hint, size=(120, 90)))

    def search(self) -> None:
        from curslink.pairing.discovery import browse

        try:
            me, _ = self.me()
        except Exception:
            return
        self.search_state.setText("Looking for Curslink devices…")

        def done(found):
            self.found = list(found)
            self.search_state.setText(f"Found {len(self.found)}" if self.found else "")
            self.draw_found()

        def failed(exc):
            self.search_state.setText("")
            self.say(f"Network discovery isn't working here ({exc}). Pair by address instead.")

        in_background(lambda: asyncio.run(browse(3.0, exclude=me.device_id)), done, failed)

    # -- pairing --------------------------------------------------------------
    def pair_by_address(self) -> None:
        from curslink.pairing.service import DEFAULT_PAIRING_PORT

        text = self.address.text().strip()
        if not text:
            return
        host, _, port = text.rpartition(":") if text.count(":") == 1 else (text, "", "")
        try:
            port_n = int(port) if port else DEFAULT_PAIRING_PORT
        except ValueError:
            self.say("The port after the colon should be a number.")
            return
        self.pair_with(host or text, port_n, host or text)

    def pair_with(self, address: str, port: int, name: str) -> None:
        if self.pairing is not None:
            return
        me, _ = self.me()
        self.message.hide()
        self.code_intro.setText(f"Connecting to {name}…")
        self.code.setText("")
        self.code_hint.hide()
        self.code_card.show()
        self.pairing = Pairing(me, address, port, self.show_code, self.paired_ok, self.pair_failed)
        self.pairing.start()

    def show_code(self, code: str, peer: str) -> None:
        self.code_intro.setText(f"On {peer}, type this code to pair:")
        self.code.setText(f"{code[:3]} {code[3:]}")
        self.code_hint.show()

    def cancel_pairing(self) -> None:
        if self.pairing:
            self.pairing.cancel()
        self.code_card.hide()

    def pair_failed(self, message: str) -> None:
        self.pairing = None
        self.code_card.hide()
        if message:
            self.say(message)

    # -- phones -------------------------------------------------------------
    def draw_phone(self, peers) -> None:
        """Hosts show the QR code; an agent says where to find it, since a
        phone pairs with the host (the computer with the keyboard and mouse)."""
        if self.app.role == "host":
            if self.qr is None:
                self.phone_text.setText(PHONE_TEXT)
                self.qr_button.show()
            return
        hosts = [p.name for p in peers if p.role == "host"]
        where = f"your host, {hosts[0]}" if hosts else "your host"
        self.phone_text.setText(
            f"Phones pair with the computer your keyboard and mouse are plugged into: {where}. Open Curslink "
            "there and go to Devices › Pair a phone. Then put the phone beside this computer's screens on "
            "its Layout page, if that's where it sits.")
        self.qr_button.hide()

    def edge_picker(self, peer):
        """Which edge of this computer's screens leads to the device: picking
        one moves the device there in the layout."""
        try:
            cfg = self.app.config()
            me, _ = self.me()
            if not cfg.has_device(peer.device_id):
                return None
            side = cfg.side_of(peer.device_id, me.device_id)
        except Exception:
            return None
        box = QComboBox()
        box.setAccessibleName(f"Edge that leads to {peer.name}")
        if side is None:
            box.addItem("Edge: as laid out", None)
        for text, value in EDGES:
            box.addItem(f"Edge: {text.lower()}", value)
        box.setCurrentIndex(max(0, box.findData(side)))
        box.setToolTip(f"Push the pointer off this edge of your screens to reach {peer.name}. "
                       "Fine-tune it on the Layout page.")

        def picked(_index: int) -> None:
            value = box.currentData()
            if value:
                self.place(peer, me.device_id, value)

        box.currentIndexChanged.connect(picked)
        return box

    def place(self, peer, anchor: str, side: str) -> None:
        cfg = self.app.config()
        try:
            # A phone is one small screen: the whole side of this computer's
            # screens leads to it, not just the bit it happens to touch.
            device = next((d for d in cfg.devices() if d.get("id") == peer.device_id), {})
            phone = [str(sc.get("id")) for sc in device.get("screens", [])] == ["phone"]
            cfg.place_beside(peer.device_id, anchor, side, whole_side=phone)
        except (KeyError, ValueError) as exc:
            self.say(f"Couldn't move {peer.name}: {exc}")
            return
        cfg.save()
        self.app.config_saved()
        where = dict((v, t.lower()) for t, v in EDGES)[side]
        self.say(f"{peer.name} is now off the {where} edge of your screens." if side in ("left", "right")
                 else f"{peer.name} is now {where} your screens.", ok=True)

    def show_qr(self) -> None:
        if self.qr is not None:
            return
        me, _ = self.me()
        cfg = host_config(self.app.config().raw(), base_dir=self.app.config_path.parent)
        self.message.hide()
        self.qr_button.setEnabled(False)
        self.qr = QrSession(me, cfg.port, cfg.relay, self.qr_ready, self.qr_paired, self.qr_failed)
        self.qr.start()

    def qr_ready(self, url: str) -> None:
        self.qr_image.setPixmap(qr_pixmap(url))
        self.qr_image.show()
        self.qr_button.hide()
        self.qr_cancel.show()
        self.phone_text.setText("Open Curslink on your phone and tap Scan QR code. The phone needs to be on "
                                "this network to pair; afterwards it can also reach you through your relay."
                                if self.app.config().raw().get("relay") else
                                "Open Curslink on your phone and tap Scan QR code. The phone needs to be on "
                                "this network.")

    def cancel_qr(self) -> None:
        if self.qr:
            self.qr.cancel()

    def _qr_done(self) -> None:
        self.qr = None
        self.qr_image.hide()
        self.qr_cancel.hide()
        self.qr_button.show()
        self.qr_button.setEnabled(True)

    def qr_failed(self, message: str) -> None:
        self._qr_done()
        if message:
            self.say(message)

    def qr_paired(self, result, address: str) -> None:
        self._qr_done()
        self.paired_ok(result, address)

    def paired_ok(self, result, address: str | None = None) -> None:
        if address is None:
            address = self.pairing.address if self.pairing else ""
            self.pairing = None
            self.code_card.hide()
        peer = result.peer
        self.store().add(peer.device_id, peer.name, peer.role, result.key, address)
        note = f"Paired with {peer.name}."
        if self.app.role == "host" and add_to_layout(self.app, peer.device_id, peer.name,
                                                    getattr(result, "extra", {}).get("screens")):
            note += (" It's been added to the right of your screens: pick another edge for it below, "
                     "or arrange it on the Layout page.")
            self.app.config_saved()
        self.say(note, ok=True)
        self.refresh()
        if self.app.role == "agent":
            # An agent has nothing to show once it's paired: keep running in
            # the tray / menu bar instead of leaving a window open.
            if not self.app.linking:
                self.app.start_linking()
            QTimer.singleShot(1800, self.app.hide_to_tray)

    def unpair(self, peer) -> None:
        answer = QMessageBox.question(
            self, "Unpair",
            f"Forget {peer.name}? Unpair on {peer.name} too, or it keeps its old key.",
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        self.store().remove(peer.device_id)
        self.refresh()

    def say(self, text: str, ok: bool = False) -> None:
        self.message.setObjectName("info" if ok else "warn")
        self.message.style().unpolish(self.message)
        self.message.style().polish(self.message)
        self.message.setText(text)
        self.message.show()
