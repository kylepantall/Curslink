"""``curslink host | agent | pair | unpair | calibrate | check | overlay-test``"""

from __future__ import annotations

import argparse
import asyncio
import itertools
import logging
import sys
from pathlib import Path

from curslink import __version__
from curslink.config import ConfigError, PairingConfig, agent_config, host_config, load_raw, pairing_config


def _host_cfg(path: str):
    p = Path(path)
    return host_config(load_raw(p), base_dir=p.parent)


def cmd_host(args) -> int:
    from curslink.app import run_host

    asyncio.run(run_host(_host_cfg(args.config)))
    return 0


def cmd_agent(args) -> int:
    from curslink.app import run_agent

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
    store.add(result.peer.device_id, result.peer.name, result.peer.role, result.key)
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


def cmd_calibrate(args) -> int:
    from curslink.app import calibrate

    cfg = _host_cfg(args.config)
    cal = calibrate(cfg)
    centroids = cal.centroids()
    if not centroids:
        print("No screens were calibrated.")
        return 1
    cal.save(cfg.gaze.calibration)
    print(f"\nSaved calibration for {len(centroids)} screen(s) to {cfg.gaze.calibration}")
    warn = 2 * cfg.gaze.settings.switch_margin_deg
    for (a, pa), (b, pb) in itertools.combinations(centroids.items(), 2):
        gap = ((pa[0] - pb[0]) ** 2 + (pa[1] - pb[1]) ** 2) ** 0.5
        if gap < warn:
            print(
                f"Warning: {a.key} and {b.key} are only {gap:.1f} deg apart; "
                "gaze may struggle to tell them apart (consider gaze.within_device = false)."
            )
    return 0


def cmd_check(args) -> int:
    cfg = _host_cfg(args.config)
    print(f"Host device: {cfg.device_id}, listening on {cfg.listen}:{cfg.port}")
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
    print(f"curslink {__version__}: mediapipe {mp.__version__}, opencv {cv2.__version__}, tk {tkinter.TkVersion}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="curslink", description=__doc__)
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("host", help="run on the computer the keyboard and mouse are attached to")
    p.add_argument("-c", "--config", default="curslink.toml")
    p.set_defaults(func=cmd_host)

    p = sub.add_parser("agent", help="run on each computer you want to control")
    p.add_argument("-c", "--config", default="curslink.toml")
    p.add_argument("--host", help="host address (overrides [agent].host)")
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

    p = sub.add_parser("calibrate", help="teach Curslink where each screen is relative to your head")
    p.add_argument("-c", "--config", default="curslink.toml")
    p.set_defaults(func=cmd_calibrate)

    p = sub.add_parser("check", help="validate a host config and show which edges connect")
    p.add_argument("-c", "--config", default="curslink.toml")
    p.set_defaults(func=cmd_check)

    p = sub.add_parser("overlay-test", help="play the switch animation")
    p.add_argument("--effect", choices=["pop", "blink"], default="pop")
    p.add_argument("x", type=int, nargs="?", default=400)
    p.add_argument("y", type=int, nargs="?", default=400)
    p.set_defaults(func=cmd_overlay_test)
    return parser


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
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
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        return args.func(args)
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 0
