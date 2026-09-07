"""Tests for camrig.bucket's rclone command builders and host resolution."""

from pathlib import Path

import pytest

from camrig import bucket
from camrig.config import Config


def _cfg() -> Config:
    cfg = Config()
    cfg.upload.rclone_remote = "r2"
    cfg.upload.bucket = "spaia-cam"
    return cfg


def test_build_fetch_command_filters_to_clip_family_only():
    cmd = bucket.build_fetch_command(_cfg(), "pi-rig-01", "2026-09-01", "clip_a.mkv", Path("/tmp/dest"))

    assert cmd[:3] == ["rclone", "copy", "r2:spaia-cam/pi-rig-01/2026-09-01"]
    assert "/tmp/dest" in cmd
    assert "+ clip_a.*" in cmd
    assert "- *" in cmd


def test_build_push_sidecars_command_excludes_the_video():
    cmd = bucket.build_push_sidecars_command(_cfg(), "pi-rig-01", "2026-09-01", "clip_a.mkv", Path("/tmp/dest"))

    assert cmd[:3] == ["rclone", "copy", "/tmp/dest"]
    assert cmd[3] == "r2:spaia-cam/pi-rig-01/2026-09-01"
    assert "+ clip_a.preview.mp4" in cmd
    assert "+ clip_a.motion.json" in cmd
    assert not any("clip_a.mkv" in arg for arg in cmd)


def test_resolve_host_returns_given_host_without_listing():
    assert bucket.resolve_host(_cfg(), "pi-rig-01") == "pi-rig-01"


def test_resolve_host_auto_detects_single_host(monkeypatch):
    monkeypatch.setattr(bucket, "list_hosts", lambda cfg: ["pi-rig-01"])
    assert bucket.resolve_host(_cfg(), None) == "pi-rig-01"


def test_resolve_host_raises_when_ambiguous(monkeypatch):
    monkeypatch.setattr(bucket, "list_hosts", lambda cfg: ["pi-rig-01", "pi-rig-02"])
    with pytest.raises(RuntimeError, match="pi-rig-01"):
        bucket.resolve_host(_cfg(), None)


def test_resolve_host_raises_when_empty(monkeypatch):
    monkeypatch.setattr(bucket, "list_hosts", lambda cfg: [])
    with pytest.raises(RuntimeError, match="No hosts"):
        bucket.resolve_host(_cfg(), None)
