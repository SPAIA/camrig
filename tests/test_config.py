"""Tests for camrig.config's TOML value patcher.

set_config_value() is a targeted line patch, not a full TOML writer -- these
pin down that it never disturbs comments or unrelated sections, since it's
used to persist tuning values into the same config.toml that's hand-edited
for capture/upload/storage settings.
"""

from pathlib import Path

from camrig.config import set_config_value

SAMPLE = """\
# top comment
[capture]
width = 1456

[postprocess]
# comment above the key
motion_threshold = 12
nice = 10

[storage]
keep_days = 2
"""


def test_updates_existing_key_in_place(tmp_path: Path):
    path = tmp_path / "config.toml"
    path.write_text(SAMPLE, encoding="utf-8")
    set_config_value(path, "postprocess", "nice", "5")
    text = path.read_text(encoding="utf-8")
    assert "nice = 5\n" in text
    assert "nice = 10\n" not in text
    # everything else untouched
    assert "# comment above the key\n" in text
    assert "motion_threshold = 12\n" in text
    assert "[storage]\nkeep_days = 2\n" in text


def test_appends_missing_key_to_existing_section(tmp_path: Path):
    path = tmp_path / "config.toml"
    path.write_text(SAMPLE, encoding="utf-8")
    set_config_value(path, "postprocess", "min_straightness", "0.5")
    text = path.read_text(encoding="utf-8")
    assert "min_straightness = 0.5\n" in text
    # inserted before the next section, not after it
    pp = text.index("[postprocess]")
    storage = text.index("[storage]")
    assert pp < text.index("min_straightness = 0.5") < storage
    assert "nice = 10\n" in text  # existing key untouched


def test_creates_missing_section(tmp_path: Path):
    path = tmp_path / "config.toml"
    path.write_text(SAMPLE, encoding="utf-8")
    set_config_value(path, "debug", "min_straightness", "0.5")
    text = path.read_text(encoding="utf-8")
    assert "[debug]\nmin_straightness = 0.5\n" in text
    assert "[storage]\nkeep_days = 2\n" in text  # unrelated section intact


def test_creates_missing_file():
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "config.toml"
        set_config_value(path, "postprocess", "min_straightness", "0.5")
        text = path.read_text(encoding="utf-8")
        assert "[postprocess]\nmin_straightness = 0.5\n" in text
