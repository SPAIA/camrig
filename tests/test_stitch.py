"""Tests for camrig.stitch: merging track fragments before filtering/scoring.

Window/time math used throughout: window=6 frames at 60 fps, so each window
index step is 0.1s. A track's end time is windows[w0 + n - 1]['f'] / fps
(the START frame of its last occupied window); its successor's gap is the
successor's start time minus that.
"""

import pytest

from camrig.pts import FrameClock
from camrig.stitch import find_groups, stitch_motion


def _track(w0, n, path, straightness=0.9, chronic=0.02, footprint_ratio=5.0, step_ratio=1.0,
          mean_area=8.0):
    return {"w0": w0, "n": n, "path": path, "straightness": straightness, "chronic": chronic,
           "footprint_ratio": footprint_ratio, "step_ratio": step_ratio, "mean_area": mean_area}


def _motion(tracks, width=100, height=100, window=6, n_windows=200):
    return {
        "width": width, "height": height, "params": {"window": window},
        "windows": [{"f": i * window, "n_frames": window} for i in range(n_windows)],
        "tracks": tracks,
    }


FPS = FrameClock.constant(60.0)


def test_two_close_tracks_merge_into_one_group():
    # a: w0=7, n=4 -> ends at window index 10 (t=1.0s), near (50, 50).
    # b: w0=12 -> starts at t=1.2s (gap 0.2s), near (51, 51): close in both.
    a = _track(w0=7, n=4, path=[[40, 40], [45, 45], [48, 48], [50, 50]])
    b = _track(w0=12, n=3, path=[[51, 51], [55, 55], [60, 60]])
    motion = _motion([a, b])

    groups = find_groups(motion, FPS, max_gap_seconds=1.0, max_gap_distance=0.05)
    assert groups == [[0, 1]]


def test_large_time_gap_stays_separate():
    a = _track(w0=0, n=3, path=[[40, 40], [45, 45], [50, 50]])
    b = _track(w0=100, n=3, path=[[51, 51], [55, 55], [60, 60]])  # far later
    motion = _motion([a, b])

    groups = find_groups(motion, FPS, max_gap_seconds=1.0, max_gap_distance=0.05)
    assert sorted(groups) == [[0], [1]]


def test_large_spatial_gap_stays_separate():
    a = _track(w0=0, n=3, path=[[40, 40], [45, 45], [50, 50]])
    b = _track(w0=2, n=3, path=[[90, 90], [92, 92], [95, 95]])  # close in time, far in space
    motion = _motion([a, b])

    groups = find_groups(motion, FPS, max_gap_seconds=1.0, max_gap_distance=0.05)
    assert sorted(groups) == [[0], [1]]


def test_three_tracks_chain_together():
    # a: w0=0, n=3 -> ends at window index 2. b: w0=3 -> gap 0.1s. b ends at
    # window index 5. c: w0=6 -> gap 0.1s.
    a = _track(w0=0, n=3, path=[[10, 10], [15, 10], [20, 10]])
    b = _track(w0=3, n=3, path=[[21, 10], [25, 10], [30, 10]])
    c = _track(w0=6, n=3, path=[[31, 10], [35, 10], [40, 10]])
    motion = _motion([a, b, c])

    groups = find_groups(motion, FPS, max_gap_seconds=1.0, max_gap_distance=0.05)
    assert groups == [[0, 1, 2]]


def test_stitching_disabled_returns_singletons():
    a = _track(w0=0, n=3, path=[[10, 10], [15, 10], [20, 10]])
    b = _track(w0=3, n=3, path=[[21, 10], [25, 10], [30, 10]])
    motion = _motion([a, b])

    assert find_groups(motion, FPS, max_gap_seconds=0.0, max_gap_distance=0.05) == [[0], [1]]
    assert find_groups(motion, FPS, max_gap_seconds=1.0, max_gap_distance=0.0) == [[0], [1]]


def test_greedy_assignment_prefers_smaller_gap_and_never_double_assigns():
    # `a` ends at (50, 50). `b` (gap 0.1s, 4 units away) and `c` (gap 0.5s, 4
    # units away in a different direction) are both plausible successors of
    # `a` alone -- `b` and `c` are ~5.7 units apart, too far to be
    # candidates for EACH OTHER. `a` should link to the closer-in-time `b`;
    # `c`, having lost its chance, stays its own singleton chain rather than
    # being incorrectly folded in some other way.
    a = _track(w0=0, n=3, path=[[40, 40], [45, 45], [50, 50]])
    b = _track(w0=3, n=3, path=[[54, 50], [60, 50], [66, 50]])   # starts 0.1s after a ends
    c = _track(w0=7, n=3, path=[[50, 54], [50, 60], [50, 66]])   # starts 0.5s after a ends
    motion = _motion([a, b, c])

    groups = find_groups(motion, FPS, max_gap_seconds=1.0, max_gap_distance=0.05)
    assert sorted(groups) == [[0, 1], [2]]


