"""Cut sections out of a captured clip, in place, to save disk and re-postprocessing time.

The capture profiles (see ``camrig.record``) are all intra-only -- every
frame is independently decodable -- so dropping a stretch of a clip is a
lossless stream copy: no frame is ever decoded or re-encoded, just kept or
discarded by frame index. ``ffmpeg``'s own ``-ss``/``-frames:v`` land exactly
on frame boundaries here (there's no GOP to round to, since every frame is a
keyframe), so the kept ranges are extracted and concatenated bit-for-bit.

Frame N of the container always lines up with line N of the clip's ``.pts``
sidecar (``camrig.postprocess``'s "index i aligns with .pts line i"), so the
same frame-index selection that trims the video also trims the sidecar --
they stay in lockstep without re-deriving anything from timestamps.

Trimming invalidates the clip's derived sidecars: ``.preview.mp4`` and
``.motion.json`` describe frame counts/timings that no longer exist, so
they're deleted here; run ``camrig postprocess --force`` afterwards to
rebuild them. Existing labels (``camrig.labels``) are a separate concern --
see ``camrig.labels.remap_labels`` -- because they're keyed by wall-clock
time within the clip, not by frame index, and need their own re-timing.

    camrig trim clip.mkv --cut 12.0-14.5 --cut 40-41.2
"""

from __future__ import annotations

import logging
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from .postprocess import motion_path, preview_path

log = logging.getLogger("camrig.trim")

PTS_HEADER = "# timecode format v2"


def invert_ranges(cuts: list[tuple[int, int]], total_frames: int) -> list[tuple[int, int]]:
    """Half-open ``[start, end)`` frame ranges to KEEP, given ranges to cut.

    ``cuts`` need not be sorted or non-overlapping -- overlapping/adjacent
    cuts are merged before inverting.
    """
    if total_frames <= 0:
        return []
    merged: list[tuple[int, int]] = []
    for start, end in sorted(cuts):
        start, end = max(0, start), min(total_frames, end)
        if start >= end:
            continue
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))

    keep: list[tuple[int, int]] = []
    pos = 0
    for start, end in merged:
        if start > pos:
            keep.append((pos, start))
        pos = end
    if pos < total_frames:
        keep.append((pos, total_frames))
    return keep


def build_frame_map(cuts: list[tuple[int, int]], total_frames: int) -> list[int | None]:
    """Old frame index -> new frame index, or ``None`` if that frame was cut."""
    mapping: list[int | None] = [None] * total_frames
    new_idx = 0
    for start, end in invert_ranges(cuts, total_frames):
        for old_idx in range(start, end):
            mapping[old_idx] = new_idx
            new_idx += 1
    return mapping


def extract_segment_cmd(video: Path, out_path: Path, start_frame: int, n_frames: int,
                        framerate: float) -> list[str]:
    """ffmpeg argv: lossless stream-copy of frames ``[start_frame, start_frame + n_frames)``.

    ``-ss`` before ``-i`` seeks to the exact frame (every frame is a keyframe
    in these profiles, so keyframe-accurate seeking *is* frame-accurate);
    ``-frames:v`` rather than ``-to`` pins the exact count so float rounding
    at the boundary can't drop or duplicate a frame.
    """
    return [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
        "-ss", f"{start_frame / framerate:.6f}",
        "-i", str(video),
        "-frames:v", str(n_frames),
        "-c", "copy", "-an",
        "-f", "matroska", str(out_path),
    ]


def concat_cmd(list_file: Path, out_path: Path) -> list[str]:
    return [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
        "-f", "concat", "-safe", "0", "-i", str(list_file),
        "-c", "copy", "-f", "matroska", str(out_path),
    ]


def count_frames_cmd(video: Path) -> list[str]:
    return [
        "ffprobe", "-v", "error", "-count_frames", "-select_streams", "v:0",
        "-show_entries", "stream=nb_read_frames",
        "-of", "default=nokey=1:noprint_wrappers=1", str(video),
    ]


def _run(cmd: list[str]) -> str:
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"command failed ({' '.join(cmd)}):\n{result.stderr}")
    return result.stdout


