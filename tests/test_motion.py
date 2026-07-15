"""Tests for camrig.motion blob detection and track linking.

Synthetic gray8 clips exercise the discriminators the analysis is built
around: a travelling dot (insect-like: high straightness, low chronic
activity), a swaying bar (plant-like: low straightness, high chronic
activity), split body parts merging into one blob, and noise rejection.
"""

import io

import numpy as np
import pytest

from camrig.motion import (
    BLOB_TRACK_V1,
    DEFAULT_DETECTOR,
    analyse,
    analyse_blob_track_v1,
    available_detectors,
)

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
    assert result["analysis"] == BLOB_TRACK_V1


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


def test_slow_crawler_visible_via_background_subtraction():
    # 1 px every 3 frames: nearly invisible to consecutive-frame differencing,
    # but clear against the EMA background.
    frames = [with_dot(10 + i // 3, 30) for i in range(48)]
    result = run(frames)
    populated = [w for w in result["windows"] if w["blobs"]]
    assert len(populated) >= 6, "slow mover should still produce blobs"


def test_detector_registry_routes_to_immutable_v1_entry_point():
    frames = [blank(), with_dot(10, 20), with_dot(12, 20)]
    payload = b"".join(f.tobytes() for f in frames)

    direct = analyse_blob_track_v1(io.BytesIO(payload), W, H, threshold=12)
    routed = analyse(
        io.BytesIO(payload), W, H, threshold=12, detector=DEFAULT_DETECTOR
    )

    assert available_detectors() == (BLOB_TRACK_V1,)
    assert routed == direct


def test_unknown_detector_is_rejected_before_processing():
    with pytest.raises(ValueError, match="Unknown detector"):
        run([blank()], detector="blob-track-v999")
