#!/usr/bin/env bash
# Enable the Pi 5's hardware watchdog and make journald logs survive a hard
# reset, so a full system freeze (kernel hang, CSI/GigE driver stall, etc.)
# self-recovers instead of needing someone to physically power-cycle the rig,
# and leaves a trail in `journalctl -b -1` to diagnose what happened.
#
# Idempotent. Run with sudo on the Pi: sudo ./setup/set_watchdog.sh
set -euo pipefail

if [[ $EUID -ne 0 ]]; then
  echo "Run as root (sudo)." >&2
  exit 1
fi

# ----- 1. Hardware watchdog device tree overlay ----------------------------
# bcm2835_wdt only binds /dev/watchdog0 if the overlay is enabled; without
# this line systemd has nothing to pet.
CONFIG_TXT=/boot/firmware/config.txt
[[ -f "$CONFIG_TXT" ]] || CONFIG_TXT=/boot/config.txt
if [[ ! -f "$CONFIG_TXT" ]]; then
  echo "Can't find config.txt (checked /boot/firmware and /boot) — is this a Pi?" >&2
  exit 1
fi

if grep -q '^dtparam=watchdog=on' "$CONFIG_TXT"; then
  echo "dtparam=watchdog=on already set in $CONFIG_TXT"
else
  echo "==> Enabling hardware watchdog in $CONFIG_TXT"
  printf '\n# camrig: bind /dev/watchdog0 so systemd can auto-reboot on a hang\ndtparam=watchdog=on\n' \
    >> "$CONFIG_TXT"
fi

# ----- 2. systemd runtime watchdog -----------------------------------------
# PID1 pings /dev/watchdog0 continuously; if it stops (system is well and
# truly wedged — kernel hang, not just a wedged process) the hardware forces
# a reboot after RuntimeWatchdogSec. Read only at boot, so this needs a reboot.
echo "==> Installing systemd watchdog drop-in"
mkdir -p /etc/systemd/system.conf.d
cat > /etc/systemd/system.conf.d/watchdog.conf <<'EOF'
# Installed by setup/set_watchdog.sh — reboot automatically if the system
# stops responding, instead of requiring a physical power cycle.
[Manager]
RuntimeWatchdogSec=15s
EOF

# ----- 3. Persistent journal -------------------------------------------------
# Default journald storage is volatile (RAM-backed), so a hard reset from the
# watchdog above would otherwise wipe the very logs needed to diagnose the
# freeze. Cap the on-disk size so it can't eat into recording/upload storage.
echo "==> Installing persistent journald drop-in"
mkdir -p /etc/systemd/journald.conf.d
cat > /etc/systemd/journald.conf.d/camrig-persistent.conf <<'EOF'
# Installed by setup/set_watchdog.sh — survive a hard reset so
# `journalctl -b -1` shows what happened right before a freeze/reboot.
[Journal]
Storage=persistent
SystemMaxUse=200M
EOF
systemctl restart systemd-journald

echo "Done."
echo "  - journald: persistent storage active now."
echo "  - watchdog: needs a reboot to take effect (config.txt is read at boot)."
echo "After rebooting, verify with:"
echo "  cat /sys/class/watchdog/watchdog0/state       # should print 'active' after any systemd start"
echo "  systemctl show -p RuntimeWatchdogUSec"
echo "  journalctl -b -1 -n 50                        # inspect the previous boot after any freeze"
