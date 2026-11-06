"""``curslink host | agent | relay | invite | calibrate | check | overlay-test``"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import itertools
import logging
import sys
from pathlib import Path

from curslink import __version__
from curslink.config import (
    ConfigError,
    agent_config,
    host_config,
    load_raw,
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


def cmd_host(args) -> int:
    from curslink.app import run_host

    asyncio.run(run_host(_host_cfg(args.config)))
    return 0


def cmd_agent(args) -> int:
    from curslink.app import run_agent

    raw = load_raw(args.config)
    if args.host:
        raw.setdefault("agent", {})["host"] = args.host
    asyncio.run(run_agent(agent_config(raw)))
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
    p.add_argument("--host", help="host LAN address (overrides [agent].host)")
    p.set_defaults(func=cmd_agent)

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
