#!/usr/bin/env bash
# Enable Pi 5 power-off-on-halt so the RTC wake alarm can re-power the board.
# Idempotent: only rewrites the EEPROM config if the key is missing/different.
#
# Run with sudo on the Pi:  sudo ./setup/set_eeprom.sh
set -euo pipefail

if [[ $EUID -ne 0 ]]; then
  echo "Run as root (sudo)." >&2
  exit 1
fi

if ! command -v rpi-eeprom-config >/dev/null 2>&1; then
  echo "rpi-eeprom-config not found — is this a Raspberry Pi 5 on Pi OS?" >&2
  exit 1
fi

current="$(rpi-eeprom-config)"

if grep -q '^POWER_OFF_ON_HALT=1' <<<"$current"; then
  echo "POWER_OFF_ON_HALT=1 already set; nothing to do."
  exit 0
fi

tmp="$(mktemp)"
trap 'rm -f "$tmp"' EXIT

# Drop any existing POWER_OFF_ON_HALT line, then append the desired value.
grep -v '^POWER_OFF_ON_HALT=' <<<"$current" >"$tmp" || true
echo 'POWER_OFF_ON_HALT=1' >>"$tmp"

echo "Applying EEPROM config:"
echo "------------------------"
cat "$tmp"
echo "------------------------"

rpi-eeprom-config --apply "$tmp"
echo "Done. Reboot for the new EEPROM config to take effect."
