"""Real per-frame capture timestamps, from a clip's ``.pts`` sidecar.

``rpicam-vid --save-pts`` (and ``camrig.basler``'s own writer) record actual
wall-clock time for every captured frame -- "timecode format v2": a header
line, then one cumulative millisecond timestamp per frame. Capture framerate
is only nominal (see ``camrig.motion``'s module docstring): under I/O
contention actual inter-frame spacing drifts -- e.g. an SD card falling
behind the write rate a 120fps capture needs, sagging to a fraction of that
in places -- so any code turning a frame/window index into wall-clock
seconds needs these real timestamps, not ``index / nominal_framerate``. Using
the nominal rate instead compounds: by the end of an affected 85s clip, the
index-based time was found ~15s (~18%) adrift of the real one.

``FrameClock`` is the shared frame-index <-> wall-clock-seconds lookup for
every consumer of a ``motion.json`` (``camrig.stitch``, ``camrig.scoring``,
``camrig.motion_debug``, ``camrig.motion_view``, ``camrig.trajectory_match``):
``FrameClock.from_pts`` wraps a clip's own real timestamps,
``FrameClock.constant`` falls back to a nominal rate for synthetic/test data
or a sidecar predating ``--save-pts``.
"""

from __future__ import annotations

import bisect
from pathlib import Path


def load_frame_times(pts_path: Path) -> list[float]:
    """Per-frame seconds since clip start, from a timecode-v2 ``.pts`` sidecar."""
    lines = pts_path.read_text(encoding="utf-8").splitlines()
    return [float(line) / 1000.0 for line in lines[1:] if line.strip()]


class FrameClock:
    """Frame index -> wall-clock seconds, and back.

    Construct via ``from_pts`` (real per-frame timestamps) or ``constant``
    (a nominal fps, reproducing the old ``index / framerate`` behaviour) --
    never directly.
    """

    def __init__(self, *, times: list[float] | None = None, fps: float | None = None) -> None:
        self._times = times
        self._fps = fps

    @classmethod
    def from_pts(cls, times: list[float]) -> "FrameClock":
        if not times:
            raise ValueError("frame_times is empty")
        return cls(times=times)

    @classmethod
    def constant(cls, fps: float) -> "FrameClock":
        return cls(fps=fps)

    def time(self, frame_idx: float) -> float:
        """Wall-clock seconds at ``frame_idx`` (may be fractional, e.g. a
        window's midpoint frame). One step past the last real timestamp
        extrapolates using the final measured inter-frame delta -- needed
        for a window's exclusive end index, which can land exactly at
        ``len(times)``.
        """
        if self._fps is not None:
            return frame_idx / self._fps
        times = self._times
        assert times is not None
        last = len(times) - 1
        if frame_idx <= last:
            lo = int(frame_idx)
            if lo == frame_idx or lo >= last:
                return times[lo]
            frac = frame_idx - lo
            return times[lo] + frac * (times[lo + 1] - times[lo])
        delta = (times[-1] - times[-2]) if len(times) >= 2 else 0.0
        return times[-1] + delta * (frame_idx - last)

    def nearest_index(self, t: float) -> int:
        """Frame index whose timestamp is closest to ``t``."""
        if self._fps is not None:
            return round(t * self._fps)
        times = self._times
        assert times is not None
        i = bisect.bisect_left(times, t)
        if i <= 0:
            return 0
        if i >= len(times):
            return len(times) - 1
        before, after = times[i - 1], times[i]
        return i - 1 if (t - before) <= (after - t) else i

    def as_list(self, n_frames: int) -> list[float]:
        """The first ``n_frames`` frames' timestamps, for handing to a
        client that just needs the raw lookup table (``camrig.motion_view``'s
        browser UI).
        """
        return [self.time(i) for i in range(n_frames)]
