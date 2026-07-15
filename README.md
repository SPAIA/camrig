# camrig — insect-tracking capture rig

Scheduled + remotely-triggered video capture on a **Raspberry Pi 5** with the
**Global Shutter camera (colour IMX296)**, built as a test rig for offline
**motion-trail tracking of tiny objects (insects)**.

What it does:

- Records a **5-minute clip every 30 minutes** during the active window (05:00–22:00).
- Lets a remote user **trigger a recording from a public Cloudflare page** (a human
  counts bugs while it records). The Pi dials out to your Cloudflare Worker — no
  inbound ports.
- **Shuts down at 22:00** and **wakes at 05:00** using the Pi 5 RTC alarm.
- **Syncs the clock via NTP** at boot (when online).
- **Post-processes each clip on-device** in the idle gap between captures: a
  low-res **H.264 preview** for scrubbing plus a **per-frame motion-metrics
  sidecar** (placeholder analysis, the seed of the motion-trail tracker).
- **Uploads each clip to Cloudflare R2 as soon as its sidecars are ready** (via
  rclone), so device space is reclaimed early; nightly + boot catch-up uploads
  cover anything recorded while offline.

## Why these choices (tracking fidelity)

- **Global shutter** — no rolling-shutter skew on fast insects.
- **Colour sensor** — colour is kept at capture (potentially useful for insect ID);
  convert to grayscale downstream if the tracking pipeline wants it. Note the Bayer
  filter costs some light sensitivity and sharpness vs the mono variant.
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

| Profile             | Codec                | Use                  | Notes                                                    |
| ------------------- | -------------------- | -------------------- | -------------------------------------------------------- |
| `mjpeg` *(default)* | Motion-JPEG in MKV   | Daily pipeline       | Intra-only, clean frames, manageable upload size.        |
| `ffv1`              | Lossless FFV1 in MKV | Fidelity experiments | Software encode likely can't sustain 60 fps at full res. |
| `raw`               | rpicam-raw Bayer     | Short fidelity tests | Huge files; NVMe only. Colour mosaic — demosaic offline. |

**Frame rate / lighting:** the IMX296 maxes around **60 fps** at full res
(1456×1088). To actually reach it the shutter must be ≤ ~16 ms; to *freeze* insect
motion use a short shutter (default `shutter_us = 2000`), which needs good lighting.
Raise `gain` if too dark. These are manual for repeatability — tune in `config.toml`.

## Second backend: Basler ace 2 mono over GigE (comparison rig)

