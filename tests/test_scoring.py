"""Tests for camrig.scoring: threshold precision/recall against camrig.labels."""

import json
from pathlib import Path

from camrig import scoring
from camrig.config import CaptureConfig, Config, PostprocessConfig
from camrig.filters import FilterThresholds
from camrig.labels import append_label
from camrig.motion import SCHEMA


def _track(w0, straightness=0.9, chronic=0.01, footprint_ratio=10.0, step_ratio=1.0, n=3):
    return {"w0": w0, "n": n, "path": [[i, 0] for i in range(n)],
            "straightness": straightness, "chronic": chronic,
            "footprint_ratio": footprint_ratio, "step_ratio": step_ratio}


def _write_motion(video: Path, tracks: list[dict]) -> None:
    motion = {
        "schema": SCHEMA, "analysis": "blob-track-v1", "width": 100, "height": 100,
        "params": {"window": 6}, "frame_count": 420,
        "windows": [{"f": i * 6, "n_frames": 6, "blobs": []} for i in range(70)],
        "tracks": tracks,
    }
    video.with_suffix(".motion.json").write_text(json.dumps(motion), encoding="utf-8")


def _cfg() -> Config:
    cfg = Config()
    cfg.capture = CaptureConfig(framerate=60.0)
    cfg.postprocess = PostprocessConfig(
        min_straightness=0.5, max_chronic=0.1,
        min_footprint_ratio=2.0, max_step_ratio=5.0,
        burst_window_seconds=1.0, burst_min_tracks=3,
    )
    return cfg


def test_score_separates_recall_from_burst_and_threshold_false_positives(tmp_path):
    video = tmp_path / "clip.mkv"
    tracks = [
        _track(w0=0),                                   # 0: insect, isolated, kept
        _track(w0=30), _track(w0=31), _track(w0=32),     # 1,2,3: other, dense cluster -> burst
        _track(w0=55),                                   # 4: other, isolated -> still a false positive
        _track(w0=33, straightness=0.1),                 # 5: other, fails straightness outright
    ]
    _write_motion(video, tracks)

    append_label(video, {"label": "insect", "source_track": 0, "source_analysis": "x",
                         "t0": 0.0, "t1": 0.1, "path": []})
    for ti in (1, 2, 3, 4, 5):
        append_label(video, {"label": "other", "source_track": ti, "source_analysis": "x",
                             "t0": 0.0, "t1": 0.1, "path": []})

    result = scoring.score(_cfg(), video)

    assert result.insect_total == 1
    assert result.insect_kept == 1
    assert result.recall == 1.0

    assert result.other_total == 5
    assert result.other_kept == 1, "only the isolated non-burst false positive should survive"
    assert [fp["source_track"] for fp in result.false_positives] == [4]


def test_score_relabelling_keeps_only_the_last_record(tmp_path):
    video = tmp_path / "clip.mkv"
    _write_motion(video, [_track(w0=0)])
    append_label(video, {"label": "other", "source_track": 0, "source_analysis": "x",
                         "t0": 0.0, "t1": 0.1, "path": []})
    append_label(video, {"label": "insect", "source_track": 0, "source_analysis": "x",
                         "t0": 0.0, "t1": 0.1, "path": []})

    result = scoring.score(_cfg(), video)
    assert result.other_total == 0
    assert result.insect_total == 1


def test_score_ignores_unsure_labels(tmp_path):
    video = tmp_path / "clip.mkv"
    _write_motion(video, [_track(w0=0)])
    append_label(video, {"label": "unsure", "source_track": 0, "source_analysis": "x",
                         "t0": 0.0, "t1": 0.1, "path": []})

    result = scoring.score(_cfg(), video)
    assert result.insect_total == 0
    assert result.other_total == 0


# w0=0 (n=3) ends at window index 2 (t=0.2s); w0=3 starts at t=0.3s -- a 0.1s
# gap, well within a stitch_max_gap_seconds=0.5 threshold. Paths continue in
# a straight line across the fragment boundary so the merged track still
# passes _cfg()'s min_straightness/max_step_ratio thresholds.
_FRAGMENT_1 = {"w0": 0, "n": 3, "path": [[0, 0], [5, 0], [10, 0]],
              "straightness": 0.9, "chronic": 0.01, "footprint_ratio": 10.0,
              "step_ratio": 1.0, "mean_area": 8.0}
_FRAGMENT_2 = {"w0": 3, "n": 3, "path": [[11, 0], [15, 0], [20, 0]],
              "straightness": 0.9, "chronic": 0.01, "footprint_ratio": 10.0,
              "step_ratio": 1.0, "mean_area": 8.0}


