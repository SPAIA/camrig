"""Tests for camrig.scoring: threshold precision/recall against camrig.labels."""

import json
from pathlib import Path

from camrig import scoring
from camrig.config import CaptureConfig, Config, PostprocessConfig
from camrig.labels import append_label
from camrig.motion import SCHEMA


def _track(w0, straightness=0.9, chronic=0.01, footprint_ratio=10.0, step_ratio=1.0):
    return {"w0": w0, "n": 3, "path": [[0, 0], [1, 0], [2, 0]],
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
