"""Ground-truth trajectory matching: compare a labelled normalized
trajectory (``camrig.labels``) against tracks from a *different*
motion-analysis run -- one using a different threshold, window, min_hits,
cell size, background alpha, min area, link distance, track length,
acceleration limit, or motion resolution (see ``camrig.motion``).

``camrig.scoring`` matches ground truth to tracks by ``source_track``, an
index into the specific ``motion.json["tracks"]`` list the label was made
against. That index is not stable across a re-run: a track can renumber,
split into several, merge, or vanish. This module instead compares a label's
actual normalized ``[x, y, t]`` path to a candidate track's -- the
independent, resolution-agnostic ground truth the label already carries.

Only Phase 1 (``camrig.optimise_filters``, searching the ``[postprocess]``
*filter* thresholds against an unchanged ``motion.json``) is implemented so
far; that stays on ``camrig.scoring``'s fast ``source_track`` path since it
never regenerates tracks. This matcher is Phase 2's foundation: a later
extraction-parameter search that *does* regenerate tracks will need it, but
that search itself is out of scope here.

Matching rule
--------------
1. **Temporal overlap.** The label's and the candidate's ``[t0, t1]``
   windows must overlap at all, or it's not a match.
2. **Spatial proximity + path correspondence, together.** At each of the
   *label's own* path timestamps that falls inside the overlap window,
   linearly interpolate the candidate's path to that same time and measure
   normalized Euclidean distance. The match distance is the *mean* of these
   per-sample distances. Averaging over several samples through the overlap
   (rather than, say, comparing single midpoints) is what makes this "path
   correspondence" rather than just "proximity": two tracks that cross paths
   once but travel apart score worse than two that stay close throughout.
3. A candidate **matches** a label if ``mean_distance <= max_distance``
   (default 0.05, ~5% of frame width/height) AND the temporal overlap covers
   at least ``min_overlap_fraction`` of the label's own duration (default
   0.3) -- a candidate that only grazes the label briefly, even if spatially
   close during that graze, isn't the same track.

A single-candidate match under-counts a label a re-run happened to split
into two or more generated tracks, each covering only *part* of its
duration. ``covered_fraction``/``is_recovered`` handle that: they union the
overlap windows of every spatially-close candidate (``mean_distance <=
max_distance``, regardless of that candidate's own overlap fraction) and
check whether that union -- not any single candidate -- covers the label.
"""

from __future__ import annotations

import bisect
import math
from dataclasses import dataclass

Point = tuple[float, float, float]  # (x, y in 0..1 normalized, t in seconds)

DEFAULT_MAX_DISTANCE = 0.05
DEFAULT_MIN_OVERLAP_FRACTION = 0.3
DEFAULT_MIN_COVERED_FRACTION = 0.5


@dataclass(frozen=True)
class Trajectory:
    """A normalized ``[x, y, t]`` path -- what a label's ``path`` field
    already is, and what a generated track becomes via
    ``from_generated_track``. ``path`` must be sorted by time ``t``.
    """

    path: tuple[Point, ...]
    provenance: str = ""  # debug/logging only, e.g. "label" or "track 42"

    @property
    def t0(self) -> float:
        return self.path[0][2]

    @property
    def t1(self) -> float:
        return self.path[-1][2]

    @property
    def duration(self) -> float:
        return max(self.t1 - self.t0, 0.0)

    def xy_at(self, t: float) -> tuple[float, float] | None:
        """Linearly interpolate normalized (x, y) at time ``t``; ``None`` if
        ``t`` falls outside ``[t0, t1]``.
        """
        if t < self.t0 or t > self.t1:
            return None
        times = [p[2] for p in self.path]
        i = bisect.bisect_left(times, t)
        if i < len(times) and times[i] == t:
            x, y, _ = self.path[i]
            return x, y
        x0, y0, t0_ = self.path[i - 1]
        x1, y1, t1_ = self.path[i]
        if t1_ == t0_:
            return x0, y0
        frac = (t - t0_) / (t1_ - t0_)
        return x0 + frac * (x1 - x0), y0 + frac * (y1 - y0)


def from_label(record: dict) -> Trajectory:
    """Build a ``Trajectory`` from a ``camrig.labels`` record's ``path``."""
    path = tuple((p[0], p[1], p[2]) for p in record["path"])
    return Trajectory(path=path, provenance=f"label(source_track={record.get('source_track')})")


