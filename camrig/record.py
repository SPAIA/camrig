"""Build and run a single capture.

Two camera backends behind one interface (``capture.camera``):

* ``rpicam`` (default) — the Pi Global Shutter colour IMX296 via rpicam-vid /
  rpicam-raw.
* ``basler``           — a Basler ace 2 mono over GigE via ``camrig.basler``
  (a producer subprocess with an rpicam-shaped interface; transport settings
  in the [basler] config section, wiring in docs/basler-gige.md).

Three intra-only profiles, all preserving small moving targets:

* ``mjpeg`` (default) — an MJPEG stream piped to/encoded by ffmpeg into a
  Matroska container. Each frame is independent (no inter-frame motion
  artefacts on tiny insects).
* ``ffv1``           — lossless intra codec via ffmpeg. Maximum fidelity, large
  files; software encoding likely cannot sustain 60 fps at full res.
* ``raw``            — the unprocessed sensor frames (rpicam: Bayer mosaic,
  demosaic offline; basler: gray8). Huge; intended for short fidelity
  experiments.

Every clip is accompanied by:
* ``<clip>.pts``  — per-frame presentation timestamps (rpicam --save-pts), so
  effective frame rate and per-frame timing can be recovered offline.
* ``<clip>.json`` — capture metadata, including the authoritative UTC start time
  used to align Cloudflare-stored bug counts to frames.

The command builders are pure functions returning argv lists so they can be unit
tested and printed under ``--dry-run`` without a camera present. rpicam flags are
version sensitive — verify against ``rpicam-vid --help`` on the target image.
"""

from __future__ import annotations

import json
import logging
import shlex
import signal
import subprocess
import sys
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

from . import __version__
from .config import BaslerConfig, CaptureConfig

log = logging.getLogger("camrig.record")

PROFILES = ("mjpeg", "ffv1", "raw")


@dataclass
class ClipPaths:
    """Resolved output paths for one clip."""

    video: Path
    pts: Path
    meta: Path


def clip_paths(day_dir: Path, profile: str, started_at: datetime) -> ClipPaths:
    stamp = started_at.strftime("%Y%m%d_%H%M%S")
    ext = ".raw" if profile == "raw" else ".mkv"
    video = day_dir / f"clip_{stamp}{ext}"
    return ClipPaths(
        video=video,
        pts=video.with_suffix(".pts"),
        meta=video.with_suffix(".json"),
    )


def _common_rpicam_args(cfg: CaptureConfig, pts_path: Path, duration_ms: int) -> list[str]:
    """rpicam arguments shared across rpicam-vid profiles."""
    args = [
        "--camera", "0",
        "--width", str(cfg.width),
        "--height", str(cfg.height),
        "--framerate", str(cfg.framerate),
        "--denoise", cfg.denoise,
        "--save-pts", str(pts_path),
        "--nopreview",
        "--timeout", str(duration_ms),
    ]
    # Manual exposure for repeatability; 0 means "leave on auto".
    if cfg.shutter_us > 0:
        args += ["--shutter", str(cfg.shutter_us)]
    if cfg.gain > 0:
        args += ["--gain", str(cfg.gain)]
    return args


def mjpeg_qv(quality: int) -> int:
    """Map rpicam-style quality (1-100, higher = better) to ffmpeg -q:v (2-31,
    lower = better) for the Basler MJPEG encode. Approximate, but keeps one
    quality knob meaning roughly the same thing on both backends."""
    return max(2, min(31, round(31 - quality * 29 / 100)))


def _basler_producer(
    cfg: CaptureConfig, basler: BaslerConfig, pts_path: Path, duration_ms: int,
    output: str,
) -> list[str]:
    """argv for the camrig.basler producer (raw gray frames on stdout/file)."""
    args = [
        sys.executable, "-m", "camrig.basler",
        "--width", str(cfg.width),
        "--height", str(cfg.height),
        "--framerate", str(cfg.framerate),
        "--timeout", str(duration_ms),
        "--save-pts", str(pts_path),
        "--pixel-format", basler.pixel_format,
        "-o", output,
    ]
    if cfg.shutter_us > 0:
        args += ["--shutter", str(cfg.shutter_us)]
    if cfg.gain > 0:
        args += ["--gain", str(cfg.gain)]
    if basler.serial:
        args += ["--serial", basler.serial]
    if basler.ip:
        args += ["--ip", basler.ip]
    if basler.packet_size > 0:
        args += ["--packet-size", str(basler.packet_size)]
    if basler.inter_packet_delay > 0:
        args += ["--inter-packet-delay", str(basler.inter_packet_delay)]
    return args


