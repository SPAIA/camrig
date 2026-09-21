"""The six ``[postprocess]`` filter thresholds ``camrig.motion_debug``/
``camrig.motion_view`` apply to ``camrig.motion`` tracks (see
``PostprocessConfig`` for the full per-field rationale), gathered here as
their own lightweight value type plus search-space bounds. Anything that
wants to try many threshold combinations cheaply (``camrig.optimise_filters``)
works with this instead of a full ``Config``/``PostprocessConfig``.
"""

from __future__ import annotations

from dataclasses import dataclass

from .config import PostprocessConfig

# (low, high, python type) search bounds for each parameter -- mirrors the
# camrig.motion_view slider ranges, since those already encode the sane
# manually-explored range for this footage.
PARAM_BOUNDS: dict[str, tuple[float, float, type]] = {
    "min_straightness": (0.0, 1.0, float),
    "max_chronic": (0.0, 1.0, float),
    "min_footprint_ratio": (0.0, 30.0, float),
    "max_step_ratio": (1.0, 50.0, float),
    "burst_window_seconds": (0.2, 5.0, float),
    "burst_min_tracks": (0, 30, int),
    # See camrig.stitch: merges track fragments before filtering/scoring.
    # Bounds come from checking labelled insects against their own
    # motion.json for plausible successors: real matches never exceeded a
    # 1.0s gap or ~0.04 normalized distance, so these bounds keep some
    # margin over that without being so wide that exploration wastes trials
    # on physically implausible mega-merges (which are also the expensive
    # ones to evaluate).
    "stitch_max_gap_seconds": (0.0, 1.5, float),
    "stitch_max_gap_distance": (0.0, 0.08, float),
}


@dataclass(frozen=True)
class FilterThresholds:
    """One point in the 8-parameter ``[postprocess]`` filter search space
    (six classification/burst thresholds plus two track-stitching ones, see
    ``camrig.stitch``).

    Duck-types as the ``pp`` argument ``camrig.motion_debug.passes_thresholds``
    expects (same attribute names), so it drops straight into the existing
    filtering code with no changes there. Defaults match
    ``PostprocessConfig``'s "no filtering" defaults.
    """

    min_straightness: float = 0.0
    max_chronic: float = 1.0
    min_footprint_ratio: float = 0.0
    max_step_ratio: float = 50.0
    burst_window_seconds: float = 1.0
    burst_min_tracks: int = 0
    stitch_max_gap_seconds: float = 0.0
    stitch_max_gap_distance: float = 0.0

    @classmethod
    def from_postprocess(cls, pp: PostprocessConfig) -> "FilterThresholds":
        return cls(
            min_straightness=pp.min_straightness,
            max_chronic=pp.max_chronic,
            min_footprint_ratio=pp.min_footprint_ratio,
            max_step_ratio=pp.max_step_ratio,
            burst_window_seconds=pp.burst_window_seconds,
            burst_min_tracks=pp.burst_min_tracks,
            stitch_max_gap_seconds=pp.stitch_max_gap_seconds,
            stitch_max_gap_distance=pp.stitch_max_gap_distance,
        )

    def as_dict(self) -> dict[str, float | int]:
        return {
            "min_straightness": self.min_straightness,
            "max_chronic": self.max_chronic,
            "min_footprint_ratio": self.min_footprint_ratio,
            "max_step_ratio": self.max_step_ratio,
            "burst_window_seconds": self.burst_window_seconds,
            "burst_min_tracks": self.burst_min_tracks,
            "stitch_max_gap_seconds": self.stitch_max_gap_seconds,
            "stitch_max_gap_distance": self.stitch_max_gap_distance,
        }
