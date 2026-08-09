"""On-demand debug tool: render motion tracks/blobs as overlays on a clip.

Not part of the automatic pipeline (``camrig.postprocess`` runs unconditionally
on every clip) -- run this by hand when you want to see what ``camrig.motion``
is or isn't catching. Requires the clip's ``.motion.json`` sidecar; run
``camrig postprocess <clip>`` first if it's missing.

Decodes the clip at the motion-analysis resolution, so blob/track coordinates
from the sidecar need no rescaling. Each window's blobs are drawn as grey
boxes; each multi-window track gets a coloured trail with a dot at its current
position, fading out (--trail-seconds) rather than persisting for the clip.

    camrig debug-motion clip.mkv
    python -m camrig.motion_debug clip.mkv -o clip.debug.mp4
"""

from __future__ import annotations

import argparse
import json
import logging
import subprocess
import sys
from pathlib import Path

import numpy as np

from .config import Config, load_config
from .motion import SCHEMA as MOTION_SCHEMA
from .postprocess import motion_path
from .record import describe_commands

log = logging.getLogger("camrig.motion_debug")

DEBUG_SUFFIX = ".motion_debug.mp4"

# Deterministic per-track colours (BGR, cycled by track index).
_PALETTE = [
    (66, 133, 244), (55, 68, 219), (0, 180, 244), (88, 157, 15),
    (188, 71, 171), (193, 172, 0), (67, 112, 255), (36, 157, 158),
]
_BLOB_COLOR = (170, 170, 170)


def debug_path(video: Path) -> Path:
    return video.with_suffix(DEBUG_SUFFIX)


def _draw_rect(frame: np.ndarray, x: int, y: int, w: int, h: int, color, thickness: int) -> None:
    fh, fw = frame.shape[:2]
    x0, y0 = max(x, 0), max(y, 0)
    x1, y1 = min(x + w, fw), min(y + h, fh)
    if x1 <= x0 or y1 <= y0:
        return
    frame[y0:min(y0 + thickness, y1), x0:x1] = color
    frame[max(y1 - thickness, y0):y1, x0:x1] = color
    frame[y0:y1, x0:min(x0 + thickness, x1)] = color
    frame[y0:y1, max(x1 - thickness, x0):x1] = color


def _draw_circle(frame: np.ndarray, cx: int, cy: int, r: int, color,
                 *, mask: np.ndarray | None = None) -> None:
    fh, fw = frame.shape[:2]
    y0, y1 = max(cy - r, 0), min(cy + r + 1, fh)
    x0, x1 = max(cx - r, 0), min(cx + r + 1, fw)
    if x1 <= x0 or y1 <= y0:
        return
    yy, xx = np.ogrid[y0:y1, x0:x1]
    disc = (xx - cx) ** 2 + (yy - cy) ** 2 <= r * r
    frame[y0:y1, x0:x1][disc] = color
    if mask is not None:
        mask[y0:y1, x0:x1][disc] = True


def _draw_line(frame: np.ndarray, x0: int, y0: int, x1: int, y1: int, color, thickness: int,
              *, mask: np.ndarray | None = None) -> None:
    fh, fw = frame.shape[:2]
    steps = max(abs(x1 - x0), abs(y1 - y0), 1)
    xs = np.linspace(x0, x1, steps + 1).round().astype(int)
    ys = np.linspace(y0, y1, steps + 1).round().astype(int)
    r = thickness // 2
    for x, y in zip(xs, ys):
        xa, xb = max(x - r, 0), min(x + r + 1, fw)
        ya, yb = max(y - r, 0), min(y + r + 1, fh)
        if xb > xa and yb > ya:
            frame[ya:yb, xa:xb] = color
            if mask is not None:
                mask[ya:yb, xa:xb] = True


def _frame_windows(windows: list[dict]) -> list[int]:
    """Expand windows' ``n_frames`` into a per-frame window-index lookup."""
    out = []
    for idx, win in enumerate(windows):
        out.extend([idx] * win["n_frames"])
    return out


def build_commands(
    video: Path, output: Path, width: int, height: int, fps: float, crf: int,
) -> list[list[str]]:
    """Return [decode, encode] argv lists: raw bgr24 frames in, drawn frames out."""
    decode = [
        "ffmpeg", "-hide_banner", "-loglevel", "error",
        "-i", str(video),
        "-vf", f"scale={width}:{height},format=bgr24",
        "-f", "rawvideo", "pipe:1",
    ]
    encode = [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
        "-f", "rawvideo", "-pix_fmt", "bgr24", "-s", f"{width}x{height}", "-r", str(fps),
        "-i", "pipe:0",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", str(crf),
        "-pix_fmt", "yuv420p", "-movflags", "+faststart",
        str(output),
    ]
    return [decode, encode]


def load_motion(video: Path) -> dict | None:
    """Load and validate a clip's .motion.json sidecar, or None (logged) if unusable."""
    motion_file = motion_path(video)
    if not motion_file.exists():
        log.error("Missing %s; run `camrig postprocess %s` first", motion_file, video)
        return None
    motion = json.loads(motion_file.read_text(encoding="utf-8"))
    if motion.get("schema") != MOTION_SCHEMA:
        log.error(
            "%s is schema %s (need %s, blob-track-v1); regenerate with "
            "`camrig postprocess --force %s`",
            motion_file, motion.get("schema"), MOTION_SCHEMA, video,
        )
        return None
    return motion


