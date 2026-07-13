from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import Mock

from camrig.record import ClipPaths, Recording, clip_paths


def _family(root: Path) -> ClipPaths:
    return ClipPaths(root / "clip.mkv", root / "clip.pts", root / "clip.json")


class ClipPathsTests(unittest.TestCase):
    def test_clip_names_include_milliseconds(self) -> None:
        root = Path("/tmp")
        first = clip_paths(
            root, "mjpeg", datetime(2026, 7, 13, 12, 0, 0, 123000, tzinfo=timezone.utc)
        )
        second = clip_paths(
            root, "mjpeg", datetime(2026, 7, 13, 12, 0, 0, 124000, tzinfo=timezone.utc)
        )
        self.assertNotEqual(first.video, second.video)
        self.assertEqual(first.video.name, "clip_20260713_120000_123.mkv")

    def test_finalize_renames_all_and_removes_part_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            final = _family(Path(tmp))
            partial = final.in_progress()
            for path in (partial.video, partial.pts, partial.meta):
                path.write_text(path.name, encoding="utf-8")

            final.finalize_from(partial)

            for path in (final.video, final.pts, final.meta):
                self.assertTrue(path.exists())
            for path in (partial.video, partial.pts, partial.meta):
                self.assertFalse(path.exists())

    def test_finalize_refuses_incomplete_capture(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            final = _family(Path(tmp))
            partial = final.in_progress()
            partial.video.write_bytes(b"video")
            partial.meta.write_text("{}", encoding="utf-8")
            # .pts missing: nothing may be renamed, not even the files present.

            with self.assertRaises(FileNotFoundError):
                final.finalize_from(partial)

            self.assertFalse(final.video.exists())
            self.assertTrue(partial.video.exists())


class RecordingWaitTests(unittest.TestCase):
    def _recording(self, *returncodes: int) -> Recording:
        recording = Recording([], _family(Path(".")))
        procs = []
        for rc in returncodes:
            proc = Mock()
            proc.wait.return_value = rc
            procs.append(proc)
        recording._procs = procs
        return recording

    def test_producer_failure_not_masked_by_clean_consumer(self) -> None:
        self.assertEqual(self._recording(3, 0).wait(), 3)

    def test_consumer_failure_reported(self) -> None:
        self.assertEqual(self._recording(0, 1).wait(), 1)

    def test_all_clean_returns_zero(self) -> None:
        self.assertEqual(self._recording(0, 0).wait(), 0)


if __name__ == "__main__":
    unittest.main()
