"""Tests for camrig.motion blob detection and track linking.

Synthetic gray8 clips exercise the discriminators the analysis is built
around: a travelling dot (insect-like: high straightness, low chronic
activity), a swaying bar (plant-like: low straightness, high chronic
activity), split body parts merging into one blob, and noise rejection.
"""

import io
import math

import numpy as np
import pytest

from camrig.motion import _link_tracks, analyse

W, H = 96, 64
BG = 20


def run(frames: list[np.ndarray], **kwargs) -> dict:
    stream = io.BytesIO(b"".join(f.astype(np.uint8).tobytes() for f in frames))
    kwargs.setdefault("threshold", 12)
    return analyse(stream, W, H, **kwargs)


def blank() -> np.ndarray:
    return np.full((H, W), BG, dtype=np.uint8)


def with_dot(x: int, y: int, size: int = 5, value: int = 200) -> np.ndarray:
    frame = blank()
    frame[y:y + size, x:x + size] = value
    return frame


def test_frame_alignment_and_metadata():
    frames = [blank() for _ in range(13)]
    result = run(frames)
    assert result["frame_count"] == 13
    assert len(result["active_fraction"]) == 13
    assert result["active_fraction"][0] == 0.0
    # 13 frames at window=6 -> two full windows + a 1-frame trailing window.
    assert [w["f"] for w in result["windows"]] == [0, 6, 12]
    assert result["windows"][-1]["n_frames"] == 1
    assert result["schema"] == 2


def test_static_scene_has_no_blobs():
    result = run([blank() for _ in range(24)])
    assert all(w["blobs"] == [] for w in result["windows"])
    assert result["tracks"] == []


def test_single_frame_noise_rejected():
    # Isolated bright pixels flickering for one frame each never reach
    # min_hits=2, so no blobs appear.
    frames = []
    rng = np.random.default_rng(42)
    for _ in range(24):
        frame = blank()
        ys = rng.integers(0, H, size=5)
        xs = rng.integers(0, W, size=5)
        frame[ys, xs] = 255
        frames.append(frame)
    result = run(frames)
    assert all(w["blobs"] == [] for w in result["windows"])


def test_moving_dot_yields_straight_low_chronic_track():
    # Dot travels left to right, 2 px/frame, across 48 frames.
    frames = [with_dot(4 + 2 * i, 30) for i in range(44)]
    result = run(frames)
    assert result["tracks"], "expected the moving dot to form a track"
    track = max(result["tracks"], key=lambda t: t["n"])
    assert track["n"] >= 5
    assert track["straightness"] > 0.9
    assert track["net"] > 50
    assert track["chronic"] < 0.4
    xs = [p[0] for p in track["path"]]
    assert xs == sorted(xs), "centroid should move monotonically right"


def test_swaying_bar_yields_unstraight_chronic_track():
    # Vertical bar oscillating +/-4 px around x=48 for the whole clip: motion
    # in place, same cells hot in every window.
    frames = []
    for i in range(48):
        frame = blank()
        x = 48 + round(4 * np.sin(i * 0.8))
        frame[10:54, x:x + 3] = 200
        frames.append(frame)
    result = run(frames)
    assert result["tracks"], "expected the swaying bar to form a track"
    track = max(result["tracks"], key=lambda t: t["n"])
    assert track["chronic"] > 0.6
    assert track["net"] < 10, "plant sway should have near-zero net displacement"


def test_nearby_body_parts_merge_into_one_blob():
    # Two 3x3 fragments 6 px apart (wing + body) moving together: cell-grid
    # labelling with 8-connectivity should fuse them into a single blob.
    frames = []
    for i in range(12):
        frame = blank()
        x = 10 + 2 * i
        frame[30:33, x:x + 3] = 200
        frame[30:33, x + 9:x + 12] = 200
        frames.append(frame)
    result = run(frames)
    populated = [w["blobs"] for w in result["windows"] if w["blobs"]]
    assert populated, "expected blobs from the moving fragments"
    assert all(len(blobs) == 1 for blobs in populated), \
        "fragments within a cell of each other should merge into one blob"


def test_distant_blobs_stay_separate_and_track_independently():
    frames = [blank() for _ in range(2)]
    for i in range(36):
        frame = with_dot(4 + 2 * i, 10)          # travels right along the top
        frame[50:55, 8:13] = 200                  # second, stationary-ish dot
        frame[50:55, 8:13] += (i % 2) * 30        # flickers so it stays "moving"
        frames.append(frame)
    result = run(frames)
    multi = [w for w in result["windows"] if len(w["blobs"]) >= 2]
    assert multi, "expected windows with two separate blobs"


def _blob(x: float, y: float) -> dict:
    return {"c": [x, y], "area": 5, "chronic": 0.0}


def test_velocity_threshold_rejects_teleport_to_unrelated_blob():
    # A track crawling at 5 px/window, then an unrelated blob spawns 70 px
    # away next window -- within max_link_dist (80) but far faster than the
    # track's established 5 px/window speed plus max_accel (40). It should
    # not be linked into the crawler's track, splitting into two tracks with
    # no single bogus long jump inside either one.
    windows = [
        {"blobs": [_blob(10, 10)]},
        {"blobs": [_blob(15, 10)]},
        {"blobs": [_blob(20, 10)]},
        {"blobs": [_blob(90, 10)]},   # 70 px from (20, 10): unrelated blob
        {"blobs": [_blob(95, 10)]},
        {"blobs": [_blob(100, 10)]},
    ]
    tracks = _link_tracks(windows, max_dist=80.0, min_track_len=3, max_accel=40.0)
    assert len(tracks) == 2, "the teleporting jump should split into two tracks"
    for track in tracks:
        hops = [math.dist(track["path"][i], track["path"][i + 1])
                for i in range(len(track["path"]) - 1)]
        assert max(hops) < 10, f"no hop should include the 70px teleport: {hops}"


def test_velocity_threshold_allows_genuine_acceleration():
    # A track speeding up hop-to-hop within max_accel should still link
    # into one continuous track, not get cut off.
    windows = [
        {"blobs": [_blob(0, 10)]},
        {"blobs": [_blob(5, 10)]},    # v = 5
        {"blobs": [_blob(20, 10)]},   # v = 15 (+10, well under max_accel=40)
        {"blobs": [_blob(50, 10)]},   # v = 30 (+15)
    ]
    tracks = _link_tracks(windows, max_dist=80.0, min_track_len=3, max_accel=40.0)
    assert len(tracks) == 1
    assert tracks[0]["n"] == 4


def test_slow_crawler_visible_via_background_subtraction():
    # 1 px every 3 frames: nearly invisible to consecutive-frame differencing,
    # but clear against the EMA background.
    frames = [with_dot(10 + i // 3, 30) for i in range(48)]
    result = run(frames)
    populated = [w for w in result["windows"] if w["blobs"]]
    assert len(populated) >= 6, "slow mover should still produce blobs"
