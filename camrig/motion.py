"""Per-frame motion metrics — PLACEHOLDER analysis, evolving toward trail tracking.

This is the swappable stage of the post-capture pipeline. ``camrig.postprocess``
pipes downscaled grayscale frames into it (ffmpeg converts from the colour
capture) and it writes a JSON sidecar of per-frame metrics. The current analysis
is deliberately simple — frame differencing — and exists to reserve the pipeline
slot; grow it into motion-trail tracking (background modelling, connected
components, track linking) here without touching capture or upload.

Keep this contract stable while iterating on the analysis:

* stdin — raw 8-bit grayscale frames, ``width * height`` bytes each, at the
  source clip's full frame rate. Frame index i corresponds to line i of the
  clip's ``.pts`` sidecar; that is what aligns metrics to wall-clock time (and
  to the Cloudflare-stored bug counts).
* ``--output`` — path of the JSON sidecar to write.

Usable standalone for experimentation on any clip:

    ffmpeg -i clip.mkv -vf scale=728:544,format=gray -f rawvideo - |
        python3 -m camrig.motion --width 728 --height 544 -o clip.motion.json

After changing the analysis, regenerate existing sidecars with
``camrig postprocess --force``.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import BinaryIO

import numpy as np

SCHEMA = 1
ANALYSIS = "frame-diff-placeholder"


def analyse(stream: BinaryIO, width: int, height: int, threshold: int) -> dict:
    """Consume raw gray8 frames from stream; return per-frame metrics.

    Placeholder metrics per frame (index-aligned with the ``.pts`` sidecar):

    * ``mean_abs_diff``   — mean absolute pixel difference vs the previous frame.
    * ``active_fraction`` — fraction of pixels whose difference exceeds
      ``threshold`` (a crude "how much of the frame moved" signal).
    """
    frame_bytes = width * height
    mean_abs_diff: list[float] = []
    active_fraction: list[float] = []
    prev: np.ndarray | None = None

    while True:
        data = stream.read(frame_bytes)
        if len(data) < frame_bytes:  # EOF (a truncated trailing frame is dropped)
            break
        frame = np.frombuffer(data, dtype=np.uint8).astype(np.int16)
        if prev is None:
            # No predecessor: emit zeros so index i keeps matching .pts line i.
            mean_abs_diff.append(0.0)
            active_fraction.append(0.0)
        else:
            diff = np.abs(frame - prev)
            mean_abs_diff.append(round(float(diff.mean()), 3))
            active_fraction.append(round(float((diff > threshold).mean()), 5))
        prev = frame

    return {
        "schema": SCHEMA,
        "analysis": ANALYSIS,
        "width": width,
        "height": height,
        "threshold": threshold,
        "frame_count": len(mean_abs_diff),
        "mean_abs_diff": mean_abs_diff,
        "active_fraction": active_fraction,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--width", type=int, required=True)
    parser.add_argument("--height", type=int, required=True)
    parser.add_argument("--threshold", type=int, default=12,
                        help="per-pixel diff (0-255) counted as active (default 12)")
    parser.add_argument("--clip", help="source clip name to embed in the sidecar")
    parser.add_argument("-o", "--output", required=True, help="JSON sidecar path")
    args = parser.parse_args(argv)

    result = analyse(sys.stdin.buffer, args.width, args.height, args.threshold)
    if args.clip:
        result = {"clip": args.clip, **result}
    Path(args.output).write_text(json.dumps(result), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
