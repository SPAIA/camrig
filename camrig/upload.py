"""Upload recorded clips to Cloudflare R2 with rclone.

rclone copy is idempotent (it skips files already present with matching
size/mtime), so re-running it is safe and provides natural catch-up after an
offline period. Objects are laid out as:

    <bucket>/<hostname>/<YYYY-MM-DD>/clip_*.mkv  (+ .pts, .json)

After a date directory uploads cleanly, each clip is marked uploaded so the
retention pruner may later reclaim its space.
"""

from __future__ import annotations

import logging
import socket
import subprocess
from datetime import date, datetime
from pathlib import Path

from .config import Config
from . import storage

log = logging.getLogger("camrig.upload")


def _rclone_remote_root(cfg: Config) -> str:
    host = socket.gethostname()
    return f"{cfg.upload.rclone_remote}:{cfg.upload.bucket}/{host}"


def remote_reachable(cfg: Config, timeout: int = 15) -> bool:
    """Check the R2 remote actually responds (more than mere connectivity)."""
    try:
        subprocess.run(
            ["rclone", "lsd", f"{cfg.upload.rclone_remote}:{cfg.upload.bucket}",
             "--contimeout", f"{timeout}s", "--timeout", f"{timeout}s",
             "--low-level-retries", "1", "--retries", "1"],
            capture_output=True, check=True,
        )
        return True
    except (subprocess.CalledProcessError, FileNotFoundError):
        return False


def upload_day(cfg: Config, base: Path, day: date, *, dry_run: bool = False) -> bool:
    """Upload one day's directory. Returns True on success."""
    day_path = base / day.isoformat()
    if not day_path.is_dir():
        log.info("No recordings for %s", day.isoformat())
        return True

    dest = f"{_rclone_remote_root(cfg)}/{day.isoformat()}"
    cmd = [
        "rclone", "copy", str(day_path), dest,
        "--transfers", "4", "--checkers", "8",
        "--retries", "10", "--low-level-retries", "20",
        "--verbose",
    ]
    log.info("Uploading %s -> %s", day_path, dest)
    if dry_run:
        print(" ".join(cmd))
        return True

    try:
        subprocess.run(cmd, check=True)
    except subprocess.CalledProcessError as exc:
        log.error("rclone upload failed (rc=%s); leaving files for next attempt", exc.returncode)
        return False

    for clip in storage.iter_clips(day_path):
        storage.mark_uploaded(clip)
    log.info("Upload complete for %s", day.isoformat())
    return True


def upload_today(cfg: Config, base: Path, *, dry_run: bool = False) -> bool:
    return upload_day(cfg, base, datetime.now().astimezone().date(), dry_run=dry_run)


def upload_pending(cfg: Config, base: Path, *, dry_run: bool = False) -> bool:
    """Catch-up: upload every day directory that has un-uploaded clips.

    Used at boot to flush anything a failed/offline nightly upload left behind.
    """
    ok = True
    for day_path in sorted(p for p in base.glob("*") if p.is_dir()):
        pending = [c for c in storage.iter_clips(day_path) if not storage.is_uploaded(c)]
        if not pending:
            continue
        try:
            day = date.fromisoformat(day_path.name)
        except ValueError:
            continue
        ok = upload_day(cfg, base, day, dry_run=dry_run) and ok
    return ok
