"""Post-capture processing: low-res preview + motion-metric sidecars.

Runs on the Pi in the idle time between captures (a 3-minute clip every half
hour leaves ~27 quiet minutes). A single niced ffmpeg invocation decodes the
full-res MJPEG once and feeds two consumers from that shared decode:

* ``<clip>.preview.mp4`` — downscaled colour H.264 preview for quick scrubbing.
  Inter-frame compression artefacts don't matter here; analysis always uses the
  original intra-only clip.
* ``<clip>.motion.json`` — per-frame blobs and tracks from the configured,
  versioned ``camrig.motion`` detector. Motion frames stay at the source frame
  rate, so metric index i aligns with ``.pts`` line i.

Everything runs under ``nice`` so a capture that starts mid-postprocess always
wins the CPU. Outputs are written as ``*.part`` and renamed into place on
success; the rclone uploader excludes ``*.part``, so a half-written file never
ships. Both sidecars land next to the clip and ride the existing day-directory
upload unchanged.

Command builders are pure functions returning argv lists (the camrig.record
convention) so they can be unit tested and printed under --dry-run.
"""

from __future__ import annotations

import logging
import subprocess
import sys
import time
from pathlib import Path

from . import storage
from .config import Config
from .record import describe_commands

log = logging.getLogger("camrig.postprocess")

PREVIEW_SUFFIX = ".preview.mp4"
MOTION_SUFFIX = ".motion.json"
# In-progress outputs; excluded from upload and renamed into place on success.
PART_SUFFIX = ".part"
# Skip clips written to this recently: they may still be recording.
SETTLE_SECONDS = 30


def preview_path(video: Path) -> Path:
    return video.with_suffix(PREVIEW_SUFFIX)


def motion_path(video: Path) -> Path:
    return video.with_suffix(MOTION_SUFFIX)


def is_processed(video: Path) -> bool:
    return preview_path(video).exists() and motion_path(video).exists()


def motion_frame_size(cfg: Config) -> tuple[int, int]:
    """Motion-analysis frame size: configured width, aspect kept, even dims."""
    width = cfg.postprocess.motion_width
    height = round(cfg.capture.height * width / cfg.capture.width / 2) * 2
    return width, max(2, height)


def build_commands(cfg: Config, video: Path) -> list[list[str]]:
    """Return [ffmpeg, motion] argv lists: one decode pass, two consumers.

    ffmpeg writes the preview itself and pipes grayscale motion frames on
    stdout into the camrig.motion consumer.
    """
    pp = cfg.postprocess
    nice = ["nice", "-n", str(pp.nice)]
    motion_w, motion_h = motion_frame_size(cfg)

    preview_filters = []
    if 0 < pp.preview_fps < cfg.capture.framerate:
        preview_filters.append(f"fps={pp.preview_fps}")
    preview_filters += [f"scale={pp.preview_width}:-2", "format=yuv420p"]

    ffmpeg = [
        *nice, "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
        "-i", str(video),
        # Output 1: colour preview (its own filter chain off the shared decode).
        "-vf", ",".join(preview_filters),
        "-c:v", "libx264", "-preset", "veryfast", "-crf", str(pp.preview_crf),
        "-movflags", "+faststart", "-an",
        "-f", "mp4", str(preview_path(video)) + PART_SUFFIX,
        # Output 2: grayscale frames at source fps for the motion consumer.
        "-vf", f"scale={motion_w}:{motion_h},format=gray",
        "-f", "rawvideo", "pipe:1",
    ]
    motion = [
        *nice, sys.executable, "-m", "camrig.motion",
        "--detector", pp.motion_detector,
        "--width", str(motion_w), "--height", str(motion_h),
        "--threshold", str(pp.motion_threshold),
        "--clip", video.name,
        "--output", str(motion_path(video)) + PART_SUFFIX,
    ]
    return [ffmpeg, motion]


def process_clip(
    cfg: Config, video: Path, *, force: bool = False, dry_run: bool = False
) -> bool:
    """Generate both sidecars for one clip. Returns True on success/skip."""
    if video.suffix != ".mkv":
        log.info("Skipping %s: postprocess handles .mkv clips only", video.name)
        return True
    if not force and is_processed(video):
        return True
    if not video.exists():
        log.error("Clip not found: %s", video)
        return False

    commands = build_commands(cfg, video)
    log.info("Postprocess: %s", describe_commands(commands))
    if dry_run:
        print(describe_commands(commands))
        return True

    started = time.monotonic()
    producer = subprocess.Popen(commands[0], stdout=subprocess.PIPE)
    consumer = subprocess.Popen(commands[1], stdin=producer.stdout)
    if producer.stdout is not None:
        producer.stdout.close()
    consumer_rc = consumer.wait()
    producer_rc = producer.wait()

    preview_part = Path(str(preview_path(video)) + PART_SUFFIX)
    motion_part = Path(str(motion_path(video)) + PART_SUFFIX)
    if producer_rc == 0 and consumer_rc == 0:
        preview_part.replace(preview_path(video))
        motion_part.replace(motion_path(video))
        log.info(
            "Postprocessed %s in %.0fs (preview %.1f MiB)",
            video.name,
            time.monotonic() - started,
            preview_path(video).stat().st_size / 2**20,
        )
        return True

    log.error(
        "Postprocess failed for %s (ffmpeg rc=%s, motion rc=%s); leaving clip for retry",
        video.name, producer_rc, consumer_rc,
    )
    for part in (preview_part, motion_part):
        part.unlink(missing_ok=True)
    return False


def process_pending(
    cfg: Config, base: Path, *, force: bool = False, dry_run: bool = False
) -> bool:
    """Catch-up: process every clip missing its sidecars. Returns overall ok.

    Used at boot/shutdown so previews and motion metrics exist before the
    day-directory upload, and by ``camrig postprocess`` to regenerate sidecars
    after the motion analysis changes (--force).
    """
    ok = True
    for clip in storage.iter_clips(base):
        if clip.suffix != ".mkv":
            continue
        if not force and is_processed(clip):
            continue
        if time.time() - clip.stat().st_mtime < SETTLE_SECONDS:
            log.info("Skipping %s: written too recently (may be recording)", clip.name)
            continue
        ok = process_clip(cfg, clip, force=force, dry_run=dry_run) and ok
    return ok
