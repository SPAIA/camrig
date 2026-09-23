"""Search the six ``[postprocess]`` filter thresholds against every labelled
clip found on disk, cheaply -- no MKV decoding, no re-running
``camrig.motion``. It only needs each clip's existing ``.motion.json`` and
the ground truth a human already wrote to ``.labels.jsonl`` via
``camrig.motion_view``'s click-to-label UI.

    camrig optimise-filters .
    camrig optimise-filters . --trials 5000 --seed 42 --min-recall 0.95

Discovers clips by their ``*.labels.jsonl`` sidecar and pairs each with its
``*.motion.json`` (skipping any without one); the ``.mkv`` itself is never
touched.

Why not Optuna: a 6-dimensional, sub-5-second-per-1000-trials search doesn't
need a TPE/Bayesian library and its dependency chain. A seeded uniform
random search that gets progressively more local around its own best finds
(below) is ~150 lines, has no new dependency, and is easy to read end to
end -- worth it here; it would not necessarily be worth it for a much
higher-dimensional or more expensive-to-evaluate search.

Objective: this is a CONSTRAINED search, not a scalar one --
``camrig.motion`` already means "labelled-insect recall matters more than
raw track count", so trials are compared first on whether they meet
``--min-recall``, and only *then* on how many non-insect tracks survive. See
``_objective_key`` for the exact tuple.
"""

from __future__ import annotations

import bisect
import json
import logging
import math
import random
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from .config import Config, set_config_value
from .filters import PARAM_BOUNDS, FilterThresholds
from .labels import LABELS_SUFFIX, load_labels
from .motion import SCHEMA as MOTION_SCHEMA
from .postprocess import motion_path
from .pts import FrameClock, load_frame_times
from .scoring import ScoreResult, score_tracks
from .stitch import precompute_candidates

log = logging.getLogger("camrig.optimise_filters")

# Every field score_tracks/passes_thresholds/burst_track_ids actually read
# off a track (see camrig.motion._link_tracks) -- the load-time check below,
# not camrig.motion_debug.load_motion's strict schema==MOTION_SCHEMA gate.
_REQUIRED_TRACK_FIELDS = {"w0", "n", "path", "straightness", "chronic",
                          "footprint_ratio", "step_ratio"}


def _load_motion_json(video: Path) -> dict | None:
    """Load a clip's motion.json for scoring, without
    ``camrig.motion_debug.load_motion``'s strict schema-equality gate.

    That gate exists for tools that decode/interpret the sidecar
    structurally (blob boxes, window pixel layout) and must refuse a
    version they don't know how to draw. This module only reads the six
    ``[postprocess]`` filter inputs plus window start-frame/track-path data,
    which has been stable since those fields were introduced -- so an old
    ``schema`` number here is a stale *label*, not necessarily missing data.
    Reject a file only if the fields this module actually needs aren't
    present, and say so either way.
    """
    motion_file = motion_path(video)
    if not motion_file.exists():
        return None
    motion = json.loads(motion_file.read_text(encoding="utf-8"))
    tracks = motion.get("tracks", [])
    if tracks and not _REQUIRED_TRACK_FIELDS.issubset(tracks[0]):
        log.warning(
            "Skipping %s: tracks are missing required field(s) %s (schema %s) -- "
            "regenerate with `camrig postprocess --force`",
            video.name, sorted(_REQUIRED_TRACK_FIELDS - tracks[0].keys()), motion.get("schema"),
        )
        return None
    if motion.get("schema") != MOTION_SCHEMA:
        log.info(
            "%s is schema %s (current: %s) but has every field this optimiser needs; using it as-is",
            video.name, motion.get("schema"), MOTION_SCHEMA,
        )
    return motion


def discover_clips(base: Path) -> list[Path]:
    """Video paths (the ``.mkv`` need not exist) for every ``*.labels.jsonl``
    under ``base`` that has a matching ``*.motion.json``.
    """
    videos = []
    for labels_file in sorted(base.rglob(f"*{LABELS_SUFFIX}")):
        stem = labels_file.name[: -len(LABELS_SUFFIX)]
        video = labels_file.with_name(stem + ".mkv")
        if not motion_path(video).exists():
            log.warning("Skipping %s: no matching %s", labels_file.name, motion_path(video).name)
            continue
        videos.append(video)
    return videos


@dataclass
class ClipDataset:
    video: Path
    motion: dict
    labels: list[dict]
    clock: FrameClock
    # Every plausible track-stitch candidate pair up to the widest
    # stitch_max_gap_seconds the search space allows (see
    # camrig.stitch.precompute_candidates), computed once here rather than
    # rescanned by every trial in the search loop.
    stitch_candidates: list[tuple] = field(default_factory=list)


