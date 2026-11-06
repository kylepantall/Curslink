"""``curslink host | agent | calibrate | check | overlay-test``"""

from __future__ import annotations

import argparse
import asyncio
import itertools
import logging
import sys
from pathlib import Path

from curslink import __version__
from curslink.config import ConfigError, agent_config, host_config, load_raw


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
