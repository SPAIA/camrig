"""Tests for camrig.motion_debug's threshold/burst filtering helpers."""

import math

from camrig.config import PostprocessConfig
from camrig.motion_debug import burst_track_ids, passes_thresholds


def _track(straightness=1.0, chronic=0.0, footprint_ratio=10.0, step_ratio=1.0, w0=0,
          path=None, duration_seconds=None):
    track = {"straightness": straightness, "chronic": chronic,
             "footprint_ratio": footprint_ratio, "step_ratio": step_ratio, "w0": w0,
             "path": path if path is not None else [[0, 0], [10, 0]]}
    if duration_seconds is not None:
        track["duration_seconds"] = duration_seconds
    return track


def test_passes_thresholds_checks_all_four_discriminators():
    pp = PostprocessConfig(min_straightness=0.5, max_chronic=0.1,
                           min_footprint_ratio=2.0, max_step_ratio=5.0)
    assert passes_thresholds(_track(straightness=0.9, chronic=0.05,
                                    footprint_ratio=3.0, step_ratio=2.0), pp)
    assert not passes_thresholds(_track(straightness=0.2), pp), "fails straightness"
    assert not passes_thresholds(_track(chronic=0.5), pp), "fails chronic"
    assert not passes_thresholds(_track(footprint_ratio=1.0), pp), "fails footprint_ratio"
    assert not passes_thresholds(_track(step_ratio=9.0), pp), "fails step_ratio"


def test_passes_thresholds_checks_duration_when_present():
    pp = PostprocessConfig(min_duration_seconds=1.0)
    assert passes_thresholds(_track(duration_seconds=1.5), pp)
    assert passes_thresholds(_track(duration_seconds=1.0), pp), "exactly at the floor should pass"
    assert not passes_thresholds(_track(duration_seconds=0.9), pp)


def test_passes_thresholds_ignores_duration_floor_when_track_has_no_duration_field():
    # A raw (un-stitched) motion.json track never carries duration_seconds
    # -- camrig.motion_debug.render doesn't stitch before filtering yet (see
    # passes_thresholds' docstring), so it can't judge duration and
    # shouldn't be silently broken by a non-zero min_duration_seconds.
    pp = PostprocessConfig(min_duration_seconds=10.0)
    assert passes_thresholds(_track(), pp)


def test_burst_track_ids_flags_dense_cluster_only():
    # window=6 frames at 60fps -> 0.1s apart. Tracks 0-4 start within 0.4s of
    # each other (a burst); track 5 starts 2s later, isolated. All share the
    # same default heading, so direction plays no part here (default
    # max_direction_deviation=pi accepts every deviation anyway).
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


def test_burst_track_ids_flags_a_directionally_coherent_cluster():
    # Five tracks all travelling +x (heading 0), starting within 0.4s of each
    # other -- a textbook wind gust. A tight direction tolerance should still
    # flag every one of them since they all agree.
    windows = [{"f": i * 6} for i in range(30)]
    motion = {"windows": windows}
    tracks = [_track(w0=w0, path=[[0, 0], [10, 0]]) for w0 in [0, 1, 2, 3, 4]]
    ids = list(range(len(tracks)))

    burst = burst_track_ids(motion, tracks, ids, framerate=60.0, window_seconds=1.0,
                            min_tracks=5, max_direction_deviation=math.radians(20))
    assert burst == {0, 1, 2, 3, 4}


def test_burst_track_ids_rescues_a_direction_outlier_within_a_dense_cluster():
    # Same dense cluster as above, but one track (index 2) heads the
    # opposite way (heading pi, e.g. an insect flying against the gust). A
    # tight tolerance should rescue it even though it's temporally part of
    # the same dense burst; a lenient (default, pi) tolerance should not.
    windows = [{"f": i * 6} for i in range(30)]
    motion = {"windows": windows}
    tracks = [_track(w0=w0, path=[[0, 0], [10, 0]]) for w0 in [0, 1, 3, 4]]
    tracks.insert(2, _track(w0=2, path=[[10, 0], [0, 0]]))  # heading pi, opposite the rest
    ids = list(range(len(tracks)))

    strict = burst_track_ids(motion, tracks, ids, framerate=60.0, window_seconds=1.0,
                             min_tracks=5, max_direction_deviation=math.radians(20))
    assert strict == {0, 1, 3, 4}, "the opposite-heading track should be rescued"

    lenient = burst_track_ids(motion, tracks, ids, framerate=60.0, window_seconds=1.0,
                              min_tracks=5)  # default max_direction_deviation=pi
    assert lenient == {0, 1, 2, 3, 4}, "with no directional refinement, everyone is flagged"


def test_burst_track_ids_treats_zero_displacement_track_as_unjudgeable():
    # A track with no net displacement has no heading to compare -- it can't
    # be rescued by direction, so it's flagged like the pre-directional
    # behaviour regardless of how strict the tolerance is.
    windows = [{"f": i * 6} for i in range(30)]
    motion = {"windows": windows}
    tracks = [_track(w0=w0, path=[[0, 0], [10, 0]]) for w0 in [0, 1, 3, 4]]
    tracks.insert(2, _track(w0=2, path=[[5, 5], [5, 5]]))  # zero net displacement
    ids = list(range(len(tracks)))

    burst = burst_track_ids(motion, tracks, ids, framerate=60.0, window_seconds=1.0,
                            min_tracks=5, max_direction_deviation=math.radians(1))
    assert 2 in burst