def load_dataset(videos: list[Path], default_framerate: float) -> list[ClipDataset]:
    """Load every clip's motion.json/labels.jsonl into memory ONCE, up
    front -- the whole point of Phase 1 is that a trial never touches disk
    (or re-scans every track pair for stitching candidates).

    Each clip uses its own real per-frame ``.pts`` timestamps when that
    sidecar exists (see ``camrig.pts``) rather than one nominal rate applied
    to every clip -- clips captured under different ``[capture]`` framerates,
    or ones whose actual capture rate drifted from nominal under I/O load,
    can all coexist correctly in one dataset this way. A clip missing its
    ``.pts`` falls back to a constant rate: its OWN captured framerate
    (``motion["framerate"]``) if present, else ``default_framerate``
    (normally ``cfg.capture.framerate``) for a sidecar written before
    ``camrig.motion --framerate`` existed.
    """
    stitch_gap_bound = PARAM_BOUNDS["stitch_max_gap_seconds"][1]
    stitch_dist_bound = PARAM_BOUNDS["stitch_max_gap_distance"][1]
    datasets = []
    for video in videos:
        motion = _load_motion_json(video)
        if motion is None:
            continue
        pts_path = video.with_suffix(".pts")
        if pts_path.exists():
            clock = FrameClock.from_pts(load_frame_times(pts_path))
        else:
            clock = FrameClock.constant(motion.get("framerate", default_framerate))
        candidates = precompute_candidates(motion, clock, stitch_gap_bound, stitch_dist_bound)
        datasets.append(ClipDataset(video=video, motion=motion, labels=load_labels(video),
                                    clock=clock, stitch_candidates=candidates))
    return datasets


@dataclass
class DatasetScore(ScoreResult):
    """``ScoreResult`` totals aggregated across every clip in the dataset,
    plus each clip's own ``ScoreResult`` for a per-clip breakdown.
    ``misses``/``false_positives`` are inherited but left empty here --
    ``source_track`` is only meaningful within its own clip's motion.json,
    so merging them across clips would be misleading; see ``per_clip`` for
    the clip-scoped detail instead.
    """

    per_clip: dict[str, ScoreResult] = field(default_factory=dict)


def evaluate(datasets: list[ClipDataset], thresholds: FilterThresholds, *,
            detail: bool = False) -> DatasetScore:
    """Score one set of thresholds against the whole dataset. ``detail=False``
    (the search loop's default) skips the misses/false_positives debug lists
    -- cheap either way, but the search runs thousands of these.
    """
    agg = DatasetScore()
    for ds in datasets:
        r = score_tracks(ds.motion, ds.labels, thresholds, ds.clock, detail=detail,
                         stitch_candidates=ds.stitch_candidates)
        agg.insect_total += r.insect_total
        agg.insect_kept += r.insect_kept
        agg.other_total += r.other_total
        agg.other_kept += r.other_kept
        agg.unsure_total += r.unsure_total
        agg.surviving_total += r.surviving_total
        if r.duration_seconds:
            agg.duration_seconds = (agg.duration_seconds or 0.0) + r.duration_seconds
        if detail:
            agg.per_clip[ds.video.name] = r
    return agg


def _objective_key(score: ScoreResult, min_recall: float) -> tuple:
    """Constrained objective, smallest-is-best (for ``min``/sorting):

    1. Trials meeting the recall floor always beat trials that don't.
    2. Among trials meeting it: fewest non-insect survivors, then highest
       recall as a tie-break safety margin.
    3. Among trials that DON'T meet it (only reached if no trial in the
       whole search does): highest recall first -- closest to the target --
       then fewest non-insect survivors.

    A clip with no labelled insects at all makes ``recall`` ``None``;
    treated as vacuously meeting the floor (nothing to recall).
    """
    recall = score.recall if score.recall is not None else 1.0
    if recall >= min_recall:
        return (0, score.non_insect_surviving, -recall)
    return (1, -recall, score.non_insect_surviving)


def _random_thresholds(rng: random.Random) -> FilterThresholds:
    values: dict[str, float | int] = {}
    for name, (lo, hi, cast) in PARAM_BOUNDS.items():
        v = rng.uniform(lo, hi)
        values[name] = round(v) if cast is int else round(v, 3)
    return FilterThresholds(**values)


def _perturb(parent: FilterThresholds, rng: random.Random, *, scale: float = 0.15) -> FilterThresholds:
    """A local move around ``parent``: each parameter nudged by Gaussian
    noise scaled to that parameter's own range, clipped back into bounds.
    This is what gives the search its "spend more trials near known-good
    regions" (TPE-like) behaviour without an actual density model.
    """
    values: dict[str, float | int] = {}
    for name, (lo, hi, cast) in PARAM_BOUNDS.items():
        current = getattr(parent, name)
        v = current + rng.gauss(0.0, (hi - lo) * scale)
        v = min(max(v, lo), hi)
        values[name] = round(v) if cast is int else round(v, 3)
    return FilterThresholds(**values)


