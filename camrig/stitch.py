"""Merge ``camrig.motion`` tracks that are almost certainly fragments of the
same physical insect -- split by ``camrig.motion._link_tracks`` losing the
blob for a window or two (a brief occlusion, a dip below ``min_area``, a
frame or two folded into a neighbouring blob) rather than by two genuinely
different animals. This is a second, coarser linking pass over already
*extracted tracks* (not blobs), so -- like ``camrig.optimise_filters`` -- it
works directly off an existing ``motion.json`` with no MKV reprocessing.

Evidence this matters: checking every labelled insect in the two sample
clips for a plausible successor (starts shortly after it ends, close to
where it ended) found one for 55% of them, and for roughly a third of ALL
labelled insects that successor was ITSELF also labelled "insect" -- i.e.
about a third of the "insect" ground truth was really the same physical
insect labelled twice as separate fragments. A track-duration discriminator
in particular is much weaker than it needs to be while that fragmentation
goes unmodelled.

Stitching rule
--------------
Track B is a candidate successor of track A if B starts after A ends,
within ``max_gap_seconds``, and B's first point is within
``max_gap_distance`` (normalized by frame width) of A's last point. Among
all candidate links, the greedy assignment mirrors
``camrig.motion._link_tracks``'s own blob-linking: sort candidates by gap
(soonest first), accept a link only if neither track's relevant end is
already claimed, so chains never branch or merge from multiple parents. A
track with no plausible predecessor starts a new chain (a singleton chain
if it also has no successor). ``max_gap_seconds <= 0`` or
``max_gap_distance <= 0`` disables stitching entirely (every track its own
singleton chain) -- the same "0 = off" convention as the burst filter.

Recomputing a merged track's features
--------------------------------------
``straightness``/``step_ratio`` are recomputed EXACTLY from the members'
concatenated path points -- ``motion.json`` keeps those in full.
``duration_seconds`` is exact too, but is NOT just the sum of each member's
own alive time -- it spans from the first member's start to the last
member's end, so it counts the gap(s) between fragments as well. That is
deliberate: a real insect's true duration includes the time it was briefly
untracked, and a duration-based filter is much weaker than it needs to be
if stitched fragments only get credited for what each one individually
covered.
``chronic``/``mean_area``/``footprint_ratio`` are NOT exactly recomputable:
their original formulas need each point's own blob bounding box, which
``motion.json`` discards once it's folded into the track's finalized scalar
fields. Those three use a point-count-weighted mean of the members' own
values instead -- a documented approximation, not a re-derivation. That's a
reasonable stand-in for chronic/mean_area (a duration-weighted average is
meaningful on its own terms); for footprint_ratio it's more of a
compromise, since a real merged trajectory typically sweeps more territory
than the average of its parts, so this likely UNDERSTATES it. Exact
recomputation would need each member's original blob bounding boxes, which
means re-running ``camrig.motion`` on the source MKV.
"""

from __future__ import annotations

import bisect
import math
from dataclasses import dataclass
from statistics import median


@dataclass
class StitchResult:
    tracks: list[dict]               # merged track dicts, same shape as motion.json tracks
    member_to_group: dict[int, int]  # raw track index (into the ORIGINAL tracks list) -> index into `tracks`


def _track_time_range(track: dict, windows: list[dict], framerate: float) -> tuple[float, float]:
    w0 = track["w0"]
    t0 = windows[w0]["f"] / framerate
    t1 = windows[w0 + track["n"] - 1]["f"] / framerate
    return t0, t1


def _find_candidates(tracks: list[dict], windows: list[dict], width: float, framerate: float,
                     max_gap_seconds: float, max_gap_distance: float) -> list[tuple[float, float, int, int]]:
    """Every ``(gap, dist, predecessor_idx, successor_idx)`` with
    ``0 <= gap <= max_gap_seconds`` AND ``dist <= max_gap_distance``, sorted
    by gap ascending. Both bounds are applied here (not left for the caller
    to filter later) so a wide precomputed bound doesn't carry a mass of
    obviously-unrelated far-apart pairs through every subsequent trial.

    Tracks (hence their start times) are already time-ordered (camrig.motion
    sorts by w0), so bisecting finds every plausible successor of a given
    track in O(log n + matches) rather than scanning all n tracks.
    """
    n = len(tracks)
    if n == 0 or max_gap_seconds <= 0 or max_gap_distance <= 0:
        return []
    starts: list[float] = []
    ends: list[float] = []
    for t in tracks:
        t0, t1 = _track_time_range(t, windows, framerate)
        starts.append(t0)
        ends.append(t1)

    candidates: list[tuple[float, float, int, int]] = []
    for i, t in enumerate(tracks):
        end_time = ends[i]
        ex, ey = t["path"][-1]
        lo = bisect.bisect_left(starts, end_time)
        hi = bisect.bisect_right(starts, end_time + max_gap_seconds)
        for j in range(lo, hi):
            if j == i:
                continue
            gap = starts[j] - end_time
            if gap < 0:
                continue
            sx, sy = tracks[j]["path"][0]
            dist = math.hypot(sx - ex, sy - ey) / width
            if dist <= max_gap_distance:
                candidates.append((gap, dist, i, j))
    candidates.sort(key=lambda c: c[0])
    return candidates


