"""Shutdown orchestration (cam-shutdown.service at 22:00, runs as root).

Finish pending postprocess and prune storage, then program the RTC wake alarm
for the next morning and power the board off. With POWER_OFF_ON_HALT=1 in the
EEPROM, the RTC re-powers the Pi at the wake time.

Deliberately does not upload here: with `upload.immediate` (the default) each
clip already ships right after its own postprocess, so there's normally
nothing left pending by shutdown anyway -- and blocking a field shutdown on a
bulk upload of whatever *is* still pending (often large full-res clips, often
over a slow or absent connection) is worse than just leaving it for the next
boot's catch-up upload or a manual `camrig upload`.
"""

from __future__ import annotations

import logging

from .config import Config
from . import postprocess, power, storage

log = logging.getLogger("camrig.shutdown")


def run(cfg: Config, *, skip_poweroff: bool = False, dry_run: bool = False) -> int:
    log.info("Shutdown tasks starting")

    base = storage.select_base_dir(cfg)
    # Finish any pending postprocess so previews/motion metrics ship tonight
    # rather than on the next boot's catch-up. Normally a no-op: clips are
    # processed right after capture.
    if cfg.postprocess.enabled:
        postprocess.process_pending(cfg, base, dry_run=dry_run)

    # Prune only, no upload -- see module docstring. Cheap/local: only touches
    # clips already marked uploaded (by the immediate per-clip path or a prior
    # catch-up), so it doesn't need network reachability.
    storage.prune(cfg, base)

    if skip_poweroff:
        log.info("skip_poweroff set; not sleeping")
        return 0

    power.sleep_until(cfg.power.wake_hour, dry_run=dry_run)
    return 0