@dataclass
class SearchOutcome:
    trials: int
    seed: int
    min_recall: float
    baseline: tuple[FilterThresholds, DatasetScore]
    best: tuple[FilterThresholds, DatasetScore, int]  # (thresholds, score, trial_index)


def search(datasets: list[ClipDataset], *, trials: int, seed: int, min_recall: float,
          baseline: FilterThresholds, elite_size: int = 12,
          explore_fraction: float = 0.3) -> SearchOutcome:
    """Seeded random search, trial 0 is always ``baseline`` (the current
    config.toml thresholds), so the report can show exactly what the search
    improved on. The first ``explore_fraction`` of the remaining trials
    sample uniformly across the whole space (global exploration); the rest
    perturb a random pick from the running top-``elite_size`` trials
    (exploitation around whatever's working). Deterministic for a given
    ``seed`` -- same dataset, same seed, same result, always.
    """
    rng = random.Random(seed)
    n_explore = max(1, round((trials - 1) * explore_fraction))

    baseline_score = evaluate(datasets, baseline, detail=True)
    best_key = _objective_key(baseline_score, min_recall)
    best_thresholds, best_score, best_index = baseline, baseline_score, 0

    # Ascending-sorted (key, trial_index, thresholds); trial_index breaks
    # ties so two equal-key entries never need to compare FilterThresholds
    # objects directly (they don't define ordering).
    elites: list[tuple[tuple, int, FilterThresholds]] = [(best_key, 0, baseline)]

    for i in range(1, trials):
        if i <= n_explore:
            thresholds = _random_thresholds(rng)
        else:
            _, _, parent = elites[rng.randrange(len(elites))]
            thresholds = _perturb(parent, rng)

        trial_score = evaluate(datasets, thresholds, detail=False)
        key = _objective_key(trial_score, min_recall)

        if key < best_key:
            best_key, best_thresholds, best_score, best_index = key, thresholds, trial_score, i

        if len(elites) < elite_size:
            bisect.insort(elites, (key, i, thresholds))
        elif key < elites[-1][0]:
            bisect.insort(elites, (key, i, thresholds))
            elites.pop()

    # Recompute the winner once with detail=True for the report (misses/
    # false_positives/per_clip weren't built during the cheap search loop).
    best_score = evaluate(datasets, best_thresholds, detail=True)
    return SearchOutcome(trials=trials, seed=seed, min_recall=min_recall,
                         baseline=(baseline, baseline_score),
                         best=(best_thresholds, best_score, best_index))


def run(cfg: Config, base: Path, *, trials: int, seed: int, min_recall: float) -> SearchOutcome | None:
    videos = discover_clips(base)
    if not videos:
        log.error("No labelled clips (*.labels.jsonl + matching *.motion.json) found under %s", base)
        return None
    datasets = load_dataset(videos, cfg.capture.framerate)
    if not datasets:
        log.error("Found %d labelled clip(s) under %s but none had a usable motion.json",
                  len(videos), base)
        return None
    baseline = FilterThresholds.from_postprocess(cfg.postprocess)
    return search(datasets, trials=trials, seed=seed, min_recall=min_recall, baseline=baseline)


def apply_best(outcome: SearchOutcome, config_path: Path) -> None:
    """Persist the best trial's thresholds into ``config_path``'s
    ``[postprocess]`` section (same mechanism ``camrig.motion_view`` uses).
    """
    best_thresholds, _, _ = outcome.best
    for name, value in best_thresholds.as_dict().items():
        cast = PARAM_BOUNDS[name][2]
        text = str(value) if cast is int else f"{value:.3f}"
        set_config_value(config_path, "postprocess", name, text)


def _score_to_dict(r: ScoreResult) -> dict:
    return {
        "insect_total": r.insect_total,
        "insect_kept": r.insect_kept,
        "recall": r.recall,
        "other_total": r.other_total,
        "other_kept": r.other_kept,
        "false_positive_rate": r.false_positive_rate,
        "unsure_total": r.unsure_total,
        "surviving_total": r.surviving_total,
        "non_insect_surviving": r.non_insect_surviving,
        "background_candidate_rate": r.background_candidate_rate,
        "duration_seconds": r.duration_seconds,
        "surviving_per_minute": r.surviving_per_minute,
    }


