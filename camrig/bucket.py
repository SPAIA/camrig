"""Run postprocess/debug-motion against clips that live only in the R2 bucket.

Both tools normally work on a clip already sitting on local disk. A clip that
was uploaded before postprocess ran, or uploaded with ``upload.full_res =
false`` (so the full-res video never touched local disk at all), has no local
copy to hand them. This fetches the clip family down to a scratch directory
with rclone, runs the existing local pipeline against that copy unchanged, and
-- for postprocess -- ships the generated sidecars back up so the bucket ends
up in the same state a normal on-device run would have left it in.

Bucket layout (matches ``camrig.upload``): ``<bucket>/<host>/<day>/clip_*.*``.
Since this typically runs from a dev machine rather than the rig that
recorded the clip, the rig's hostname is a required piece of addressing --
pass ``--host``, or leave it off and let ``resolve_host`` use the only host
present in the bucket.

    camrig bucket-postprocess 2026-09-01 clip_20260901_060000.mkv
    camrig bucket-debug-motion 2026-09-01 clip_20260901_060000.mkv
"""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path

from .config import Config
from . import postprocess

log = logging.getLogger("camrig.bucket")


def remote_day_path(cfg: Config, host: str, day: str) -> str:
    return f"{cfg.upload.rclone_remote}:{cfg.upload.bucket}/{host}/{day}"


def list_hosts(cfg: Config) -> list[str]:
    """Directory names at the bucket root (one per rig that has uploaded)."""
    result = subprocess.run(
        ["rclone", "lsd", f"{cfg.upload.rclone_remote}:{cfg.upload.bucket}"],
        capture_output=True, text=True, check=True,
    )
    return [line.split()[-1] for line in result.stdout.splitlines() if line.strip()]


def resolve_host(cfg: Config, host: str | None) -> str:
    """Return the given host, or the bucket's only host if none was given."""
    if host:
        return host
    hosts = list_hosts(cfg)
    if len(hosts) == 1:
        return hosts[0]
    if not hosts:
        raise RuntimeError(f"No hosts found under {cfg.upload.rclone_remote}:{cfg.upload.bucket}")
    raise RuntimeError(
        f"Multiple hosts in bucket ({', '.join(sorted(hosts))}); pass --host to pick one"
    )


def build_fetch_command(cfg: Config, host: str, day: str, clip_name: str, dest: Path) -> list[str]:
    """rclone argv to pull one clip family (video + any existing sidecars) down."""
    stem = Path(clip_name).stem
    return [
        "rclone", "copy", remote_day_path(cfg, host, day), str(dest),
        "--filter", f"+ {stem}.*", "--filter", "- *",
        "--transfers", "4", "--verbose",
    ]


def build_push_sidecars_command(cfg: Config, host: str, day: str, clip_name: str, src: Path) -> list[str]:
    """rclone argv to ship generated sidecars back up (never the video itself)."""
    stem = Path(clip_name).stem
    return [
        "rclone", "copy", str(src), remote_day_path(cfg, host, day),
        "--filter", f"+ {stem}{postprocess.PREVIEW_SUFFIX}",
        "--filter", f"+ {stem}{postprocess.MOTION_SUFFIX}",
        "--filter", "- *",
        "--transfers", "4", "--verbose",
    ]


def fetch_clip(cfg: Config, host: str, day: str, clip_name: str, dest: Path, *, dry_run: bool = False) -> Path:
    """Download one clip's family into dest. Returns the local clip path."""
    dest.mkdir(parents=True, exist_ok=True)
    cmd = build_fetch_command(cfg, host, day, clip_name, dest)
    log.info("Fetching %s/%s/%s -> %s", host, day, clip_name, dest)
    if dry_run:
        print(" ".join(cmd))
    else:
        subprocess.run(cmd, check=True)
    return dest / clip_name


def push_sidecars(cfg: Config, host: str, day: str, clip_name: str, src: Path, *, dry_run: bool = False) -> None:
    """Upload newly generated preview/motion sidecars back to the bucket."""
    cmd = build_push_sidecars_command(cfg, host, day, clip_name, src)
    log.info("Pushing sidecars for %s/%s/%s", host, day, clip_name)
    if dry_run:
        print(" ".join(cmd))
    else:
        subprocess.run(cmd, check=True)


def postprocess_clip(
    cfg: Config, host: str, day: str, clip_name: str, dest: Path,
    *, force: bool = False, dry_run: bool = False,
) -> bool:
    """Fetch one clip from the bucket, postprocess it, push the sidecars back."""
    video = fetch_clip(cfg, host, day, clip_name, dest, dry_run=dry_run)
    if dry_run:
        # process_clip's own dry-run still requires the file to exist (it
        # inspects the clip to build the ffmpeg command), which a dry-run
        # fetch deliberately skipped -- so just name the steps that follow.
        print(f"(then postprocess {video}, as `camrig postprocess` would, and push its sidecars back)")
        return True
    if not postprocess.process_clip(cfg, video, force=force, dry_run=False):
        return False
    push_sidecars(cfg, host, day, clip_name, dest, dry_run=False)
    return True


def debug_motion_clip(
    cfg: Config, host: str, day: str, clip_name: str, dest: Path,
    *, output: Path | None = None, fps: float | None = None,
    trail_seconds: float = 3.0, dry_run: bool = False,
) -> bool:
    """Fetch one clip + its .motion.json from the bucket and render the debug preview locally.

    Requires the clip already has a ``.motion.json`` sidecar in the bucket --
    run ``camrig bucket-postprocess`` first if it doesn't.
    """
    from . import motion_debug

    video = fetch_clip(cfg, host, day, clip_name, dest, dry_run=dry_run)
    if dry_run:
        # The ffmpeg command depends on width/height read from the fetched
        # .motion.json, so there's nothing more concrete to print until then.
        print(f"(then render debug preview for {video}, as `camrig debug-motion` would)")
        return True
    return motion_debug.run(
        cfg, video, output=output, fps=fps, trail_seconds=trail_seconds, dry_run=False,
    )
