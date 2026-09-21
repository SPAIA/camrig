"""Tests for camrig.trajectory_match: synthetic trajectories with obvious
expected outcomes for each case the matcher must cope with (see its module
docstring for the matching rule).
"""

from camrig.trajectory_match import (
    Trajectory,
    best_matches,
    covered_fraction,
    from_generated_track,
    from_label,
    is_recovered,
    match,
)


def _line(t0: float, t1: float, x0: float, y0: float, x1: float, y1: float, n: int = 11) -> Trajectory:
    """A straight-line trajectory from (x0, y0) at t0 to (x1, y1) at t1, sampled at n points."""
    path = []
    for i in range(n):
        frac = i / (n - 1)
        path.append((x0 + frac * (x1 - x0), y0 + frac * (y1 - y0), t0 + frac * (t1 - t0)))
    return Trajectory(path=tuple(path))


def test_regenerated_track_matching_original_closely():
    label = _line(0.0, 2.0, 0.1, 0.1, 0.5, 0.5)
    candidate = _line(0.0, 2.0, 0.1, 0.1, 0.5, 0.5)  # identical path
    result = match(label, candidate)
    assert result.matched
    assert result.mean_distance == 0.0
    assert result.overlap_fraction == 1.0


def test_regenerated_track_slightly_shifted_still_matches():
    label = _line(0.0, 2.0, 0.1, 0.1, 0.5, 0.5)
    candidate = _line(0.0, 2.0, 0.11, 0.11, 0.51, 0.51)  # shifted by 0.01 throughout
    result = match(label, candidate)
    assert result.matched
    assert 0 < result.mean_distance < 0.02


def test_large_shift_fails_the_distance_threshold():
    label = _line(0.0, 2.0, 0.1, 0.1, 0.5, 0.5)
    candidate = _line(0.0, 2.0, 0.3, 0.3, 0.7, 0.7)  # shifted by 0.2 throughout
    result = match(label, candidate)
    assert not result.matched
    assert result.mean_distance > 0.05


def test_no_temporal_overlap_never_matches():
    label = _line(0.0, 2.0, 0.1, 0.1, 0.5, 0.5)
    candidate = _line(5.0, 7.0, 0.1, 0.1, 0.5, 0.5)  # same path, much later
    result = match(label, candidate)
    assert not result.matched
    assert result.overlap_seconds == 0.0
    assert result.mean_distance is None


def test_unrelated_track_same_time_window_not_matched():
    label = _line(0.0, 2.0, 0.1, 0.1, 0.1, 0.1)      # stationary near top-left
    candidate = _line(0.0, 2.0, 0.9, 0.9, 0.9, 0.9)  # stationary near bottom-right, same time
    result = match(label, candidate)
    assert result.overlap_seconds == 2.0  # temporal overlap exists
    assert not result.matched            # but spatially unrelated


def test_short_overlap_fails_min_overlap_fraction():
    label = _line(0.0, 10.0, 0.1, 0.1, 0.1, 0.1)
    # Candidate matches spatially but is only alive for the last 1s of a 10s label.
    candidate = _line(9.0, 10.0, 0.1, 0.1, 0.1, 0.1)
    result = match(label, candidate)
    assert result.mean_distance == 0.0
    assert not result.matched
    assert result.overlap_fraction < 0.3


def test_split_track_not_matched_individually_but_recovered_via_union():
    # A single continuous label, split by a re-run into two generated tracks
    # that together cover it but neither alone reaches a strict per-track
    # overlap requirement.
    label = _line(0.0, 2.0, 0.1, 0.1, 0.5, 0.5)
    first_half = _line(0.0, 1.0, 0.1, 0.1, 0.3, 0.3)
    second_half = _line(1.0, 2.0, 0.3, 0.3, 0.5, 0.5)

    strict_kwargs = {"min_overlap_fraction": 0.6}
    assert not match(label, first_half, **strict_kwargs).matched
    assert not match(label, second_half, **strict_kwargs).matched

    assert is_recovered(label, [first_half, second_half])
    assert covered_fraction(label, [first_half, second_half]) > 0.9


def test_covered_fraction_zero_with_no_candidates():
    label = _line(0.0, 2.0, 0.1, 0.1, 0.5, 0.5)
    assert covered_fraction(label, []) == 0.0
    assert not is_recovered(label, [])


def test_best_matches_ranks_matched_before_unmatched_and_by_distance():
    label = _line(0.0, 2.0, 0.1, 0.1, 0.5, 0.5)
    close = _line(0.0, 2.0, 0.1, 0.1, 0.5, 0.5)
    far = _line(0.0, 2.0, 0.9, 0.9, 0.9, 0.9)
    no_overlap = _line(10.0, 12.0, 0.1, 0.1, 0.5, 0.5)

    ranked = best_matches(label, [far, close, no_overlap])
    assert [r.candidate for r in ranked] == [close, far]  # no_overlap excluded entirely
    assert ranked[0].matched and not ranked[1].matched


def test_from_label_round_trips_path():
    record = {"source_track": 7, "path": [[0.1, 0.2, 0.0], [0.3, 0.4, 1.0]]}
    traj = from_label(record)
    assert traj.path == ((0.1, 0.2, 0.0), (0.3, 0.4, 1.0))
    assert traj.t0 == 0.0 and traj.t1 == 1.0
    assert "7" in traj.provenance


def test_from_generated_track_normalizes_pixels_and_derives_time():
    motion = {
        "width": 200, "height": 100,
        "windows": [
            {"f": 0, "n_frames": 6}, {"f": 6, "n_frames": 6}, {"f": 12, "n_frames": 6},
        ],
    }
    track = {"w0": 0, "path": [[20.0, 10.0], [40.0, 20.0], [60.0, 30.0]]}
    traj = from_generated_track(track, motion, framerate=60.0, index=3)
    assert traj.path[0] == (0.1, 0.1, 3 / 60.0)
    assert traj.path[1] == (0.2, 0.2, 9 / 60.0)
    assert traj.path[2] == (0.3, 0.3, 15 / 60.0)
    assert "3" in traj.provenance


def test_xy_at_interpolates_and_is_none_outside_range():
    traj = _line(0.0, 2.0, 0.0, 0.0, 2.0, 0.0)
    assert traj.xy_at(1.0) == (1.0, 0.0)
    assert traj.xy_at(-1.0) is None
    assert traj.xy_at(3.0) is None
