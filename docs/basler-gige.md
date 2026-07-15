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

## This rig's camera: a2A2448-105g5c (colour, 5GBASE-T)

The deployed unit is an **a2A2448-105g5c** — IMX548, 2448×2048, 105 fps at
full resolution, **colour** (so mono-vs-Bayer comparisons need the `m`
variant; this one compares sensor/optics only), with a 5GBASE-T port that
negotiates down to whatever the Pi side offers. The sensor gets faster as the
ROI height shrinks, so below full frame the *link* is always the constraint:
`fps_max ≈ link_bytes_per_s / (width × height)` for Mono8.

On the built-in 1 GbE port (~110 MB/s with headroom):

| Use                        | ROI       | max fps |
| -------------------------- | --------- | ------- |
| Full field of view         | 2448×2048 | ~21     |
| Full-width flight corridor | 2448×1200 | ~37     |
| Balanced                   | 1600×1200 | ~57     |
| IMX296 comparison twin     | 1456×1088 | 60–69   |
| Max temporal resolution    | 1200×864  | **105** (sensor cap) |

Verify each step up with a 10 s clip: flat `.pts` deltas = keeping up;
doubled deltas or `grab failed` in stderr = link or encode saturated.

### 2.5 GbE upgrade (~€20): full sensor at ~50 fps

The camera speaks NBASE-T (5G/2.5G/1G), so a **USB 3.0 → 2.5GbE adapter on a
Realtek RTL8156/RTL8156B** (UGREEN, Cable Matters, Plugable USBC-E2500, …)
plugged into a **blue USB 3 port** gives a 2.5G link ≈ 280 MB/s payload —
**2448×2048 @ ~50 fps**. Works out of the box on Pi OS (`r8152` driver).
Checklist:

- Not every cheap USB dongle qualifies: `ethtool ethX` must list
  `2500baseT/Full` under supported link modes (`lsusb` ID `0bda:8156` is the
  right chip). A 1GbE adapter gains nothing over the built-in port.
- The camera moves to the adapter's interface (usually `eth1`): re-point the
  nmcli profile — `sudo nmcli con mod basler connection.interface-name eth1
  ethernet.mtu 9000 && sudo nmcli con up basler`.
- At >200 MB/s use jumbo frames + `packet_size = 8192` and the rmem sysctl
  below; past that the Pi's software MJPEG encode and clip sizes become the
  limits, not the wire (`raw` profile to NVMe for short bursts).

### Colour fidelity bursts

`Mono8` is right for the daily tracking pipeline. For occasional colour
reference data set `pixel_format = "BayerRG8"` **with `profile = "ffv1"`**:
same bandwidth as Mono8, and the lossless gray FFV1 path preserves the Bayer
mosaic bit-exactly for offline demosaicing. Never pair Bayer with `mjpeg` —
lossy compression of the mosaic wrecks demosaicing. FFV1 encode speed limits
this to short, lower-fps bursts.

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
