# Basler ace 2 mono over GigE — setup on the Pi 5

The second capture backend (`capture.camera = "basler"`) drives a Basler
ace 2 mono camera over Gigabit Ethernet for side-by-side comparison with the
IMX296. Same profiles, same sidecars (`.pts` from the camera's hardware
timestamps, `.json` metadata), same postprocess/upload chain — clips from both
cameras land in the same day directories and R2 layout.

## Why this pairing is a good comparison

- Mono sensor: no Bayer filter, so more light sensitivity and per-pixel
  sharpness than the colour IMX296 — exactly the trade the README flags.
- Global shutter (all ace 2 mono models used here), so the no-skew property is
  preserved.
- Hardware per-frame timestamps from the camera's own 1 GHz clock go straight
  into the `.pts` sidecar — timing is authoritative even if the Pi stalls.

## Wiring and network layout

Connect the camera **directly to the Pi's Ethernet port** (no switch needed;
GigE cameras auto-MDI-X, any Cat5e+ cable). The Pi then needs its internet
(Tailscale, R2 uploads, NTP) over **Wi-Fi** — `eth0` becomes a dedicated
camera link carrying ~1 Gbit/s of frames.

Power: the plain GigE ace 2 models are **not** powered over the data cable
unless you have a PoE model + injector. Check your model; non-PoE cameras need
the 6-pin Hirose power connector.

### Give eth0 a static address

Pick a private subnet that nothing else uses, e.g. `192.168.42.0/24`:

```bash
sudo nmcli con add type ethernet ifname eth0 con-name basler \
  ipv4.method manual ipv4.addresses 192.168.42.1/24
sudo nmcli con up basler
```

(Do **not** set a gateway on this connection — the default route must stay on
Wi-Fi.)

### Give the camera an address

Out of the box the camera falls back to link-local (169.254.x.x), which pylon
can reach but is slow to enumerate. Assigning a persistent static IP is nicer:

```bash
# See what's on the link (works even across subnets — discovery is broadcast):
/opt/camrig/venv/bin/python -m camrig.basler --list

# Persist a static IP into the camera with Basler's tool (part of the pylon
# SDK; run it from any machine on the link, or use pylon Viewer on a laptop):
#   pylon-ipconfig / "pylon IP Configurator" → set 192.168.42.2/24
```

Then set in `/etc/camrig/config.toml`:

```toml
[capture]
camera = "basler"
width = 1920      # your model's native size, e.g. a2A1920-51gm = 1920x1200
height = 1200
framerate = 50    # see bandwidth note below

[basler]
ip = "192.168.42.2"
```

## Bandwidth: what frame rate fits down the wire

GigE carries ~115 MB/s of pixel payload. Mono8 needs
`width × height × fps` bytes/s:

| Resolution | Max fps over GigE (Mono8) |
| ---------- | ------------------------- |
| 1920×1200  | ~50                       |
| 1600×1100  | ~65                       |
| 1456×1088  | ~72 (IMX296-matched ROI)  |

The producer logs a warning at start-up if the configured rate exceeds the
link. For an apples-to-apples comparison with the IMX296 you can set a centred
1456×1088 ROI on the Basler and run both at 60 fps.

The Pi-side encode is the other ceiling: MJPEG-encoding gray frames in
software (ffmpeg) sustains full-rate on the Pi 5, but keep an eye on
`camrig record --seconds 10` CPU usage at your chosen resolution; the `ffv1`
profile will likely not keep up at full rate (same caveat as the IMX296).

## Packet size / dropped frames

Two `[basler]` knobs, in order of preference:

1. **`packet_size`** — try jumbo frames first: `sudo ip link set eth0 mtu 9000`
   (add `ethernet.mtu 9000` to the nmcli connection to persist). If that
   sticks (`ip link show eth0`), set `packet_size = 8192`. If the NIC refuses
   the MTU, stay at `1500` — it works, just with more per-packet CPU.
2. **`inter_packet_delay`** — if you still see `grab failed` lines in the
   journal, raise this (start ~1000 ticks) to pace the camera's bursts at the
   cost of peak bandwidth (may force a lower fps).

Also raise the kernel receive buffer if grabs fail at high rates:

```bash
sudo sysctl -w net.core.rmem_max=16777216 net.core.rmem_default=16777216
```

(persist in `/etc/sysctl.d/90-camrig-gige.conf`).

## Install

```bash
# assignments must come AFTER sudo — sudo's env_reset strips variables set
# before it, and the script would silently skip pypylon
sudo WITH_BASLER=1 CAM_USER=spaia ./setup/install.sh
```

This adds `pypylon` (which bundles the pylon runtime — no separate SDK needed
on the Pi) to the venv. Everything else is unchanged.

## Verify

```bash
/opt/camrig/venv/bin/python -m camrig.basler --list       # camera visible?
/opt/camrig/venv/bin/camrig record --camera basler --dry-run
/opt/camrig/venv/bin/camrig record --camera basler --seconds 10
/opt/camrig/venv/bin/camrig focus --camera basler          # focus the lens
```

Check the 10 s clip: `.pts` deltas should match the configured frame rate with
no gaps (a dropped frame shows up as a doubled delta), and `ffprobe` should
report the configured resolution. The metadata sidecar records
`"camera": "basler"` / `"sensor": "basler-ace2-mono"` so downstream analysis
can tell the rigs apart.

## Switching between cameras

`capture.camera` in the config selects the default for scheduled captures and
the remote trigger; `--camera` on `camrig record` / `camrig focus` overrides
per-invocation. Both cameras can stay connected — they are driven by different
stacks (libcamera vs pylon) and don't contend, but the supervisor still
records from only one (the configured backend) at a time.
