"""Score the current ``[postprocess]`` discriminator thresholds against a
clip's hand-labelled ground truth (``camrig.labels``), instead of eyeballing
whether a threshold change actually helped.

    camrig label-score clip.mkv

Requires the clip's ``.motion.json`` (``camrig.postprocess``) and
``.labels.jsonl`` (``camrig.motion_view``'s click-to-label UI) sidecars.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from .config import Config
from .labels import load_labels
from .motion_debug import burst_track_ids, load_motion, passes_thresholds


@dataclass
class ScoreResult:
    insect_total: int = 0
    insect_kept: int = 0
    other_total: int = 0
    other_kept: int = 0
    misses: list[dict] = field(default_factory=list)          # insects filtered out
    false_positives: list[dict] = field(default_factory=list)  # other tracks still kept

    @property
    def recall(self) -> float | None:
        return self.insect_kept / self.insect_total if self.insect_total else None

    @property
    def false_positive_rate(self) -> float | None:
        return self.other_kept / self.other_total if self.other_total else None


def score(cfg: Config, video: Path) -> ScoreResult | None:
    """Score ``cfg.postprocess``'s thresholds against ``video``'s labels sidecar.

    Deduplicates labels by ``source_track`` (last one wins, matching
    ``camrig.labels``' relabelling rule) and ignores ``unsure`` tracks --
    scoring only makes sense against a confident insect/other call.
    """
    motion = load_motion(video)
    if motion is None:
        return None
    tracks = motion["tracks"]
    pp = cfg.postprocess

    candidate_ids = [ti for ti, t in enumerate(tracks) if passes_thresholds(t, pp)]
    candidate_id_set = set(candidate_ids)
    burst_ids = burst_track_ids(
        motion, tracks, candidate_ids, cfg.capture.framerate,
        pp.burst_window_seconds, pp.burst_min_tracks,
    )

    by_track: dict[int, dict] = {}
    for record in load_labels(video):
        by_track[record["source_track"]] = record

    result = ScoreResult()
    for source_track, record in by_track.items():
        label = record["label"]
        if label == "unsure" or source_track >= len(tracks):
            continue
        kept = source_track in candidate_id_set and source_track not in burst_ids
        entry = {"source_track": source_track, "t0": record["t0"]}
        if label == "insect":
            result.insect_total += 1
            if kept:
                result.insect_kept += 1
            else:
                result.misses.append(entry)
        elif label == "other":
            result.other_total += 1
            if kept:
                result.other_kept += 1
                result.false_positives.append(entry)
    return result
