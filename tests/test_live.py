"""Tests for the live trail viewer's streaming tracker and plumbing (no camera)."""

import io
import json

import numpy as np

from camrig import live
from camrig.config import PostprocessConfig
from camrig.live import EventLog, LiveConfig, LiveSession, LiveTracker, downscale, live_dims

W, H = 96, 64
BG = 20


def blank() -> np.ndarray:
    return np.full((H, W), BG, dtype=np.uint8)


def with_dot(x: int, y: int, size: int = 5, value: int = 200) -> np.ndarray:
    frame = blank()
    frame[y:y + size, x:x + size] = value
    return frame


def feed(tracker: LiveTracker, frames) -> list[dict]:
    return [m for m in (tracker.push(f) for f in frames) if m is not None]


def test_live_dims_avoid_yuv_padding():
    assert live_dims(768, 16 / 9) == (768, 432)
    w, h = live_dims(1000, 4 / 3)
    assert w % 128 == 0 and h % 16 == 0


def test_downscale_block_averages():
    y = np.arange(16, dtype=np.uint8).reshape(4, 4)
    assert downscale(y, 1) is y
    np.testing.assert_allclose(downscale(y, 2), [[2.5, 4.5], [10.5, 12.5]])


def test_moving_dot_becomes_passing_trail():
    tracker = LiveTracker(W, H, LiveConfig(framerate=60), PostprocessConfig())
    msgs = feed(tracker, [blank()] + [with_dot(4 + 2 * i, 30) for i in range(36)])
    last = msgs[-1]
    assert [t["ok"] for t in last["tracks"]] == [True]
    pts = last["tracks"][0]["pts"]
    assert pts[-1][0] > pts[0][0] + 30
    assert last["w"] == len(msgs) - 1


def test_trail_is_trimmed_to_trail_seconds():
    cfg = LiveConfig(framerate=60, trail_seconds=0.3)  # 3 windows of 6 frames
    tracker = LiveTracker(W, H, cfg, PostprocessConfig())
    msgs = feed(tracker, [blank()] + [with_dot(4 + 2 * i, 30) for i in range(36)])
    track = msgs[-1]["tracks"][0]
    assert len(track["pts"]) == 3
    assert track["w0"] == msgs[-1]["w"] - 2


def test_filters_apply_and_chronic_builds_up_for_stationary_motion():
    # A blob flickering in place: its cells stay hot every window, so the
    # running chronic estimate climbs and a max_chronic filter rejects it.
    pp = PostprocessConfig(max_chronic=0.5)
    tracker = LiveTracker(W, H, LiveConfig(framerate=60, chronic_seconds=1.0), pp)
    frames = [blank()] + [with_dot(40 + (i % 2), 30, size=8, value=200 if i % 2 else 120)
                          for i in range(120)]
    msgs = feed(tracker, frames)
    tracks = msgs[-1]["tracks"]
    assert tracks and not any(t["ok"] for t in tracks)
    assert tracker.cell_activity.max() > 0.5


def test_event_log_replays_missed_messages():
    log = EventLog(maxlen=3)
    for i in range(5):
        log.append(str(i).encode())
    assert log.since(3, timeout=0) == (5, [b"3", b"4"])  # message seqs are 1-based
    assert log.since(0, timeout=0) == (5, [b"2", b"3", b"4"])  # older ones dropped
    log.close()
    assert log.since(5, timeout=0) == (5, [])


def test_build_live_commands(monkeypatch):
    monkeypatch.setattr(live, "rpicam_camera_index", lambda camera: "1")
    cam, enc = live.build_live_commands(LiveConfig(lens_position=0.0))
    assert cam[cam.index("--codec") + 1] == "yuv420"
    assert cam[cam.index("--autofocus-mode") + 1] == "auto"
    cam, _ = live.build_live_commands(LiveConfig(lens_position=3.5))
    assert cam[-4:] == ["--autofocus-mode", "manual", "--lens-position", "3.5"]
    cam, _ = live.build_live_commands(LiveConfig(camera="rpicam"))
    assert "--autofocus-mode" not in cam
    assert enc[enc.index("-s") + 1] == "768x432"


def test_session_processes_yuv_stream_into_events():
    cfg = LiveConfig(width=W, height=H, motion_width=W, framerate=60, view_fps=15)
    session = LiveSession(cfg, PostprocessConfig())
    chroma = bytes(W * H // 2)
    frames = [blank()] + [with_dot(4 + 2 * i, 30) for i in range(36)]
    stream = io.BytesIO(b"".join(f.tobytes() + chroma for f in frames))
    session._process(stream)
    seq, items = session.events.since(0, timeout=0)
    assert seq == len(frames) // 6
    msgs = [json.loads(item) for item in items]
    assert any(t["ok"] for t in msgs[-1]["tracks"])
    assert session._raw.closed and session.events.closed
