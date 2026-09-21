"""Tests for camrig.optimise_filters: dataset discovery, aggregation, the
constrained objective, and end-to-end search determinism.
"""

import json
from pathlib import Path

from camrig import optimise_filters as of
from camrig.filters import FilterThresholds
from camrig.labels import append_label
from camrig.motion import SCHEMA
from camrig.scoring import ScoreResult


def _track(w0, straightness=0.9, chronic=0.01, footprint_ratio=10.0, step_ratio=1.0, mean_area=8.0):
    return {"w0": w0, "n": 3, "path": [[0, 0], [1, 0], [2, 0]],
            "straightness": straightness, "chronic": chronic,
            "footprint_ratio": footprint_ratio, "step_ratio": step_ratio, "mean_area": mean_area}


def _write_clip(video: Path, tracks: list[dict], labels: list[tuple[str, int]]) -> None:
    motion = {
        "schema": SCHEMA, "analysis": "blob-track-v1", "width": 100, "height": 100,
        "params": {"window": 6}, "frame_count": 600,
        "windows": [{"f": i * 6, "n_frames": 6, "blobs": []} for i in range(100)],
        "tracks": tracks,
    }
    video.with_suffix(".motion.json").write_text(json.dumps(motion), encoding="utf-8")
    for label, ti in labels:
        append_label(video, {"label": label, "source_track": ti, "source_analysis": "x",
                             "t0": 0.0, "t1": 0.1, "path": []})


def test_discover_clips_pairs_labels_with_motion_and_skips_orphans(tmp_path):
    a = tmp_path / "clip_a.mkv"
    _write_clip(a, [_track(w0=0)], [("insect", 0)])
    # An orphan labels.jsonl with no matching motion.json should be skipped.
    (tmp_path / "clip_b.labels.jsonl").write_text(
        json.dumps({"label": "insect", "source_track": 0, "source_analysis": "x",
                    "t0": 0.0, "t1": 0.1, "path": []}) + "\n",
        encoding="utf-8",
    )

    found = of.discover_clips(tmp_path)
    assert found == [a]


def test_load_dataset_reads_motion_and_labels(tmp_path):
    video = tmp_path / "clip.mkv"
    _write_clip(video, [_track(w0=0)], [("insect", 0)])

    datasets = of.load_dataset([video], default_framerate=60.0)
    assert len(datasets) == 1
    ds = datasets[0]
    assert ds.motion["tracks"] == [_track(w0=0)]
    assert len(ds.labels) == 1
    assert ds.framerate == 60.0


def test_load_dataset_uses_each_clips_own_framerate_over_the_default(tmp_path):
    # clip_a was captured (and sidecar-tagged) at 120fps; clip_b's sidecar
    # predates camrig.motion --framerate, so it falls back to the caller's
    # default_framerate. A dataset can mix both without one clobbering the
    # other -- that's the whole point of storing framerate per clip.
    a = tmp_path / "clip_a.mkv"
    _write_clip(a, [_track(w0=0)], [("insect", 0)])
    motion_a = json.loads(a.with_suffix(".motion.json").read_text())
    motion_a["framerate"] = 120.0
    a.with_suffix(".motion.json").write_text(json.dumps(motion_a), encoding="utf-8")

    b = tmp_path / "clip_b.mkv"
    _write_clip(b, [_track(w0=0)], [("insect", 0)])  # no "framerate" key -> uses the default

    datasets = {ds.video.name: ds for ds in of.load_dataset([a, b], default_framerate=60.0)}
    assert datasets["clip_a.mkv"].framerate == 120.0
    assert datasets["clip_b.mkv"].framerate == 60.0


def test_evaluate_aggregates_across_clips(tmp_path):
    a = tmp_path / "clip_a.mkv"
    b = tmp_path / "clip_b.mkv"
    _write_clip(a, [_track(w0=0), _track(w0=10, straightness=0.1)],
               [("insect", 0), ("other", 1)])
    _write_clip(b, [_track(w0=0)], [("insect", 0)])
    datasets = of.load_dataset([a, b], default_framerate=60.0)

    thresholds = FilterThresholds(min_straightness=0.5)
    agg = of.evaluate(datasets, thresholds, detail=True)

    assert agg.insect_total == 2
    assert agg.insect_kept == 2       # both straightness=0.9 insects pass
    assert agg.other_total == 1
    assert agg.other_kept == 0        # straightness=0.1 other filtered out
    assert set(agg.per_clip) == {"clip_a.mkv", "clip_b.mkv"}


