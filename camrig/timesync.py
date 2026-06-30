"""NTP time synchronisation at boot.

The Pi shuts down nightly; if it loses power and has no RTC battery, the clock is
wrong on wake. Accurate time matters here because Cloudflare-stored bug counts are
aligned to video frames via the clip's UTC start time. This module waits for
connectivity, then forces systemd-timesyncd to step the clock.
"""

from __future__ import annotations

import logging
import socket
import subprocess
import time

log = logging.getLogger("camrig.timesync")


def has_connectivity(host: str = "1.1.1.1", port: int = 443, timeout: float = 3.0) -> bool:
    """Cheap TCP reachability check (Cloudflare 1.1.1.1:443 by default)."""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def wait_for_network(timeout: float = 60.0, interval: float = 3.0) -> bool:
    """Poll for connectivity up to ``timeout`` seconds."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if has_connectivity():
            return True
        time.sleep(interval)
    return False


def _ntp_synchronised() -> bool:
    try:
        out = subprocess.run(
            ["timedatectl", "show", "-p", "NTPSynchronized", "--value"],
            capture_output=True, text=True, check=True,
        )
        return out.stdout.strip() == "yes"
    except (subprocess.CalledProcessError, FileNotFoundError):
        return False


def sync_time(network_timeout: float = 60.0, sync_timeout: float = 30.0) -> bool:
    """Force an NTP sync if online. Returns True if the clock is synchronised.

    Skips gracefully (returns False) when offline so the rest of boot proceeds.
    """
    if not wait_for_network(timeout=network_timeout):
        log.warning("No network within %.0fs; skipping NTP sync", network_timeout)
        return False

    try:
        subprocess.run(["timedatectl", "set-ntp", "true"], check=True)
        # Restart the daemon to force an immediate step rather than waiting for
        # its own poll interval.
        subprocess.run(
            ["systemctl", "restart", "systemd-timesyncd"], check=False
        )
    except (subprocess.CalledProcessError, FileNotFoundError) as exc:
        log.error("Could not enable NTP: %s", exc)
        return False

    deadline = time.monotonic() + sync_timeout
    while time.monotonic() < deadline:
        if _ntp_synchronised():
            log.info("Clock synchronised via NTP: %s", time.strftime("%Y-%m-%d %H:%M:%S"))
            return True
        time.sleep(2.0)

    log.warning("NTP did not report synchronised within %.0fs", sync_timeout)
    return False