def test_stitch_motion_disabled_is_a_no_op_on_track_values():
    a = _track(w0=0, n=3, path=[[10, 10], [15, 10], [20, 10]], straightness=0.77)
    motion = _motion([a])

    result = stitch_motion(motion, FPS, max_gap_seconds=0.0, max_gap_distance=0.0)
    assert len(result.tracks) == 1
    merged = result.tracks[0]
    assert merged["straightness"] == 0.77
    assert merged["path"] == a["path"]
    assert merged["members"] == (0,)
    assert result.member_to_group == {0: 0}


def test_stitch_motion_recomputes_straightness_and_step_ratio_over_merged_path():
    # A straight line split into two fragments should recombine into one
    # perfectly straight, low-step-ratio merged track.
    a = _track(w0=0, n=3, path=[[0, 0], [10, 0], [20, 0]])
    b = _track(w0=3, n=3, path=[[21, 0], [30, 0], [40, 0]])
    motion = _motion([a, b])

    result = stitch_motion(motion, FPS, max_gap_seconds=1.0, max_gap_distance=0.05)
    assert len(result.tracks) == 1
    merged = result.tracks[0]
    assert merged["members"] == (0, 1)
    assert merged["n"] == 6
    assert merged["path"] == a["path"] + b["path"]
    assert merged["straightness"] > 0.99
    assert merged["step_ratio"] < 1.5
    assert merged["w0"] == a["w0"]


def test_stitch_motion_weights_chronic_and_mean_area_by_point_count():
    a = _track(w0=0, n=3, path=[[0, 0], [10, 0], [20, 0]], chronic=0.10, mean_area=10.0)
    b = _track(w0=3, n=1, path=[[21, 0]], chronic=0.50, mean_area=30.0)
    motion = _motion([a, b])

    result = stitch_motion(motion, FPS, max_gap_seconds=1.0, max_gap_distance=0.05)
    merged = result.tracks[0]
    # point-count-weighted mean: (0.10*3 + 0.50*1) / 4 = 0.2; (10*3 + 30*1) / 4 = 15
    assert merged["chronic"] == round(0.2, 3)
    assert merged["mean_area"] == round(15.0, 1)


def test_duration_seconds_for_a_singleton_track_is_its_own_alive_time():
    # w0=0, n=3 -> occupies windows 0,1,2. Duration spans from window 0's
    # start to window 2's end (i.e. window 2's start plus one more window
    # step): 3 windows * 0.1s/window = 0.3s.
    a = _track(w0=0, n=3, path=[[0, 0], [10, 0], [20, 0]])
    motion = _motion([a])

    result = stitch_motion(motion, FPS, max_gap_seconds=0.0, max_gap_distance=0.0)
    assert result.tracks[0]["duration_seconds"] == pytest.approx(0.3)


def test_duration_seconds_for_a_merged_track_counts_the_gap_between_fragments():
    # a: w0=0, n=3 -> own span [0.0s, 0.3s) (0.3s alive).
    # b: w0=6, n=2 -> starts at t=0.6s (a 0.3s gap after a's own end), own
    # span [0.6s, 0.8s) (0.2s alive).
    # A duration computed as "sum of each fragment's own alive time" would
    # give 0.3+0.2=0.5s -- WRONG, since it ignores the fact the animal was
    # (probably) still around, untracked, during the 0.3s gap. The true
    # span from a's start to b's end is 0.8s.
    a = _track(w0=0, n=3, path=[[0, 0], [10, 0], [20, 0]])
    b = _track(w0=6, n=2, path=[[21, 0], [30, 0]])
    motion = _motion([a, b])

    result = stitch_motion(motion, FPS, max_gap_seconds=1.0, max_gap_distance=0.05)
    assert len(result.tracks) == 1
    assert result.tracks[0]["duration_seconds"] == pytest.approx(0.8)


def test_member_to_group_maps_every_raw_track():
    a = _track(w0=0, n=3, path=[[10, 10], [15, 10], [20, 10]])
    b = _track(w0=3, n=3, path=[[21, 10], [25, 10], [30, 10]])
    c = _track(w0=50, n=3, path=[[90, 90], [92, 92], [95, 95]])  # unrelated, own group
    motion = _motion([a, b, c])

    result = stitch_motion(motion, FPS, max_gap_seconds=1.0, max_gap_distance=0.05)
    assert result.member_to_group[0] == result.member_to_group[1]
    assert result.member_to_group[2] != result.member_to_group[0]
    assert len(result.tracks) == 2
