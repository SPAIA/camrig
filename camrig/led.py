"""Onboard status LED control (Pi activity LED via sysfs).

Two independent uses of the same physical LED, serialised against each
other so they never fight over brightness state:

* flash() -- a brief "about to record" cue right before each capture.
* heartbeat() -- a steady pulse for as long as the supervisor process is
  alive, so a rig that still has power but has crashed or hung is visually
  distinguishable from one that's actually running -- see README.md's
  Crash resilience section for the failure mode this closes.

Different Pi models/kernels expose the activity LED under different names
in /sys/class/leds, and writing to it needs root (or a udev rule granting
the service user access, see setup/set_led_perms.sh) -- both cases degrade
to a logged no-op rather than failing the capture.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from pathlib import Path

log = logging.getLogger("camrig.led")

_CANDIDATE_LEDS = ["ACT", "led0", "PWR", "led1"]

# Guards brightness writes so flash()'s multi-write cue and heartbeat()'s
# steady pulse -- running on different threads/tasks -- never interleave.
_lock = threading.Lock()


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


def _try_write(path: Path, value: str) -> bool:
    try:
        path.write_text(value)
        return True
    except OSError:
        return False


def flash(times: int = 3, *, on_ms: int = 150, off_ms: int = 150) -> None:
    """Flash the onboard activity LED, then restore its previous trigger.

    Best-effort: logs and returns on any failure (LED not found, no
    permission to write sysfs) rather than raising, since a flash cue is
    never worth blocking or failing a recording. Runs off the event loop
    (see supervisor.py's asyncio.to_thread call), so blocking briefly on
    _lock here is harmless.
    """
    if times <= 0:
        return
    led_dir = _find_led_dir()
    if led_dir is None:
        log.debug("No onboard LED found under /sys/class/leds; skipping flash")
        return

    brightness = led_dir / "brightness"
    trigger = led_dir / "trigger"
    with _lock:
        original_trigger = _current_trigger(trigger)
        if not _try_write(trigger, "none\n"):
            log.warning("Could not flash LED at %s (permissions?)", led_dir)
            return
        for _ in range(times):
            _try_write(brightness, "1\n")
            time.sleep(on_ms / 1000)
            _try_write(brightness, "0\n")
            time.sleep(off_ms / 1000)
        if original_trigger is not None:
            _try_write(trigger, f"{original_trigger}\n")


def _pulse_if_free(brightness: Path, value: str) -> None:
    """Write one heartbeat edge, but skip it rather than block if flash()
    currently owns the LED -- the next edge, ~1-2s later, just picks it back
    up, so a skipped pulse is invisible and never worth stalling the event
    loop over."""
    if _lock.acquire(blocking=False):
        try:
            _try_write(brightness, value)
        finally:
            _lock.release()


async def heartbeat(*, interval_seconds: float = 2.0, pulse_ms: int = 100) -> None:
    """Pulse the onboard LED forever as a liveness signal for the supervisor.

    Meant to run as one of the supervisor's gathered tasks for its whole
    lifetime (see Supervisor.run). A rig that's powered but has crashed or
    hung shows a dark or static LED; one that's actually running shows this
    steady pulse -- distinguishing "board has power" from "camrig is
    actually functioning" without needing to SSH in. Best-effort like
    flash(): a missing/unwritable LED just logs once and returns, since the
    LED is a cue, not a dependency of the capture pipeline.
    """
    led_dir = _find_led_dir()
    if led_dir is None:
        log.info("No onboard LED found under /sys/class/leds; heartbeat disabled")
        return

    brightness = led_dir / "brightness"
    trigger = led_dir / "trigger"
    original_trigger = _current_trigger(trigger)
    if not _try_write(trigger, "none\n"):
        log.warning("Could not claim LED at %s for heartbeat (permissions?)", led_dir)
        return

    pulse_s = pulse_ms / 1000
    rest_s = max(0.0, interval_seconds - pulse_s)
    try:
        while True:
            _pulse_if_free(brightness, "1\n")
            await asyncio.sleep(pulse_s)
            _pulse_if_free(brightness, "0\n")
            await asyncio.sleep(rest_s)
    finally:
        if original_trigger is not None:
            _try_write(trigger, f"{original_trigger}\n")
