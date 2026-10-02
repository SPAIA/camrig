"""Tests for camrig.label_debug's label selection and trail timing."""

from pathlib import Path

from camrig.label_debug import default_labels_path, latest_labels, visible_trail

PATH = [[0.0, 0.0, 1.0], [1.0, 0.0, 2.0], [1.0, 1.0, 3.0]]


def test_default_labels_path_strips_derived_suffixes():
    for name in ("clip_a.mkv", "clip_a.preview.mp4", "clip_a.motion_debug.mp4"):
        assert default_labels_path(Path("d") / name) == Path("d/clip_a.labels.jsonl")


def test_latest_labels_uses_last_record_per_track_and_filters():
    records = [
        {"label": "insect", "source_track": 1, "t0": 5},
        {"label": "other", "source_track": 2, "t0": 1},
        {"label": "other", "source_track": 1, "t0": 5},  # relabelled
        {"label": "insect", "source_track": 3, "t0": 2},
    ]
    assert [r["source_track"] for r in latest_labels(records, {"insect"})] == [3]
    assert [r["source_track"] for r in latest_labels(records, {"insect", "other"})] == [2, 3, 1]


def test_visible_trail_interpolates_head_and_fades():
    assert visible_trail(PATH, 0.5, 3.0) == []
    assert visible_trail(PATH, 1.5, 3.0) == [(0.0, 0.0), (0.5, 0.0)]
    assert visible_trail(PATH, 3.5, 1.0) == [(1.0, 1.0)]
    assert visible_trail(PATH, 7.0, 3.0) == []
    assert len(visible_trail(PATH, 100.0, None)) == 3
