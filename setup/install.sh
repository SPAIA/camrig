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
# WITH_BASLER=1 adds pypylon for the Basler ace 2 GigE backend
# (capture.camera = "basler"; network setup in docs/basler-gige.md).
# NB: pass it AFTER sudo (sudo WITH_BASLER=1 ./setup/install.sh) — sudo's
# env_reset strips assignments that come before it.
if [[ "${WITH_BASLER:-0}" == "1" ]]; then
  echo "    (with Basler backend: installing camrig[basler] + pypylon)"
  "$PREFIX/venv/bin/pip" install "$REPO_DIR[basler]"
else
  echo "    (rpicam only; sudo WITH_BASLER=1 ... adds the Basler backend)"
  "$PREFIX/venv/bin/pip" install "$REPO_DIR"
fi

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
  install -m 0640 "$REPO_DIR/config/rclone.conf.example" "$ETC/rclone.conf"
  echo "    wrote $ETC/rclone.conf TEMPLATE — fill in your R2 credentials"
fi
if [[ ! -f "$ETC/device_token" ]]; then
  touch "$ETC/device_token"
  echo "    created empty $ETC/device_token — paste the Worker device bearer token"
fi
# The supervisor runs as $CAM_USER and must read both secrets (rclone uploads,
# Worker bearer token); keep them root-owned but group-readable.
chown "root:$CAM_USER" "$ETC/rclone.conf" "$ETC/device_token"
chmod 0640 "$ETC/rclone.conf" "$ETC/device_token"
# rclone (run by root services) reads this config path explicitly.
export RCLONE_CONFIG="$ETC/rclone.conf"

echo "==> Installing systemd units"
install -m 0644 "$REPO_DIR/systemd/"*.service "$REPO_DIR/systemd/"*.timer /etc/systemd/system/
# Point every service that shells out to rclone at the per-rig config.
for unit in cam-boot cam-shutdown cam-supervisor; do
  mkdir -p "/etc/systemd/system/$unit.service.d"
  printf '[Service]\nEnvironment=RCLONE_CONFIG=%s\n' "$ETC/rclone.conf" \
    > "/etc/systemd/system/$unit.service.d/rclone.conf"
done
# Patch the camera user (and its primary group, which must match so the
# root:$CAM_USER 0640 secrets stay readable) into the supervisor unit.
if [[ "$CAM_USER" != "spaia" ]]; then
  sed -i "s/^User=spaia/User=$CAM_USER/" /etc/systemd/system/cam-supervisor.service
  sed -i "s/^Group=spaia/Group=$CAM_USER/" /etc/systemd/system/cam-supervisor.service
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
  2. Fill $ETC/rclone.conf with your R2 credentials (root:$CAM_USER, chmod 0640).
  3. Paste the Worker device token into $ETC/device_token.
  4. Reboot so the EEPROM change and group membership take effect.

Verify with:
  /opt/camrig/venv/bin/camrig status
  /opt/camrig/venv/bin/camrig record --dry-run
  systemctl status cam-supervisor
  systemctl list-timers cam-shutdown.timer
EOF
