"""Tests for camrig.motion_debug's threshold/burst filtering helpers."""

from camrig.config import PostprocessConfig
from camrig.motion_debug import burst_track_ids, passes_thresholds


def _track(straightness=1.0, chronic=0.0, footprint_ratio=10.0, step_ratio=1.0, w0=0):
    return {"straightness": straightness, "chronic": chronic,
            "footprint_ratio": footprint_ratio, "step_ratio": step_ratio, "w0": w0}


def test_passes_thresholds_checks_all_four_discriminators():
    pp = PostprocessConfig(min_straightness=0.5, max_chronic=0.1,
                           min_footprint_ratio=2.0, max_step_ratio=5.0)
    assert passes_thresholds(_track(straightness=0.9, chronic=0.05,
                                    footprint_ratio=3.0, step_ratio=2.0), pp)
    assert not passes_thresholds(_track(straightness=0.2), pp), "fails straightness"
    assert not passes_thresholds(_track(chronic=0.5), pp), "fails chronic"
    assert not passes_thresholds(_track(footprint_ratio=1.0), pp), "fails footprint_ratio"
    assert not passes_thresholds(_track(step_ratio=9.0), pp), "fails step_ratio"


def test_burst_track_ids_flags_dense_cluster_only():
    # window=6 frames at 60fps -> 0.1s apart. Tracks 0-4 start within 0.4s of
    # each other (a burst); track 5 starts 2s later, isolated.
    windows = [{"f": i * 6} for i in range(30)]
    motion = {"windows": windows}
    tracks = [_track(w0=w0) for w0 in [0, 1, 2, 3, 4, 20]]
    ids = list(range(len(tracks)))

    burst = burst_track_ids(motion, tracks, ids, framerate=60.0,
                            window_seconds=1.0, min_tracks=5)
    assert burst == {0, 1, 2, 3, 4}


def test_burst_track_ids_disabled_when_min_tracks_zero():
    windows = [{"f": i * 6} for i in range(10)]
    motion = {"windows": windows}
    tracks = [_track(w0=w0) for w0 in range(5)]
    ids = list(range(len(tracks)))

    assert burst_track_ids(motion, tracks, ids, framerate=60.0,
                           window_seconds=1.0, min_tracks=0) == set()


def test_burst_track_ids_ignores_tracks_outside_the_given_ids():
    # A dense cluster that was already filtered out (not in ids) shouldn't be
    # flagged or counted toward another cluster's threshold.
    windows = [{"f": i * 6} for i in range(10)]
    motion = {"windows": windows}
    tracks = [_track(w0=w0) for w0 in [0, 1, 2, 3, 4]]

    assert burst_track_ids(motion, tracks, ids=[0, 1], framerate=60.0,
                           window_seconds=1.0, min_tracks=5) == set()
