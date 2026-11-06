# Curslink architecture

## Goals

1. Looking at a device moves your keyboard and mouse to it, quickly and
   without false switches when you glance around.
2. Pushing the pointer off a screen edge moves it to the neighbouring
   device, with a visible animation where it lands.
3. Works across Windows, macOS and Linux today, with Android as a target
   later.

## Key decisions

### Forward input in software instead of re-pairing Bluetooth

Moving a Bluetooth mouse to another computer means disconnecting and
re-pairing, which takes seconds, differs per OS and per device, and fails
often. Curslink keeps the keyboard and mouse attached to the **host** and
forwards their events over the LAN to an **agent** on the target device.
Switching takes a few milliseconds (about 5 ms host → agent cursor move on
loopback in testing) and works with any wired, wireless or Bluetooth
device.

### One host, many agents

The host owns the physical input and the webcam. Agents only replay
input. This keeps all decision making (gaze, edges, hotkeys) in one place,
and keeps agents tiny, which matters for porting them to Android.

### A shared virtual desktop layout

Every screen of every device is placed in one global pixel space in
`curslink.toml`, arranged like the real desk. Edge switching is then plain
geometry (`curslink/layout.py`): if a push off one device's screen lands
on another device's screen, switch. Small gaps between screens are
bridged (`gap_tolerance_px`) and the entry point is inset a few pixels so
the pointer doesn't bounce straight back.

While the host is active, the OS clamps its real cursor at the desktop
edge, so a cursor resting on an outer edge pixel counts as a push. While a
remote device is active, the host's cursor is parked and suppressed, and
relative motion moves a virtual cursor across the remote screens.

### Gaze from head pose, with per-user calibration

MediaPipe Face Landmarker gives a face transformation matrix per frame.
We decompose it into yaw and pitch, smooth it with a 1-euro filter (low
jitter when still, low lag when turning), and classify it against angles
recorded during `curslink calibrate`:

* nearest calibrated screen wins,
* but only if it is within `max_distance_deg` (looking at the desk or a
  phone changes nothing),
* and is at least `switch_margin_deg` closer than the current screen
  (hysteresis),