def _greedy_chains(n: int, candidates: list[tuple[float, float, int, int]], *,
                   max_gap_seconds: float, max_gap_distance: float) -> list[list[int]]:
    """Greedy chain assignment over gap-sorted ``candidates`` -- see the
    module docstring for the rule. ``candidates`` may be a superset (a wider
    gap bound than ``max_gap_seconds`` needs); out-of-threshold pairs are
    filtered here rather than by the caller.
    """
    successor: dict[int, int] = {}         # predecessor index -> its accepted successor index
    predecessor_claimed: set[int] = set()  # successor indices already claimed by some predecessor
    for gap, dist, i, j in candidates:
        if gap > max_gap_seconds or dist > max_gap_distance:
            continue
        if i in successor or j in predecessor_claimed:
            continue
        successor[i] = j
        predecessor_claimed.add(j)

    groups: list[list[int]] = []
    for i in range(n):
        if i in predecessor_claimed:
            continue  # part of a chain started earlier
        chain = [i]
        cur = i
        while cur in successor:
            cur = successor[cur]
            chain.append(cur)
        groups.append(chain)
    return groups


def precompute_candidates(motion: dict, framerate: float, max_gap_seconds_bound: float,
                          max_gap_distance_bound: float) -> list[tuple[float, float, int, int]]:
    """Every plausible-successor pair up to ``max_gap_seconds_bound``/
    ``max_gap_distance_bound`` (typically
    ``camrig.filters.PARAM_BOUNDS["stitch_max_gap_seconds"][1]``/
    ``["stitch_max_gap_distance"][1]``, the widest a search will ever try),
    computed ONCE per clip. ``camrig.optimise_filters`` calls this once when
    loading a clip's dataset, then every trial filters this list down to its
    own smaller thresholds (see ``find_groups_from_candidates``) instead of
    re-scanning every track pair -- this is what keeps a trial with
    stitching in the search space cheap.
    """
    return _find_candidates(motion["tracks"], motion["windows"], motion["width"], framerate,
                            max_gap_seconds_bound, max_gap_distance_bound)


def find_groups_from_candidates(n_tracks: int, candidates: list[tuple[float, float, int, int]], *,
                                max_gap_seconds: float, max_gap_distance: float) -> list[list[int]]:
    """Same result as ``find_groups``, but from a ``precompute_candidates()``
    list instead of ``motion`` -- filtering+greedy-assignment over an
    already-computed candidate list, no per-track-pair distance/gap work.
    """
    if n_tracks == 0 or max_gap_seconds <= 0 or max_gap_distance <= 0:
        return [[i] for i in range(n_tracks)]
    return _greedy_chains(n_tracks, candidates,
                          max_gap_seconds=max_gap_seconds, max_gap_distance=max_gap_distance)


def find_groups(motion: dict, framerate: float, *,
                max_gap_seconds: float, max_gap_distance: float) -> list[list[int]]:
    """Partition every track index in ``motion["tracks"]`` into time-ordered
    chains of likely-same-object fragments (see the module docstring for the
    rule). Each chain is a list of raw track indices, oldest first. A track
    with no plausible predecessor/successor is its own singleton chain.

    Self-contained (computes its own candidates) -- fine for one-shot use
    (``camrig label-score``, tests). A hot loop evaluating many
    ``max_gap_seconds``/``max_gap_distance`` combinations against the SAME
    clip (``camrig.optimise_filters``) should call ``precompute_candidates``
    once and reuse ``find_groups_from_candidates`` instead.
    """
    tracks = motion["tracks"]
    n = len(tracks)
    if n == 0 or max_gap_seconds <= 0 or max_gap_distance <= 0:
        return [[i] for i in range(n)]
    candidates = _find_candidates(tracks, motion["windows"], motion["width"], framerate,
                                  max_gap_seconds, max_gap_distance)
    return _greedy_chains(n, candidates, max_gap_seconds=max_gap_seconds, max_gap_distance=max_gap_distance)


