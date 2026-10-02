"""On-demand debug tool: render only the human-labelled trails onto a video.

Like ``camrig.motion_debug`` but driven by ``<clip>.labels.jsonl`` (see
``camrig.labels``) instead of ``.motion.json`` -- no blobs, no unlabelled
tracks, just what was clicked as e.g. ``insect`` in ``camrig.motion_view``.
Label paths are normalised ``[x, y, t]`` in clip seconds, so this works on
any rendering of the clip (the full-res ``.mkv``, ``.preview.mp4``, a
``.motion_debug.mp4``) without needing the motion sidecar.

Frame time is taken as ``frame_index / fps`` of the input video (plus
``--time-offset``), which matches the clip clock for the preview (its
``fps=`` filter keeps timing from t=0).

    camrig debug-labels clip.preview.mp4
    python -m camrig.label_debug clip.preview.mp4 --only insect,unsure -o out.mp4
"""

from __future__ import annotations

import argparse
import bisect
import json
import logging
import subprocess
import sys
from pathlib import Path

import numpy as np

from .labels import LABELS, LABELS_SUFFIX
from .motion_debug import _PALETTE, _draw_circle, _draw_line, build_commands
from .record import describe_commands

log = logging.getLogger("camrig.label_debug")

LABEL_DEBUG_SUFFIX = ".label_debug.mp4"
DEFAULT_TRAIL_SECONDS = 3.0

# BGR. Insects cycle through the motion_debug palette so neighbours stay
# distinguishable; other labels get one fixed colour each.
_LABEL_COLORS = {"other": (60, 60, 230), "unsure": (0, 200, 230)}

# Suffixes that sit between the clip stem and the extension of a derived video.
_DERIVED_SUFFIXES = (".preview", ".motion_debug", ".label_debug")


def default_labels_path(video: Path) -> Path:
    """clip.mkv / clip.preview.mp4 / clip.motion_debug.mp4 -> clip.labels.jsonl"""
    stem = video.stem
    for suffix in _DERIVED_SUFFIXES:
        if stem.endswith(suffix):
            stem = stem[: -len(suffix)]
            break
    return video.with_name(stem + LABELS_SUFFIX)


def latest_labels(records: list[dict], only: set[str]) -> list[dict]:
    """Last record per ``source_track`` (relabels append), kept if its label is in ``only``."""
    by_track: dict[object, dict] = {}
    for i, record in enumerate(records):
        by_track[record.get("source_track", ("unkeyed", i))] = record
    return sorted((r for r in by_track.values() if r["label"] in only), key=lambda r: r["t0"])


def visible_trail(path: list[list[float]], t: float, trail_seconds: float | None) -> list[tuple[float, float]]:
    """Points of ``path`` visible at time ``t``, ending at an interpolated head.

    Empty before the track starts or once its last point is older than
    ``trail_seconds`` (None keeps the full trail forever).
    """
    times = [p[2] for p in path]
    if t < times[0]:
        return []
    if trail_seconds is not None and t - times[-1] > trail_seconds:
        return []
    lo_t = -float("inf") if trail_seconds is None else t - trail_seconds
    hi = bisect.bisect_right(times, t)
    lo = bisect.bisect_left(times, lo_t)
    pts = [(x, y) for x, y, _ in path[lo:hi]]
    if hi < len(path):
        # Interpolate the head between the last passed point and the next.
        (x0, y0, t0), (x1, y1, t1) = path[hi - 1], path[hi]
        a = (t - t0) / (t1 - t0) if t1 > t0 else 0.0
        pts.append((x0 + a * (x1 - x0), y0 + a * (y1 - y0)))
    return pts


def probe(video: Path) -> tuple[int, int, float]:
    """(width, height, fps) of the first video stream."""
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=width,height,avg_frame_rate", "-of", "json", str(video)],
        check=True, capture_output=True, text=True,
    ).stdout
    stream = json.loads(out)["streams"][0]
    num, den = stream["avg_frame_rate"].split("/")
    return int(stream["width"]), int(stream["height"]), float(num) / float(den)


