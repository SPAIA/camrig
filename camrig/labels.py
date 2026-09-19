"""Human-labelled motion tracks: ground truth for scoring the insect-vs-plant
discriminators (straightness/chronic, see ``camrig.motion``) instead of eyeballing
threshold changes.

Written by ``camrig.motion_view``'s click-to-label UI. One JSON object per line
(JSON Lines) so labelling a long clip only ever appends, never rewrites the
file. Coordinates are normalised (``x = pixel_x / frame_width``) and times are
wall-clock seconds into the clip rather than window indices, so a label stays
meaningful even if the motion analysis is later re-run at a different
resolution or window size -- only ``source_track``/``source_analysis`` are
tied to the specific analysis run that produced them.

    clip.mkv -> clip.labels.jsonl

Each record::

    {
      "label": "insect",           # one of LABELS
      "source_track": 42,          # index into that run's motion.json tracks[]
      "source_analysis": "blob-track-v1",
      "t0": 12.4, "t1": 13.1,
      "path": [[0.183, 0.472, 12.4], [0.201, 0.461, 12.5], ...]  # [x, y, t]
    }

A track may be labelled more than once (relabelling appends rather than
edits); consumers should treat the last record for a given ``source_track``
as authoritative.
"""

from __future__ import annotations

import json
from pathlib import Path

LABELS_SUFFIX = ".labels.jsonl"
LABELS = ("insect", "other", "unsure")


def labels_path(video: Path) -> Path:
    return video.with_suffix(LABELS_SUFFIX)


def append_label(video: Path, record: dict) -> None:
    with labels_path(video).open("a", encoding="utf-8") as f:
        f.write(json.dumps(record) + "\n")


def load_labels(video: Path) -> list[dict]:
    path = labels_path(video)
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def rewrite_labels(video: Path, records: list[dict]) -> None:
    """Overwrite the sidecar with exactly ``records`` (unlike append_label).

    Only for whole-file migrations like ``remap_labels`` -- normal labelling
    always appends, so relabelling a track is never lost.
    """
    path = labels_path(video)
    path.write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")


def remap_labels(video: Path, framerate: float,
                 frame_map: list[int | None]) -> tuple[list[dict], list[dict]]:
    """Re-time every label after ``camrig.trim.apply_cuts`` shortens the clip.

    Labels are keyed by wall-clock seconds into the clip, not frame index, so
    a cut earlier in the clip leaves their ``t``/``t0``/``t1`` pointing at the
    wrong moment (or, for a track inside the cut itself, at footage that no
    longer exists). Every path point is re-derived from ``frame_map`` -- a
    label survives only if *all* of its points map to a kept frame; otherwise
    it's dropped, since a track truncated by a cut isn't the ground truth it
    was labelled as.

    Returns ``(kept, dropped)``, both re-timed/original records, so the
    caller can report what was lost. Rewrites the sidecar with ``kept``.
    """
    kept, dropped = [], []
    for record in load_labels(video):
        new_path = []
        survives = True
        for x, y, t in record["path"]:
            old_idx = round(t * framerate)
            new_idx = frame_map[old_idx] if 0 <= old_idx < len(frame_map) else None
            if new_idx is None:
                survives = False
                break
            new_path.append([x, y, round(new_idx / framerate, 3)])
        if survives:
            kept.append({**record, "path": new_path, "t0": new_path[0][2], "t1": new_path[-1][2]})
        else:
            dropped.append(record)
    rewrite_labels(video, kept)
    return kept, dropped
