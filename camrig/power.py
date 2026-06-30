"""Power management: schedule the Pi 5 RTC wake alarm and power off.

The Pi 5 has a built-in RTC that can re-power the board from a halted state,
provided the EEPROM has ``POWER_OFF_ON_HALT=1`` (set by setup/set_eeprom.sh).
We compute the next wake datetime, program /sys/class/rtc/rtc0/wakealarm with an
absolute epoch, then power off. Requires root (the cam-shutdown unit runs as root).
"""

from __future__ import annotations

import logging
import subprocess
from datetime import datetime, timedelta
from pathlib import Path

log = logging.getLogger("camrig.power")

WAKEALARM = Path("/sys/class/rtc/rtc0/wakealarm")


def next_wake_time(wake_hour: int, now: datetime | None = None) -> datetime:
    """Return the next occurrence of wake_hour:00 local time, strictly future."""
    now = now or datetime.now().astimezone()
    target = now.replace(hour=wake_hour, minute=0, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return target


def set_wake_alarm(wake_at: datetime, *, dry_run: bool = False) -> None:
    """Program the RTC wake alarm for an absolute local time."""
    epoch = int(wake_at.timestamp())
    log.info("Setting RTC wake alarm for %s (epoch %d)", wake_at.isoformat(), epoch)
    if dry_run:
        print(f"echo 0 > {WAKEALARM}; echo {epoch} > {WAKEALARM}")
        return
    # Must clear the alarm before setting a new value, or the write is rejected.
    WAKEALARM.write_text("0\n")
    WAKEALARM.write_text(f"{epoch}\n")


def power_off(*, dry_run: bool = False) -> None:
    log.info("Powering off")
    if dry_run:
        print("systemctl poweroff")
        return
    subprocess.run(["systemctl", "poweroff"], check=True)


def sleep_until(wake_hour: int, *, dry_run: bool = False) -> None:
    """Set the wake alarm for the next wake_hour and power the board off."""
    wake_at = next_wake_time(wake_hour)
    set_wake_alarm(wake_at, dry_run=dry_run)
    power_off(dry_run=dry_run)
