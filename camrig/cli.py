"""camrig command-line entrypoint.

Subcommands:
  supervise   Run the long-lived capture supervisor + Cloudflare link (service).
  record      Record a single clip now (testing / manual; supports --dry-run).
  focus       Serve a live focus-assist page (manual lens; reach it over Tailscale).
  boot        Boot tasks: NTP sync + catch-up upload + prune.
  shutdown    Upload today, set RTC wake alarm, power off.
  status      Print resolved config + selected storage and exit.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys

from .config import load_config
from . import storage


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


def _cmd_supervise(args, cfg) -> int:
    from .supervisor import Supervisor
    from .cloudlink import CloudLink

    sup = Supervisor(cfg)
    link = None if args.no_cloud else CloudLink(cfg)
    try:
        asyncio.run(sup.run(cloudlink=link))
    except KeyboardInterrupt:
        return 0
    return 0


def _cmd_record(args, cfg) -> int:
    from . import record

    base = storage.select_base_dir(cfg)
    day_dir = storage.day_dir(base)
    if args.profile:
        cfg.capture.profile = args.profile
    record.record_clip(
        cfg.capture, day_dir,
        trigger="triggered" if args.triggered else "scheduled",
        duration_seconds=args.seconds,
        dry_run=args.dry_run,
    )
    return 0


def _cmd_focus(args, cfg) -> int:
    from .focus import FocusConfig, run

    focus_cfg = FocusConfig.from_capture(
        cfg.capture,
        width=args.width,
        height=args.height,
        framerate=args.framerate,
        quality=args.quality,
        port=args.port,
        shutter_us=args.shutter,
        gain=args.gain,
    )
    return run(focus_cfg, dry_run=args.dry_run)


def _cmd_boot(args, cfg) -> int:
    from . import boot
    return boot.run(cfg, dry_run=args.dry_run)


def _cmd_shutdown(args, cfg) -> int:
    from . import shutdown
    return shutdown.run(cfg, skip_poweroff=args.skip_poweroff, dry_run=args.dry_run)


def _cmd_status(args, cfg) -> int:
    base = storage.select_base_dir(cfg)
    print(f"profile         : {cfg.capture.profile}")
    print(f"resolution      : {cfg.capture.width}x{cfg.capture.height}@{cfg.capture.framerate}")
    print(f"window          : {cfg.schedule.start_hour:02d}:00-{cfg.schedule.stop_hour:02d}:00 "
          f"every {cfg.schedule.interval_min}min")
    print(f"storage base    : {base}")
    print(f"free space (GiB): {storage.free_gib(base):.1f}")
    print(f"worker          : {cfg.cloud.worker_ws_url} (device {cfg.cloud.device_id})")
    print(f"token present   : {cfg.device_token() is not None}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="camrig", description=__doc__)
    parser.add_argument("-v", "--verbose", action="store_true")
    parser.add_argument("-c", "--config", help="path to config.toml")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("supervise", help="run the capture supervisor service")
    p.add_argument("--no-cloud", action="store_true", help="disable the Cloudflare link")
    p.set_defaults(func=_cmd_supervise)

    p = sub.add_parser("record", help="record a single clip now")
    p.add_argument("--profile", choices=["mjpeg", "ffv1", "raw"])
    p.add_argument("--seconds", type=int, help="override clip length")
    p.add_argument("--triggered", action="store_true", help="tag as a manual session")
    p.add_argument("--dry-run", action="store_true", help="print the command, do not run")
    p.set_defaults(func=_cmd_record)

    p = sub.add_parser("focus", help="serve a live focus-assist page")
    p.add_argument("--port", type=int, default=8080, help="HTTP port (default 8080)")
    p.add_argument("--width", type=int, help="stream width (default: full sensor)")
    p.add_argument("--height", type=int, help="stream height (default: full sensor)")
    p.add_argument("--framerate", type=int, default=15,
                   help="stream fps; lower it if the link is slow (default 15)")
    p.add_argument("--quality", type=int, default=80, help="MJPEG quality (default 80)")
    p.add_argument("--shutter", type=int, dest="shutter",
                   help="manual shutter (us); default auto-expose")
    p.add_argument("--gain", type=float, help="analogue gain; default auto")
    p.add_argument("--dry-run", action="store_true", help="print the command, do not run")
    p.set_defaults(func=_cmd_focus)

    p = sub.add_parser("boot", help="boot tasks: ntp sync + catch-up upload")
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(func=_cmd_boot)

    p = sub.add_parser("shutdown", help="upload today, set wake alarm, power off")
    p.add_argument("--skip-poweroff", action="store_true", help="do everything but power off")
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(func=_cmd_shutdown)

    p = sub.add_parser("status", help="print resolved config and storage")
    p.set_defaults(func=_cmd_status)

    args = parser.parse_args(argv)
    _setup_logging(args.verbose)
    cfg = load_config(args.config)
    return args.func(args, cfg)


if __name__ == "__main__":
    sys.exit(main())
