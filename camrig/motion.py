"""Blob detection + track linking over background-subtracted motion masks.

This is the swappable stage of the post-capture pipeline. ``camrig.postprocess``
pipes downscaled grayscale frames into it (ffmpeg converts from the colour
capture) and it writes a JSON sidecar of motion blobs and tracks.

The analysis (``blob-track-v1``):

1. Each frame is differenced against a slow exponential-moving-average
   background (not the previous frame — consecutive diffs lose slow crawlers
   and split fast fliers into old/new-position dipoles) and thresholded to a
   binary motion mask.
2. Masks are accumulated over short windows (default 6 frames ≈ 100 ms at
   60 fps). A pixel must be hot in ``min_hits`` frames to count, which drops
   single-frame sensor noise; body parts of one animal (wings, legs) land
   close together within a window and fuse into one region.
3. The accumulated mask is reduced to a coarse cell grid (default 8×8 px) and
   connected components are labelled there — the quantisation merges fragments
   within a cell of each other and keeps labelling cheap without scipy.
4. Blobs are linked window-to-window into tracks (greedy nearest-centroid).
   Per-track ``straightness`` (net displacement / path length) and per-blob
   ``chronic`` (how persistently its cells were active over the whole clip)
   are the plant discriminators: insects travel through fresh cells, swaying
   vegetation oscillates in place over the same cells for minutes.

Keep this contract stable while iterating on the analysis:

* stdin — raw 8-bit grayscale frames, ``width * height`` bytes each, at the
  source clip's full frame rate. Frame index i corresponds to line i of the
  clip's ``.pts`` sidecar; that is what aligns metrics to wall-clock time (and
  to the Cloudflare-stored bug counts). Windows record their ``frame_start``
  so blob times resolve the same way.
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
import math
import sys
from pathlib import Path
from typing import BinaryIO, Callable

import numpy as np

SCHEMA = 2
BLOB_TRACK_V1 = "blob-track-v1"
DEFAULT_DETECTOR = BLOB_TRACK_V1
# Backwards-compatible alias for code that imported the previous constant.
ANALYSIS = BLOB_TRACK_V1

Detector = Callable[..., dict]

# Active pixels a cell needs before it participates in blob labelling. Together
# with min_hits this is the noise floor: a blob must be >= CELL_MIN_PX pixels
# hot for >= min_hits frames of a window.
CELL_MIN_PX = 2

_NEIGHBOURS = [(-1, -1), (-1, 0), (-1, 1), (0, -1), (0, 1), (1, -1), (1, 0), (1, 1)]


def _label_cells(active: np.ndarray) -> list[list[tuple[int, int]]]:
    """8-connected components over a boolean cell grid; returns cell coords."""
    todo = set(zip(*(a.tolist() for a in np.nonzero(active))))
    components: list[list[tuple[int, int]]] = []
    while todo:
        stack = [todo.pop()]
        comp = []
        while stack:
            cy, cx = stack.pop()
            comp.append((cy, cx))
            for dy, dx in _NEIGHBOURS:
                n = (cy + dy, cx + dx)
                if n in todo:
                    todo.remove(n)
                    stack.append(n)
        components.append(comp)
    return components


def _window_blobs(hits: np.ndarray, min_hits: int, cell: int,
                  min_area: int) -> list[dict]:
    """Extract blobs from one window's per-pixel hit counts.

    Each blob carries a private ``_cells`` list (cell-grid coords) used later
    for chronic-activity scoring; it is stripped before serialisation.
    """
    h, w = hits.shape
    ch, cw = h // cell, w // cell
    blocks = hits[:ch * cell, :cw * cell].reshape(ch, cell, cw, cell)
    active_px = (blocks >= min_hits).sum(axis=(1, 3))
    cell_hits = blocks.sum(axis=(1, 3), dtype=np.int32)
    cell_peak = blocks.max(axis=(1, 3))

    blobs = []
    for comp in _label_cells(active_px >= CELL_MIN_PX):
        area = int(sum(active_px[c] for c in comp))
        if area < min_area:
            continue
        weight = sum(int(cell_hits[c]) for c in comp) or 1
        cx = sum((c[1] + 0.5) * cell * int(cell_hits[c]) for c in comp) / weight
        cy = sum((c[0] + 0.5) * cell * int(cell_hits[c]) for c in comp) / weight
        ys = [c[0] for c in comp]
        xs = [c[1] for c in comp]
        blobs.append({
            "c": [round(cx, 1), round(cy, 1)],
            "area": area,
            "bbox": [min(xs) * cell, min(ys) * cell,
                     (max(xs) - min(xs) + 1) * cell, (max(ys) - min(ys) + 1) * cell],
            "peak": int(max(cell_peak[c] for c in comp)),
            "_cells": comp,
        })
    return blobs


def _link_tracks(windows: list[dict], max_dist: float) -> list[dict]:
    """Greedy nearest-centroid linking of blobs across consecutive windows."""
    open_tracks: list[dict] = []  # {'path': [(w_idx, blob)], ...}
    done: list[dict] = []

    for w_idx, win in enumerate(windows):
        blobs = win["blobs"]
        candidates = []
        for ti, track in enumerate(open_tracks):
            tx, ty = track["path"][-1][1]["c"]
            for bi, blob in enumerate(blobs):
                d = math.dist((tx, ty), blob["c"])
                if d <= max_dist:
                    candidates.append((d, ti, bi))
        candidates.sort(key=lambda t: t[0])
        used_t: set[int] = set()
        used_b: set[int] = set()
        for _, ti, bi in candidates:
            if ti in used_t or bi in used_b:
                continue
            used_t.add(ti)
            used_b.add(bi)
            open_tracks[ti]["path"].append((w_idx, blobs[bi]))
        still_open = []
        for ti, track in enumerate(open_tracks):
            (still_open if ti in used_t else done).append(track)
        open_tracks = still_open
        for bi, blob in enumerate(blobs):
            if bi not in used_b:
                open_tracks.append({"path": [(w_idx, blob)]})
    done.extend(open_tracks)

    tracks = []
    for track in done:
        path = track["path"]
        if len(path) < 2:  # lone blobs are already in windows[]
            continue
        points = [blob["c"] for _, blob in path]
        path_len = sum(math.dist(points[i], points[i + 1])
                       for i in range(len(points) - 1))
        net = math.dist(points[0], points[-1])
        tracks.append({
            "w0": path[0][0],
            "n": len(path),
            "path": points,
            "net": round(net, 1),
            "len": round(path_len, 1),
            "straightness": round(net / path_len, 3) if path_len > 0 else 0.0,
            "mean_area": round(sum(b["area"] for _, b in path) / len(path), 1),
            "chronic": round(sum(b["chronic"] for _, b in path) / len(path), 3),
        })
    tracks.sort(key=lambda t: t["w0"])
    return tracks


def analyse_blob_track_v1(stream: BinaryIO, width: int, height: int, threshold: int,
                          window: int = 6, min_hits: int = 2, cell: int = 8,
                          bg_alpha: float = 0.05, min_area: int = 4,
                          max_link_dist: float = 80.0) -> dict:
    """Version 1 EMA-background blob detector and window-level track linker.

    Versioned detector functions are immutable experiment definitions. Add a new
    function and registry entry for changed behaviour instead of editing this one
    after field data has been produced with it.
    """
    frame_bytes = width * height
    active_fraction: list[float] = []
    windows: list[dict] = []
    bg: np.ndarray | None = None
    hits = np.zeros((height, width), dtype=np.uint8)
    frames_in_window = 0
    window_start = 0
    ch, cw = height // cell, width // cell
    chronic_counts = np.zeros((ch, cw), dtype=np.uint32)

    def flush_window() -> None:
        nonlocal frames_in_window, window_start
        blobs = _window_blobs(hits, min_hits, cell, min_area)
        for blob in blobs:
            for c in blob["_cells"]:
                chronic_counts[c] += 1
        windows.append({"f": window_start, "n_frames": frames_in_window,
                        "blobs": blobs})
        hits.fill(0)
        window_start += frames_in_window
        frames_in_window = 0

    while True:
        data = stream.read(frame_bytes)
        if len(data) < frame_bytes:  # EOF (a truncated trailing frame is dropped)
            break
        frame = np.frombuffer(data, dtype=np.uint8).reshape(height, width)
        frame = frame.astype(np.float32)
        if bg is None:
            # No background yet: emit zero so index i keeps matching .pts line i.
            bg = frame.copy()
            active_fraction.append(0.0)
        else:
            mask = np.abs(frame - bg) > threshold
            active_fraction.append(round(float(mask.mean()), 5))
            hits += mask
            bg += bg_alpha * (frame - bg)
        frames_in_window += 1
        if frames_in_window == window:
            flush_window()
    if frames_in_window:
        flush_window()

    # Chronic activity: fraction of windows each blob's cells were active in.
    # Insects pass through cells; vegetation keeps the same cells hot all clip.
    n_windows = len(windows) or 1
    for win in windows:
        for blob in win["blobs"]:
            cells = blob.pop("_cells")
            blob["chronic"] = round(
                sum(float(chronic_counts[c]) for c in cells) / (len(cells) * n_windows), 3)

    return {
        "schema": SCHEMA,
        "analysis": BLOB_TRACK_V1,
        "width": width,
        "height": height,
        "params": {
            "threshold": threshold, "window": window, "min_hits": min_hits,
            "cell": cell, "bg_alpha": bg_alpha, "min_area": min_area,
            "max_link_dist": max_link_dist,
        },
        "frame_count": len(active_fraction),
        "active_fraction": active_fraction,
        "windows": windows,
        "tracks": _link_tracks(windows, max_link_dist),
    }


DETECTORS: dict[str, Detector] = {
    BLOB_TRACK_V1: analyse_blob_track_v1,
}


def available_detectors() -> tuple[str, ...]:
    """Return stable detector IDs accepted by the API and CLI."""
    return tuple(sorted(DETECTORS))


def analyse(stream: BinaryIO, width: int, height: int, threshold: int,
            window: int = 6, min_hits: int = 2, cell: int = 8,
            bg_alpha: float = 0.05, min_area: int = 4,
            max_link_dist: float = 80.0,
            detector: str = DEFAULT_DETECTOR) -> dict:
    """Run a named, versioned detector over a raw gray8 frame stream."""
    try:
        detector_fn = DETECTORS[detector]
    except KeyError:
        choices = ", ".join(available_detectors())
        raise ValueError(
            f"Unknown detector {detector!r}; available detectors: {choices}"
        ) from None
    return detector_fn(
        stream, width, height, threshold, window=window, min_hits=min_hits,
        cell=cell, bg_alpha=bg_alpha, min_area=min_area,
        max_link_dist=max_link_dist,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--detector", choices=available_detectors(),
                        default=DEFAULT_DETECTOR,
                        help="versioned detector implementation (default %(default)s)")
    parser.add_argument("--width", type=int, required=True)
    parser.add_argument("--height", type=int, required=True)
    parser.add_argument("--threshold", type=int, default=12,
                        help="per-pixel diff vs background (0-255) counted as hot (default 12)")
    parser.add_argument("--window", type=int, default=6,
                        help="frames accumulated per blob-extraction window (default 6)")
    parser.add_argument("--min-hits", type=int, default=2,
                        help="frames a pixel must be hot within a window (default 2)")
    parser.add_argument("--cell", type=int, default=8,
                        help="cell size in px for blob labelling/merging (default 8)")
    parser.add_argument("--bg-alpha", type=float, default=0.05,
                        help="EMA background adaption rate per frame (default 0.05)")
    parser.add_argument("--min-area", type=int, default=4,
                        help="minimum blob area in active pixels (default 4)")
    parser.add_argument("--max-link-dist", type=float, default=80.0,
                        help="max centroid jump in px to link blobs across windows (default 80)")
    parser.add_argument("--clip", help="source clip name to embed in the sidecar")
    parser.add_argument("-o", "--output", required=True, help="JSON sidecar path")
    args = parser.parse_args(argv)

    result = analyse(sys.stdin.buffer, args.width, args.height, args.threshold,
                     window=args.window, min_hits=args.min_hits, cell=args.cell,
                     bg_alpha=args.bg_alpha, min_area=args.min_area,
                     max_link_dist=args.max_link_dist, detector=args.detector)
    if args.clip:
        result = {"clip": args.clip, **result}
    Path(args.output).write_text(json.dumps(result), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
