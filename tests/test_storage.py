from __future__ import annotations

import os
import tempfile
import time
import unittest
from pathlib import Path

from camrig.storage import sweep_partials


def _age(path: Path, seconds: int) -> None:
    stamp = time.time() - seconds
    os.utime(path, (stamp, stamp))


def _write_family(day: Path, stem: str, *suffixes: str, age: int = 3600) -> list[Path]:
    parts = []
    for suffix in suffixes:
        part = day / f"{stem}{suffix}.part"
        part.write_text(part.name, encoding="utf-8")
        _age(part, age)
        parts.append(part)
    return parts


class SweepPartialsTests(unittest.TestCase):
    def test_salvages_complete_family_and_deletes_incomplete(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            day = Path(tmp) / "2026-07-13"
            day.mkdir()
            _write_family(day, "clip_a", ".mkv", ".pts", ".json")
            _write_family(day, "clip_b", ".mkv", ".json")  # no pts: incomplete

            touched = sweep_partials(Path(tmp))

            self.assertEqual(touched, 2)
            for suffix in (".mkv", ".pts", ".json"):
                self.assertTrue((day / f"clip_a{suffix}").exists())
            self.assertEqual(list(day.glob("clip_b*")), [])
            self.assertEqual(list(day.glob("*.part")), [])

    def test_skips_recent_families(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            day = Path(tmp) / "2026-07-13"
            day.mkdir()
            parts = _write_family(day, "clip_live", ".mkv", ".pts", ".json", age=0)

            touched = sweep_partials(Path(tmp))

            self.assertEqual(touched, 0)
            for part in parts:
                self.assertTrue(part.exists())

    def test_deletes_orphaned_postprocess_outputs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            day = Path(tmp) / "2026-07-13"
            day.mkdir()
            # Finished clip whose postprocess died mid-write.
            (day / "clip_c.mkv").write_bytes(b"video")
            _write_family(day, "clip_c", ".preview.mp4", ".motion.json")

            sweep_partials(Path(tmp))

            self.assertTrue((day / "clip_c.mkv").exists())
            self.assertEqual(list(day.glob("*.part")), [])

    def test_dry_run_changes_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            day = Path(tmp) / "2026-07-13"
            day.mkdir()
            parts = _write_family(day, "clip_d", ".mkv", ".pts", ".json")

            touched = sweep_partials(Path(tmp), dry_run=True)

            self.assertEqual(touched, 1)
            for part in parts:
                self.assertTrue(part.exists())


if __name__ == "__main__":
    unittest.main()