def from_generated_track(track: dict, motion: dict, framerate: float, *,
                         index: int | None = None) -> Trajectory:
    """Normalize a raw ``camrig.motion`` track from a (possibly different)
    run's ``motion.json`` into the same ``[x in 0..1, y in 0..1, t seconds]``
    form labels use, so it can be compared to a label regardless of that
    run's resolution or window size. Mirrors ``camrig.motion_view``'s
    client-side ``saveLabel()``, which builds a label's path the same way.
    """
    width, height = motion["width"], motion["height"]
    windows = motion["windows"]
    w0 = track["w0"]
    points = []
    for i, (x, y) in enumerate(track["path"]):
        win = windows[w0 + i]
        t = (win["f"] + win["n_frames"] / 2) / framerate
        points.append((x / width, y / height, t))
    provenance = f"track {index}" if index is not None else f"track w0={w0}"
    return Trajectory(path=tuple(points), provenance=provenance)


@dataclass(frozen=True)
class MatchResult:
    candidate: Trajectory
    overlap_seconds: float
    overlap_fraction: float          # overlap_seconds / label.duration
    mean_distance: float | None      # None if there's no usable overlap sample
    matched: bool


def _overlap(a: Trajectory, b: Trajectory) -> tuple[float, float] | None:
    lo = max(a.t0, b.t0)
    hi = min(a.t1, b.t1)
    return (lo, hi) if hi > lo else None


def match(label: Trajectory, candidate: Trajectory, *,
         max_distance: float = DEFAULT_MAX_DISTANCE,
         min_overlap_fraction: float = DEFAULT_MIN_OVERLAP_FRACTION) -> MatchResult:
    """Score one candidate against one label. See the module docstring for
    the matching rule.
    """
    ov = _overlap(label, candidate)
    if ov is None or label.duration <= 0:
        return MatchResult(candidate, 0.0, 0.0, None, False)
    lo, hi = ov
    samples = [p[2] for p in label.path if lo <= p[2] <= hi]
    if not samples:
        # The overlap window exists but no label sample falls inside it (a
        # short overlap between two coarsely-sampled paths) -- fall back to
        # the window's midpoint so a genuine overlap isn't silently scored
        # as "no data".
        samples = [(lo + hi) / 2]

    distances = []
    for t in samples:
        lxy = label.xy_at(t)
        cxy = candidate.xy_at(t)
        if lxy is not None and cxy is not None:
            distances.append(math.hypot(lxy[0] - cxy[0], lxy[1] - cxy[1]))

    overlap_fraction = (hi - lo) / label.duration
    if not distances:
        return MatchResult(candidate, hi - lo, overlap_fraction, None, False)

    mean_distance = sum(distances) / len(distances)
    matched = mean_distance <= max_distance and overlap_fraction >= min_overlap_fraction
    return MatchResult(candidate, hi - lo, overlap_fraction, mean_distance, matched)


def best_matches(label: Trajectory, candidates: list[Trajectory], **kwargs) -> list[MatchResult]:
    """Every candidate with some temporal overlap, best (matched, then
    lowest distance) first.
    """
    results = [match(label, c, **kwargs) for c in candidates]
    results = [r for r in results if r.overlap_seconds > 0]
    results.sort(key=lambda r: (not r.matched, r.mean_distance if r.mean_distance is not None else math.inf))
    return results


def covered_fraction(label: Trajectory, candidates: list[Trajectory], *,
                     max_distance: float = DEFAULT_MAX_DISTANCE) -> float:
    """Fraction of the label's duration covered by the UNION of every
    spatially-close candidate's overlap window (``mean_distance <=
    max_distance``), regardless of any single candidate's own overlap
    fraction. Handles a label a re-run split into multiple generated tracks,
    none of which alone passes ``match()``'s ``min_overlap_fraction``.
    """
    if label.duration <= 0:
        return 0.0
    intervals = []
    for c in candidates:
        r = match(label, c, max_distance=max_distance, min_overlap_fraction=0.0)
        if r.mean_distance is not None and r.mean_distance <= max_distance:
            ov = _overlap(label, c)
            if ov:
                intervals.append(ov)
    if not intervals:
        return 0.0
    intervals.sort()
    merged = [intervals[0]]
    for lo, hi in intervals[1:]:
        plo, phi = merged[-1]
        if lo <= phi:
            merged[-1] = (plo, max(phi, hi))
        else:
            merged.append((lo, hi))
    covered = sum(hi - lo for lo, hi in merged)
    return min(covered / label.duration, 1.0)


def is_recovered(label: Trajectory, candidates: list[Trajectory], *,
                 max_distance: float = DEFAULT_MAX_DISTANCE,
                 min_covered_fraction: float = DEFAULT_MIN_COVERED_FRACTION) -> bool:
    """Whether ``label`` is recovered by ``candidates`` taken together,
    covering split tracks that ``match()`` alone would miss.
    """
    return covered_fraction(label, candidates, max_distance=max_distance) >= min_covered_fraction
