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
TLS 1.2 with ECDHE-PSK. The PSK is derived from the shared secret with
scrypt. This gives encryption, mutual authentication and forward secrecy
with no certificates to manage. It needs Python 3.13+ (stdlib TLS-PSK).
JSON keeps the protocol easy to implement in Kotlin for Android.

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
| `curslink/net/` | Host server and agent client |
| `curslink/security.py` | TLS-PSK contexts |
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
2. **Discovery and pairing UX**: mDNS so agents find the host, a pairing
   code instead of hand-written secrets, auto-detected screen sizes.
3. **Tray app / settings UI** with a drag-and-drop layout editor and a
   live gaze preview.
4. **Linux Wayland** via uinput/evdev, **macOS** relative motion from
   event deltas instead of cursor parking.
5. **Clipboard sharing** between devices.
6. Optional eye-gaze refinement for choosing between close monitors.
