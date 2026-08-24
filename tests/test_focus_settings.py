"""Tests for the focus page's /settings form (camrig.focus).

apply_settings_form() is the piece that turns a submitted form dict into
config.toml writes -- these pin down type coercion (int/float/bool/str) and
the checkbox convention (absent = False), since HTML forms never submit an
unchecked checkbox.
"""

from pathlib import Path

from camrig.config import load_config
from camrig.focus import apply_settings_form, render_settings_page

CONFIG_SAMPLE = (Path(__file__).parent.parent / "config" / "config.toml").read_text()


def test_apply_settings_form_writes_typed_values(tmp_path: Path):
    path = tmp_path / "config.toml"
    path.write_text(CONFIG_SAMPLE, encoding="utf-8")

    form = {
        "capture.width": ["2000"],
        "capture.gain": ["4.5"],
        "cloud.device_id": ["rig-02"],
        # every currently-true bool must be resubmitted or it flips off
        "led.enabled": ["1"],
        "captive.enabled": ["1"],
    }
    errors = apply_settings_form(path, form)
    assert errors == []

    cfg = load_config(path)
    assert cfg.capture.width == 2000
    assert cfg.capture.gain == 4.5
    assert cfg.cloud.device_id == "rig-02"
    assert cfg.led.enabled is True
    assert cfg.captive.enabled is True
    # postprocess.enabled defaults True in the sample; omitted -> unchecked -> False
    assert cfg.postprocess.enabled is False


def test_apply_settings_form_reports_invalid_numbers(tmp_path: Path):
    path = tmp_path / "config.toml"
    path.write_text(CONFIG_SAMPLE, encoding="utf-8")

    errors = apply_settings_form(path, {"capture.width": ["not-a-number"]})
    assert any("capture.width" in e for e in errors)
    # the bad field is skipped, original value untouched
    cfg = load_config(path)
    assert cfg.capture.width == 1456


def test_render_settings_page_includes_every_field(tmp_path: Path):
    path = tmp_path / "config.toml"
    path.write_text(CONFIG_SAMPLE, encoding="utf-8")
    cfg = load_config(path)

    html = render_settings_page(cfg).decode("utf-8")
    assert 'name="capture.width"' in html
    assert 'name="upload.enabled"' in html
    assert 'value="1456"' in html
