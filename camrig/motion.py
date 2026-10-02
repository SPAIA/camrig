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
   vegetation oscillates in place over the same cells for minutes. Two more
   per-track discriminators catch what those two miss: ``step_ratio``
   (largest hop / median hop -- one implausible jump between otherwise-steady
   steps) and ``footprint_ratio`` (bounding-box footprint swept by the whole
   track / mean per-point box size -- a blob that just churns shape/size in
   place without ever sweeping new territory). None of the four are applied
   here; ``camrig.motion_debug``/``camrig.motion_view`` filter on them via
   ``[postprocess]`` thresholds, tuned against ``camrig.labels`` ground truth
   (``camrig.scoring``).

Keep this contract stable while iterating on the analysis:

* stdin — raw 8-bit grayscale frames, ``width * height`` bytes each, at the
  source clip's full frame rate. Frame index i corresponds to line i of the
  clip's ``.pts`` sidecar; that is what aligns metrics to wall-clock time (and
  to the Cloudflare-stored bug counts). Windows record their ``frame_start``
  so blob times resolve the same way.
* ``--framerate`` — the clip's own capture frame rate, stored verbatim as
  ``"framerate"`` in the sidecar. Nothing in this module's own analysis
  needs it (windows/tracks stay in frame-index space throughout), but every
  *consumer* that converts a frame/window index to wall-clock time
  (``camrig.scoring``, ``camrig.stitch``, ``camrig.motion_debug``,
  ``camrig.motion_view``) needs the TRUE rate this clip was captured at, not
  whatever ``config.toml`` currently says -- those can differ once clips
  captured under different ``[capture]`` settings coexist. A sidecar written
  before this field existed has no ``"framerate"`` key; consumers fall back
  to ``cfg.capture.framerate`` for those.
* ``--output`` — path of the JSON sidecar to write.

Usable standalone for experimentation on any clip:

    ffmpeg -i clip.mkv -vf scale=728:544,format=gray -f rawvideo - |
        python3 -m camrig.motion --width 728 --height 544 --framerate 60 \
            -o clip.motion.json

After changing the analysis, regenerate existing sidecars with
``camrig postprocess --force``.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path
from statistics import median
from typing import BinaryIO

import numpy as np

SCHEMA = 4
ANALYSIS = "blob-track-v1"

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


class TrackLinker:
    """Greedy nearest-centroid linking of blobs, one window at a time.

    ``max_dist`` alone caps every track at the same flat search radius, so a
    track that's been crawling at a few px/window can suddenly "teleport" to
    an unrelated blob that happens to land within max_dist, drawing a bogus
    straight-line jump. Once a track has an established velocity (its last
    hop distance), the next hop is additionally capped at that velocity plus
    ``max_accel``, so a link can only extend as fast as the track has
    actually been shown to accelerate; a fresh (one-point) track has no
    velocity yet and still uses the flat ``max_dist``.

    Stateful so the same linking drives both the batch ``_link_tracks`` and
    the live viewer (``camrig.live``), which steps it as windows arrive.
    """

    def __init__(self, max_dist: float, max_accel: float = 40.0) -> None:
        self.max_dist = max_dist
        self.max_accel = max_accel
        self.open: list[dict] = []  # {'id': int, 'path': [(w_idx, blob)]}
        self._next_id = 0

    def step(self, w_idx: int, blobs: list[dict]) -> list[dict]:
        """Link one window's blobs onto the open tracks; return the tracks it closed."""
        candidates = []
        for ti, track in enumerate(self.open):
            path = track["path"]
            tx, ty = path[-1][1]["c"]
            if len(path) >= 2:
                px, py = path[-2][1]["c"]
                recent_v = math.dist((px, py), (tx, ty))
                cap = min(self.max_dist, recent_v + self.max_accel)
            else:
                cap = self.max_dist
            for bi, blob in enumerate(blobs):
                d = math.dist((tx, ty), blob["c"])
                if d <= cap:
                    candidates.append((d, ti, bi))
        candidates.sort(key=lambda t: t[0])
        used_t: set[int] = set()
        used_b: set[int] = set()
        for _, ti, bi in candidates:
            if ti in used_t or bi in used_b:
                continue
            used_t.add(ti)
            used_b.add(bi)
            self.open[ti]["path"].append((w_idx, blobs[bi]))
        still_open: list[dict] = []
        closed: list[dict] = []
        for ti, track in enumerate(self.open):
            (still_open if ti in used_t else closed).append(track)
        self.open = still_open
        for bi, blob in enumerate(blobs):
            if bi not in used_b:
                self.open.append({"id": self._next_id, "path": [(w_idx, blob)]})
                self._next_id += 1
        return closed

    def finish(self) -> list[dict]:
        """Close and return every still-open track."""
        closed, self.open = self.open, []
        return closed


