#!/usr/bin/env bash
# Let the camrig service user write /sys/class/leds/<LED>/{brightness,trigger}
# without root, so camrig.led can actually flash the activity LED as a
# capture cue (see camrig/led.py) instead of silently no-op'ing on
# PermissionError.
#
# Idempotent. Run with sudo on the Pi: sudo ./setup/set_led_perms.sh [cam_user]
set -euo pipefail

if [[ $EUID -ne 0 ]]; then
  echo "Run as root (sudo)." >&2
  exit 1
fi

CAM_USER="${1:-${CAM_USER:-spaia}}"
if ! id "$CAM_USER" >/dev/null 2>&1; then
  echo "User '$CAM_USER' does not exist. Pass it as an argument: set_led_perms.sh <user>" >&2
  exit 1
fi

# Same search order as camrig.led._CANDIDATE_LEDS -- whichever this kernel
# actually exposes for the onboard activity LED.
LED=""
for name in ACT led0 PWR led1; do
  if [[ -d "/sys/class/leds/$name" ]]; then
    LED="$name"
    break
  fi
done
if [[ -z "$LED" ]]; then
  echo "No onboard LED found under /sys/class/leds; nothing to do." >&2
  exit 0
fi

RULES_FILE=/etc/udev/rules.d/99-camrig-led.rules
echo "==> Installing udev rule granting $CAM_USER write access to $LED"
cat > "$RULES_FILE" <<EOF
# Installed by setup/set_led_perms.sh -- let $CAM_USER flash the onboard
# activity LED as a capture cue (camrig/led.py) without running the
# supervisor as root. Sysfs LED attributes don't take udev's MODE=/GROUP=
# directly, hence the RUN+= chgrp/chmod on add.
ACTION=="add", SUBSYSTEM=="leds", KERNEL=="$LED", RUN+="/bin/chgrp $CAM_USER /sys%p/brightness /sys%p/trigger", RUN+="/bin/chmod g+w /sys%p/brightness /sys%p/trigger"
EOF

udevadm control --reload-rules
# Replays the "add" event for the LED that's already present from boot, so
# this takes effect now instead of needing a reboot.
udevadm trigger --subsystem-match=leds --action=add

echo "Done. Verify with:"
echo "  ls -l /sys/class/leds/$LED/brightness /sys/class/leds/$LED/trigger"
