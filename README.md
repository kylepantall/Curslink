<p align="center"><img src="docs/images/icon.png" width="128" alt="Curslink icon"></p>

<h1 align="center">Curslink</h1>

**Look at a device, and your mouse and keyboard follow.**

Curslink lets one keyboard and mouse drive all your computers. A webcam
notices which screen you're looking at and sends your pointer and typing
there. Or push the pointer off the edge of one screen and it pops out on the
next computer, with a quick "pop" or "blink" so you always see where it went.

[**Download the latest release**](https://github.com/kylepantall/Curslink/releases/latest) ·
[How it works](#how-it-works) · [Getting started](#getting-started) ·
[FAQ](#faq)

## Why Curslink

* **Look to switch.** Glance at your laptop and your keyboard and mouse are
  on your laptop. No hotkeys, no reaching for a KVM switch.
* **Or slide across.** Arrange your screens like your desk and move the
  pointer between computers as if they were one big display.
* **Any mouse, any keyboard.** Your Bluetooth or USB devices stay connected
  to one computer; Curslink forwards their input over your network in
  milliseconds, instead of slowly re-pairing.
* **Pair with a code.** Devices find each other on your network. Type a
  6-digit code once and they're linked, with their own encryption key.
* **Works across the internet too**, through a small relay you host yourself.
* **Windows, macOS and Linux, and Android phones.** Push the pointer off
  the edge you picked for your phone and it wakes up, even locked, with
  your mouse and keyboard on it.
* **Keeps itself up to date**, with every download checked before it installs.

> Curslink is young (0.x). It works day to day, but expect rough edges, and
> please [open an issue](https://github.com/kylepantall/Curslink/issues)
> when you hit one.

## How it works

```
 ┌──────────── host (keyboard + mouse + webcam) ────────────┐        ┌──── agent ────┐
 │ webcam → head pose → gaze classifier ┐                   │  TLS   │               │
 │                                      ├→ router ──────────┼───────→│ inject input  │
 │ keyboard/mouse capture ──────────────┘   (who's active?) │  LAN   │ + animation   │
 └──────────────────────────────────────────────────────────┘        └───────────────┘
```

* The **host** is the computer your keyboard, mouse and webcam are plugged
  into. It captures your input and, while you're using another computer,
  sends it there instead.
* Every other computer runs an **agent**, which plays back what it receives
  and shows the switch animation.
* The webcam only estimates which way your head is turned, on the host
  itself. No video leaves your computer.
* **Drag files across, either way.** Drag files or folders over the edge
  to another computer (or hold them and look at it), and back. They start
  sending straight away, and on the other side it's a real drag: drop on the
  desktop or in a folder and they're saved there, drop on an app and it
  opens them. Let go before a big file has arrived and the drop waits, with
  a progress popup.
* **App mirror.** Drag a window by its title bar over the edge to another
  computer and it opens there, live, its own shape and real size, with no
  frame of its own (a Mac app on a PC looks like the Mac app). Point into it
  and your mouse and keyboard are on the computer the app runs on; its own
  title bar moves it, and dragging it back home puts it away. Files dragged
  into it go to the app. A phone's screen can be dragged across too, and a
  phone shows computers' apps. Turn it off in Settings > App mirror.
* **One clipboard.** Copy text, a picture or files on one computer and
  paste them on any other. Passwords copied from a password manager stay
  put, and copies over 25 MB aren't shared. Turn it off in Settings >
  Clipboard.

## Getting started

### 1. Install it on every computer

| Platform | Download | What it does |
| --- | --- | --- |
| Windows 10/11 | `curslink-X.Y.Z-windows-x64-setup.exe` | Installs for your user (no admin needed) and adds `curslink` to your PATH |
| macOS (Apple silicon) | `curslink-X.Y.Z-macos-arm64.pkg` | Installs `Curslink.app` to `/Applications` and `curslink` to `/usr/local/bin` |
| Debian / Ubuntu | `curslink-X.Y.Z-linux-x64.deb` | `sudo apt install ./curslink-*.deb` |

Get them from the [latest release](https://github.com/kylepantall/Curslink/releases/latest).
They include everything, so you don't need Python. Portable `.zip` and
`.tar.gz` builds are there too if you'd rather not install.

The installers aren't code-signed yet. On Windows, click **More info**, then
**Run anyway**. On macOS, right-click the `.pkg` and choose **Open**.

### 2. Set it up

**With the app:** open **Curslink** from the Start menu, Applications, or
your app menu (or run `curslink gui`). It asks whether this computer is the
host or an agent, then helps you pair devices, arrange screens and
calibrate the camera. Close the window and it keeps running in the tray or
menu bar.

**Line up the screens with your phone:** on the host's Layout page, choose
**Line up with camera…**. Every screen (on every linked computer) shows a
few square patterns. With the Curslink Android app the camera opens by
itself (or tap **Line up screens with the camera** in the app): sit where
you usually sit and hold the phone up in front of your eyes with every
screen in the picture. The patterns move around each screen a few times a
second, so the camera measures each screen's whole face from where it is:
how far away it stands, at what angle, how big, and how curved if it's a
curved monitor. A screen shows a green edge once it's measured, the layout
on the computer keeps adjusting, and it stops by itself when every screen
is lined up, usually in a few seconds. (A monitor that doesn't report its
size needs the phone moved a little from side to side; the phone says so.)
To finish, the screens draw short bright lines across where they meet:
from where you sit each should look like one straight line. The camera
measures any step in them and moves the layout until they join, so you can
see for yourself that it's right (they stay up a few seconds, and the
Layout page says afterwards how many were checked and how close they came).
A computer's own two screens are checked against each other too. Last, where
one screen stands in front of another, both draw one pattern of blocks and
the overlap is nudged until the blocks carry on unbroken across the edge:
hold the phone close to your eyes for that (once they join they stay up
five seconds more, still being measured, so you can see it for yourself). A laptop that's turned in or
tipped back is seen against the monitor as a leaning shape, not a
rectangle: the line-up measures that shape, and the mirror and the pointer
go by it. (What lines up still depends on where you look from. **Settings >
Overlapping screens > Follow my head** is an experiment that watches your
head with this computer's camera and keeps the overlap lined up as you
move.) The pattern step also measures the frame (bezel) round the screen
in front: with **Settings > Overlapping screens > Allow for the frame round
the screen in front** on, that screen shows everything it and its frame
hide of the one behind, so a line of text never runs under the frame. A
line-up that passes these checks is saved by itself; one that doesn't
(or a single photo) is left for you to check and press **Save layout**.
Without the
app, take one photo of the desk with any camera and choose it there: that
works too, but one photo can't tell how far away each screen is.

**From the command line:**

1. Copy [`examples/curslink.toml`](examples/curslink.toml) to each device and
   edit it: describe every device's screens in one shared layout (arranged
   like your desk) on the host, and set `[agent]` `device_id` and `name` on
   agents.
2. Check the layout on the host: `curslink check`
3. Start agents: `curslink agent` (on each other device)
4. Pair them: run `curslink pair` on the host, pick a device from the list,
   and type the 6-digit code it shows into the window that pops up on that
   device. No secrets to copy, and agents find the host on the network by
   themselves. Add `--as host` if the config also has an `[agent]` table,
   like the example. (You can also run `curslink pair` on an agent while
   the host is running.)
5. Teach it where your screens are: `curslink calibrate` (on the host;
   look at each screen when asked)
6. Run the host: `curslink host`

`curslink pair --list` shows what a device is paired with and
`curslink unpair ID` forgets one. If your network blocks multicast,
`curslink pair --address 192.168.1.20` pairs with a device directly.

### Over the internet (self-hosted relay)

When the devices aren't on the same network, run a relay somewhere they
can all reach, for example a small VPS with a DNS name. The relay only
matches devices up and forwards encrypted bytes; it never sees your
pairing secret, so it can't read or inject keystrokes.

1. On the server: `curslink relay --init` writes `relay.toml` with a random
   relay key. Open TCP port 24852, then run `curslink relay -c relay.toml`
   (see [`examples/relay.toml`](examples/relay.toml)).
2. Make a pairing code:
   `curslink invite --relay relay.example.com:24852 --relay-key <key>`
3. Put the code in `curslink.toml` on the host **and** every agent, in place
   of `secret`:
   ```toml
   [relay]
   code = "curslink1-..."
   ```
   (or set `CURSLINK_PAIRING_CODE`). `[relay] address` and `key` with a
   normal `secret` work too.
4. Start `curslink host` and `curslink agent` as usual. Agents can drop
   `[agent].host`; if they keep it they try the LAN first and fall back to
   the relay. Set `[host] lan = false` to only accept agents via the relay.

Devices you've already paired with `curslink pair` don't need a pairing
code: give them just `[relay] address` and `key`. Each paired agent then
meets the host in its own relay room, derived from that pair's key, and a
laptop paired at home keeps working through the relay when it's away.

Hotkeys on the host:

| Keys | Action |
| --- | --- |
| `Ctrl+Alt+Home` | Bring input back to the host, always |
| `Ctrl+Alt+G` | Pause / resume gaze switching |

Try the animations with `curslink overlay-test --effect pop`.

### Install from source

Prefer Python? With Python 3.13+:

```bash
pip install "curslink @ git+https://github.com/kylepantall/Curslink"          # every device
pip install "curslink[gaze] @ git+https://github.com/kylepantall/Curslink"    # host, for the camera
```

### Updates

Installed copies keep themselves up to date. When `curslink host` or
`curslink agent` starts, it checks for a newer release, downloads the
installer for your platform, checks it against the release's
`SHA256SUMS.txt`, installs it and carries on with the new version. macOS and
Linux ask for your password; Windows updates silently. While it runs, it
checks once a day and installs what it finds at the next start.

* `curslink update` updates right now (`--check` only looks, `--pre`
  includes release candidates).
* To turn it down or off, set `CURSLINK_NO_UPDATE=1` or add this to
  `curslink.toml`:

  ```toml
  [update]
  check = true        # look for new releases
  auto = true         # install them at startup (false: just tell me)
  prerelease = false
  ```

## FAQ

**Does the camera record me?** No. Frames are processed in memory on the
host to estimate head angle, then thrown away. Nothing is saved or sent.

**Do I need a webcam?** No. Without one, use edge switching (push the
pointer off a screen) and the hotkeys.

**Will it work with my Bluetooth mouse and keyboard?** Yes. They stay
connected to the host as they are; Curslink forwards their input in
software, so any mouse or keyboard works.

**Do shortcuts like Alt+F4 and Cmd+Tab go to the other computer?** Yes.
While input is on another computer, the host's own system shortcuts are
off, so they go there instead. From a Mac keyboard to a PC, Option is Alt
(Option+F4 is Alt+F4, also with the Spotlight or Launchpad key in F4's
spot) and Cmd is Ctrl. Ctrl+Alt+Delete and Windows+L can't be sent:
Windows only takes them from a real keyboard.

**Is my typing safe on the network?** Yes. Everything is encrypted, and
only devices you've paired can connect. See [Security](#security).

**Can two computers use a cable between them?** Yes, when the cable makes a
network: a Thunderbolt or USB4 cable with Thunderbolt Bridge on a Mac and
the USB4 P2P or Thunderbolt Networking adapter on Windows, or an Ethernet
cable straight between them. Curslink finds the other computer on the
cable, moves the link onto it (when the pointer is back on the host, so
nothing is cut off) and goes back to the LAN within a few seconds of it
being unplugged. Settings > Network shows which one is in use. Mac to
Windows over USB-C often doesn't link up at all: if neither computer shows
the other in its network settings, a plain wired LAN does as well.

**Wayland?** Not yet; Wayland blocks global input capture. Use an X11
session for now.

## Platform notes

* **macOS**: allow Accessibility and Input Monitoring for your terminal (or
  Python) in System Settings → Privacy & Security, plus Camera on the host.
* **Linux**: X11 sessions only for now. Camera switching needs the EGL and
  GLES libraries (`libegl1 libgles2` on Debian and Ubuntu), which most
  desktops already have. Wayland blocks global input capture
  and injection; a uinput backend is planned.
* **Windows**: input can't be injected into apps running as administrator
  unless the agent runs elevated too.
* **Android** (8 or later; `curslink-X.Y.Z-android.apk` from the release):
  install it, turn on *Curslink mouse and keyboard* in Settings →
  Accessibility, and allow it to run in the background. Then pair: on the
  computer, open **Devices → Pair a phone** and scan the code with the app
  (or pick the phone under Nearby and type the code on the phone). Choose
  which edge of your screens leads to it next to it under *Paired*.
  Android has no way for an app to move a real mouse pointer, so Curslink
  draws its own and turns clicks, drags and scrolls into touches;
  right-click is a long press, middle-click is Home, and Esc or the
  mouse's back button is Back. Typing goes straight into the text field
  on Android 13 and later (basic typing before that). A PIN or
  fingerprint lock still asks to be unlocked when the phone wakes; you can click the PIN in with the mouse. If it won't
  link, **Export logs** in the app shares what the phone tried and what
  the computer answered (never the keys).
  The app updates itself from the releases too: it looks a few times a
  day, downloads the new APK on Wi-Fi (checked against `SHA256SUMS.txt`)
  and installs it while the screen is off. Android asks once to let
  Curslink install apps and to confirm the first update; on Android 12
  and later the ones after that go in by themselves. *Updates* in the app
  shows the version, checks by hand, and turns this off.

## Security

All traffic is encrypted and mutually authenticated with TLS-PSK (ECDHE,
forward secret). Pairing gives every host and agent pair its own random
key, agreed with a Diffie-Hellman exchange and confirmed by the code you
type, so someone else on the network can neither listen in nor slip in
between. Keys live in `pairings.json` in your user config folder, readable
only by you. A device that isn't paired can neither connect to the host
nor impersonate it.

A pairing request only succeeds if someone at the other device types the
code, and three wrong codes lock pairing for a minute. Set
`[pairing] enabled = false` once everything is paired if you'd rather not
listen for requests at all.

The older shared `secret` still works for devices you haven't paired. Use
a long, random one, since anyone with it can send keystrokes to your agents.

Through a relay the same TLS runs end to end between host and agent; the
relay only forwards ciphertext. The relay key just controls who may use
your relay. A pairing code contains the pairing secret, so share it like a
password.

## Contributing

Bug reports and ideas are welcome in [issues](https://github.com/kylepantall/Curslink/issues). To work on Curslink:


```bash
pip install -e ".[dev]"
pytest
```

To build the packaged app locally: `pip install ".[gaze]" pyinstaller`, then
`pyinstaller packaging/curslink.spec` (output in `dist/curslink/`, and on macOS
also `dist/Curslink.app`).

Releasing (maintainers): bump the version in `pyproject.toml` and `curslink/__init__.py`,
then push a tag such as `v0.2.0`. The Release workflow builds every platform
and publishes a GitHub release with the installers, the archives and
`SHA256SUMS.txt`; installed copies pick it up on their next start. To build an
installer locally after PyInstaller: `packaging/make-installer.sh 0.2.0
linux-x64 out` (needs Inno Setup on Windows, Xcode tools on macOS, `dpkg-deb`
on Linux).

Design notes and the roadmap live in [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

## License

Curslink is free to download, install and use, but it isn't open source:
the code may not be reused or redistributed. See [LICENSE](LICENSE).
