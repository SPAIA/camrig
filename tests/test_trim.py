"""Tests for camrig.trim (lossless in-place clip cutting) and the
camrig.labels re-timing it triggers.

ffmpeg/ffprobe aren't invoked for real here -- apply_cuts()'s calls through
camrig.trim._run are faked, standing in for "extract segment" / "concat" /
"count frames" without needing the binaries installed. The fake still writes
real files at the paths ffmpeg would have, so the real rename/replace logic
in apply_cuts is exercised end to end.
"""

from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import patch

from camrig import labels, trim
from camrig.pts import FrameClock


class InvertRangesTests(unittest.TestCase):
    def test_no_cuts_keeps_everything(self) -> None:
        self.assertEqual(trim.invert_ranges([], 10), [(0, 10)])

    def test_single_middle_cut(self) -> None:
        self.assertEqual(trim.invert_ranges([(3, 5)], 10), [(0, 3), (5, 10)])

    def test_cut_touching_start_and_end(self) -> None:
        self.assertEqual(trim.invert_ranges([(0, 2), (8, 10)], 10), [(2, 8)])

    def test_overlapping_and_adjacent_cuts_merge(self) -> None:
        self.assertEqual(trim.invert_ranges([(2, 5), (4, 6), (6, 7)], 10), [(0, 2), (7, 10)])

    def test_unsorted_cuts(self) -> None:
        self.assertEqual(trim.invert_ranges([(6, 8), (1, 3)], 10), [(0, 1), (3, 6), (8, 10)])

    def test_cut_beyond_end_is_clamped(self) -> None:
        self.assertEqual(trim.invert_ranges([(8, 100)], 10), [(0, 8)])


class BuildFrameMapTests(unittest.TestCase):
    def test_maps_kept_frames_and_nones_out_cut_ones(self) -> None:
        mapping = trim.build_frame_map([(3, 5)], 10)
        self.assertEqual(mapping, [0, 1, 2, None, None, 3, 4, 5, 6, 7])


class _FakeRunner:
    """Stands in for camrig.trim._run: fakes ffmpeg/ffprobe by name."""

    def __init__(self) -> None:
        self.expected: dict[str, int] = {}

    def __call__(self, cmd: list[str]) -> str:
        if cmd[0] == "ffmpeg":
            out_path = Path(cmd[-1])
            if "-frames:v" in cmd:
                self.expected[str(out_path)] = int(cmd[cmd.index("-frames:v") + 1])
            out_path.write_text("fake-video-bytes")
            return ""
        if cmd[0] == "ffprobe":
            return str(self.expected.get(cmd[-1], 0))
        raise AssertionError(f"unexpected command: {cmd}")


def _write_pts(path: Path, n: int) -> None:
    lines = [trim.PTS_HEADER] + [f"{i * 16.667:.3f}" for i in range(n)]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


