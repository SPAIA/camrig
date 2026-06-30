# camrig — insect-tracking capture rig

Scheduled + remotely-triggered video capture on a **Raspberry Pi 5** with the
**Global Shutter mono camera (IMX296)**, built as a test rig for offline
**motion-trail tracking of tiny objects (insects)**.

What it does:

- Records a **5-minute clip every 30 minutes** during the active window (05:00–22:00).
- Lets a remote user **trigger a recording from a public Cloudflare page** (a human
  counts bugs while it records). The Pi dials out to your Cloudflare Worker — no
  inbound ports.
- **Shuts down at 22:00** and **wakes at 05:00** using the Pi 5 RTC alarm.
- **Syncs the clock via NTP** at boot (when online).
- **Uploads each day's clips to Cloudflare R2** via rclone.

## Why these choices (tracking fidelity)

- **Global shutter** — no rolling-shutter skew on fast insects.
- **Mono sensor** — no Bayer interpolation, more light-sensitive, sharper small targets.
- **Intra-only capture** — every frame independent. Inter-frame codecs (H.264) run
  motion compensation that creates artefacts exactly on tiny moving objects, so we
  avoid them. (This is also why a **Pi 5 is better than a Pi 4** here, despite the
  Pi 4's hardware H.264 encoder — we don't want lossy inter-frame video, and the Pi 5
  has the NVMe bandwidth + CPU for clean intra/lossless capture and a built-in RTC.)
- **Temporal denoise OFF** (`cdn_off`) — 3D/temporal denoise smears or erases
  insect-sized moving targets.
- **Per-frame timestamps** (`.pts`) + JSON metadata per clip — for accurate offline
  velocity/trajectory work and to align Cloudflare-stored bug counts to frames.

## Capture profiles (switchable, `capture.profile`)

| Profile | Codec | Use | Notes |
|---|---|---|---|
| `mjpeg` *(default)* | Motion-JPEG in MKV | Daily pipeline | Intra-only, clean frames, manageable upload size. |
| `ffv1` | Lossless FFV1 in MKV | Fidelity experiments | Software encode likely can't sustain 60 fps at full res. |
| `raw` | rpicam-raw mono | Short fidelity tests | Huge files; NVMe only. |

**Frame rate / lighting:** the IMX296 maxes around **60 fps** at full res
(1456×1088). To actually reach it the shutter must be ≤ ~16 ms; to *freeze* insect
motion use a short shutter (default `shutter_us = 2000`), which needs good lighting.
Raise `gain` if too dark. These are manual for repeatability — tune in `config.toml`.

## Install (on the Pi, Debian Trixie / Pi OS)

```bash
git clone <this repo> cam_raw_capture && cd cam_raw_capture
sudo CAM_USER=spaia ./setup/install.sh      # CAM_USER = the camera/login user
```

`install.sh` (idempotent) installs `ffmpeg`, `rclone`, `rpicam-apps`, creates a
**venv** at `/opt/camrig/venv` (Trixie enforces PEP 668, so we never touch system
Python), installs config to `/etc/camrig`, enables the systemd units, and sets the
EEPROM for RTC wake.

Then finish the three secrets/config items it can't guess:

1. **`/etc/camrig/config.toml`** — storage paths, `worker_ws_url`, `device_id`, R2
   `bucket`. See [`config/config.toml`](config/config.toml) for every key.
2. **`/etc/camrig/rclone.conf`** (chmod 0600) — R2 credentials; template in
   [`config/rclone.conf.example`](config/rclone.conf.example).
3. **`/etc/camrig/device_token`** (chmod 0600) — the bearer token the Worker expects.

Reboot so the EEPROM change and `video`/`render` group membership take effect.

## Storage

Prefers an NVMe mountpoint (`storage.nvme_mount`, default `/mnt/nvme`) for sustained
write bandwidth; **falls back to the SD card** (`storage.sd_fallback_dir`) if the
NVMe mount is absent or not writable. Layout:

```
<base>/recordings/2026-06-30/clip_20260630_101502.mkv
                              clip_20260630_101502.pts    # per-frame timestamps
                              clip_20260630_101502.json   # capture metadata
```

Uploaded to `r2:<bucket>/<hostname>/2026-06-30/…`. Retention prunes **uploaded**
clips older than `keep_days` once free space drops below `min_free_gb`.

## Remote trigger (Cloudflare)

The Pi runs an outbound WebSocket client to your Worker. You implement the Worker +
Durable Object per **[`docs/worker-brief.md`](docs/worker-brief.md)** (Turnstile +
rate limit on the public trigger; bug counts stored in Cloudflare D1 only). The Pi
reports the authoritative NTP-synced session start time so counts align to frames.

Admin/SSH access to the Pi itself is via **Tailscale** (unchanged); only the public
*trigger* path goes through Cloudflare.

## CLI

```bash
/opt/camrig/venv/bin/camrig status                 # resolved config + storage
/opt/camrig/venv/bin/camrig record --dry-run       # print the exact capture command
/opt/camrig/venv/bin/camrig record --seconds 10    # capture a 10s test clip
/opt/camrig/venv/bin/camrig supervise --no-cloud   # run scheduler without Cloudflare
/opt/camrig/venv/bin/camrig boot                   # NTP sync + catch-up upload
/opt/camrig/venv/bin/camrig shutdown --skip-poweroff   # upload+arm wake, but stay up
```

## Verify on the Pi

1. `camrig record --dry-run` — sanity-check the rpicam/ffmpeg command per profile.
2. `camrig record --seconds 10` — confirm the `.mkv` plays and `.pts` deltas imply
   ~60 fps (`frames / (last_pts − first_pts)`). Compare `cdn_off` vs `auto` denoise
   on a moving target.
3. From a tailnet/public page hit `POST /api/trigger` → the Pi starts within ~1 s;
   confirm `session_started.started_at_utc` reaches the Worker and a posted count
   aligns via the formula in the brief.
4. **Wake test:** `sudo bash -c 'echo 0 > /sys/class/rtc/rtc0/wakealarm; echo +120 >
   /sys/class/rtc/rtc0/wakealarm'; sudo systemctl poweroff` — board should wake in
   ~2 min (requires `POWER_OFF_ON_HALT=1`; check `rpi-eeprom-config`).
5. `camrig boot` with a small clip present → objects appear in R2.
6. `systemctl status cam-supervisor`; `systemctl list-timers cam-shutdown.timer`;
   watch `journalctl -u cam-supervisor -f` across a 30-minute boundary.

> rpicam flags vary by `rpicam-apps` version. If a capture errors, check
> `rpicam-vid --help` / `rpicam-raw --help` on your image and adjust
> [`camrig/record.py`](camrig/record.py).

## Roadmap / out of scope (notes)

- **Battery deployment:** add an **ESP32 wake-on-GPIO** companion (Pi 5
  `WAKE_ON_GPIO=1`) so the Pi can sleep between sessions and wake on demand. Today's
  web trigger assumes the Pi is already on during the daytime window.
- **Basler dart (USB3):** a second capture backend slots into `camrig/record.py`
  behind the same profile/metadata interface.
- **Live preview** in the page via a libcamera low-res second stream.
