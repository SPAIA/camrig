#!/usr/bin/env bash
# Install camrig on a Raspberry Pi 5 (Pi OS, Debian Trixie).
#
#   sudo ./setup/install.sh
#
# Idempotent. Creates a venv (Trixie enforces PEP 668, so we never touch the
# system Python), installs the package, lays down config under /etc/camrig, and
# enables the systemd units. Secrets (rclone.conf, device_token) are NOT created
# here — see README; this only drops examples if nothing exists yet.
set -euo pipefail

PREFIX=/opt/camrig
ETC=/etc/camrig
CAM_USER="${CAM_USER:-spaia}"
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

if [[ $EUID -ne 0 ]]; then
  echo "Run as root (sudo)." >&2
  exit 1
fi

if ! id "$CAM_USER" >/dev/null 2>&1; then
  echo "User '$CAM_USER' does not exist. Set CAM_USER=<youruser> and re-run." >&2
  exit 1
fi

echo "==> Installing system packages (ffmpeg, rclone, python venv, rpicam-apps)"
apt-get update
apt-get install -y ffmpeg rclone python3-venv rpicam-apps

echo "==> Creating Python venv at $PREFIX/venv"
mkdir -p "$PREFIX"
python3 -m venv "$PREFIX/venv"
"$PREFIX/venv/bin/pip" install --upgrade pip
"$PREFIX/venv/bin/pip" install "$REPO_DIR"

echo "==> Granting $CAM_USER camera access (video, render groups)"
usermod -aG video,render "$CAM_USER"

echo "==> Installing config to $ETC"
mkdir -p "$ETC"
if [[ ! -f "$ETC/config.toml" ]]; then
  install -m 0644 "$REPO_DIR/config/config.toml" "$ETC/config.toml"
  echo "    wrote $ETC/config.toml (review it!)"
else
  echo "    $ETC/config.toml exists; leaving it"
fi
if [[ ! -f "$ETC/rclone.conf" ]]; then
  install -m 0600 "$REPO_DIR/config/rclone.conf.example" "$ETC/rclone.conf"
  echo "    wrote $ETC/rclone.conf TEMPLATE — fill in your R2 credentials (chmod 0600)"
fi
if [[ ! -f "$ETC/device_token" ]]; then
  touch "$ETC/device_token"
  chmod 0600 "$ETC/device_token"
  echo "    created empty $ETC/device_token — paste the Worker device bearer token"
fi
# rclone (run by root services) reads this config path explicitly.
export RCLONE_CONFIG="$ETC/rclone.conf"

echo "==> Installing systemd units"
install -m 0644 "$REPO_DIR/systemd/"*.service "$REPO_DIR/systemd/"*.timer /etc/systemd/system/
# Point services at the per-rig rclone config so root finds the R2 remote.
mkdir -p /etc/systemd/system/cam-boot.service.d /etc/systemd/system/cam-shutdown.service.d
printf '[Service]\nEnvironment=RCLONE_CONFIG=%s\n' "$ETC/rclone.conf" \
  | tee /etc/systemd/system/cam-boot.service.d/rclone.conf >/dev/null
printf '[Service]\nEnvironment=RCLONE_CONFIG=%s\n' "$ETC/rclone.conf" \
  > /etc/systemd/system/cam-shutdown.service.d/rclone.conf
# Patch the camera user into the supervisor unit if it differs from default.
if [[ "$CAM_USER" != "spaia" ]]; then
  sed -i "s/^User=spaia/User=$CAM_USER/" /etc/systemd/system/cam-supervisor.service
fi

systemctl daemon-reload
systemctl enable --now cam-supervisor.service
systemctl enable --now cam-boot.service
systemctl enable --now cam-shutdown.timer

echo "==> Setting EEPROM POWER_OFF_ON_HALT for RTC wake"
"$REPO_DIR/setup/set_eeprom.sh" || echo "    (EEPROM step skipped/failed — run setup/set_eeprom.sh manually)"

cat <<EOF

Done. Next steps:
  1. Edit $ETC/config.toml (storage paths, worker_ws_url, device_id, bucket).
  2. Fill $ETC/rclone.conf with your R2 credentials (chmod 0600).
  3. Paste the Worker device token into $ETC/device_token.
  4. Reboot so the EEPROM change and group membership take effect.

Verify with:
  /opt/camrig/venv/bin/camrig status
  /opt/camrig/venv/bin/camrig record --dry-run
  systemctl status cam-supervisor
  systemctl list-timers cam-shutdown.timer
EOF
