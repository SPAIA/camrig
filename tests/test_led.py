"""Tests for camrig.led (onboard status LED control).

flash() and heartbeat() share one physical LED via a lock so they never
write brightness out of turn -- these tests pin down the sysfs read/write
sequence and that heartbeat backs off instead of racing an in-progress
flash, rather than exercising real hardware.
"""

import asyncio
from pathlib import Path

from camrig import led


def _fake_led(tmp_path: Path, trigger_options: str = "none [mmc0] heartbeat") -> Path:
    led_dir = tmp_path / "ACT"
    led_dir.mkdir()
    (led_dir / "brightness").write_text("0\n")
    (led_dir / "trigger").write_text(trigger_options)
    return led_dir


def test_flash_restores_original_trigger(tmp_path, monkeypatch):
    led_dir = _fake_led(tmp_path)
    monkeypatch.setattr(led, "_find_led_dir", lambda: led_dir)

    led.flash(times=2, on_ms=1, off_ms=1)

    assert (led_dir / "trigger").read_text() == "mmc0\n"
    assert (led_dir / "brightness").read_text() == "0\n"


def test_flash_noop_without_led(monkeypatch):
    monkeypatch.setattr(led, "_find_led_dir", lambda: None)
    led.flash()  # must not raise or touch the filesystem


def test_heartbeat_claims_none_trigger_and_restores_on_cancel(tmp_path, monkeypatch):
    led_dir = _fake_led(tmp_path)
    monkeypatch.setattr(led, "_find_led_dir", lambda: led_dir)

    async def run_briefly():
        task = asyncio.create_task(led.heartbeat(interval_seconds=0.02, pulse_ms=5))
        await asyncio.sleep(0.08)
        # While running, heartbeat owns the trigger as "none" (manual control).
        assert (led_dir / "trigger").read_text() == "none\n"
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(run_briefly())

    # Cancellation must still restore whatever trigger was active before.
    assert (led_dir / "trigger").read_text() == "mmc0\n"


def test_heartbeat_skips_pulse_when_flash_holds_the_lock(tmp_path, monkeypatch):
    led_dir = _fake_led(tmp_path)
    monkeypatch.setattr(led, "_find_led_dir", lambda: led_dir)
    brightness = led_dir / "brightness"

    led._lock.acquire()  # simulate flash() mid-sequence
    try:
        led._pulse_if_free(brightness, "1\n")
        # Skipped, not blocked: brightness untouched while the lock is held.
        assert brightness.read_text() == "0\n"
    finally:
        led._lock.release()

    led._pulse_if_free(brightness, "1\n")
    assert brightness.read_text() == "1\n"
