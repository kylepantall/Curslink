"""``curslink gui | host | agent | pair | unpair | relay | invite | calibrate | check | update | overlay-test``"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import sys
from pathlib import Path

from curslink import __version__
from curslink.config import (
    ConfigError,
    PairingConfig,
    agent_config,
    host_config,
    load_raw,
    pairing_config,
    relay_server_config,
)

RELAY_CONFIG_TEMPLATE = """\
# Curslink relay server. Run it somewhere your devices can all reach, such as
# a small VPS with a DNS name, then: curslink relay -c relay.toml
#
# The relay only matches devices up and forwards encrypted bytes. It never
# sees your pairing secret, so it cannot read or inject keystrokes.

[relay]
listen = "0.0.0.0"     # use "::" to listen on IPv6 as well
port = {port}
# Devices need this key to use the relay. Keep it private.
key = "{key}"
max_connections = 256
max_rooms = 64          # one room per Curslink setup
"""


def _host_cfg(path: str):
    p = Path(path)
    return host_config(load_raw(p), base_dir=p.parent)


def _updates(path: str, argv: list[str]) -> None:
    """Install a newer release before starting, then keep an eye out for the next."""
    from curslink import update

    p = Path(path)
    raw = load_raw(p) if p.exists() else {}
    update.on_start(raw, argv)
    update.watch(raw)


def _events(args) -> None:
    if getattr(args, "events", False):
        from curslink import events

        events.enable()


def cmd_host(args) -> int:
    from curslink.app import run_host

    cfg = _host_cfg(args.config)
    _events(args)
    _updates(args.config, args.argv)
    asyncio.run(run_host(cfg))
    return 0


def cmd_agent(args) -> int:
    from curslink.app import run_agent

    _updates(args.config, args.argv)
    _events(args)
    raw = load_raw(args.config)
    if args.host:
        raw.setdefault("agent", {})["host"] = args.host
    asyncio.run(run_agent(agent_config(raw, base_dir=Path(args.config).parent)))
    return 0


def _this_device(args):
    """(DeviceInfo, PairingConfig) for the device this config describes."""
    from curslink.pairing.handshake import DeviceInfo

    path = Path(args.config)
    raw = load_raw(path)
    role = args.role
    if role is None:
        has_host, has_agent = "host" in raw, "agent" in raw
        if has_host == has_agent:
            raise ConfigError("this config has both [host] and [agent] (or neither); pass --as host or --as agent")
        role = "host" if has_host else "agent"
    if role == "host":
        cfg = host_config(raw, base_dir=path.parent)
        return DeviceInfo(cfg.device_id, cfg.name, "host"), cfg.pairing
    cfg = agent_config(raw, base_dir=path.parent)
    return DeviceInfo(cfg.device_id, cfg.name, "agent"), cfg.pairing


def _pick(found, store) -> object | None:
    print("Curslink devices on this network:\n")
    for i, f in enumerate(found, 1):
        paired = " (paired)" if store.get(f.device_id) else ""
        print(f"  {i}. {f.name} [{f.role}, id {f.device_id}] at {f.address}{paired}")
    while True:
        choice = input("\nPair with which device? Number, or Enter to cancel: ").strip()
        if not choice:
            return None
        if choice.isdigit() and 1 <= int(choice) <= len(found):
            return found[int(choice) - 1]
        print("  not one of the numbers above")


async def _pair(me, pcfg: PairingConfig, args) -> int:
    from curslink.pairing.discovery import browse
    from curslink.pairing.handshake import PairingError, initiate
    from curslink.pairing.service import DEFAULT_PAIRING_PORT
    from curslink.pairing.store import PairingStore

    store = PairingStore(pcfg.store)
    if args.address:
        host, _, port = args.address.partition(":")
        address, port, label = host, int(port or DEFAULT_PAIRING_PORT), args.address
    else:
        print("Looking for Curslink devices on the network...")
        try:
            found = await browse(args.timeout, exclude=me.device_id)
        except Exception as exc:
            print(f"Network discovery failed ({exc}). Use --address HOST to pair directly.", file=sys.stderr)
            return 1
        found = [f for f in found if f.role != me.role]
        want = "an agent" if me.role == "host" else "a host"
        if not found:
            print(f"No {want} found. Is `curslink {'agent' if me.role == 'host' else 'host'}` running "
                  "on the other device, on the same network? You can also use --address HOST.")
            return 1
        target = await asyncio.get_running_loop().run_in_executor(None, _pick, found, store)
        if target is None:
            return 1
        address, port, label = target.address, target.pairing_port, target.name

    def show_code(code, peer):
        print(f"\nOn {peer.name}, enter this code to pair:\n\n        {code}\n")
        print("Waiting for it to be entered...")

    try:
        reader, writer = await asyncio.wait_for(asyncio.open_connection(address, port), 5)
    except (OSError, asyncio.TimeoutError) as exc:
        print(f"Can't reach {label}: {exc}", file=sys.stderr)
        return 1
    try:
        result = await initiate(reader, writer, me, show_code)
    except PairingError as exc:
        print(f"Pairing failed: {exc}", file=sys.stderr)
        return 1
    except (ConnectionError, OSError) as exc:
        print(f"Pairing failed: lost the connection ({exc})", file=sys.stderr)
        return 1
    finally:
        writer.close()
    store.add(result.peer.device_id, result.peer.name, result.peer.role, result.key, address)
    print(f"Paired with {result.peer.name}. Keys saved to {store.path}")
    if me.role == "host":
        print(f"Make sure [[devices]] in your config has an entry with id = \"{result.peer.device_id}\".")
    return 0


def cmd_pair(args) -> int:
    me, pcfg = _this_device(args)
    if args.list:
        from curslink.pairing.store import PairingStore

        store = PairingStore(pcfg.store)
        peers = store.peers()
        if not peers:
            print(f"Not paired with anything yet ({store.path})")
        for p in peers:
            print(f"{p.device_id}: {p.name} ({p.role})")
        return 0
    return asyncio.run(_pair(me, pcfg, args))


def cmd_unpair(args) -> int:
    from curslink.pairing.store import PairingStore

    path = Path(args.config)
    pcfg = pairing_config(load_raw(path), path.parent) if path.exists() else PairingConfig()
    store = PairingStore(pcfg.store)
    if not store.remove(args.device_id):
        print(f"Not paired with {args.device_id!r}", file=sys.stderr)
        return 1
    print(f"Removed {args.device_id}. Unpair on that device too, or it will keep its old key.")
    return 0


def cmd_relay(args) -> int:
    from curslink.net.relay import DEFAULT_RELAY_PORT, RelayServer, generate_key

    path = Path(args.config)
    if args.init:
        if path.exists():
            print(f"{path} already exists; not overwriting it", file=sys.stderr)
            return 1
        key = generate_key()
        path.write_text(RELAY_CONFIG_TEMPLATE.format(port=DEFAULT_RELAY_PORT, key=key))
        with contextlib.suppress(OSError):
            path.chmod(0o600)
        print(f"Wrote {path} with a new relay key.")
        print(f"Start it with: curslink relay -c {path}")
        print(f"Then make a pairing code: curslink invite --relay <your-dns-name>:{DEFAULT_RELAY_PORT} --relay-key {key}")
        return 0
    cfg = relay_server_config(load_raw(path))
    server = RelayServer(
        cfg.key,
        host=cfg.listen,
        port=cfg.port,
        max_connections=cfg.max_connections,
        max_rooms=cfg.max_rooms,
    )
    asyncio.run(server.serve_forever())
    return 0


def cmd_invite(args) -> int:
    from curslink.net.relay import encode_pairing_code, generate_key, parse_address
    from curslink.security import MIN_SECRET_LENGTH

    try:
        parse_address(args.relay)
    except ValueError as exc:
        raise ConfigError(str(exc)) from exc
    secret = args.secret or generate_key()
    if len(secret) < MIN_SECRET_LENGTH:
        raise ConfigError(f"the pairing secret must be at least {MIN_SECRET_LENGTH} characters")
    code = encode_pairing_code(args.relay, args.relay_key, secret)
    print("Pairing code (treat it like a password; it holds the pairing secret):\n")
    print(code)
    print("\nPut it in curslink.toml on the host AND every agent, replacing `secret`:\n")
    print("[relay]")
    print(f'code = "{code}"')
    print("\nor set the CURSLINK_PAIRING_CODE environment variable.")
    return 0


def cmd_calibrate(args) -> int:
    from curslink.app import calibrate, close_pairs

    cfg = _host_cfg(args.config)
    cal = calibrate(cfg)
    centroids = cal.centroids()
    if not centroids:
        print("No screens were calibrated.")
        return 1
    cal.save(cfg.gaze.calibration)
    print(f"\nSaved calibration for {len(centroids)} screen(s) to {cfg.gaze.calibration}")
    for a, b, gap in close_pairs(cal, cfg.gaze.settings.switch_margin_deg):
        print(
            f"Warning: {a.key} and {b.key} are only {gap:.1f} deg apart; "
            "gaze may struggle to tell them apart (consider gaze.within_device = false)."
        )
    return 0


def cmd_check(args) -> int:
    cfg = _host_cfg(args.config)
    if cfg.lan:
        print(f"Host device: {cfg.device_id}, listening on {cfg.listen}:{cfg.port}")
    else:
        print(f"Host device: {cfg.device_id}, LAN listener off")
    if cfg.relay:
        print(f"Relay: {cfg.relay.address} (room {cfg.relay.room[:8]})")
    for device in cfg.layout.devices.values():
        print(f"- {device.id} ({device.name})")
        for s in device.screens:
            print(f"    {s.id}: {s.width}x{s.height} at ({s.x}, {s.y}), OS origin ({s.os_x}, {s.os_y})")
    for s in cfg.layout.screens(cfg.device_id):
        for edge, (dx, dy) in {"left": (-1, 0), "right": (1, 0), "top": (0, -1), "bottom": (0, 1)}.items():
            probe = {
                "left": (s.x, s.center[1]),
                "right": (s.right - 1, s.center[1]),
                "top": (s.center[0], s.y),
                "bottom": (s.center[0], s.bottom - 1),
            }[edge]
            hit = cfg.layout.find_crossing(
                cfg.device_id, *probe, dx, dy, cfg.router.gap_tolerance_px
            )
            if hit:
                print(f"Edge: {cfg.device_id}/{s.id} {edge} -> {hit.screen.device_id}/{hit.screen.id}")
    print("Config OK")
    return 0


def cmd_update(args) -> int:
    from curslink import update

    path = Path(args.config)
    settings = update.UpdateSettings()
    if path.exists():
        try:
            settings = update.update_settings(load_raw(path))
        except ValueError as exc:
            raise ConfigError(str(exc)) from exc
    if args.pre:
        settings.prerelease = True
    kind = update.install_kind()
    try:
        release = update.check(settings)
    except update.UpdateError as exc:
        print(f"Update check failed: {exc}", file=sys.stderr)
        return 1
    if release is None:
        print(f"Curslink {__version__} is the newest release.")
        return 0
    print(f"Curslink {release.version} is out (you have {__version__}): {release.url}")
    if args.check:
        return 0
    if kind is None:
        print('This copy runs from source; update it with: pip install -U "curslink[gaze] @ git+https://github.com/kylepantall/Curslink"')
        return 0
    if not args.yes:
        if not sys.stdin.isatty():
            print("Pass --yes to install it without asking.", file=sys.stderr)
            return 1
        if input("Install it now? [Y/n] ").strip().lower() not in ("", "y", "yes"):
            return 1

    def progress(done: int, total: int) -> None:
        if total:
            print(f"\rDownloading... {done * 100 // total}%", end="", flush=True)

    try:
        message = update.update(release, kind, progress=progress)
    except update.UpdateError as exc:
        print(f"\nUpdate failed: {exc}", file=sys.stderr)
        return 1
    print(f"\n{message}")
    return 0


def cmd_gui(args) -> int:
    try:
        from curslink.gui.app import main as gui_main
    except ImportError as exc:
        print(f"The Curslink app needs Qt, which isn't installed ({exc}).\n"
              'Install it with: pip install "curslink[gui]"', file=sys.stderr)
        return 1
    argv = ["--minimized"] if args.minimized else []
    if args.verbose:
        argv.append("--verbose")
    if args.config:
        argv += ["--config", args.config]
    return gui_main(argv)


def wants_gui(argv: list[str]) -> bool:
    """Opening the installed app (no arguments, no terminal) starts the GUI.

    ``curslinkw`` is the Windows build without a console window; the macOS
    app and Linux menu entry pass ``gui`` themselves, but a double-clicked
    executable with no terminal attached gets the window too.
    """
    if argv or not getattr(sys, "frozen", False):
        return False
    if Path(sys.executable).stem.lower() == "curslinkw":
        return True
    stdin = sys.stdin
    return stdin is None or not stdin.isatty()


def cmd_overlay_test(args) -> int:
    import time

    from curslink.overlay import OverlayProcess

    overlay = OverlayProcess()
    overlay.start()
    time.sleep(0.8)
    overlay.show(args.effect, args.x, args.y)
    time.sleep(1.0)
    overlay.close()
    return 0


def _selftest() -> int:
    """Checks a packaged build carries what it loads at runtime (used by CI)."""
    import numpy as np
    import mediapipe as mp
    from mediapipe.tasks.python import vision  # noqa: F401

    import cv2  # noqa: F401
    import tkinter  # noqa: F401

    mp.Image(image_format=mp.ImageFormat.SRGB, data=np.zeros((4, 4, 3), dtype=np.uint8))

    # The app: Qt, its SVG and local-socket modules, and the icons as package data.
    import os

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    import PySide6
    from PySide6.QtWidgets import QApplication

    from curslink.gui.common import app_icon, tray_icon

    qt = QApplication.instance() or QApplication(["curslink"])
    if app_icon().isNull() or tray_icon("running", True).isNull():
        raise SystemExit("the app icons didn't load")
    if sys.platform == "darwin":
        # The window glass, the Dock icon and camera names need pyobjc.
        import AppKit  # noqa: F401
        import AVFoundation  # noqa: F401
        import objc  # noqa: F401
    import curslink.gui.window  # noqa: F401
    import curslink.gui.tray  # noqa: F401
    import PySide6.QtNetwork  # noqa: F401
    del qt
    print(f"curslink {__version__}: mediapipe {mp.__version__}, opencv {cv2.__version__}, "
          f"tk {tkinter.TkVersion}, qt {PySide6.__version__}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="curslink", description=__doc__)
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("gui", help="open the Curslink app (window and tray icon)")
    p.add_argument("--minimized", action="store_true", help="start in the tray without opening the window")
    p.add_argument("-c", "--config", help="use this curslink.toml (default: your user config folder)")
    p.set_defaults(func=cmd_gui)

    p = sub.add_parser("host", help="run on the computer the keyboard and mouse are attached to")
    p.add_argument("-c", "--config", default="curslink.toml")
    # Status events on stdout and commands on stdin, for the app (see curslink/events.py).
    p.add_argument("--events", action="store_true", help=argparse.SUPPRESS)
    p.set_defaults(func=cmd_host)

    p = sub.add_parser("agent", help="run on each computer you want to control")
    p.add_argument("-c", "--config", default="curslink.toml")
    p.add_argument("--host", help="host LAN address (overrides [agent].host)")
    p.add_argument("--events", action="store_true", help=argparse.SUPPRESS)
    p.set_defaults(func=cmd_agent)

    p = sub.add_parser("pair", help="find Curslink devices on the network and pair with one")
    p.add_argument("-c", "--config", default="curslink.toml")
    p.add_argument("--as", dest="role", choices=["host", "agent"],
                   help="which role this device plays, if the config has both [host] and [agent]")
    p.add_argument("--address", help="pair with HOST[:PORT] directly instead of searching the network")
    p.add_argument("--timeout", type=float, default=3.0, help="seconds to search the network")
    p.add_argument("--list", action="store_true", help="show the devices this one is paired with")
    p.set_defaults(func=cmd_pair)

    p = sub.add_parser("unpair", help="forget a paired device")
    p.add_argument("device_id")
    p.add_argument("-c", "--config", default="curslink.toml")
    p.set_defaults(func=cmd_unpair)

    p = sub.add_parser("relay", help="run a relay server so devices can link over the internet")
    p.add_argument("-c", "--config", default="relay.toml")
    p.add_argument("--init", action="store_true", help="write a new relay config with a random key")
    p.set_defaults(func=cmd_relay)

    p = sub.add_parser("invite", help="make a pairing code for linking devices through a relay")
    p.add_argument("--relay", required=True, help="relay address, e.g. relay.example.com:24852")
    p.add_argument("--relay-key", required=True, help="the key from the relay's config")
    p.add_argument("--secret", help="reuse an existing pairing secret (default: a new random one)")
    p.set_defaults(func=cmd_invite)

    p = sub.add_parser("calibrate", help="teach Curslink where each screen is relative to your head")
    p.add_argument("-c", "--config", default="curslink.toml")
    p.set_defaults(func=cmd_calibrate)

    p = sub.add_parser("check", help="validate a host config and show which edges connect")
    p.add_argument("-c", "--config", default="curslink.toml")
    p.set_defaults(func=cmd_check)

    p = sub.add_parser("update", help="install the newest Curslink release")
    p.add_argument("-c", "--config", default="curslink.toml", help="read [update] settings from here, if it exists")
    p.add_argument("--check", action="store_true", help="only say whether there's a newer release")
    p.add_argument("--yes", "-y", action="store_true", help="install without asking")
    p.add_argument("--pre", action="store_true", help="include pre-releases")
    p.set_defaults(func=cmd_update)

    p = sub.add_parser("overlay-test", help="play the switch animation")
    p.add_argument("--effect", choices=["pop", "blink"], default="pop")
    p.add_argument("x", type=int, nargs="?", default=400)
    p.add_argument("y", type=int, nargs="?", default=400)
    p.set_defaults(func=cmd_overlay_test)
    return parser


def _keep_matplotlib_cache() -> None:
    """MediaPipe imports matplotlib, and the packaged app's matplotlib gets a
    new temporary config folder each launch, so it rebuilt its font cache
    (several seconds) every time. Point it at a folder that lasts."""
    if not getattr(sys, "frozen", False):
        return
    import os

    from curslink.diagnostics import log_dir

    folder = log_dir().parent / "matplotlib" if sys.platform != "darwin" else (
        Path.home() / "Library" / "Caches" / "Curslink" / "matplotlib")
    try:
        folder.mkdir(parents=True, exist_ok=True)
        os.environ["MPLCONFIGDIR"] = str(folder)
        os.environ.setdefault("MPLBACKEND", "Agg")
    except OSError:
        pass


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    _keep_matplotlib_cache()
    if argv[:1] == ["_overlay-helper"]:
        # The packaged app re-runs itself as the animation helper (see overlay.py).
        from curslink.overlay import run_helper

        run_helper()
        return 0
    if argv[:1] == ["_pair-dialog"]:
        from curslink.pairing.dialog import main as dialog_main

        return dialog_main(argv[1:])
    if argv[:1] == ["_selftest"]:
        return _selftest()
    if argv[:1] == ["_macperms"]:
        # A fresh process: macOS caches these answers for the life of a process.
        import json

        from curslink import macperms

        print(json.dumps({k: macperms.granted(k) for k in macperms.PERMISSIONS}))
        return 0
    if argv[:1] == ["_install-kind"]:
        # How this copy was installed (used by CI to check the installers).
        from curslink.update import install_kind

        print(install_kind() or "source")
        return 0
    if wants_gui(argv):
        argv = ["gui"]
    args = build_parser().parse_args(argv)
    args.argv = argv
    from curslink import diagnostics

    process = args.command if args.command in ("host", "agent", "relay") else "cli"
    if args.command == "gui":
        # The app sets up its own logs once it knows it's the only copy running.
        logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                            format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    else:
        diagnostics.setup(process, verbose=args.verbose)
    try:
        return args.func(args)
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 0