def _build_basler_commands(
    cfg: CaptureConfig, basler: BaslerConfig, paths: ClipPaths, duration_ms: int
) -> list[list[str]]:
    """Basler pipelines: same profiles, ffmpeg does the encoding.

    The producer emits raw gray8 frames, so mjpeg/ffv1 encode on the Pi.
    ffmpeg's mjpeg encoder has no grayscale mode — yuvj444p keeps full
    luma resolution and the (flat) chroma planes cost almost no bits.
    """
    profile = cfg.profile
    if profile == "raw":
        # Producer writes sensor frames (gray8 for Mono8) straight to disk.
        return [_basler_producer(cfg, basler, paths.pts, duration_ms,
                                 str(paths.video))]

    producer = _basler_producer(cfg, basler, paths.pts, duration_ms, "-")
    ffmpeg_in = [
        "ffmpeg", "-hide_banner", "-loglevel", "warning", "-y",
        "-f", "rawvideo", "-pix_fmt", "gray",
        "-s", f"{cfg.width}x{cfg.height}",
        "-r", str(cfg.framerate),
        "-i", "-",
    ]
    if profile == "mjpeg":
        return [producer, [
            *ffmpeg_in,
            "-c:v", "mjpeg", "-q:v", str(mjpeg_qv(cfg.quality)),
            "-pix_fmt", "yuvj444p",
            str(paths.video),
        ]]
    if profile == "ffv1":
        return [producer, [
            *ffmpeg_in,
            "-c:v", "ffv1", "-level", "3", "-pix_fmt", "gray",
            str(paths.video),
        ]]
    raise ValueError(f"Unknown capture profile: {profile!r} (expected one of {PROFILES})")


def build_commands(
    cfg: CaptureConfig,
    paths: ClipPaths,
    duration_ms: int,
    basler: BaslerConfig | None = None,
) -> list[list[str]]:
    """Return the pipeline as a list of argv lists.

    A single-element list runs one process; a two-element list is a producer
    piped into a consumer (camera stdout -> ffmpeg stdin).
    """
    if cfg.camera == "basler":
        return _build_basler_commands(cfg, basler or BaslerConfig(), paths, duration_ms)
    if cfg.camera != "rpicam":
        raise ValueError(f"Unknown camera backend: {cfg.camera!r} (expected rpicam|basler)")
    profile = cfg.profile
    if profile == "mjpeg":
        rpicam = [
            "rpicam-vid", *_common_rpicam_args(cfg, paths.pts, duration_ms),
            "--codec", "mjpeg",
            "--quality", str(cfg.quality),
            "-o", "-",
        ]
        ffmpeg = [
            "ffmpeg", "-hide_banner", "-loglevel", "warning", "-y",
            "-f", "mjpeg", "-i", "-",
            "-c", "copy",
            str(paths.video),
        ]
        return [rpicam, ffmpeg]

    if profile == "ffv1":
        # Emit raw YUV420 frames and losslessly encode them with ffmpeg.
        rpicam = [
            "rpicam-vid", *_common_rpicam_args(cfg, paths.pts, duration_ms),
            "--codec", "yuv420",
            "-o", "-",
        ]
        ffmpeg = [
            "ffmpeg", "-hide_banner", "-loglevel", "warning", "-y",
            "-f", "rawvideo", "-pix_fmt", "yuv420p",
            "-s", f"{cfg.width}x{cfg.height}",
            "-r", str(cfg.framerate),
            "-i", "-",
            "-c:v", "ffv1", "-level", "3",
            str(paths.video),
        ]
        return [rpicam, ffmpeg]

    if profile == "raw":
        rpicam = [
            "rpicam-raw",
            "--camera", "0",
            "--width", str(cfg.width),
            "--height", str(cfg.height),
            "--framerate", str(cfg.framerate),
            "--denoise", cfg.denoise,
            "--save-pts", str(paths.pts),
            "--nopreview",
            "--timeout", str(duration_ms),
            "-o", str(paths.video),
        ]
        if cfg.shutter_us > 0:
            rpicam += ["--shutter", str(cfg.shutter_us)]
        if cfg.gain > 0:
            rpicam += ["--gain", str(cfg.gain)]
        return [rpicam]

    raise ValueError(f"Unknown capture profile: {profile!r} (expected one of {PROFILES})")


