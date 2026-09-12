from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import Mock, patch

from camrig.config import BaslerConfig, CaptureConfig
from camrig.record import ClipPaths, Recording, build_commands, clip_paths, resolve_auto_lock


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


class AutoLockTests(unittest.TestCase):
    def test_basler_build_commands_passes_auto_lock_flags(self) -> None:
        cfg = CaptureConfig(camera="basler", profile="raw", auto_lock=True,
                             auto_lock_warmup_ms=1500, shutter_us=0, gain=0.0)
        paths = _family(Path("/tmp"))
        [producer] = build_commands(cfg, paths, 1000, basler=BaslerConfig())
        self.assertIn("--auto-lock", producer)
        i = producer.index("--auto-lock-timeout-ms")
        self.assertEqual(producer[i + 1], "1500")

    def test_resolve_auto_lock_is_noop_for_basler(self) -> None:
        cfg = CaptureConfig(camera="basler", auto_lock=True, shutter_us=0, gain=0.0)
        self.assertIs(resolve_auto_lock(cfg), cfg)

    def test_resolve_auto_lock_is_noop_when_disabled(self) -> None:
        cfg = CaptureConfig(camera="rpicam", auto_lock=False, shutter_us=0, gain=0.0)
        self.assertIs(resolve_auto_lock(cfg), cfg)

    def test_resolve_auto_lock_is_noop_when_both_manual(self) -> None:
        cfg = CaptureConfig(camera="rpicam", auto_lock=True, shutter_us=2000, gain=4.0)
        self.assertIs(resolve_auto_lock(cfg), cfg)

    def test_resolve_auto_lock_probes_rpicam_metadata(self) -> None:
        cfg = CaptureConfig(camera="rpicam", auto_lock=True, shutter_us=0, gain=0.0)

        def fake_run(args, **kwargs):
            meta_path = Path(args[args.index("--metadata") + 1])
            meta_path.write_text(
                json.dumps({"ExposureTime": 8234.0, "AnalogueGain": 3.5}),
                encoding="utf-8",
            )
            return Mock(returncode=0)

        with patch("camrig.record.subprocess.run", side_effect=fake_run) as run:
            resolved = resolve_auto_lock(cfg)

        run.assert_called_once()
        self.assertEqual(resolved.shutter_us, 8234)
        self.assertEqual(resolved.gain, 3.5)
        # Original cfg is untouched; only the resolved copy carries the probe.
        self.assertEqual(cfg.shutter_us, 0)

    def test_resolve_auto_lock_keeps_manual_channel_fixed(self) -> None:
        cfg = CaptureConfig(camera="rpicam", auto_lock=True, shutter_us=2000, gain=0.0)

        def fake_run(args, **kwargs):
            self.assertIn("--shutter", args)
            meta_path = Path(args[args.index("--metadata") + 1])
            meta_path.write_text(
                json.dumps({"ExposureTime": 2000.0, "AnalogueGain": 6.0}),
                encoding="utf-8",
            )
            return Mock(returncode=0)

        with patch("camrig.record.subprocess.run", side_effect=fake_run):
            resolved = resolve_auto_lock(cfg)

        self.assertEqual(resolved.shutter_us, 2000)  # unchanged, was manual
        self.assertEqual(resolved.gain, 6.0)  # probed, was auto


class RpicamAfTests(unittest.TestCase):
    def test_build_commands_uses_camera_index_1(self) -> None:
        cfg = CaptureConfig(camera="rpicam-af", profile="mjpeg", lens_position=5.0)
        paths = _family(Path("/tmp"))
        [rpicam, _ffmpeg] = build_commands(cfg, paths, 1000)
        i = rpicam.index("--camera")
        self.assertEqual(rpicam[i + 1], "1")

    def test_build_commands_pins_resolved_lens_position(self) -> None:
        cfg = CaptureConfig(camera="rpicam-af", profile="raw", lens_position=7.5)
        paths = _family(Path("/tmp"))
        [rpicam] = build_commands(cfg, paths, 1000)
        self.assertIn("--autofocus-mode", rpicam)
        i = rpicam.index("--lens-position")
        self.assertEqual(rpicam[i + 1], "7.5")

    def test_build_commands_omits_af_flags_when_unresolved(self) -> None:
        cfg = CaptureConfig(camera="rpicam-af", profile="mjpeg", lens_position=0.0)
        paths = _family(Path("/tmp"))
        [rpicam, _ffmpeg] = build_commands(cfg, paths, 1000)
        self.assertNotIn("--autofocus-mode", rpicam)
        self.assertNotIn("--lens-position", rpicam)

    def test_resolve_auto_lock_always_focuses_rpicam_af(self) -> None:
        """Autofocus runs before every clip regardless of auto_lock."""
        cfg = CaptureConfig(camera="rpicam-af", auto_lock=False,
                             shutter_us=2000, gain=4.0, lens_position=0.0)

        def fake_run(args, **kwargs):
            self.assertIn("--autofocus-mode", args)
            meta_path = Path(args[args.index("--metadata") + 1])
            meta_path.write_text(json.dumps({"LensPosition": 3.25}), encoding="utf-8")
            return Mock(returncode=0)

        with patch("camrig.record.subprocess.run", side_effect=fake_run) as run:
            resolved = resolve_auto_lock(cfg)

        run.assert_called_once()
        self.assertEqual(resolved.lens_position, 3.25)
        # Manual exposure untouched: auto_lock was off.
        self.assertEqual(resolved.shutter_us, 2000)
        self.assertEqual(resolved.gain, 4.0)

    def test_resolve_auto_lock_is_noop_when_lens_position_manual(self) -> None:
        cfg = CaptureConfig(camera="rpicam-af", lens_position=2.0)
        self.assertIs(resolve_auto_lock(cfg), cfg)

    def test_resolve_auto_lock_combines_exposure_and_focus_probe(self) -> None:
        """One warm-up capture resolves both when auto_lock and AF both apply."""
        cfg = CaptureConfig(camera="rpicam-af", auto_lock=True, shutter_us=0, gain=0.0,
                             lens_position=0.0, auto_lock_warmup_ms=1000,
                             autofocus_warmup_ms=2500)

        def fake_run(args, **kwargs):
            self.assertIn("--autofocus-mode", args)
            i = args.index("--timeout")
            self.assertEqual(args[i + 1], "2500")  # max(1000, 2500)
            meta_path = Path(args[args.index("--metadata") + 1])
            meta_path.write_text(
                json.dumps({"ExposureTime": 5000.0, "AnalogueGain": 2.0,
                            "LensPosition": 1.5}),
                encoding="utf-8",
            )
            return Mock(returncode=0)

        with patch("camrig.record.subprocess.run", side_effect=fake_run) as run:
            resolved = resolve_auto_lock(cfg)

        run.assert_called_once()
        self.assertEqual(resolved.shutter_us, 5000)
        self.assertEqual(resolved.gain, 2.0)
        self.assertEqual(resolved.lens_position, 1.5)


if __name__ == "__main__":
    unittest.main()
