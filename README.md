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

Requires Python 3.13+ on every device.

```bash
# on every device
pip install "curslink @ git+https://github.com/kylepantall/Curslink"
# on the host, for camera switching
pip install "curslink[gaze] @ git+https://github.com/kylepantall/Curslink"
```

1. Copy [`examples/curslink.toml`](examples/curslink.toml) to each device and
   edit it: set a long shared `secret`, describe every device's screens in
   one shared layout (arranged like your desk), and set `[agent]` on agents.
2. Check the layout on the host: `curslink check`
3. Start agents: `curslink agent` (on each other device)
4. Teach it where your screens are: `curslink calibrate` (on the host;
   look at each screen when asked)
5. Run the host: `curslink host`

Hotkeys on the host:

| Keys | Action |
| --- | --- |
| `Ctrl+Alt+Home` | Bring input back to the host, always |
| `Ctrl+Alt+G` | Pause / resume gaze switching |

Try the animations with `curslink overlay-test --effect pop`.

## Platform notes

* **macOS**: allow Accessibility and Input Monitoring for your terminal (or
  Python) in System Settings → Privacy & Security, plus Camera on the host.
* **Linux**: X11 sessions only for now. Wayland blocks global input capture
  and injection; a uinput backend is planned.
* **Windows**: input can't be injected into apps running as administrator
  unless the agent runs elevated too.

## Security

All traffic is encrypted and mutually authenticated with TLS-PSK (ECDHE,
forward secret) keyed from the shared `secret`. A device without the
secret can neither connect to the host nor impersonate it. Use a long,
random secret, since anyone with it can send keystrokes to your agents.

## Development

```bash
pip install -e ".[dev]"
pytest
```

Design notes and the roadmap live in [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).
