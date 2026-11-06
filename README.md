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

Install Curslink on each device from the
[latest release](https://github.com/kylepantall/Curslink/releases/latest).
The builds include camera switching and need no Python.

| Platform | Installer | Installs to |
| --- | --- | --- |
| Windows | `curslink-X.Y.Z-windows-x64-setup.exe` | `%LOCALAPPDATA%\Programs\Curslink` (no admin needed), added to your PATH |
| macOS (Apple silicon) | `curslink-X.Y.Z-macos-arm64.pkg` | `/usr/local/lib/curslink`, with `curslink` in `/usr/local/bin` |
| Linux (Debian, Ubuntu) | `curslink-X.Y.Z-linux-x64.deb` | `/opt/curslink`, with `curslink` in `/usr/bin` (`sudo apt install ./curslink-*.deb`) |

The installers aren't code-signed yet: on Windows click "More info" then
"Run anyway" in SmartScreen; on macOS right-click the `.pkg` and choose
Open. Portable archives (`.zip`, `.tar.gz`) are there too if you'd rather
unpack and run `curslink` from the folder.

### Updates

An installed Curslink keeps itself up to date. Each time `curslink host` or
`curslink agent` starts, it checks GitHub for a newer release, downloads the
installer for your platform, checks it against the release's
`SHA256SUMS.txt`, installs it and restarts with the same arguments. On macOS
and Linux installing asks for your password; on Windows it runs silently. While
running, it checks once a day and logs when a new version is out, which then
installs at the next start.

* `curslink update` updates by hand (`--check` only looks, `--pre` includes
  release candidates).
* Turn it off or down in `curslink.toml`, or set `CURSLINK_NO_UPDATE=1`:

  ```toml
  [update]
  check = true        # look for new releases
  auto = true         # install them at startup (false: just say so)
  prerelease = false
  ```
* While the repository is private, set `CURSLINK_GITHUB_TOKEN` to a GitHub
  token that can read it, or the check can't see the releases.

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
and publishes a GitHub release with the installers, the archives and
`SHA256SUMS.txt`; installed copies pick it up on their next start. To build an
installer locally after PyInstaller: `packaging/make-installer.sh 0.2.0
linux-x64 out` (needs Inno Setup on Windows, Xcode tools on macOS, `dpkg-deb`
on Linux).

Design notes and the roadmap live in [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).
