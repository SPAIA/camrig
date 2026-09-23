"""Fast, same-run scoring: check ``[postprocess]`` filter thresholds against
a clip's hand-labelled ground truth (``camrig.labels``), instead of
eyeballing whether a threshold change actually helped.

    camrig label-score clip.mkv

Requires the clip's ``.motion.json`` (``camrig.postprocess``) and
``.labels.jsonl`` (``camrig.motion_view``'s click-to-label UI) sidecars.

This module matches ground truth to filter survivors by ``source_track`` --
the label's index into *that run's* ``motion.json["tracks"]``. That is only
valid when scoring against the SAME motion.json the labels were made
against: a re-run of ``camrig.motion`` with different parameters (threshold,
window, min_hits, cell size, background alpha, min area, link distance,
track length, acceleration limit, motion resolution) can renumber, split, or
drop tracks entirely, so track 42 in a new run is not necessarily the same
physical track as track 42 in the old one.

* Phase 1 (``camrig.optimise_filters``, searching the six ``[postprocess]``
  thresholds against existing ``.motion.json`` sidecars) stays on this fast
  path -- it never regenerates ``motion.json``, so ``source_track`` stays
  valid throughout the search.
* Phase 2 (future work: searching ``camrig.motion``'s own extraction
  parameters, which *does* regenerate tracks) must instead match labels to
  generated tracks by their actual normalized path -- see
  ``camrig.trajectory_match``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from .config import Config
from .filters import FilterThresholds
from .labels import load_labels
from .motion_debug import burst_track_ids, load_motion, passes_thresholds
from .pts import FrameClock, load_frame_times
from .stitch import StitchResult, stitch_motion


@dataclass
class ScoreResult:
    insect_total: int = 0
    insect_kept: int = 0
    other_total: int = 0
    other_kept: int = 0
    unsure_total: int = 0
    # Every track surviving the filters, labelled or not -- not just the
    # labelled subset below.
    surviving_total: int = 0
    duration_seconds: float | None = None
    misses: list[dict] = field(default_factory=list)          # insects filtered out
    false_positives: list[dict] = field(default_factory=list)  # labelled-other tracks still kept

    @property
    def recall(self) -> float | None:
        return self.insect_kept / self.insect_total if self.insect_total else None

    @property
    def false_positive_rate(self) -> float | None:
        return self.other_kept / self.other_total if self.other_total else None

    @property
    def non_insect_surviving(self) -> int:
        """Surviving tracks not labelled insect: labelled other, labelled
        unsure, or never labelled at all.

        This is deliberately NOT called "false positives": a human labeller
        may not have caught every insect in the clip, so an unlabelled
        survivor is a background *candidate*, not a proven false detection.
        """
        return self.surviving_total - self.insect_kept

    @property
    def background_candidate_rate(self) -> float | None:
        return self.non_insect_surviving / self.surviving_total if self.surviving_total else None

    @property
    def surviving_per_minute(self) -> float | None:
        if not self.duration_seconds:
            return None
        return self.surviving_total / (self.duration_seconds / 60)


def _resolve_group_label(labels_in_group: set[str]) -> str:
    """When camrig.stitch merges raw tracks that carry different labels
    (e.g. a human labelled one fragment "insect" and never labelled its
    sibling, or -- the common case that motivated stitching -- labelled TWO
    fragments of the same physical insect "insect" separately), the merged
    group needs exactly one ground-truth label. Priority: insect > other >
    unsure. If ANY fragment of a stitched trajectory was ever confidently
    called an insect, the whole event counts as one -- consistent with
    recall mattering more than raw track count (see the optimiser's
    objective). With stitching disabled every group has exactly one member,
    so this is never ambiguous.
    """
    if "insect" in labels_in_group:
        return "insect"
    if "other" in labels_in_group:
        return "other"
    return "unsure"


def _compute_survival(motion: dict, thresholds: FilterThresholds, clock: FrameClock, *,
                      stitch_candidates: list[tuple] | None = None,
                      ) -> tuple[StitchResult, set[int], set[int]]:
    """Shared by ``score_tracks`` and ``survivor_ids``: stitch, then apply
    the scalar thresholds, then the burst filter. Returns ``(stitched,
    candidate_group_ids, burst_excluded_group_ids)`` -- GROUP ids (indices
    into ``stitched.tracks``), not raw track indices; ``candidate_group_ids
    - burst_excluded_group_ids`` is the final surviving set.
    """
    stitched = stitch_motion(motion, clock,
                             max_gap_seconds=thresholds.stitch_max_gap_seconds,
                             max_gap_distance=thresholds.stitch_max_gap_distance,
                             candidates=stitch_candidates)
    tracks = stitched.tracks
    stitched_motion = {**motion, "tracks": tracks}

    candidate_ids = [ti for ti, t in enumerate(tracks) if passes_thresholds(t, thresholds)]
    candidate_id_set = set(candidate_ids)
    burst_ids = burst_track_ids(
        stitched_motion, tracks, candidate_ids, clock,
        thresholds.burst_window_seconds, thresholds.burst_min_tracks,
        max_direction_deviation=thresholds.burst_max_direction_deviation,
    )
    return stitched, candidate_id_set, burst_ids


def survivor_ids(motion: dict, thresholds: FilterThresholds, clock: FrameClock) -> tuple[set[int], set[int]]:
    """Every RAW ``motion["tracks"]`` index currently kept by the full
    filter pipeline (stitching, then the scalar thresholds, then the
    directional-burst filter) -- the same computation
    ``camrig.optimise_filters``/``score_tracks`` use, but reported as raw
    track indices (a fragment's own index, even if it only survives because
    it got stitched into a longer group) instead of merged-group or
    label-matched counts.

    For ``camrig.motion_view``'s live viewer, so it highlights what the
    real pipeline keeps instead of a simplified client-side reimplementation
    of just the four original scalar thresholds (which has no notion of
    stitching, duration, or directional burst exclusion).

    Returns ``(passing, burst_excluded)``: ``passing`` is every raw track
    whose group cleared the scalar thresholds (straightness/chronic/
    footprint_ratio/step_ratio/duration) -- burst-excluded tracks are
    still included here, matching ``camrig.motion_view``'s "drawn muted,
    not hidden" convention for a burst-caught track someone might want to
    rescue. ``burst_excluded`` is the subset of ``passing`` that the burst
    filter then dropped. A raw track not in ``passing`` at all failed the
    scalar thresholds outright.
    """
    stitched, candidate_group_ids, burst_group_ids = _compute_survival(motion, thresholds, clock)
    passing = {raw_ti for raw_ti, group_id in stitched.member_to_group.items()
              if group_id in candidate_group_ids}
    burst_excluded = {raw_ti for raw_ti, group_id in stitched.member_to_group.items()
                      if group_id in burst_group_ids}
    return passing, burst_excluded


def score_tracks(motion: dict, labels: list[dict], thresholds: FilterThresholds,
                 clock: FrameClock, *, detail: bool = True,
                 stitch_candidates: list[tuple] | None = None) -> ScoreResult:
    """Score ``thresholds`` against already-loaded ``motion``/``labels`` for
    ONE clip. Pure in-memory, no I/O -- cheap enough to call thousands of
    times per clip (``camrig.optimise_filters``' search loop).

    First stitches ``motion["tracks"]`` per ``thresholds.stitch_max_gap_*``
    (see ``camrig.stitch``; a no-op when those are at their off defaults),
    then filters/scores the merged tracks. A label's ``source_track`` maps
    to whichever merged group it landed in; multiple labels landing in the
    same group (see ``_resolve_group_label``) count as ONE ground-truth
    instance, not one per label -- otherwise a stitched insect that was
    labelled twice as separate fragments would silently need to be "caught"
    twice for full recall.

    ``unsure`` tracks are counted but excluded from recall/false-positive
    accounting. Set ``detail=False`` to skip building the ``misses``/
    ``false_positives`` debug lists, saving a little work in a tight loop
    that only needs the summary counts. Pass ``stitch_candidates`` (from
    ``camrig.stitch.precompute_candidates``, computed once per clip by
    ``camrig.optimise_filters.load_dataset``) to avoid recomputing
    track-pair candidates on every call in a search loop.
    """
    stitched, candidate_id_set, burst_ids = _compute_survival(
        motion, thresholds, clock, stitch_candidates=stitch_candidates)
    surviving_ids = candidate_id_set - burst_ids

    by_raw_track: dict[int, dict] = {}
    for record in labels:
        by_raw_track[record["source_track"]] = record  # last one wins (relabelling)

    group_labels: dict[int, set[str]] = {}
    group_example: dict[int, dict] = {}
    n_raw_tracks = len(motion["tracks"])
    for raw_ti, record in by_raw_track.items():
        if raw_ti >= n_raw_tracks:
            continue
        group_id = stitched.member_to_group.get(raw_ti)
        if group_id is None:
            continue
        group_labels.setdefault(group_id, set()).add(record["label"])
        group_example.setdefault(group_id, record)

    result = ScoreResult(surviving_total=len(surviving_ids))
    frame_count = motion.get("frame_count")
    if frame_count:
        result.duration_seconds = clock.time(frame_count) - clock.time(0)

    for group_id, labels_in_group in group_labels.items():
        label = _resolve_group_label(labels_in_group)
        kept = group_id in surviving_ids
        record = group_example[group_id]
        if label == "insect":
            result.insect_total += 1
            if kept:
                result.insect_kept += 1
            elif detail:
                result.misses.append({"source_track": record["source_track"], "t0": record["t0"]})
        elif label == "other":
            result.other_total += 1
            if kept:
                result.other_kept += 1
                if detail:
                    result.false_positives.append({"source_track": record["source_track"], "t0": record["t0"]})
        elif label == "unsure":
            result.unsure_total += 1
    return result


def score(cfg: Config, video: Path) -> ScoreResult | None:
    """Load ``video``'s sidecars and score ``cfg.postprocess``'s current
    thresholds against them. Thin wrapper around ``score_tracks`` for
    one-off use (``camrig label-score``); ``camrig.optimise_filters`` loads
    each clip's motion/labels once and calls ``score_tracks`` directly many
    times instead of re-reading files per trial.

    Uses this clip's own real per-frame ``.pts`` timestamps when the sidecar
    exists (see ``camrig.pts``), falling back to a constant nominal rate --
    this clip's OWN captured framerate (``motion["framerate"]``) if present,
    else ``cfg.capture.framerate`` -- for a clip missing its ``.pts`` or
    written before ``camrig.motion --framerate`` existed.
    """
    motion = load_motion(video)
    if motion is None:
        return None
    labels = load_labels(video)
    thresholds = FilterThresholds.from_postprocess(cfg.postprocess)
    pts_path = video.with_suffix(".pts")
    if pts_path.exists():
        clock = FrameClock.from_pts(load_frame_times(pts_path))
    else:
        clock = FrameClock.constant(motion.get("framerate", cfg.capture.framerate))
    return score_tracks(motion, labels, thresholds, clock)
