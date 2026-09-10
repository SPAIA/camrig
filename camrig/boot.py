"""Boot orchestration (cam-boot.service, oneshot at startup).

Order: request cam-captive.service (the AP/captive-portal focus fallback,
camrig.captive) first thing -- a no-op if there's actually internet, since
that service re-checks reachability itself before touching the network. Then
sync the clock via NTP (if online), sweep *.part staging files a crash or
power-off left behind (salvaging complete captures), finish any postprocess a
crash or power-off interrupted (so previews/motion sidecars exist), then
prune storage. The supervisor service starts independently and begins
recording regardless of network state.

Deliberately does not upload here: a boot-time bulk upload competes for
bandwidth/CPU with recording, focus, and the captive portal right when a rig
is freshly powered up in the field -- often on a slow or flaky mobile
connection, which is exactly when that contention hurts most. With
`upload.immediate` (the default) each clip ships right after its own
postprocess anyway, so there's normally nothing built up to catch up on. The
trade-off: a clip that *did* miss immediate upload (R2 unreachable at the
time) no longer retries automatically on its own -- run `camrig upload`
manually next time there's a decent connection. Nothing is lost either way:
an unuploaded clip is never pruned (see storage.prune), it just waits on disk.
"""

from __future__ import annotations

import logging
import subprocess

from .config import Config
from . import postprocess, storage, timesync

log = logging.getLogger("camrig.boot")


def run(cfg: Config, *, dry_run: bool = False) -> int:
    log.info("Boot tasks starting")

    if cfg.captive.enabled and not dry_run:
        log.info("Requesting cam-captive.service (no-op if already online)")
        subprocess.Popen(["systemctl", "start", "--no-block", "cam-captive.service"])

    synced = timesync.sync_time()
    log.info("NTP synchronised: %s", synced)

    base = storage.select_base_dir(cfg)
    swept = storage.sweep_partials(base, dry_run=dry_run)
    if swept:
        log.info("Swept %d interrupted .part famil(ies)", swept)
    if cfg.postprocess.enabled:
        postprocess.process_pending(cfg, base, dry_run=dry_run)

    storage.prune(cfg, base)

    return 0