def describe_commands(commands: list[list[str]]) -> str:
    """Human-readable shell rendering of a pipeline, for --dry-run/logging."""
    return " | ".join(shlex.join(cmd) for cmd in commands)


def write_metadata(
    paths: ClipPaths,
    cfg: CaptureConfig,
    *,
    trigger: str,
    started_at: datetime,
    session_id: str | None,
    extra: dict | None = None,
) -> None:
    """Write the JSON metadata sidecar for a clip."""
    meta = {
        "schema": 1,
        "camrig_version": __version__,
        "clip": paths.video.name,
        "trigger": trigger,  # "scheduled" | "triggered"
        "session_id": session_id,
        "started_at_utc": started_at.astimezone(timezone.utc).isoformat(),
        "capture": asdict(cfg),
        "camera": cfg.camera,
        "sensor": "imx296" if cfg.camera == "rpicam" else "basler-ace2-mono",
    }
    if extra:
        meta.update(extra)
    paths.meta.write_text(json.dumps(meta, indent=2), encoding="utf-8")


class Recording:
    """A running capture pipeline that can be stopped early.

    Used by the supervisor: ``start()`` launches the processes, ``stop()`` sends
    SIGINT (rpicam finalises the stream cleanly) and waits for the consumer.
    """

    def __init__(self, commands: list[list[str]], paths: ClipPaths):
        self._commands = commands
        self.paths = paths
        self._procs: list[subprocess.Popen] = []

    def start(self) -> None:
        if len(self._commands) == 1:
            self._procs = [subprocess.Popen(self._commands[0])]
            return
        producer = subprocess.Popen(self._commands[0], stdout=subprocess.PIPE)
        consumer = subprocess.Popen(self._commands[1], stdin=producer.stdout)
        # Allow the producer to receive SIGPIPE if the consumer exits.
        if producer.stdout is not None:
            producer.stdout.close()
        self._procs = [producer, consumer]

    def stop(self, timeout: float = 10.0) -> int:
        """Stop the capture early and return the consumer's exit code."""
        producer = self._procs[0]
        if producer.poll() is None:
            producer.send_signal(signal.SIGINT)
        return self.wait(timeout=timeout)

    def wait(self, timeout: float | None = None) -> int:
        """Wait for the whole pipeline to finish; return last process rc."""
        rc = 0
        for proc in self._procs:
            try:
                rc = proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                proc.kill()
                rc = proc.wait()
        return rc

    def poll(self) -> int | None:
        """Return the consumer exit code if finished, else None."""
        return self._procs[-1].poll() if self._procs else None


def record_clip(
    cfg: CaptureConfig,
    day_dir: Path,
    *,
    trigger: str = "scheduled",
    session_id: str | None = None,
    duration_seconds: int | None = None,
    dry_run: bool = False,
    basler: BaslerConfig | None = None,
) -> ClipPaths:
    """Record one clip synchronously (used by the CLI and for tests).

    The supervisor uses the lower-level Recording class for cancellable manual
    sessions; this helper is the simple blocking path.
    """
    started_at = datetime.now().astimezone()
    paths = clip_paths(day_dir, cfg.profile, started_at)
    duration_ms = int((duration_seconds or cfg.clip_seconds) * 1000)
    commands = build_commands(cfg, paths, duration_ms, basler=basler)

    log.info("Capture (%s): %s", trigger, describe_commands(commands))
    if dry_run:
        print(describe_commands(commands))
        return paths

    write_metadata(
        paths, cfg, trigger=trigger, started_at=started_at, session_id=session_id
    )
    recording = Recording(commands, paths)
    recording.start()
    rc = recording.wait()
    if rc != 0:
        log.error("Capture exited with code %s", rc)
    return paths
