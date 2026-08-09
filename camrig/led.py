"""Onboard status LED control (Pi activity LED via sysfs).

Used to flash the LED a few times before each capture starts, as a visible
cue to anyone near the rig. Different Pi models/kernels expose the activity
LED under different names in /sys/class/leds, and writing to it needs root
(or a udev rule granting the service user access) — both cases degrade to a
logged no-op rather than failing the capture.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path

log = logging.getLogger("camrig.led")

_CANDIDATE_LEDS = ["ACT", "led0", "PWR", "led1"]


def _find_led_dir() -> Path | None:
    base = Path("/sys/class/leds")
    for name in _CANDIDATE_LEDS:
        d = base / name
        if d.is_dir():
            return d
    return None


def _current_trigger(trigger_path: Path) -> str | None:
    """Pull the active trigger name out of sysfs's bracketed-list format."""
    try:
        content = trigger_path.read_text()
    except OSError:
        return None
    for word in content.split():
        if word.startswith("[") and word.endswith("]"):
            return word[1:-1]
    return None


def flash(times: int = 3, *, on_ms: int = 150, off_ms: int = 150) -> None:
    """Flash the onboard activity LED, then restore its previous trigger.

    Best-effort: logs and returns on any failure (LED not found, no
    permission to write sysfs) rather than raising, since a flash cue is
    never worth blocking or failing a recording.
    """
    if times <= 0:
        return
    led_dir = _find_led_dir()
    if led_dir is None:
        log.debug("No onboard LED found under /sys/class/leds; skipping flash")
        return

    brightness = led_dir / "brightness"
    trigger = led_dir / "trigger"
    original_trigger = _current_trigger(trigger)
    try:
        trigger.write_text("none\n")
        for _ in range(times):
            brightness.write_text("1\n")
            time.sleep(on_ms / 1000)
            brightness.write_text("0\n")
            time.sleep(off_ms / 1000)
    except OSError:
        log.warning("Could not flash LED at %s (permissions?)", led_dir, exc_info=True)
    finally:
        if original_trigger is not None:
            try:
                trigger.write_text(f"{original_trigger}\n")
            except OSError:
                pass