def read_pts(path: Path) -> tuple[str, list[str]]:
    lines = path.read_text(encoding="utf-8").splitlines()
    if not lines:
        return PTS_HEADER, []
    return lines[0], lines[1:]


def write_pts(path: Path, header: str, data: list[str]) -> None:
    body = header + "\n" + "".join(line + "\n" for line in data)
    path.write_text(body, encoding="utf-8")


@dataclass
class TrimResult:
    frames_before: int
    frames_after: int
    frame_map: list[int | None]  # old frame index -> new frame index, or None if cut


def describe_cuts(video: Path, framerate: float, cuts: list[tuple[int, int]]) -> list[list[str]]:
    """The ffmpeg/ffprobe commands trimming would run, for --dry-run/testing."""
    pts_path = video.with_suffix(".pts")
    _, data_lines = read_pts(pts_path) if pts_path.exists() else (PTS_HEADER, [])
    total = len(data_lines)
    keep = invert_ranges(cuts, total)
    return [extract_segment_cmd(video, Path(f"seg{i}.mkv"), s, e - s, framerate)
            for i, (s, e) in enumerate(keep)]


def apply_cuts(video: Path, framerate: float, cuts: list[tuple[int, int]]) -> TrimResult:
    """Cut ``cuts`` (frame-index ranges to remove) out of ``video`` in place.

    Rewrites ``video`` and its ``.pts`` sidecar to keep only the surviving
    frames, verifies each extracted segment's frame count against what was
    asked for (aborting, with the original files untouched, on a mismatch),
    then deletes the now-stale ``.preview.mp4``/``.motion.json`` sidecars.
    """
    if video.suffix != ".mkv":
        raise ValueError(f"trim only supports .mkv clips, not {video.suffix}")

    pts_path = video.with_suffix(".pts")
    if not pts_path.exists():
        raise ValueError(f"missing {pts_path.name}; can't count/align frames without it")
    header, data_lines = read_pts(pts_path)
    total = len(data_lines)
    if total == 0:
        raise ValueError(f"{pts_path.name} is empty")

    keep = invert_ranges(cuts, total)
    if not keep:
        raise ValueError("cuts would remove the entire clip")
    if keep == [(0, total)]:
        raise ValueError("cuts don't overlap the clip; nothing to do")

    mapping = build_frame_map(cuts, total)
    frames_after = sum(e - s for s, e in keep)

    video_part = Path(f"{video}.part")
    pts_part = Path(f"{pts_path}.part")
    # A tempdir on the same filesystem as the clip so the final segment/concat
    # output can be moved into place with a cheap atomic rename, not a copy.
    with tempfile.TemporaryDirectory(dir=video.parent) as tmp:
        tmp_dir = Path(tmp)
        segment_paths = []
        for i, (start, end) in enumerate(keep):
            seg_path = tmp_dir / f"seg{i}.mkv"
            _run(extract_segment_cmd(video, seg_path, start, end - start, framerate))
            got = int(_run(count_frames_cmd(seg_path)).strip() or 0)
            if got != end - start:
                raise RuntimeError(
                    f"segment {i} ({start}:{end}) extracted {got} frames, expected {end - start} "
                    f"-- aborting, {video.name} is untouched"
                )
            segment_paths.append(seg_path)

        if len(segment_paths) == 1:
            segment_paths[0].replace(video_part)
        else:
            list_file = tmp_dir / "concat.txt"
            list_file.write_text(
                "".join(f"file '{p}'\n" for p in segment_paths), encoding="utf-8"
            )
            _run(concat_cmd(list_file, video_part))

        new_data = [line for start, end in keep for line in data_lines[start:end]]
        write_pts(pts_part, header, new_data)

        # pts before video: a crash between renames leaves a clip whose pts
        # sidecar is ahead of it, which postprocess/motion-view would notice
        # (frame-count mismatch) rather than silently misaligning the two.
        pts_part.replace(pts_path)
        video_part.replace(video)

    for stale in (preview_path(video), motion_path(video)):
        stale.unlink(missing_ok=True)

    log.info("Trimmed %s: %d -> %d frames (%d cut)", video.name, total, frames_after,
             total - frames_after)
    return TrimResult(total, frames_after, mapping)