def test_objective_key_prefers_meeting_recall_floor_over_fewer_survivors():
    meets_floor = ScoreResult(insect_total=10, insect_kept=10, surviving_total=50)   # recall 1.0, 40 non-insect
    misses_floor = ScoreResult(insect_total=10, insect_kept=8, surviving_total=8)     # recall 0.8, 0 non-insect

    key_meets = of._objective_key(meets_floor, min_recall=0.95)
    key_misses = of._objective_key(misses_floor, min_recall=0.95)
    assert key_meets < key_misses, "meeting the recall floor must always win, even with more survivors"


def test_objective_key_minimises_non_insect_survivors_once_floor_is_met():
    fewer_survivors = ScoreResult(insect_total=10, insect_kept=10, surviving_total=15)
    more_survivors = ScoreResult(insect_total=10, insect_kept=10, surviving_total=30)
    assert of._objective_key(fewer_survivors, 0.95) < of._objective_key(more_survivors, 0.95)


def test_objective_key_treats_no_labelled_insects_as_vacuously_meeting_floor():
    no_insects = ScoreResult(insect_total=0, insect_kept=0, surviving_total=3)
    key = of._objective_key(no_insects, min_recall=0.95)
    assert key[0] == 0  # tier 0: floor considered met


def test_search_trial_zero_is_always_the_given_baseline(tmp_path):
    video = tmp_path / "clip.mkv"
    _write_clip(video, [_track(w0=0), _track(w0=10, straightness=0.1)],
               [("insect", 0), ("other", 1)])
    datasets = of.load_dataset([video], default_framerate=60.0)
    baseline = FilterThresholds(min_straightness=0.3)

    outcome = of.search(datasets, trials=5, seed=1, min_recall=0.95, baseline=baseline)
    assert outcome.baseline[0] == baseline


def test_search_is_deterministic_for_a_given_seed(tmp_path):
    video = tmp_path / "clip.mkv"
    tracks = [_track(w0=i, straightness=0.5 + 0.01 * i, chronic=0.5 - 0.005 * i) for i in range(30)]
    labels = [("insect" if i % 3 == 0 else "other", i) for i in range(30)]
    _write_clip(video, tracks, labels)
    datasets = of.load_dataset([video], default_framerate=60.0)
    baseline = FilterThresholds()

    outcome1 = of.search(datasets, trials=40, seed=7, min_recall=0.9, baseline=baseline)
    outcome2 = of.search(datasets, trials=40, seed=7, min_recall=0.9, baseline=baseline)
    assert outcome1.best[0] == outcome2.best[0]
    assert outcome1.best[2] == outcome2.best[2]


def test_search_never_does_worse_than_baseline_on_the_objective(tmp_path):
    video = tmp_path / "clip.mkv"
    tracks = [_track(w0=i, straightness=0.5 + 0.01 * i, chronic=0.5 - 0.005 * i) for i in range(30)]
    labels = [("insect" if i % 3 == 0 else "other", i) for i in range(30)]
    _write_clip(video, tracks, labels)
    datasets = of.load_dataset([video], default_framerate=60.0)
    baseline = FilterThresholds()

    outcome = of.search(datasets, trials=60, seed=3, min_recall=0.9, baseline=baseline)
    baseline_key = of._objective_key(outcome.baseline[1], 0.9)
    best_key = of._objective_key(outcome.best[1], 0.9)
    assert best_key <= baseline_key


def test_apply_best_writes_thresholds_to_config(tmp_path):
    video = tmp_path / "clip.mkv"
    _write_clip(video, [_track(w0=0)], [("insect", 0)])
    datasets = of.load_dataset([video], default_framerate=60.0)
    baseline = FilterThresholds()
    outcome = of.search(datasets, trials=3, seed=1, min_recall=0.95, baseline=baseline)

    config_path = tmp_path / "config.toml"
    of.apply_best(outcome, config_path)

    text = config_path.read_text(encoding="utf-8")
    best_thresholds = outcome.best[0]
    assert f"min_straightness = {best_thresholds.min_straightness:.3f}" in text
    assert f"burst_min_tracks = {best_thresholds.burst_min_tracks}" in text


def test_format_report_includes_dataset_and_best_sections(tmp_path):
    video = tmp_path / "clip.mkv"
    _write_clip(video, [_track(w0=0), _track(w0=10, straightness=0.1)],
               [("insect", 0), ("other", 1)])
    datasets = of.load_dataset([video], default_framerate=60.0)
    outcome = of.search(datasets, trials=5, seed=1, min_recall=0.95, baseline=FilterThresholds())

    report = of.format_report(outcome)
    assert "Dataset" in report
    assert "labelled insects: 1" in report
    assert "labelled other:   1" in report
    assert "Baseline" in report
    assert "Best (trial" in report
    assert "clip.mkv:" in report