def track_metrics(path: list[tuple[int, dict]]) -> dict:
    """Summarise one linked path ([(w_idx, blob)], blobs carrying ``chronic``)."""
    points = [blob["c"] for _, blob in path]
    steps = [math.dist(points[i], points[i + 1]) for i in range(len(points) - 1)]
    path_len = sum(steps)
    net = math.dist(points[0], points[-1])

    # step_ratio: largest single hop vs. the track's own median hop. A real
    # flight has roughly steady step sizes (ratio near 1); a mismatched
    # link between two unrelated blobs shows up as one implausible jump
    # among otherwise-small steps (see TrackLinker's accel cap above --
    # this catches what slips past it, e.g. a fresh track's unconstrained
    # first hop). Capped rather than left as inf so it stays valid JSON.
    step_med = median(steps) if steps else 0.0
    if step_med > 0:
        step_ratio = round(max(steps) / step_med, 2)
    else:
        step_ratio = 999.0 if steps and max(steps) > 0 else 1.0

    # footprint_ratio: the bounding box spanning every point's blob vs. the
    # mean size of a single point's blob. Real insects sweep into cells
    # their box never covered before, growing the footprint well past one
    # blob's own size; a blob that just changes shape/size in place (e.g.
    # foliage catching light differently) can still drag its *weighted*
    # centroid around -- looking straight and fast by straightness/chronic
    # alone -- without the box ever really leaving one spot.
    bboxes = [b["bbox"] for _, b in path]
    fx0 = min(b[0] for b in bboxes)
    fy0 = min(b[1] for b in bboxes)
    fx1 = max(b[0] + b[2] for b in bboxes)
    fy1 = max(b[1] + b[3] for b in bboxes)
    footprint_area = (fx1 - fx0) * (fy1 - fy0)
    mean_bbox_area = sum(b[2] * b[3] for b in bboxes) / len(bboxes)
    footprint_ratio = round(footprint_area / mean_bbox_area, 2) if mean_bbox_area > 0 else 1.0

    return {
        "w0": path[0][0],
        "n": len(path),
        "path": points,
        "net": round(net, 1),
        "len": round(path_len, 1),
        "straightness": round(net / path_len, 3) if path_len > 0 else 0.0,
        "mean_area": round(sum(b["area"] for _, b in path) / len(path), 1),
        "chronic": round(sum(b["chronic"] for _, b in path) / len(path), 3),
        "step_ratio": step_ratio,
        "footprint_ratio": footprint_ratio,
    }


def _link_tracks(windows: list[dict], max_dist: float, min_track_len: int = 3,
                 max_accel: float = 40.0) -> list[dict]:
    """Link every window's blobs into tracks (see TrackLinker) and summarise them."""
    linker = TrackLinker(max_dist, max_accel)
    done: list[dict] = []
    for w_idx, win in enumerate(windows):
        done.extend(linker.step(w_idx, win["blobs"]))
    done.extend(linker.finish())

    # A 2-point track is mathematically dead straight (net == path_len for
    # one segment) no matter what made it, so a single spurious one-hop link
    # between unrelated blobs would always pass a straightness filter.
    # Require a second linked hop to confirm it.
    tracks = [track_metrics(t["path"]) for t in done if len(t["path"]) >= min_track_len]
    tracks.sort(key=lambda t: t["w0"])
    return tracks


