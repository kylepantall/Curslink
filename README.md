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
   edit it: set a long shared `secret`, describe every device's screens in
   one shared layout (arranged like your desk), and set `[agent]` on agents.
2. Check the layout on the host: `curslink check`
3. Start agents: `curslink agent` (on each other device)
4. Teach it where your screens are: `curslink calibrate` (on the host;
   look at each screen when asked)
5. Run the host: `curslink host`

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
forward secret) keyed from the shared `secret`. A device without the
secret can neither connect to the host nor impersonate it. Use a long,
random secret, since anyone with it can send keystrokes to your agents.

Through a relay the same TLS runs end to end between host and agent; the
relay only forwards ciphertext. The relay key just controls who may use
your relay. A pairing code contains the pairing secret, so share it like a
password.

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
