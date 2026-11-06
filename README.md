# Curslink

**Look at a device, and your mouse and keyboard follow.**

Curslink shares one keyboard and mouse between several computers. A webcam
watches which way your head is turned and, when you look at another
device's screen, your pointer and keystrokes go there. You can also just
push the pointer off the edge of a screen into the next device, with a
"pop" or "blink" animation so you always see where it landed.

> Status: MVP foundation. Windows, macOS and Linux (X11) for the host and
> agents. Android is on the roadmap (see [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)).

## How it works

```
 ┌──────────── host (keyboard + mouse + webcam) ────────────┐        ┌──── agent ────┐
 │ webcam → head pose → gaze classifier ┐                   │  TLS   │               │
 │                                      ├→ router ──────────┼───────→│ inject input  │
 │ keyboard/mouse capture ──────────────┘   (who's active?) │  LAN   │ + animation   │
 └──────────────────────────────────────────────────────────┘        └───────────────┘
```

* The **host** is the computer your keyboard and mouse are connected to.
  It captures input and, while another device is active, suppresses it
  locally and forwards it.
* Each other computer runs an **agent**, which replays what it receives.
* Your Bluetooth devices stay paired to the host. Curslink redirects their
  input in software instead of re-pairing, which takes milliseconds rather
  than several seconds and works with any mouse or keyboard.

## Quick start

Download the build for each device from the
[latest release](https://github.com/kylepantall/Curslink/releases/latest)
(`windows-x64.zip`, `macos-arm64.tar.gz` or `linux-x64.tar.gz`), unpack it,
and run `curslink` from the unpacked folder. The builds include camera
switching and need no Python.

Or install from source with Python 3.13+:

```bash
# on every device
pip install "curslink @ git+https://github.com/kylepantall/Curslink"
# on the host, for camera switching
pip install "curslink[gaze] @ git+https://github.com/kylepantall/Curslink"
```

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

Hotkeys on the host:

| Keys | Action |
| --- | --- |
| `Ctrl+Alt+Home` | Bring input back to the host, always |
| `Ctrl+Alt+G` | Pause / resume gaze switching |

Try the animations with `curslink overlay-test --effect pop`.

## Platform notes

* **macOS**: allow Accessibility and Input Monitoring for your terminal (or
  Python) in System Settings → Privacy & Security, plus Camera on the host.
* **Linux**: X11 sessions only for now. Camera switching needs the EGL and
  GLES libraries (`libegl1 libgles2` on Debian and Ubuntu), which most
  desktops already have. Wayland blocks global input capture
  and injection; a uinput backend is planned.
* **Windows**: input can't be injected into apps running as administrator
  unless the agent runs elevated too.

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

## Development

```bash
pip install -e ".[dev]"
pytest
```

To build the packaged app locally: `pip install ".[gaze]" pyinstaller`, then
`pyinstaller packaging/curslink.spec` (output in `dist/curslink/`).

Releasing: bump the version in `pyproject.toml` and `curslink/__init__.py`,
then push a tag such as `v0.2.0`. The Release workflow builds every platform
and publishes a GitHub release with the archives and `SHA256SUMS.txt`.

Design notes and the roadmap live in [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).
