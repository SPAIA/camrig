"""Tests for camrig.readings: reading body, queueing, and send outcomes."""

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from camrig import readings
from camrig.config import CaptureConfig, Config, PostprocessConfig
from camrig.motion import SCHEMA


def _track(w0, straightness=0.9, chronic=0.01, n=3):
    return {"w0": w0, "n": n, "path": [[10 * i, 50] for i in range(n)],
            "straightness": straightness, "chronic": chronic, "mean_area": 5.0,
            "footprint_ratio": 10.0, "step_ratio": 1.0}


def _write_clip(video: Path, tracks: list[dict]) -> None:
    motion = {
        "schema": SCHEMA, "analysis": "blob-track-v1", "width": 100, "height": 100,
        "params": {"window": 6}, "frame_count": 600,
        "windows": [{"f": i * 6, "n_frames": 6, "blobs": []} for i in range(100)],
        "tracks": tracks,
    }
    video.with_suffix(".motion.json").write_text(json.dumps(motion), encoding="utf-8")
    video.with_suffix(".json").write_text(
        json.dumps({"started_at_utc": "2026-10-01T10:00:00.123456+02:00"}), encoding="utf-8")


def _cfg(url: str = "http://127.0.0.1:9/") -> Config:
    cfg = Config()
    cfg.capture = CaptureConfig(framerate=60.0)
    cfg.postprocess = PostprocessConfig(min_straightness=0.5, max_chronic=0.1)
    cfg.readings.url = url
    cfg.readings.token = "secret"
    cfg.cloud.device_id = "rig-7"
    return cfg


def test_build_reading_counts_only_filter_survivors(tmp_path):
    video = tmp_path / "clip.mkv"
    _write_clip(video, [_track(0), _track(20, straightness=0.1), _track(40)])

    reading = readings.build_reading(_cfg(), video)

    assert reading["deviceId"] == "rig-7"
    assert reading["spotId"] == 22
    assert reading["insectCount"] == 2 == len(reading["trails"])
    assert reading["startTime"] == "2026-10-01T08:00:00.123Z"
    assert reading["endTime"] == "2026-10-01T08:00:10.123Z"  # 600 frames @ 60fps
    trail = reading["trails"][0]
    assert trail["startTime"] == "2026-10-01T08:00:00.173Z"  # first window midpoint
    assert trail["path"][1] == [0.15, 0.1, 0.5]


class _Server:
    """Local stand-in for the readings endpoint, replying with ``status``."""

    def __init__(self, status: int):
        self.requests: list[tuple[dict, dict]] = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                body = self.rfile.read(int(self.headers["Content-Length"]))
                outer.requests.append((dict(self.headers), json.loads(body)))
                self.send_response(outer.status)
                self.end_headers()
                self.wfile.write(b'{"ok": true}')

            def log_message(self, *args):
                pass

        self.status = status
        self.httpd = HTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.httpd.server_port}/api/device/readings"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()


@pytest.fixture
def server():
    servers = []

    def make(status):
        servers.append(_Server(status))
        return servers[-1]
    yield make
    for s in servers:
        s.close()


def _queue_one(tmp_path, cfg) -> Path:
    video = tmp_path / "day" / "clip.mkv"
    video.parent.mkdir()
    _write_clip(video, [_track(0)])
    assert readings.enqueue(cfg, tmp_path, video)
    return readings.pending_dir(tmp_path) / "clip.json"


def test_flush_sends_and_dequeues_on_201(tmp_path, server):
    srv = server(201)
    cfg = _cfg(srv.url)
    queued = _queue_one(tmp_path, cfg)

    assert readings.flush(cfg, tmp_path) == readings.SENT
    assert not queued.exists()
    headers, body = srv.requests[0]
    assert headers["Authorization"] == "Bearer secret"
    assert headers["Content-Type"] == "application/json"
    assert headers["User-Agent"].startswith("camrig/")
    assert body["insectCount"] == 1
    assert readings.flush(cfg, tmp_path) == readings.EMPTY


def test_flush_moves_rejected_reading_aside(tmp_path, server):
    srv = server(400)
    cfg = _cfg(srv.url)
    queued = _queue_one(tmp_path, cfg)

    assert readings.flush(cfg, tmp_path) == readings.SENT
    assert not queued.exists()
    assert (readings.failed_dir(tmp_path) / "clip.json").exists()
    assert "HTTP 400" in (readings.failed_dir(tmp_path) / "clip.response.txt").read_text()


@pytest.mark.parametrize("status", [500, 503, 403, 429])
def test_flush_keeps_reading_queued_on_server_error(tmp_path, server, status):
    cfg = _cfg(server(status).url)
    queued = _queue_one(tmp_path, cfg)

    assert readings.flush(cfg, tmp_path) == readings.RETRY
    assert queued.exists()


def test_flush_keeps_reading_queued_when_offline(tmp_path):
    cfg = _cfg("http://127.0.0.1:9/")  # discard port: connection refused
    queued = _queue_one(tmp_path, cfg)

    before = queued.read_bytes()
    assert readings.flush(cfg, tmp_path) == readings.RETRY
    assert queued.read_bytes() == before  # same startTime on every retry