def render(
    cfg: Config, video: Path, motion: dict, output: Path,
    *, fps: float | None = None, trail_seconds: float = 3.0, dry_run: bool = False,
) -> bool:
    """Draw motion.json's blobs/tracks onto video, writing an annotated preview."""
    width, height = motion["width"], motion["height"]
    windows, tracks = motion["windows"], motion["tracks"]
    out_fps = fps or cfg.capture.framerate
    window_frames = motion.get("params", {}).get("window", 6)
    trail_windows = max(round(trail_seconds * out_fps / window_frames), 1)

    commands = build_commands(video, output, width, height, out_fps, cfg.postprocess.preview_crf)
    log.info("Debug preview: %s", describe_commands(commands))
    if dry_run:
        print(describe_commands(commands))
        return True

    frame_windows = _frame_windows(windows)
    frame_bytes = width * height * 3
    total_frames = motion.get("frame_count", len(frame_windows))
    decoder = subprocess.Popen(commands[0], stdout=subprocess.PIPE)
    encoder = subprocess.Popen(commands[1], stdin=subprocess.PIPE)
    assert decoder.stdout is not None and encoder.stdin is not None

    # Recomputed from scratch each time the current window changes (not every
    # frame -- that's still ~10x/sec, cheap) and blitted onto frames via the
    # mask in between. Bounding each track's trail to its last `trail_windows`
    # points keeps this O(windows * trail_windows) regardless of how long a
    # track lives, and gives the fade-after-a-few-seconds look for free.
    trail_canvas = np.zeros((height, width, 3), dtype=np.uint8)
    trail_mask = np.zeros((height, width), dtype=bool)

    def _rebuild_trails(w_idx: int) -> None:
        trail_canvas.fill(0)
        trail_mask.fill(False)
        for ti, track in enumerate(tracks):
            w0 = track["w0"]
            if w_idx < w0:
                continue
            idx_end = min(w_idx - w0, track["n"] - 1)
            idx_start = max(0, idx_end - trail_windows + 1)
            pts = track["path"][idx_start:idx_end + 1]
            color = _PALETTE[ti % len(_PALETTE)]
            for (x0, y0), (x1, y1) in zip(pts, pts[1:]):
                _draw_line(trail_canvas, round(x0), round(y0), round(x1), round(y1), color, 1,
                          mask=trail_mask)
            if pts:
                cx, cy = pts[-1]
                _draw_circle(trail_canvas, round(cx), round(cy), 2, color, mask=trail_mask)

    frame_idx = 0
    prev_w_idx = -1
    log_every = max(total_frames // 20, 1)
    while True:
        data = decoder.stdout.read(frame_bytes)
        if len(data) < frame_bytes:
            break
        frame = np.frombuffer(data, dtype=np.uint8).reshape(height, width, 3).copy()
        w_idx = frame_windows[frame_idx] if frame_idx < len(frame_windows) else len(windows) - 1

        if w_idx != prev_w_idx:
            _rebuild_trails(w_idx)
            prev_w_idx = w_idx

        frame[trail_mask] = trail_canvas[trail_mask]

        for blob in windows[w_idx]["blobs"]:
            x, y, bw, bh = blob["bbox"]
            _draw_rect(frame, x, y, bw, bh, _BLOB_COLOR, 1)

        encoder.stdin.write(frame.tobytes())
        frame_idx += 1
        if frame_idx % log_every == 0:
            log.info("Debug preview: %d/%d frames (%.0f%%)",
                     frame_idx, total_frames, 100 * frame_idx / total_frames)

    decoder.stdout.close()
    encoder.stdin.close()
    decoder_rc = decoder.wait()
    encoder_rc = encoder.wait()
    if decoder_rc != 0 or encoder_rc != 0:
        log.error("Debug preview failed for %s (decode rc=%s, encode rc=%s)",
                   video.name, decoder_rc, encoder_rc)
        output.unlink(missing_ok=True)
        return False

    log.info("Wrote %s (%d frames, %d tracks)", output, frame_idx, len(tracks))
    return True


def run(cfg: Config, video: Path, *, output: Path | None = None, fps: float | None = None,
        trail_seconds: float = 3.0, dry_run: bool = False) -> bool:
    motion = load_motion(video)
    if motion is None:
        return False
    return render(cfg, video, motion, output or debug_path(video),
                 fps=fps, trail_seconds=trail_seconds, dry_run=dry_run)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("clip", help="path to a .mkv clip (its .motion.json sidecar must exist)")
    parser.add_argument("-o", "--output", help="output path (default: <clip>.motion_debug.mp4)")
    parser.add_argument("--fps", type=float, help="output frame rate (default: capture.framerate)")
    parser.add_argument("--trail-seconds", type=float, default=3.0,
                        help="how long a track's trail stays visible before fading (default 3.0)")
    parser.add_argument("-c", "--config", help="path to config.toml")
    parser.add_argument("--dry-run", action="store_true", help="print the ffmpeg commands, do not run")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    cfg = load_config(args.config)
    video = Path(args.clip)
    output = Path(args.output) if args.output else None
    ok = run(cfg, video, output=output, fps=args.fps, trail_seconds=args.trail_seconds,
            dry_run=args.dry_run)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
