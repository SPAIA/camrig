"""Shutdown orchestration (cam-shutdown.service at 22:00, runs as root).

Upload today's clips while there is still network, prune storage, then program
the RTC wake alarm for the next morning and power the board off. With
POWER_OFF_ON_HALT=1 in the EEPROM, the RTC re-powers the Pi at the wake time.

If the upload fails (e.g. offline), files remain on disk and the next boot's
catch-up upload flushes them — so we still proceed to sleep.
"""

from __future__ import annotations

import logging

from .config import Config
from . import power, storage, upload

log = logging.getLogger("camrig.shutdown")


def run(cfg: Config, *, skip_poweroff: bool = False, dry_run: bool = False) -> int:
    log.info("Shutdown tasks starting")

    if cfg.upload.enabled:
        base = storage.select_base_dir(cfg)
        if upload.remote_reachable(cfg):
            upload.upload_today(cfg, base, dry_run=dry_run)
            storage.prune(cfg, base)
        else:
            log.warning("R2 not reachable; leaving today's clips for boot catch-up")

    if skip_poweroff:
        log.info("skip_poweroff set; not sleeping")
        return 0

    power.sleep_until(cfg.power.wake_hour, dry_run=dry_run)
    return 0