def outcome_to_dict(outcome: SearchOutcome) -> dict:
    """JSON-serializable record of one ``search()`` run -- thresholds and
    scores for both the baseline and the winner, plus the winner's per-clip
    breakdown. Nothing about ``optimise-filters`` persists this on its own
    (a run's report only ever went to stdout); the CLI's ``--save`` writes
    this out so a run can be compared against a later one instead of living
    only in scrollback.
    """
    baseline_th, baseline_score = outcome.baseline
    best_th, best_score, best_index = outcome.best
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "trials": outcome.trials,
        "seed": outcome.seed,
        "min_recall": outcome.min_recall,
        "clips": sorted(baseline_score.per_clip),
        "baseline": {
            "thresholds": baseline_th.as_dict(),
            "score": _score_to_dict(baseline_score),
        },
        "best": {
            "trial_index": best_index,
            "thresholds": best_th.as_dict(),
            "score": _score_to_dict(best_score),
            "per_clip": {name: _score_to_dict(r) for name, r in best_score.per_clip.items()},
        },
    }


def _fmt_pct(x: float | None) -> str:
    return f"{x:.1%}" if x is not None else "n/a"


def _fmt_rate(x: float | None) -> str:
    return f"{x:.1f}" if x is not None else "n/a"


def _format_thresholds(t: FilterThresholds) -> list[str]:
    return [
        f"  min_straightness:              {t.min_straightness:.3f}",
        f"  max_chronic:                   {t.max_chronic:.3f}",
        f"  min_footprint_ratio:           {t.min_footprint_ratio:.2f}",
        f"  max_step_ratio:                {t.max_step_ratio:.1f}",
        f"  min_duration_seconds:          {t.min_duration_seconds:.2f}",
        f"  burst_window_seconds:          {t.burst_window_seconds:.2f}",
        f"  burst_min_tracks:              {t.burst_min_tracks}",
        f"  burst_max_direction_deviation: {t.burst_max_direction_deviation:.3f} rad"
        f" ({math.degrees(t.burst_max_direction_deviation):.0f}°)",
        f"  stitch_max_gap_seconds:        {t.stitch_max_gap_seconds:.2f}",
        f"  stitch_max_gap_distance:       {t.stitch_max_gap_distance:.3f}",
    ]


def _format_stats(r: ScoreResult) -> list[str]:
    # non_insect_surviving is every surviving track that isn't labelled insect
    # (confirmed labelled-other + never-labelled survivors). Reported here as
    # "false positives" under the explicit assumption that an unlabelled
    # survivor is not an insect -- true whenever labelling was exhaustive,
    # optimistic otherwise (see camrig.scoring.ScoreResult.non_insect_surviving
    # for the fully-hedged version of this number).
    survive_suffix = f"  ({_fmt_rate(r.surviving_per_minute)}/min)" if r.surviving_per_minute is not None else ""
    return [
        f"  insects retained: {r.insect_kept}/{r.insect_total}  ({_fmt_pct(r.recall)} recall)",
        f"  false positives:  {r.non_insect_surviving}  (surviving, not labelled insect; "
        "assumes unlabelled survivors are non-insect)",
        f"  surviving tracks: {r.surviving_total}{survive_suffix}",
    ]


def format_report(outcome: SearchOutcome) -> str:
    baseline_th, baseline_score = outcome.baseline
    best_th, best_score, best_index = outcome.best

    lines = [
        "Dataset", "-------",
        f"clips:            {len(baseline_score.per_clip)}",
        f"labelled insects: {baseline_score.insect_total}",
        f"labelled other:   {baseline_score.other_total}",
        f"unsure:           {baseline_score.unsure_total} (ignored for optimisation)",
        "",
        "Baseline (current config.toml)",
        "-------------------------------",
        *_format_thresholds(baseline_th),
        *_format_stats(baseline_score),
        "",
        f"Best (trial {best_index}/{outcome.trials}, seed={outcome.seed}, "
        f"target recall >= {outcome.min_recall:.0%})",
        "-" * 60,
        *_format_thresholds(best_th),
        *_format_stats(best_score),
    ]
    if best_score.recall is None or best_score.recall < outcome.min_recall:
        lines.append(f"  WARNING: no trial reached {outcome.min_recall:.0%} recall; showing the "
                     "closest (highest-recall) trial found instead.")
    lines += ["", "Per-clip (best trial)", "----------------------"]
    for name in sorted(best_score.per_clip):
        r = best_score.per_clip[name]
        survive_suffix = f" ({_fmt_rate(r.surviving_per_minute)}/min)" if r.surviving_per_minute is not None else ""
        lines.append(
            f"{name}: insects {r.insect_kept}/{r.insect_total} ({_fmt_pct(r.recall)})  "
            f"false positives {r.non_insect_surviving}  surviving {r.surviving_total}{survive_suffix}"
        )
    return "\n".join(lines)