class ApplyCutsTests(unittest.TestCase):
    def test_cuts_middle_range_and_shrinks_pts(self) -> None:
        with self._clip(10) as video:
            preview = video.with_suffix(".preview.mp4")
            motion = video.with_suffix(".motion.json")
            preview.write_text("stale-preview")
            motion.write_text("stale-motion")

            with patch("camrig.trim._run", side_effect=_FakeRunner()):
                result = trim.apply_cuts(video, framerate=60.0, cuts=[(3, 5)])

            self.assertEqual(result.frames_before, 10)
            self.assertEqual(result.frames_after, 8)
            self.assertEqual(result.frame_map, [0, 1, 2, None, None, 3, 4, 5, 6, 7])
            self.assertEqual(video.read_text(), "fake-video-bytes")
            _, data = trim.read_pts(video.with_suffix(".pts"))
            self.assertEqual(len(data), 8)
            self.assertFalse(preview.exists())
            self.assertFalse(motion.exists())

    def test_single_keep_range_skips_concat(self) -> None:
        with self._clip(10) as video:
            with patch("camrig.trim._run", side_effect=_FakeRunner()):
                result = trim.apply_cuts(video, framerate=60.0, cuts=[(8, 10)])
            self.assertEqual(result.frames_after, 8)

    def test_frame_count_mismatch_aborts_without_touching_originals(self) -> None:
        with self._clip(10) as video:
            original_video = video.read_text()
            original_pts = video.with_suffix(".pts").read_text()
            runner = _FakeRunner()

            def always_wrong_count(cmd):
                out = runner(cmd)
                return "999" if cmd[0] == "ffprobe" else out

            with patch("camrig.trim._run", side_effect=always_wrong_count):
                with self.assertRaises(RuntimeError):
                    trim.apply_cuts(video, framerate=60.0, cuts=[(3, 5)])

            self.assertEqual(video.read_text(), original_video)
            self.assertEqual(video.with_suffix(".pts").read_text(), original_pts)
            self.assertFalse(Path(f"{video}.part").exists())

    def test_cuts_covering_whole_clip_raise(self) -> None:
        with self._clip(10) as video:
            with self.assertRaises(ValueError):
                trim.apply_cuts(video, framerate=60.0, cuts=[(0, 10)])

    def _clip(self, n_frames: int):
        import tempfile

        class _Ctx:
            def __enter__(inner_self):
                inner_self.tmp = tempfile.TemporaryDirectory()
                root = Path(inner_self.tmp.name)
                video = root / "clip.mkv"
                video.write_text("original-video-bytes")
                _write_pts(video.with_suffix(".pts"), n_frames)
                return video

            def __exit__(inner_self, *exc):
                inner_self.tmp.cleanup()

        return _Ctx()


class RemapLabelsTests(unittest.TestCase):
    def test_survives_when_all_points_kept_and_drops_when_any_point_cut(self) -> None:
        with self._clip() as video:
            fps = 60.0
            labels.append_label(video, {
                "label": "insect", "source_track": 1, "source_analysis": "blob-track-v1",
                "t0": 0 / fps, "t1": 2 / fps,
                "path": [[0.1, 0.1, 0 / fps], [0.2, 0.2, 2 / fps]],
            })
            labels.append_label(video, {
                "label": "other", "source_track": 2, "source_analysis": "blob-track-v1",
                "t0": 3 / fps, "t1": 4 / fps,
                "path": [[0.5, 0.5, 3 / fps], [0.6, 0.6, 4 / fps]],
            })

            frame_map = trim.build_frame_map([(3, 5)], 10)
            clock = FrameClock.constant(fps)
            kept, dropped = labels.remap_labels(video, clock, clock, frame_map)

            self.assertEqual(len(kept), 1)
            self.assertEqual(kept[0]["source_track"], 1)
            self.assertEqual(len(dropped), 1)
            self.assertEqual(dropped[0]["source_track"], 2)
            # Reloading from disk matches what was returned (sidecar rewritten).
            self.assertEqual(labels.load_labels(video), kept)

    def test_retimes_points_after_an_earlier_cut(self) -> None:
        with self._clip() as video:
            fps = 60.0
            labels.append_label(video, {
                "label": "insect", "source_track": 9, "source_analysis": "blob-track-v1",
                "t0": 6 / fps, "t1": 7 / fps,
                "path": [[0.1, 0.1, 6 / fps], [0.2, 0.2, 7 / fps]],
            })
            frame_map = trim.build_frame_map([(3, 5)], 10)  # frames 6,7 -> 4,5

            clock = FrameClock.constant(fps)
            kept, _ = labels.remap_labels(video, clock, clock, frame_map)

            self.assertEqual(kept[0]["path"][0][2], round(4 / fps, 3))
            self.assertEqual(kept[0]["path"][1][2], round(5 / fps, 3))
            self.assertEqual(kept[0]["t0"], round(4 / fps, 3))
            self.assertEqual(kept[0]["t1"], round(5 / fps, 3))

    def _clip(self):
        import tempfile

        class _Ctx:
            def __enter__(inner_self):
                inner_self.tmp = tempfile.TemporaryDirectory()
                inner_self.video = Path(inner_self.tmp.name) / "clip.mkv"
                return inner_self.video

            def __exit__(inner_self, *exc):
                inner_self.tmp.cleanup()

        return _Ctx()


if __name__ == "__main__":
    unittest.main()
