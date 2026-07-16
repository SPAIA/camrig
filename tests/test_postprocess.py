"""Tests for detector selection and knob plumbing through postprocess/config.

Behaviour is code x knobs, so these cover both axes: that the configured
detector and its knobs reach the motion subprocess, and that an unusable
detector or knob is rejected at config load rather than after a clip has been
decoded.
"""

from pathlib import Path

import pytest

from camrig.config import Config, load_config
from camrig.postprocess import build_commands


def write_config(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "config.toml"
    path.write_text(body, encoding="utf-8")
    return path


def param_args(motion: list[str]) -> dict[str, str]:
    return dict(
        motion[i + 1].split("=", 1)
        for i, arg in enumerate(motion) if arg == "--param"
    )


def test_build_commands_selects_configured_detector():
    cfg = Config()
    cfg.postprocess.motion_detector = "blob-track-v1"

    _, motion = build_commands(cfg, Path("/tmp/clip.mkv"))

    assert motion[motion.index("--detector") + 1] == "blob-track-v1"


def test_configured_knobs_reach_the_motion_command():
    cfg = Config()
    cfg.postprocess.motion_params = {"blob-track-v1": {"bg_alpha": 0.02, "cell": 4}}

    _, motion = build_commands(cfg, Path("/tmp/clip.mkv"))

    assert param_args(motion) == {"bg_alpha": "0.02", "cell": "4"}


def test_knobs_for_other_detectors_are_not_passed():
    # Knob tables for inactive detectors stay in the file untouched, so a
    # comparison run can switch detector without re-tuning from scratch.
    cfg = Config()
    cfg.postprocess.motion_detector = "blob-track-v1"
    cfg.postprocess.motion_params = {
        "blob-track-v1": {"cell": 4},
        "some-future-v9": {"cell": 99},
    }

    _, motion = build_commands(cfg, Path("/tmp/clip.mkv"))

    assert param_args(motion) == {"cell": "4"}


def test_no_configured_knobs_passes_no_params():
    _, motion = build_commands(Config(), Path("/tmp/clip.mkv"))

    assert "--param" not in motion


def test_config_loads_detector_and_knobs(tmp_path):
    path = write_config(tmp_path, """
[postprocess]
motion_detector = "blob-track-v1"

[postprocess.motion_params."blob-track-v1"]
bg_alpha = 0.02
""")

    cfg = load_config(path)

    assert cfg.postprocess.motion_detector == "blob-track-v1"
    assert cfg.postprocess.active_motion_params() == {"bg_alpha": 0.02}


def test_unknown_detector_is_rejected_at_load(tmp_path):
    path = write_config(tmp_path, '[postprocess]\nmotion_detector = "nope-v9"\n')

    with pytest.raises(ValueError, match="blob-track-v1"):
        load_config(path)


def test_unknown_knob_is_rejected_at_load(tmp_path):
    path = write_config(tmp_path, """
[postprocess.motion_params."blob-track-v1"]
bg_alpah = 0.02
""")

    with pytest.raises(ValueError, match="bg_alpah"):
        load_config(path)


def test_uncoercible_knob_is_rejected_at_load(tmp_path):
    path = write_config(tmp_path, """
[postprocess.motion_params."blob-track-v1"]
cell = 8.5
""")

    with pytest.raises(ValueError, match="whole number"):
        load_config(path)


def test_retired_motion_threshold_key_is_rejected(tmp_path):
    # Silently ignoring it would revert a tuned threshold to the default.
    path = write_config(tmp_path, "[postprocess]\nmotion_threshold = 30\n")

    with pytest.raises(ValueError, match="has moved"):
        load_config(path)


def test_missing_config_file_still_loads_defaults(tmp_path):
    cfg = load_config(tmp_path / "absent.toml")

    assert cfg.postprocess.motion_detector == "blob-track-v1"
    assert cfg.postprocess.active_motion_params() == {}
