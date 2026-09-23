#!/usr/bin/env bash
# Deploy the latest committed code to this rig: pull, reinstall into the
# camrig venv, restart the capture supervisor so it actually picks up the
# change.
#
#   ./setup/deploy.sh
#
# camrig installs as a regular (non-editable) pip package, so `git pull`
# alone only updates this clone -- it never touches the installed copy under
# /opt/camrig/venv, and an already-running cam-supervisor keeps old code in
# memory regardless (Python doesn't hot-reload). All three steps below are
# required every time; this just bundles the sequence that was previously
# run by hand.
#
# Run as the normal user (not root/sudo) -- git pull needs to run as
# whoever owns this clone, so only the two steps that actually need root
# (reinstalling into /opt/camrig, restarting the systemd unit) invoke sudo
# themselves, prompting for a password if it isn't already cached.
set -euo pipefail

PREFIX=/opt/camrig
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SERVICE=cam-supervisor.service

if [[ $EUID -eq 0 ]]; then
  echo "Run this as the normal user, not root/sudo -- git pull needs to run as" >&2
  echo "whoever owns $REPO_DIR. The two steps that need root ask for sudo themselves." >&2
  exit 1
fi

if ! git -C "$REPO_DIR" diff --quiet HEAD --; then
  echo "Uncommitted changes in $REPO_DIR -- commit, stash, or discard them before deploying." >&2
  git -C "$REPO_DIR" status --short
  exit 1
fi

echo "==> Pulling latest code in $REPO_DIR"
git -C "$REPO_DIR" pull --ff-only

echo "==> Reinstalling camrig into $PREFIX/venv"
sudo "$PREFIX/venv/bin/pip" install --force-reinstall --no-deps "$REPO_DIR"

echo "==> Restarting $SERVICE"
sudo systemctl restart "$SERVICE"

echo "==> Done. Current status:"
sudo systemctl status "$SERVICE" --no-pager -l | head -5