def test_score_dedupes_stitched_fragments_labelled_insect_twice(tmp_path):
    video = tmp_path / "clip.mkv"
    _write_motion(video, [_FRAGMENT_1, _FRAGMENT_2])
    append_label(video, {"label": "insect", "source_track": 0, "source_analysis": "x",
                         "t0": 0.0, "t1": 0.1, "path": []})
    append_label(video, {"label": "insect", "source_track": 1, "source_analysis": "x",
                         "t0": 0.0, "t1": 0.1, "path": []})

    cfg = _cfg()
    cfg.postprocess.stitch_max_gap_seconds = 0.5
    cfg.postprocess.stitch_max_gap_distance = 0.5

    result = scoring.score(cfg, video)
    assert result.insect_total == 1, "two fragments of the same physical insect should count once"
    assert result.insect_kept == 1


def test_score_group_label_prefers_insect_over_other():
    from camrig.scoring import _resolve_group_label
    assert _resolve_group_label({"insect", "other"}) == "insect"
    assert _resolve_group_label({"other", "unsure"}) == "other"
    assert _resolve_group_label({"unsure"}) == "unsure"


def test_score_min_duration_seconds_filters_short_tracks(tmp_path):
    video = tmp_path / "clip.mkv"
    short = _track(w0=0, n=3)    # 3 * 6 / 60 = 0.3s
    long = _track(w0=30, n=10)   # 10 * 6 / 60 = 1.0s
    _write_motion(video, [short, long])
    append_label(video, {"label": "insect", "source_track": 0, "source_analysis": "x",
                         "t0": 0.0, "t1": 0.1, "path": []})
    append_label(video, {"label": "insect", "source_track": 1, "source_analysis": "x",
                         "t0": 0.0, "t1": 0.1, "path": []})

    cfg = _cfg()
    cfg.postprocess.min_duration_seconds = 0.5

    result = scoring.score(cfg, video)
    assert result.insect_total == 2
    assert result.insect_kept == 1, "only the 1.0s track should clear a 0.5s duration floor"
    assert [m["source_track"] for m in result.misses] == [0]


def test_score_stitching_off_by_default_keeps_fragments_separate(tmp_path):
    video = tmp_path / "clip.mkv"
    _write_motion(video, [_FRAGMENT_1, _FRAGMENT_2])
    append_label(video, {"label": "insect", "source_track": 0, "source_analysis": "x",
                         "t0": 0.0, "t1": 0.1, "path": []})
    append_label(video, {"label": "insect", "source_track": 1, "source_analysis": "x",
                         "t0": 0.0, "t1": 0.1, "path": []})

    result = scoring.score(_cfg(), video)  # _cfg() leaves stitch_* at their 0.0 "off" default
    assert result.insect_total == 2


def _motion(tracks: list[dict], n_windows: int = 70) -> dict:
    return {
        "schema": SCHEMA, "analysis": "blob-track-v1", "width": 100, "height": 100,
        "params": {"window": 6}, "frame_count": n_windows * 6,
        "windows": [{"f": i * 6, "n_frames": 6, "blobs": []} for i in range(n_windows)],
        "tracks": tracks,
    }


def test_survivor_ids_reports_passing_tracks_by_raw_index():
    passes = _track(w0=0, straightness=0.9, chronic=0.01)
    fails = _track(w0=10, straightness=0.1)  # fails min_straightness below
    motion = _motion([passes, fails])
    thresholds = FilterThresholds(min_straightness=0.5, max_chronic=0.1)

    passing, burst_excluded = scoring.survivor_ids(motion, thresholds, framerate=60.0)
    assert passing == {0}
    assert burst_excluded == set()


def test_survivor_ids_includes_burst_excluded_tracks_in_passing_but_flags_them():
    # Three tracks, all individually good, all starting close together --
    # burst_min_tracks=3 should catch all of them, but "passing" (drawn
    # muted, not hidden -- see camrig.motion_view) should still list them.
    tracks = [_track(w0=w0) for w0 in (0, 1, 2)]
    motion = _motion(tracks)
    thresholds = FilterThresholds(min_straightness=0.5, max_chronic=0.1,
                                  burst_window_seconds=1.0, burst_min_tracks=3)

    passing, burst_excluded = scoring.survivor_ids(motion, thresholds, framerate=60.0)
    assert passing == {0, 1, 2}
    assert burst_excluded == {0, 1, 2}


def test_survivor_ids_marks_every_raw_member_of_a_surviving_stitched_group():
    # Neither fragment alone is much of a track, but stitched together they
    # form one straight, low-chronic trajectory that passes. Both raw
    # indices should show as passing -- camrig.motion_view can highlight a
    # short-looking fragment as "actually part of something we're keeping".
    motion = _motion([_FRAGMENT_1, _FRAGMENT_2])
    thresholds = FilterThresholds(min_straightness=0.5, max_chronic=0.1,
                                  stitch_max_gap_seconds=0.5, stitch_max_gap_distance=0.5)

    passing, burst_excluded = scoring.survivor_ids(motion, thresholds, framerate=60.0)
    assert passing == {0, 1}
    assert burst_excluded == set()