class MotionAccumulator:
    """Steps 1-3 of the analysis, one frame at a time.

    Differences each frame against the EMA background, accumulates the hot
    pixels over a window and, when the window fills, extracts its blobs.
    Shared by the batch ``analyse`` and the live viewer (``camrig.live``).
    """

    def __init__(self, width: int, height: int, threshold: int, window: int = 6,
                 min_hits: int = 2, cell: int = 8, bg_alpha: float = 0.05,
                 min_area: int = 4) -> None:
        self.threshold = threshold
        self.window = window
        self.min_hits = min_hits
        self.cell = cell
        self.bg_alpha = bg_alpha
        self.min_area = min_area
        self.bg: np.ndarray | None = None
        self.hits = np.zeros((height, width), dtype=np.uint8)
        self.frames_in_window = 0
        self.window_start = 0

    def push(self, frame: np.ndarray) -> tuple[float, dict | None]:
        """Feed one gray frame; return (active pixel fraction, completed window or None)."""
        frame = frame.astype(np.float32)
        if self.bg is None:
            # No background yet: emit zero so index i keeps matching .pts line i.
            self.bg = frame.copy()
            active = 0.0
        else:
            mask = np.abs(frame - self.bg) > self.threshold
            active = round(float(mask.mean()), 5)
            self.hits += mask
            self.bg += self.bg_alpha * (frame - self.bg)
        self.frames_in_window += 1
        if self.frames_in_window == self.window:
            return active, self.flush()
        return active, None

    def flush(self) -> dict | None:
        """Close the current (possibly partial) window; None if it holds no frames."""
        if not self.frames_in_window:
            return None
        blobs = _window_blobs(self.hits, self.min_hits, self.cell, self.min_area)
        win = {"f": self.window_start, "n_frames": self.frames_in_window, "blobs": blobs}
        self.hits.fill(0)
        self.window_start += self.frames_in_window
        self.frames_in_window = 0
        return win


def analyse(stream: BinaryIO, width: int, height: int, threshold: int,
            window: int = 6, min_hits: int = 2, cell: int = 8,
            bg_alpha: float = 0.05, min_area: int = 4,
            max_link_dist: float = 80.0, min_track_len: int = 3,
            max_accel: float = 40.0) -> dict:
    """Consume raw gray8 frames from stream; return blobs, tracks and metrics."""
    frame_bytes = width * height
    active_fraction: list[float] = []
    windows: list[dict] = []
    acc = MotionAccumulator(width, height, threshold, window=window, min_hits=min_hits,
                            cell=cell, bg_alpha=bg_alpha, min_area=min_area)
    ch, cw = height // cell, width // cell
    chronic_counts = np.zeros((ch, cw), dtype=np.uint32)

    def add_window(win: dict | None) -> None:
        if win is None:
            return
        for blob in win["blobs"]:
            for c in blob["_cells"]:
                chronic_counts[c] += 1
        windows.append(win)

    while True:
        data = stream.read(frame_bytes)
        if len(data) < frame_bytes:  # EOF (a truncated trailing frame is dropped)
            break
        frame = np.frombuffer(data, dtype=np.uint8).reshape(height, width)
        active, win = acc.push(frame)
        active_fraction.append(active)
        add_window(win)
    add_window(acc.flush())

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
        "analysis": ANALYSIS,
        "width": width,
        "height": height,
        "params": {
            "threshold": threshold, "window": window, "min_hits": min_hits,
            "cell": cell, "bg_alpha": bg_alpha, "min_area": min_area,
            "max_link_dist": max_link_dist, "min_track_len": min_track_len,
            "max_accel": max_accel,
        },
        "frame_count": len(active_fraction),
        "active_fraction": active_fraction,
        "windows": windows,
        "tracks": _link_tracks(windows, max_link_dist, min_track_len, max_accel),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
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
    parser.add_argument("--min-track-len", type=int, default=3,
                        help="min linked points (windows) for a track to be kept; "
                             "2-point tracks are always perfectly straight so this "
                             "is the floor for straightness to mean anything (default 3)")
    parser.add_argument("--max-accel", type=float, default=40.0,
                        help="max px/window a track's speed may increase hop-to-hop, "
                             "on top of its own last hop distance; stops a slow/still "
                             "track from teleporting to an unrelated blob within "
                             "max-link-dist (default 40)")
    parser.add_argument("--framerate", type=float, required=True,
                        help="clip's own capture frame rate (frames/sec); stored in the "
                             "sidecar so consumers convert frame/window indices to "
                             "wall-clock time using this clip's true rate, not whatever "
                             "config.toml currently says")
    parser.add_argument("--clip", help="source clip name to embed in the sidecar")
    parser.add_argument("-o", "--output", required=True, help="JSON sidecar path")
    args = parser.parse_args(argv)

    result = analyse(sys.stdin.buffer, args.width, args.height, args.threshold,
                     window=args.window, min_hits=args.min_hits, cell=args.cell,
                     bg_alpha=args.bg_alpha, min_area=args.min_area,
                     max_link_dist=args.max_link_dist, min_track_len=args.min_track_len,
                     max_accel=args.max_accel)
    result["framerate"] = args.framerate
    if args.clip:
        result = {"clip": args.clip, **result}
    Path(args.output).write_text(json.dumps(result), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