Set `capture.camera = "basler"` (or per-run `camrig record --camera basler`) to
capture from a **Basler ace 2 mono** connected to the Pi's Ethernet port — a
global-shutter mono sensor to compare against the colour IMX296 (no Bayer
filter: more light sensitivity and per-pixel sharpness). The backend
(`camrig/basler.py`, via pypylon) presents the same interface as rpicam — same
profiles, `.pts` per-frame timestamps (from the camera's **hardware clock**),
metadata sidecar, postprocess, upload, and `camrig focus --camera basler` for
lens focusing — so clips from both cameras flow through one pipeline and are
distinguished by the `camera`/`sensor` fields in their `.json`.

Install with `sudo WITH_BASLER=1 ./setup/install.sh` (the assignment must come
after `sudo`, which strips preceding env vars); the `[basler]` config
section selects the device and tunes the GigE transport. **Wiring, IP setup,
and bandwidth limits (GigE caps Mono8 at ~115 MB/s ≈ width×height×fps) are in
[`docs/basler-gige.md`](docs/basler-gige.md).** The Pi's internet then moves to
Wi-Fi — eth0 becomes the dedicated camera link.

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
2. **`/etc/camrig/rclone.conf`** (root:$CAM_USER, 0640 — the supervisor reads it) —
   R2 credentials; template in [`config/rclone.conf.example`](config/rclone.conf.example).
   The `endpoint` is the account root (`https://<ACCOUNT_ID>.r2.cloudflarestorage.com`),
   **without** the bucket name.
3. **`/etc/camrig/device_token`** (root:$CAM_USER, 0640) — the bearer token the Worker expects.

Reboot so the EEPROM change and `video`/`render` group membership take effect.

## Storage

Prefers an NVMe mountpoint (`storage.nvme_mount`, default `/mnt/nvme`) for sustained
write bandwidth; **falls back to the SD card** (`storage.sd_fallback_dir`) if the
NVMe mount is absent or not writable. Layout:

```
<base>/recordings/2026-06-30/clip_20260630_101502.mkv
                              clip_20260630_101502.pts          # per-frame timestamps
                              clip_20260630_101502.json         # capture metadata
                              clip_20260630_101502.preview.mp4  # low-res preview
                              clip_20260630_101502.motion.json  # per-frame motion metrics
```

Uploaded to `r2:<bucket>/<hostname>/2026-06-30/…`. Retention prunes **uploaded**
clips (and their sidecars) older than `keep_days` once free space drops below
`min_free_gb`.

## Post-capture processing (preview + motion)

After each finished clip the supervisor runs a background job (`[postprocess]`
in config): **one niced ffmpeg decodes the MJPEG once** and feeds two consumers —
a downscaled colour H.264 preview written next to the clip, and downscaled
grayscale frames piped into `python -m camrig.motion`, which writes per-frame
metrics as JSON. Jobs are serialised and run at `nice 10`, so a capture that
starts mid-postprocess always wins the CPU; the ~27 idle minutes per half-hour
slot are far more than the ~2–4 minutes a clip needs. Half-written outputs use a
`*.part` name that the uploader excludes.

Capture outputs are staged the same way: a clip records as `*.part` and is only
renamed to its final name (video last, after the pts/json sidecars) once every
pipeline stage exits cleanly, so upload and catch-up scans never see a clip
that is still recording or truncated. Staging files orphaned by a crash or
power cut are swept at boot — complete families are salvaged into place,
incomplete ones deleted.

As soon as a clip's sidecars exist it is **uploaded to R2 immediately** and
marked, making it prune-eligible right away (retention still honours
`keep_days`/`min_free_gb`) instead of holding the day on disk until the nightly
upload. If the upload or postprocess fails, the clip simply stays unmarked and
the boot/shutdown catch-up ships it — including any sidecars that arrived late.

Two `[upload]` switches control this behaviour:

- `immediate = false` — revert to nightly/boot-only uploads.
- `full_res = false` — ship **only the sidecars** (preview, motion, pts, json);
  the full-res video never leaves the device and retention prunes it after
  `keep_days`, so pull any clip worth keeping before then.

**The motion analysis is a placeholder** (frame differencing: per-frame
`mean_abs_diff` and `active_fraction`). Iterate toward motion-trail tracking in
[`camrig/motion.py`](camrig/motion.py) — it reads raw gray8 frames on stdin at
the source frame rate (metric index *i* aligns with `.pts` line *i*) and writes
the JSON sidecar; keep that contract and nothing else needs to change. After
changing it, rebuild sidecars with `camrig postprocess --force`. The preview is
for humans only — analysis always reads the original intra-only clip, so H.264
artefacts in the preview don't matter.

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
/opt/camrig/venv/bin/camrig postprocess            # preview+motion for pending clips
/opt/camrig/venv/bin/camrig postprocess --force    # regenerate (e.g. new motion code)
/opt/camrig/venv/bin/camrig upload                 # flush pending clips to R2 + prune
/opt/camrig/venv/bin/camrig focus                  # live focus-assist page (see below)
/opt/camrig/venv/bin/camrig supervise --no-cloud   # run scheduler without Cloudflare
/opt/camrig/venv/bin/camrig boot                   # NTP sync + catch-up upload
/opt/camrig/venv/bin/camrig shutdown --skip-poweroff   # upload+arm wake, but stay up
```

## Focusing the lens (headless, over Tailscale)

The IMX296 uses a **manual-focus** C/CS-mount lens — you set focus by turning the
lens ring. Since the Pi is headless and reached over Tailscale, `camrig focus`
serves a browser page with a live view and a **sharpness score**:

```bash
/opt/camrig/venv/bin/camrig focus                  # full sensor, :8080, auto-exposure
/opt/camrig/venv/bin/camrig focus --framerate 8    # lower fps if the link is slow
/opt/camrig/venv/bin/camrig focus --shutter 2000 --gain 4   # match capture exposure
```

It prints the tailnet URLs to open (e.g. `http://pi-rig-01.<tailnet>.ts.net:8080/`).
On the page, **turn the lens ring until the focus score peaks** — the bar is
relative to the best value seen since the last reset. A centre ROI box marks the
region measured (adjust its size with the slider). Toggle **audio** for a tone
whose pitch rises as focus improves, so you can watch the lens instead of the
screen. The score is a variance-of-Laplacian sharpness metric computed in the
browser; denoise is forced off while focusing so softness can't be hidden.

Stop with Ctrl-C. Nothing is recorded — it only streams while the page is open.
The camera can't be recording (scheduled capture) at the same time, so run this
during setup or pause `cam-supervisor` first.

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
- ~~**Basler (second backend)**~~ — done: the ace 2 GigE backend lives in
  [`camrig/basler.py`](camrig/basler.py) behind the same profile/metadata
  interface. A USB3 dart would reuse the same module (pypylon is
  transport-agnostic; drop the GigE-specific `[basler]` keys).
- **Live preview** in the page via a libcamera low-res second stream.

## License

Apache License 2.0 — see [LICENSE](LICENSE).
Copyright 2026 Playstate UG (trading as SPAIA).