def render(
    video: Path, records: list[dict], output: Path, *, trail_seconds: float | None,
    time_offset: float = 0.0, crf: int = 23, dry_run: bool = False,
) -> bool:
    width, height, fps = probe(video)
    commands = build_commands(video, output, width, height, fps, crf)
    log.info("Label debug: %s", describe_commands(commands))
    if dry_run:
        print(describe_commands(commands))
        return True

    thickness = max(2, round(width / 360))
    head_r = thickness + 1
    colors = [
        _PALETTE[i % len(_PALETTE)] if r["label"] == "insect" else _LABEL_COLORS.get(r["label"], (255, 255, 255))
        for i, r in enumerate(records)
    ]
    # Records are sorted by t0; only those already started can be visible.
    starts = [r["t0"] for r in records]

    frame_bytes = width * height * 3
    decoder = subprocess.Popen(commands[0], stdout=subprocess.PIPE)
    encoder = subprocess.Popen(commands[1], stdin=subprocess.PIPE)
    assert decoder.stdout is not None and encoder.stdin is not None

    frame_idx = 0
    while True:
        data = decoder.stdout.read(frame_bytes)
        if len(data) < frame_bytes:
            break
        frame = np.frombuffer(data, dtype=np.uint8).reshape(height, width, 3).copy()
        t = frame_idx / fps + time_offset
        for i in range(bisect.bisect_right(starts, t)):
            pts = visible_trail(records[i]["path"], t, trail_seconds)
            if not pts:
                continue
            px = [(round(x * width), round(y * height)) for x, y in pts]
            for (x0, y0), (x1, y1) in zip(px, px[1:]):
                _draw_line(frame, x0, y0, x1, y1, colors[i], thickness)
            if t <= records[i]["t1"]:
                _draw_circle(frame, *px[-1], head_r, colors[i])
        encoder.stdin.write(frame.tobytes())
        frame_idx += 1

    decoder.stdout.close()
    encoder.stdin.close()
    decoder_rc, encoder_rc = decoder.wait(), encoder.wait()
    if decoder_rc != 0 or encoder_rc != 0:
        log.error("Label debug failed for %s (decode rc=%s, encode rc=%s)",
                  video.name, decoder_rc, encoder_rc)
        output.unlink(missing_ok=True)
        return False
    log.info("Wrote %s (%d frames, %d labelled tracks)", output, frame_idx, len(records))
    return True


def run(video: Path, *, labels: Path | None = None, output: Path | None = None,
        only: set[str] = frozenset({"insect"}), trail_seconds: float | None = DEFAULT_TRAIL_SECONDS,
        time_offset: float = 0.0, dry_run: bool = False) -> bool:
    labels = labels or default_labels_path(video)
    if not labels.exists():
        log.error("Missing %s (pass --labels)", labels)
        return False
    raw = [json.loads(line) for line in labels.read_text(encoding="utf-8").splitlines() if line.strip()]
    records = latest_labels(raw, only)
    log.info("%d of %d labelled tracks match %s", len(records), len({r.get("source_track") for r in raw}),
             ",".join(sorted(only)))
    out = output or video.with_name(default_labels_path(video).name.removesuffix(LABELS_SUFFIX) + LABEL_DEBUG_SUFFIX)
    return render(video, records, out, trail_seconds=trail_seconds,
                  time_offset=time_offset, dry_run=dry_run)


def add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("video", help="video to draw on (.mkv, .preview.mp4, ...)")
    parser.add_argument("-l", "--labels", help="labels file (default: <clip>.labels.jsonl next to the video)")
    parser.add_argument("-o", "--output", help="output path (default: <clip>.label_debug.mp4)")
    parser.add_argument("--only", default="insect",
                        help=f"comma-separated labels to draw, from {','.join(LABELS)} (default: insect)")
    parser.add_argument("--trail-seconds", type=float, default=DEFAULT_TRAIL_SECONDS,
                        help=f"how long a trail stays visible (default {DEFAULT_TRAIL_SECONDS})")
    parser.add_argument("--persist", action="store_true", help="keep every trail on screen for the whole clip")
    parser.add_argument("--time-offset", type=float, default=0.0,
                        help="seconds added to the video's frame time to get label time")
    parser.add_argument("--dry-run", action="store_true", help="print the ffmpeg commands, do not run")


def run_from_args(args) -> bool:
    return run(
        Path(args.video),
        labels=Path(args.labels) if args.labels else None,
        output=Path(args.output) if args.output else None,
        only={s.strip() for s in args.only.split(",") if s.strip()},
        trail_seconds=None if args.persist else args.trail_seconds,
        time_offset=args.time_offset, dry_run=args.dry_run,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    add_arguments(parser)
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    return 0 if run_from_args(args) else 1


if __name__ == "__main__":
    sys.exit(main())
