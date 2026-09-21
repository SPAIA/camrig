"""Tests for camrig.filters: the FilterThresholds value type shared between
camrig.scoring and camrig.optimise_filters.
"""

from camrig.config import PostprocessConfig
from camrig.filters import PARAM_BOUNDS, FilterThresholds


def test_from_postprocess_copies_the_eight_filter_fields():
    pp = PostprocessConfig(
        min_straightness=0.4, max_chronic=0.2, min_footprint_ratio=3.0,
        max_step_ratio=12.0, burst_window_seconds=0.8, burst_min_tracks=5,
        stitch_max_gap_seconds=0.6, stitch_max_gap_distance=0.03,
        # unrelated PostprocessConfig fields should have no bearing:
        motion_threshold=99, preview_width=1,
    )
    t = FilterThresholds.from_postprocess(pp)
    assert t.min_straightness == 0.4
    assert t.max_chronic == 0.2
    assert t.min_footprint_ratio == 3.0
    assert t.max_step_ratio == 12.0
    assert t.burst_window_seconds == 0.8
    assert t.burst_min_tracks == 5
    assert t.stitch_max_gap_seconds == 0.6
    assert t.stitch_max_gap_distance == 0.03


def test_as_dict_round_trips_through_param_bounds_keys():
    t = FilterThresholds()
    d = t.as_dict()
    assert set(d.keys()) == set(PARAM_BOUNDS.keys())
    assert FilterThresholds(**d) == t


def test_defaults_mean_no_filtering():
    t = FilterThresholds()
    assert t.min_straightness == 0.0 and t.max_chronic == 1.0
    assert t.min_footprint_ratio == 0.0
    assert t.burst_min_tracks == 0  # disables the burst filter entirely
    assert t.stitch_max_gap_seconds == 0.0  # disables stitching entirely
    assert t.stitch_max_gap_distance == 0.0
