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

### Confirming with the agent's own camera

When an agent has its own screen (two laptops and one monitor, say), the
host's camera can see your head turn away from its screens but can't be
sure you're now looking at the laptop. An agent with `[agent.gaze]`
enabled runs its own head-pose tracker and tells the host, in `facing`
frames, whether a face is looking straight at it (within `facing_deg` of
the camera's "straight on", debounced, refreshed every second). The host
(`curslink/gaze/confirm.py`) then only switches when both cameras agree:

* a gaze switch to an agent with a camera waits until that agent reports
  you facing it;
* an agent reporting you facing it gets the pointer when the host's
  camera confirms you've turned away from the host's screens (or towards
  that agent's calibrated screen). Losing your face on the host camera
  counts as turned away, as long as that camera is still working.

So the agent doesn't need to be calibrated on the host at all. Agents
without a camera keep the host-only behaviour, and `confirm_with_agents =
false` in `[gaze]` turns the check off. Agents only send `facing` to a
host that lists it in `welcome.features`, so mixed versions keep working.

### Any device's camera, on its own

The host's camera isn't required. When it's off, uncalibrated, or has
stopped seeing anyone for `host_alive_s`, the cameras that are left decide
on their own ("facing only"):

* turning to a device whose camera sees you moves the pointer there (only
  the turn, not the once-a-second "still facing" refresh, so two cameras
  that both see you don't pass the pointer back and forth, and "bring
  input home" sticks until you look away and back);
* when the device you're on stops seeing you, the pointer goes to another
  device that does, or home to the host if the host has no camera of its
  own (with two devices, not looking at the laptop means looking back at
  the host); if the host's camera is on and doesn't see you either, it
  stays put.

The host's own camera without a calibration works the same way: it only
reports whether you face the host (`[gaze] facing_deg`, `facing_dwell_ms`,
`center_*`), like an agent's. So "laptop camera only", "both cameras, no
calibration" and "calibrated host plus agent cameras" all work.

Gaze picks a *device*, not a monitor: looking at another computer brings
the pointer back to wherever you left it there, and looking between the
monitors of the device you're on does nothing. Monitors listed in `[gaze]
jump_screens` (Settings > "Jump to the monitor you look at") are the
exception: looking at one puts the pointer on that monitor, where it last
was there, even on the device you're already using. Only a calibrated look
counts; a device's own camera only knows the device, so it still picks the
device. (The older `within_device = true` turns every monitor on.)

While the desktop app calibrates, the host keeps linking but hands its
camera to the app (`calibrate on` on the service's stdin). For a screen of
another computer the app sends `look {...}`, and the host passes a `look`
frame to that agent, which shows the same "Look here" card, without
buttons, on that screen. Only agents that list `look` in `hello.features`
get one. Afterwards the app restarts the service, which brings gaze back
with the new calibration.

Head pose, not eye tracking, was chosen for the MVP because it is robust
on ordinary webcams at desk distance. Separate monitors are usually 20 to
40 degrees apart, which head pose resolves well. Adding eye gaze for finer
targets is a possible later upgrade.

### Dragging files to another device

`curslink/files/`. On a Mac host, the drag pasteboard says what's being
dragged: when input leaves with the button held, the host checks whether
its change count moved since the button went down and it holds file URLs
(`curslink/files/macdrag.py`). Windows and Linux don't tell a program that
isn't under the pointer that a file is being dragged, so there the host's
overlay helper puts invisible drop targets where the drag will be:

* while the left button is held and the pointer moves, thin strips along
  the screen sides that lead to another device; a file dragged to the edge
  passes over one, and the OS tells the helper its paths;
* when input leaves with the button held some other way (a look, a
  hotkey), a target over every host screen (the "shield"), and the host
  nudges the parked pointer a pixel so the OS reports the drag to it.

With input on another device and a file drag held, the host sends `drag
{card}` and that agent's helper shows a placeholder (name, kind, size)
following its pointer. Letting go there isn't forwarded as a click: the
host sends the files as `file_*` frames on the same TLS-PSK connection
(32 KiB per frame, written only as fast as the link drains, so input queued
behind them is barely delayed), and ends the drag on the host by letting
an injected Escape (which cancels a drag on macOS and Windows) and then a
button-up through the grab, over the shield, which refuses any drop, so the
source app does nothing. The agent writes into a hidden staging
folder, moves the files into Downloads only once all of them have arrived,
never overwrites (`name (1).ext`), cleans paths (no `..`, nothing absolute)
and shows a progress popup. Coming back to the host with the button held
just carries on the drag there.

Files start moving as soon as the drag crosses (agents that list `drop`
get `drag {id, card}` and the bytes straight away; `drag {drop, id}` or
`drag {hide, id}` follows). Agents that list `file_ack` acknowledge bytes
as they arrive (`file_ack {id, n}`, asked for by `file_begin {ack: true}`)
and the host keeps only 128 KB in flight while the drag is carried (512 KB
once dropped), so the pointer's moves and the drop never queue behind file
data in the socket buffers or on the Wi-Fi. The receiving side then makes it a real drop
(`curslink/files/native.py`): the card follows the pointer at once, empty
placeholders with the real names go in a temp folder, the overlay helper
puts a small window under the pointer and starts a real OS drag of those
paths with the file card as its image (on a Mac straight away through
AppKit, as the button is held already; elsewhere once the device presses
the button on it). Such a drag has no mouse-down of the Mac's own, so apps
under it see no mouse-dragged events and many only take the drop once in
front: while it's held, the window the pointer rests over for a quarter of a
second is brought forward (`curslink/files/focus.py`; a Windows agent does
the same for drags from the host). Let go on the desktop or in a
folder and the file manager copies them there; on an app, it gets the temp
paths. If you let go before every byte has arrived, the button-up waits
(the agent simply doesn't send it yet; a Mac host's event tap holds the
real one back) with a progress popup, then the drop happens. Where a real
drag can't be started (a Windows or Linux host, a Mac or Linux agent, or it
doesn't start within 1.5 s) the card follows the pointer and the files go
to Downloads.

The other way, agent to host: coming home with an agent's left button held,
the router doesn't send that button-up but `drag_out`. The agent usually
knows already: while the host holds its left button, thin drop targets line
the outer edges of its screens, and the drag tells them what it carries as
it reaches the edge. Otherwise it covers its screens with the shield and
nudges the pointer so the OS reports the drag (on Windows, falling back to
the selection in the Explorer window in front). It answers
`drag_out {id, card}` at once, then ends its own drag with Escape; the host
sends `fetch {id}` at once and the files come as `file_*` frames from the
agent, into the same real-drop machinery on the host. Gaze may carry a
drag home this way too (otherwise it never moves the pointer mid-drag). Agents list `files` in `hello.features`
(`[agent.files] enabled = false` turns it off); others never see these frames.

### Mirror mode: seeing behind a screen in front

With overlapping screens a laptop on the desk hides part of the monitor
behind it, often the corner with the clock, the tray and notifications. With
`[layout] mirror = true`, while you use the screen behind, every screen in
front of it shows the part it covers, live (`curslink.mirror`).

- The host plans it: from the layout and the screen in use it works out one
  stream per screen in front (`Layout.covering`), for any pair of devices,
  including the host itself and two screens of one computer, and sends each
  device the whole of what it should capture and show (`mirror` frames).
- The device behind captures only the covered rectangle in its overlay
  helper (`QScreen.grabWindow`), skips unchanged frames, and sends JPEGs
  sized to fit one protocol frame (`mirror_frame`). The host passes them to
  the device in front, which draws them in a click-through window over the
  same part of its screen and answers `mirror_ack`; the answer goes back to
  the device capturing, which keeps at most two frames on their way, so a
  slow link drops frames instead of queueing them in front of the pointer.
- The pointer: with `mirror_pointer` (on by default) the layout lets the
  device behind keep the covered part (`Layout.pointer_through`), so the
  pointer stays on the screen in use there; the host sends where it is in
  the mirror (`mirror_pointer`) and the mirror draws it.
- Lining up: screens are laid out in their own pixels, which aren't the
  same size on every monitor, so the layout's overlap is only roughly what's
  covered. "Line up overlap…" on the Layout page takes one click on each
  screen (`curslink.gui.fitcalib`, on another computer through the same
  `pick`/`hint` path as edge calibration): on the screen behind, where a
  corner of the screen in front sits; on the screen in front, where a corner
  of the screen behind sits behind it. Two corners from opposite sides of
  the covered area give a `Fit`: how big one's pixels are against the
  other's, and where it stands, saved as `[[layout.fit]]`;
  `Layout.covered_parts` then says what to capture behind and where it
  shows in front.
- Fine-tuning: what lines up depends on where you sit (the screen in front
  is nearer to you), so "Fine-tune…" (`curslink.gui.fittune`) holds the
  mirror up and moves or resizes it with the arrow keys and +/- while you
  look. Each change goes to the service as `fit {...}`
  (`curslink.mirror.apply_fit`), so it shows at once; Save writes the
  `[[layout.fit]]` without a restart.
- Curslink's own overlay windows (the edge indicator, mirrors) are kept out
  of captures (`curslink.capturehide`; on macOS the capture also leaves out
  the helper's own windows), so they don't show up twice.
- Phones take part like any agent that lists the `mirror` feature: the app
  draws mirrors in its pointer overlay, and captures with Android's screen
  capture (MediaProjection), which asks the first time it's needed.

### Carrying Isolink's floating chats

Isolink, the messenger, floats chats in small windows of their own on a
computer. Drag one over an edge, or look at another device while holding
it, and the same chat opens there under the pointer, still held. Each
device's Curslink is an Isolink Connect app (`curslink/isolink.py`): it says
hello on `127.0.0.1:47231` asking for the `windows` capability, Isolink asks
the person once and gives it a secret, and from then on Isolink tells it
which chat is being dragged (`windows.drag`) and opens one on request
(`windows.open`, held). Chats and the account are per-app handles, the same
on all of the person's devices, so two devices are linked to one Isolink
account when their `me` handles match.

The host decides (`curslink/popups.py`). Agents report their Isolink's
state (`isolink {me, windows, dragging}`, only to a host whose welcome lists
`popups`). On a switch away from a device dragging a chat to one with the
same `me`, the new device presses the left button where its pointer is and
opens the chat held (`popup_in` → `popup_result` on an agent), the old one
lets go where it is (an injected button-up on the host, the router's usual
release on an agent), and once the chat is open on the new device it closes
on the old one (`popup_close`). A chat dragged on an agent isn't asked about
as a file (`drag_out`), and gaze may move input mid-drag with one held.
`[isolink] enabled` on the host and `[agent.isolink] enabled` turn it off.

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

### Pointer motion: sent at once, played back evenly

Each mouse report is sent as soon as it is captured (no batching beyond
what the host's loop hasn't got to yet; TCP_NODELAY is on, as asyncio sets
it), stamped with the host's capture time (`move.ts`). Measured over real
TLS-PSK on one machine at 1000 reports a second, a report reaches the
agent in 0.4 ms typically and under 5 ms at worst, so Curslink's own path
isn't what makes a pointer lag.

Real links aren't that even: Wi-Fi in particular holds packets for tens of
milliseconds and then delivers them together. The agent therefore plays
moves back on the host's timeline (`curslink/smoothing.py`), a few
milliseconds behind it, and moves the pointer between reports at 250 Hz.
How far behind adapts to the link as it goes: the target is the 98th
percentile of how late reports have arrived over the last three seconds,
plus one gap between reports (needed to have the next point to move
towards), capped at 120 ms. On a clean link with a fast mouse that's about
2 ms. The delay glides towards its target rather than jumping, since a
jump in the delay is a jump or freeze of the pointer: playback runs
slightly slow while it builds slack and slightly fast while it catches up. Clicks, scrolls and keys don't wait;
the pointer snaps to the newest position first, so they land where the
host saw them.

### Pointer moves by UDP

Over TCP a lost packet holds up every frame behind it until it's resent,
and the retransmission timeout is 200 ms or more: on Wi-Fi that was most
of the p95 round trip. A move is an absolute position, so a lost one only
matters until the next one arrives a few ms later. So when the agent
reached the host directly (not through a relay), moves go as UDP datagrams
to the same port number as the TCP listener (`curslink/net/udp.py`), and
everything else stays on TLS.

The host hands the agent a fresh random key and connection id inside the
TLS session (`welcome.udp`), only if `hello.features` lists `"udp"`. The
agent probes the host twice a second from its own socket, which also lets
the replies through its firewall; the host acknowledges each probe and
sends moves by UDP only while the agent's probes say it is hearing those
acknowledgements. When they stop (network change, firewall), moves go back
to TCP within about 1.6 s. Datagrams are encrypted (HMAC-SHA256 keystream)
and authenticated (truncated HMAC-SHA256), with strictly increasing
sequence numbers against replay. The agent drops any move older by `ts`
than the newest it has seen on either channel, and ignores UDP moves while
input isn't on it. Before a click, scroll, key, leave or drag, the host
repeats its latest UDP-only move over TCP, so those land where the pointer
is even if that datagram was lost.

A direct Wi-Fi link was considered and isn't possible between a Mac and a
PC: macOS has no public Wi-Fi Direct API and AWDL is Apple-only. Joining
one machine's hotspot removes the access point hop but takes the other off
its network, for little gain. A cable (Ethernet, or a Thunderbolt bridge)
is the fastest link and needs nothing from Curslink.

### Network diagnostics and the optimiser

Settings > Network shows each link as the running host or agent measures
it, from a `network` event every 2 s (`curslink/net/check.py`): the path
(direct with moves by UDP, direct TCP only, or the relay), the round trip
and jitter of the host's pings over TLS (last minute), packet loss by UDP
and the interface the connection goes out on (Wi-Fi, wired, VPN), graded
good / fair / poor with plain-language tips. Loss is counted from the
gaps in sequence numbers (`curslink/net/quality.py`): the host counts the
agent's probes, and the agent counts the host's ACKs and moves and reports
that every 5 s in a `netstats` frame (only to a host whose
`welcome.features` lists `"netstats"`; the host sends the agent its own
round-trip numbers in its pings in return, only to an agent that listed
`"netstats"`). "Check the network" sends the `netcheck` command: an mDNS
browse (does this computer see its own announcement, and the devices it
links with?) and the list of interfaces.

With `[network] optimise` on (the default), the host re-tunes each link
every 2 s, with thresholds that switch on at worse numbers than they switch
off at and a 20 s hold so nothing flaps:

* lossy UDP (1% or more lost): each move is sent twice, the second a
  `MOVE2` datagram 4 ms later with the same sequence number, so the first to
  arrive is taken. Only to agents that list `"netstats"` (older agents and
  the Android app would ignore MOVE2 anyway);
* a fair link (p95 60 ms+, 1% loss or 15 ms jitter): at most 250 moves a
  second, the agent's own playback rate; a poor one (p95 150 ms+ or 5%
  loss): 100 a second. Moves are absolute, so a held-back move is replaced
  by the next and the newest goes when its turn comes; a click, key or
  leave sends the held one first. A good link sends every move at once;
* all of Curslink's sockets are marked DSCP EF, which most Wi-Fi access
  points send in the WMM voice queue, ahead of bulk traffic.

An agent connected through the relay looks for the host on the LAN every
30 s and reconnects directly once it answers there (never while input is on
it).

Both ends log where time goes every 10 seconds while input is remote: the
host logs mouse report rate, loop wait, event tap time and ping round trip;
the agent logs arrival rate, lateness, longest gap, playback delay and
injection time. That's the first thing to read in a diagnostics bundle
when the pointer feels laggy.

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

### The desktop app

`curslink gui` (what the installers' Start menu entry, Curslink.app and the
Linux menu entry open) is a Qt (PySide6) window plus a tray / menu bar
icon. Closing the window leaves it in the tray, and it can start at sign-in
(a per-user `Run` value on Windows, a LaunchAgent on macOS, an XDG autostart
entry on Linux: `curslink/autostart.py`).

The app does not run the host or agent itself. It starts the same
`curslink host` / `curslink agent` the command line runs, with `--events`:
the child prints one `@curslink {json}` line per event on stdout (running,
agent connected, switch, gaze paused, log records) and takes commands on
stdin (`home`, `gaze pause|resume|toggle`, `stop`), and it stops when stdin
closes, so it never outlives the app (`curslink/events.py`). That keeps Qt
on the main thread, asyncio, input capture and the camera in their own
process, and a crash in one away from the other. The app also writes
`curslink.toml` (with tomlkit, which keeps comments) for the layout editor,
settings and first-run setup, and restarts the child to apply them.

The icon masters are in `curslink/assets` (drawn on Apple's 824-in-1024
grid); `packaging/icons/make_icons.py` turns them into the committed `.ico`,
`.icns` and Linux PNGs.

### Windows: admin apps, UAC prompts and the lock screen

Windows drops input a normal program injects into a program running as
administrator (UIPI), and UAC prompts and the lock screen sit on the secure
"Winlogon" desktop, which only SYSTEM can reach. The opt-in Settings toggle
"Control admin apps and UAC prompts" (one admin prompt) copies
`curslink-helper.exe`, a small one-file build, into
`Program Files\Curslink Helper` and installs it as the `CurslinkHelper`
service (`curslink/winhelper`):

* the service (LocalSystem) keeps one injector running in the console
  session, starting a new one on a user switch and restarting it with
  backoff if it dies;
* the injector (SYSTEM, in that session) serves a named pipe only that
  session's user and SYSTEM can open, local clients only, and before each
  command moves its thread to whichever desktop has the input;
* the agent sends input to the pipe when it's there and injects it itself
  when it isn't, so turning the helper on or off needs no restart.

The copy lives in Program Files so a program running as the user can't
replace what SYSTEM runs; its logs are there too for the same reason. The
trade-off is that anything running as the user can drive the pipe, UAC
prompts included, which is why it's off by default. Turning it off (or
uninstalling Curslink) removes the service and the copy. Ctrl+Alt+Del
can't be injected at all.

## Code map

| Module | Role |
| --- | --- |
| `curslink/router.py` | State machine: who's active, edges, gaze, hotkeys, key release |
| `curslink/layout.py` | Virtual desktop geometry, edge crossing, OS coordinate mapping |
| `curslink/gaze/headpose.py` | Rotation → yaw/pitch, 1-euro filter |
| `curslink/gaze/zones.py` | Calibration file, gaze classifier |
| `curslink/gaze/confirm.py` | Agent "facing me" detector, host two-camera arbiter |
| `curslink/gaze/tracker.py` | Webcam + MediaPipe thread |
| `curslink/inputs/` | pynput capture (host) and injection (agent), key names |
| `curslink/net/` | Host server, agent client, relay server and relay client |
| `curslink/security.py` | TLS-PSK contexts, hello authentication |
| `curslink/pairing/` | Pairing store, code-confirmed handshake, mDNS discovery, pairing listener and window |
| `curslink/overlay.py` | Pop/blink animation helper |
| `curslink/isolink.py`, `popups.py` | Isolink Connect client; carrying its floating chats between devices |
| `curslink/winhelper/` | Windows input helper service: admin apps, UAC prompts, lock screen |
| `curslink/app.py`, `cli.py` | Wiring and the `curslink` command |
| `curslink/gui/` | The desktop app: window pages, tray, child process |
| `curslink/events.py` | Status events and commands between the app and its child |
| `curslink/configedit.py`, `autostart.py` | Writing `curslink.toml`; start at sign-in |

The router, layout, gaze maths, protocol and networking have no platform
dependencies and are covered by the test suite, including real TLS
connections between a host and an agent.

## The Android agent (`android/`)

A Kotlin app speaking the same protocol. `android/core` is a plain JVM
library (TLS-PSK through BouncyCastle, since Android's own TLS dropped PSK;
pairing; UDP moves; the relay), tested in CI against the real Python host
(`android/interop/host.py`). The app around it:

* A foreground service keeps the link open while the phone sleeps (the app
  asks to be left out of battery optimisation, and asks the host for a ping
  every 5 s, `hello.ping_s`, instead of every 0.5 s). It advertises the
  phone over mDNS for pairing and finds the host the same way.
* On `enter` it turns the screen on (`ACQUIRE_CAUSES_WAKEUP`) and opens an
  invisible activity over the lock screen that asks to dismiss it. The
  running accessibility service is what lets it start from the background.
* An `AccessibilityService` draws the pointer in an accessibility overlay
  (shape from the node under it), plays the left button as one continued
  touch stroke, scrolls by sliding a finger with a pause before lifting (no
  fling), and types through the Android 13 accessibility input method.
* The phone reports its screen in dp, upright; the host's coordinates are
  scaled onto the screen as it's turned now, so rotating keeps the edge.
* Pairing by QR code: the host's Devices page shows `curslink://pair?...`
  with a one-time token; the phone proves it in place of a typed code (see
  `curslink.pairing.qr`). Pairing messages carry `screens`, so the host lays
  the phone out at its real size before it ever connects.

## Roadmap

1. **Android agent**: done in `android/` (see below). Still open: the host
   acting as a Bluetooth HID mouse/keyboard to the phone, which would give
   Android's own pointer and hover; files and Isolink popups to a phone.
2. **Pairing UX**: answer incoming pairing requests in the app's window
   rather than the Tk dialog. (Discovery, code pairing, pairing from the
   app and adding a paired agent to the layout are done.)
3. **App polish**: a live camera preview while calibrating, and signed,
   notarised builds so the installers don't warn.
4. **Linux Wayland** via uinput/evdev, **macOS** relative motion from
   event deltas instead of cursor parking.
5. **Clipboard sharing** between devices.
6. Optional eye-gaze refinement for choosing between close monitors.
