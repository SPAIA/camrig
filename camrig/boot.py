"""Boot orchestration (cam-boot.service, oneshot at startup).

Order: sync the clock via NTP (if online), sweep *.part staging files a crash
or power-off left behind (salvaging complete captures), finish any postprocess
a crash or power-off interrupted (so previews/motion sidecars exist before upload), then
flush any clips a failed or offline nightly upload left behind, then prune
storage, then request cam-captive.service (the AP/captive-portal focus
fallback, camrig.captive) -- a no-op if there's actually internet, since that
service re-checks reachability itself before touching the network. The
supervisor service starts independently and begins recording regardless of
network state.
"""

from __future__ import annotations

import logging
import subprocess

from .config import Config
from . import postprocess, storage, timesync, upload

log = logging.getLogger("camrig.boot")


def run(cfg: Config, *, dry_run: bool = False) -> int:
    log.info("Boot tasks starting")
    synced = timesync.sync_time()
    log.info("NTP synchronised: %s", synced)

    base = storage.select_base_dir(cfg)
    swept = storage.sweep_partials(base, dry_run=dry_run)
    if swept:
        log.info("Swept %d interrupted .part famil(ies)", swept)
    if cfg.postprocess.enabled:
        postprocess.process_pending(cfg, base, dry_run=dry_run)

    if cfg.upload.enabled:
        if upload.remote_reachable(cfg):
            upload.upload_pending(cfg, base, dry_run=dry_run)
            storage.prune(cfg, base)
        else:
            log.warning("R2 not reachable; deferring catch-up upload")

    if cfg.captive.enabled and not dry_run:
        log.info("Requesting cam-captive.service (no-op if already online)")
        subprocess.Popen(["systemctl", "start", "--no-block", "cam-captive.service"])
    return 0