* for at least `dwell_ms` (quick glances don't switch).

Calibration makes the maths indifferent to camera placement and sign
conventions: the camera can sit on any monitor.

Gaze never switches while a mouse button is held (no yanking mid-drag),
and can be paused with a hotkey.

Head pose, not eye tracking, was chosen for the MVP because it is robust
on ordinary webcams at desk distance. Separate monitors are usually 20 to
40 degrees apart, which head pose resolves well. Adding eye gaze for finer
targets is a possible later upgrade.

### Safety

* `Ctrl+Alt+Home` always returns input to the host.
* Keys and buttons held during a switch are released on the device being
  left, and an agent releases everything if the connection drops, so
  nothing gets stuck down.
* If the active agent disconnects or stops answering pings (7 s), input
  returns to the host.

### Transport and security

Newline-delimited JSON over TCP (`curslink/protocol.py`), wrapped in
TLS 1.2 with ECDHE-PSK. This gives encryption, mutual authentication and
forward secrecy with no certificates to manage. It needs Python 3.13+
(stdlib TLS-PSK). JSON keeps the protocol easy to implement in Kotlin for
Android.

The PSK is either a per-pair **link key** from pairing (identity
`curslink-v2:<agent id>`, looked up in the host's pairing store) or the
legacy shared `secret` stretched with scrypt (identity `curslink-v1`). The
stdlib PSK callback can't say which connection it serves, so the agent's
`hello` carries `auth = HMAC(key, tls-unique channel binding || device id)`,
which stops one paired device from claiming another's id. Link keys are
transport independent: a relayed connection runs the same TLS-PSK end to
end.

### Nearby pairing

Running hosts and agents advertise `_curslink._tcp` over mDNS
(`curslink/pairing/discovery.py`) with their id, name, role, pairing port
and, for hosts, the input port. `curslink pair` lists what it finds; picking
one opens a plain TCP connection to that device's pairing listener
(`curslink/pairing/service.py`, port 24851) and runs the handshake in
`curslink/pairing/handshake.py`:

1. The initiator sends a hash commitment to its DH public value, the
   recipient answers with its own value, the initiator reveals its value.
   (2048-bit MODP group 14, standard library only.)
2. Both derive a 6-digit code and the 32-byte link key from the shared
   secret and the whole transcript, with HKDF-SHA256.
3. The initiator shows its code; the recipient pops up a window (terminal
   prompt without a desktop) asking for it. A matching code proves there is
   no man in the middle: the commitment means an attacker gets one
   one-in-a-million guess per attempt. Three failures lock the listener
   for a minute.
4. The recipient sends a MAC with the new key; both store it in
   `pairings.json` (per-user config dir, mode 600).

The handshake runs over any asyncio stream pair, so pairing through a relay
uses the same code. A paired agent with no `[agent].host` finds its host
by id on the LAN before every connection attempt.

### Linking over the internet: the relay

`curslink relay` is a small self-hosted rendezvous server
(`curslink/net/relay.py`). The host and agents both dial *out* to it,
usually by a DNS name, so no ports need opening at home and nobody needs
to know anyone's IP address.

* **Who may use the relay**: clients and relay prove to each other that
  they know the relay key with an HMAC challenge-response. The key never
  crosses the network, and a fake relay is caught.
* **Matching**: each client sends a room id, a one-way HMAC of the
  stretched pairing secret, so host and agents with the same secret meet
  without the relay learning the secret.
  Paired devices use one room per pair instead,
  `HMAC(link_key, "curslink/v2/relay-room" || host id)`; the host keeps a
  waiting link for every paired agent and picks up new pairings within
  a few seconds (`app.HostRelays`).
* **End-to-end encryption**: once paired the relay only copies bytes, and
  host and agent run the same TLS-PSK handshake they use on a LAN through
  it (the host acts as TLS server on its outgoing socket). The relay sees
  ciphertext only and cannot read or inject input; a device with the wrong
  secret never completes the handshake.
* **Latency**: one TCP hop through the relay with Nagle off and no
  re-encryption, so the added delay is the extra network distance. Pick a
  relay close to your devices. On loopback the relay adds well under a
  millisecond.
* **Several agents**: the host keeps one spare connection parked at the
  relay (pinged every 20 s to keep NATs open) and opens another each time
  an agent takes it.
* **Fallback**: an agent with both a LAN address and a relay tries the LAN
  first for 2 s, so a laptop uses the direct path at home and the relay
  elsewhere.

`curslink invite` packs the relay address, relay key and a fresh pairing
secret into one `curslink1-...` code to paste into every device's config.

### The animation

A small helper process (`python -m curslink.overlay`) owns a hidden Tk
root and draws a short-lived, borderless, always-on-top, click-through
window where the pointer lands: an expanding "pop" ring or a two-flash
"blink". It is a separate process because Tk must own the main thread
on macOS, while the host and agent run asyncio there.

## Code map

| Module | Role |
| --- | --- |
| `curslink/router.py` | State machine: who's active, edges, gaze, hotkeys, key release |
| `curslink/layout.py` | Virtual desktop geometry, edge crossing, OS coordinate mapping |
| `curslink/gaze/headpose.py` | Rotation → yaw/pitch, 1-euro filter |
| `curslink/gaze/zones.py` | Calibration file, gaze classifier |
| `curslink/gaze/tracker.py` | Webcam + MediaPipe thread |
| `curslink/inputs/` | pynput capture (host) and injection (agent), key names |
| `curslink/net/` | Host server, agent client, relay server and relay client |
| `curslink/security.py` | TLS-PSK contexts, hello authentication |
| `curslink/pairing/` | Pairing store, code-confirmed handshake, mDNS discovery, pairing listener and window |
| `curslink/overlay.py` | Pop/blink animation helper |
| `curslink/app.py`, `cli.py` | Wiring and the `curslink` command |

The router, layout, gaze maths, protocol and networking have no platform
dependencies and are covered by the test suite, including real TLS
connections between a host and an agent.

## Roadmap

1. **Android agent** (Kotlin): speaks the same protocol. Pointer and
   gestures via an `AccessibilityService` (`dispatchGesture`), text via
   a custom IME or accessibility actions. Alternative path: the host acts
   as a Bluetooth HID keyboard/mouse to the phone (Linux BlueZ can do
   this), which needs no app on the phone at all.
2. **Pairing UX**: a paired agent added to the layout automatically from
   the screens it reports, and pairing from a tray app instead of the
   terminal. (Discovery and code pairing are done.)
3. **Tray app / settings UI** with a drag-and-drop layout editor and a
   live gaze preview.
4. **Linux Wayland** via uinput/evdev, **macOS** relative motion from
   event deltas instead of cursor parking.
5. **Clipboard sharing** between devices.
6. Optional eye-gaze refinement for choosing between close monitors.