def _duration_seconds(first: dict, last: dict, windows: list[dict], window_frames: int,
                      framerate: float) -> float:
    """Wall-clock span from the start of ``first``'s first window to the end
    of ``last``'s last window. For a singleton group (``first is last``)
    this is just that one track's own alive time. For a merged group it
    also counts any GAP between fragments -- the whole reason duration
    becomes a much stronger discriminator post-stitching is that a real
    insect's true duration includes the time it was briefly untracked, not
    just the sum of what each fragment individually covered.
    """
    start = windows[first["w0"]]["f"] / framerate
    end = (windows[last["w0"] + last["n"] - 1]["f"] + window_frames) / framerate
    return round(end - start, 3)


def _merge_group(indices: list[int], tracks: list[dict], windows: list[dict],
                 window_frames: int, framerate: float) -> dict:
    """Build one merged track dict from raw track indices, oldest first."""
    members = [tracks[i] for i in indices]
    duration_seconds = _duration_seconds(members[0], members[-1], windows, window_frames, framerate)
    if len(members) == 1:
        merged = dict(members[0])
        merged["members"] = tuple(indices)
        merged["duration_seconds"] = duration_seconds
        return merged

    path: list[list[float]] = []
    for m in members:
        path.extend(m["path"])
    steps = [math.dist(path[k], path[k + 1]) for k in range(len(path) - 1)]
    path_len = sum(steps)
    net = math.dist(path[0], path[-1])
    step_med = median(steps) if steps else 0.0
    if step_med > 0:
        step_ratio = round(max(steps) / step_med, 2)
    else:
        step_ratio = 999.0 if steps and max(steps) > 0 else 1.0

    total_n = sum(m["n"] for m in members)

    def weighted(key: str) -> float:
        return sum(m[key] * m["n"] for m in members) / total_n

    return {
        "w0": members[0]["w0"],
        "n": total_n,
        "path": path,
        "net": round(net, 1),
        "len": round(path_len, 1),
        "straightness": round(net / path_len, 3) if path_len > 0 else 0.0,
        "mean_area": round(weighted("mean_area"), 1),
        "chronic": round(weighted("chronic"), 3),
        "step_ratio": step_ratio,
        "footprint_ratio": round(weighted("footprint_ratio"), 2),
        "duration_seconds": duration_seconds,
        "members": tuple(indices),
    }


def stitch_motion(motion: dict, framerate: float, *,
                  max_gap_seconds: float, max_gap_distance: float,
                  candidates: list[tuple[float, float, int, int]] | None = None) -> StitchResult:
    """Merge ``motion["tracks"]`` into stitched tracks, each carrying a
    ``duration_seconds`` field (see ``_duration_seconds``) alongside the
    usual scalar discriminators. With stitching disabled (``max_gap_seconds
    <= 0`` or ``max_gap_distance <= 0``) this is a no-op on every OTHER
    field: one group per raw track, in the same order, each carrying its own
    single-member ``"members"`` tuple -- existing filtering/scoring code
    that reads the usual track fields behaves exactly as it did before
    stitching existed.

    Pass ``candidates`` (from ``precompute_candidates``) to reuse a
    precomputed candidate list instead of recomputing one from scratch --
    see ``find_groups``'s docstring for when that matters.
    """
    tracks = motion["tracks"]
    windows = motion["windows"]
    window_frames = motion.get("params", {}).get("window", 6)
    if candidates is not None:
        groups = find_groups_from_candidates(len(tracks), candidates,
                                             max_gap_seconds=max_gap_seconds,
                                             max_gap_distance=max_gap_distance)
    else:
        groups = find_groups(motion, framerate,
                             max_gap_seconds=max_gap_seconds, max_gap_distance=max_gap_distance)
    merged_tracks: list[dict] = []
    member_to_group: dict[int, int] = {}
    for group_id, indices in enumerate(groups):
        merged_tracks.append(_merge_group(indices, tracks, windows, window_frames, framerate))
        for raw_i in indices:
            member_to_group[raw_i] = group_id
    return StitchResult(tracks=merged_tracks, member_to_group=member_to_group)
