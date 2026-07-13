"""Basler ace 2 (GigE) capture producer — the second camera backend.

Runs as its own process (``python -m camrig.basler``) with an rpicam-shaped
interface, so the rest of camrig (Recording pipelines, the supervisor's SIGINT
stop, --dry-run printing) treats both backends identically:

* raw frames go to ``-o -`` (stdout, piped into ffmpeg for mjpeg/ffv1 muxing)
  or straight to a file (profile "raw");
* ``--save-pts`` writes the same "timecode format v2" sidecar rpicam does,
  from the camera's own hardware timestamps (GigE tick clock), so per-frame
  timing stays authoritative even if the Pi stalls;
* ``--timeout`` is the capture length in ms (0 = run until stopped);
* SIGINT finishes the clip cleanly (flush pts, close camera, exit 0).

pypylon (the Basler pylon SDK wrapper) is imported lazily inside main(), so
importing this module — and every --dry-run path that only builds argv — works
on machines without the SDK installed.

Exposure semantics follow rpicam: ``--shutter`` in µs (0 = auto-expose) and
``--gain`` 0 = auto. Note the unit difference: Basler gain is in **dB**, not
an analogue multiplier.

Network/transport tuning (packet size, inter-packet delay, device selection)
comes from the [basler] config section via flags; see docs/basler-gige.md for
wiring and IP setup on the Pi.
"""

from __future__ import annotations

import argparse
import signal
import sys
import time

# GigE payload ceiling (bytes/s) after packet overhead; used only to warn.
_GIGE_BYTES_PER_S = 115_000_000


def _log(msg: str) -> None:
    """Producer diagnostics go to stderr; stdout may be the frame pipe."""
    print(f"camrig.basler: {msg}", file=sys.stderr, flush=True)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m camrig.basler",
        description="Basler GigE capture producer (rpicam-shaped interface)",
    )
    p.add_argument("--width", type=int, required=False, default=0,
                   help="ROI width (0 = full sensor); centred, clamped to sensor")
    p.add_argument("--height", type=int, required=False, default=0,
                   help="ROI height (0 = full sensor)")
    p.add_argument("--framerate", type=float, default=30.0)
    p.add_argument("--shutter", type=int, default=0, help="exposure in us; 0 = auto")
    p.add_argument("--gain", type=float, default=0.0, help="gain in dB; 0 = auto")
    p.add_argument("--timeout", type=int, default=0,
                   help="capture length in ms; 0 = until SIGINT")
    p.add_argument("--save-pts", dest="save_pts", default=None,
                   help="write per-frame timestamps (timecode format v2)")
    p.add_argument("-o", "--output", required=False, default="-",
                   help='"-" = raw frames on stdout; else a file path')
    p.add_argument("--serial", default="", help="select camera by serial number")
    p.add_argument("--ip", default="", help="select camera by IP address")
    p.add_argument("--packet-size", type=int, default=0,
                   help="GevSCPSPacketSize; 0 = leave camera default")
    p.add_argument("--inter-packet-delay", type=int, default=0,
                   help="GevSCPD ticks between packets; 0 = none")
    p.add_argument("--pixel-format", default="Mono8")
    p.add_argument("--list", action="store_true",
                   help="list reachable Basler cameras and exit")
    return p


def _set(camera, name: str, value) -> bool:
    """Best-effort GenICam node write; missing/read-only nodes just warn.

    Node availability differs across pylon versions and camera families, so a
    failed optional write must not kill the capture.
    """
    try:
        getattr(camera, name).SetValue(value)
        return True
    except Exception as exc:  # pypylon raises various GenICam exception types
        _log(f"could not set {name}={value!r}: {exc}")
        return False


def _align(value: int, minimum: int, inc: int) -> int:
    """Clamp down to the node's increment grid (GenICam values must align)."""
    inc = inc or 1
    return minimum + ((value - minimum) // inc) * inc


def _configure_roi(camera, width: int, height: int) -> tuple[int, int]:
    """Set a centred ROI, clamped and increment-aligned; return actual size."""
    # Reset offsets first so Width/Height can grow to the requested size.
    _set(camera, "OffsetX", camera.OffsetX.GetMin())
    _set(camera, "OffsetY", camera.OffsetY.GetMin())

    wmax, hmax = camera.Width.GetMax(), camera.Height.GetMax()
    want_w = width or wmax
    want_h = height or hmax
    w = _align(min(want_w, wmax), camera.Width.GetMin(), camera.Width.GetInc())
    h = _align(min(want_h, hmax), camera.Height.GetMin(), camera.Height.GetInc())
    if (w, h) != (want_w, want_h):
        _log(f"requested {want_w}x{want_h}, using {w}x{h} (sensor {wmax}x{hmax})")
    camera.Width.SetValue(w)
    camera.Height.SetValue(h)
    _set(camera, "OffsetX",
         _align((wmax - w) // 2, camera.OffsetX.GetMin(), camera.OffsetX.GetInc()))
    _set(camera, "OffsetY",
         _align((hmax - h) // 2, camera.OffsetY.GetMin(), camera.OffsetY.GetInc()))
    return w, h


def _list_cameras(pylon) -> int:
    """List cameras, including GigE devices pylon can see but not open
    (wrong subnet / unconfigured IP) — those are the common bring-up state."""
    try:
        tl = pylon.TlFactory.GetInstance().CreateTl("BaslerGigE")
        devices = tl.EnumerateAllDevices()
    except Exception:  # no GigE transport available; fall back to openable only
        devices = pylon.TlFactory.GetInstance().EnumerateDevices()
    if not devices:
        print("No Basler cameras found — not even by broadcast discovery.")
        print("Check: cable/camera power, link up (ip -br link show eth0), and")
        print("that eth0 has an IPv4 address (discovery needs a source address).")
        return 1
    openable = {d.GetSerialNumber()
                for d in pylon.TlFactory.GetInstance().EnumerateDevices()}
    for dev in devices:
        def _get(name):
            try:
                return getattr(dev, f"Get{name}")()
            except Exception:
                return "-"
        state = ("ok" if dev.GetSerialNumber() in openable
                 else "UNREACHABLE (IP not on eth0's subnet — fix camera or Pi IP)")
        print(f"{dev.GetModelName()}  serial={dev.GetSerialNumber()}  "
              f"ip={_get('IpAddress')}/{_get('SubnetMask')}  "
              f"mac={_get('MacAddress')}  [{state}]")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    try:
        from pypylon import pylon
    except ImportError:
        _log("pypylon is not installed — install with: pip install 'camrig[basler]'")
        return 1

    if args.list:
        return _list_cameras(pylon)

    # Select the device: serial and/or IP filters, else first camera found.
    info = pylon.CDeviceInfo()
    if args.serial:
        info.SetSerialNumber(args.serial)
    if args.ip:
        info.SetIpAddress(args.ip)
    try:
        camera = pylon.InstantCamera(
            pylon.TlFactory.GetInstance().CreateFirstDevice(info)
        )
        camera.Open()
    except Exception as exc:
        _log(f"cannot open camera: {exc}")
        return 1

    try:
        return _capture(pylon, camera, args)
    finally:
        try:
            camera.Close()
        except Exception:
            pass


def _capture(pylon, camera, args) -> int:
    _log(f"using {camera.GetDeviceInfo().GetModelName()} "
         f"serial={camera.GetDeviceInfo().GetSerialNumber()}")

    _set(camera, "PixelFormat", args.pixel_format)
    width, height = _configure_roi(camera, args.width, args.height)

    # Manual exposure for repeatability when given; otherwise auto (rpicam
    # semantics: 0 = auto).
    if args.shutter > 0:
        _set(camera, "ExposureAuto", "Off")
        _set(camera, "ExposureTime", float(args.shutter))
    else:
        _set(camera, "ExposureAuto", "Continuous")
    if args.gain > 0:
        _set(camera, "GainAuto", "Off")
        _set(camera, "Gain", float(args.gain))
    else:
        _set(camera, "GainAuto", "Continuous")

    _set(camera, "AcquisitionFrameRateEnable", True)
    _set(camera, "AcquisitionFrameRate", float(args.framerate))

    # GigE transport tuning (see docs/basler-gige.md).
    if args.packet_size > 0:
        _set(camera, "GevSCPSPacketSize", args.packet_size)
    if args.inter_packet_delay > 0:
        _set(camera, "GevSCPD", args.inter_packet_delay)

    if args.pixel_format == "Mono8":
        rate = width * height * args.framerate
        if rate > _GIGE_BYTES_PER_S:
            _log(f"warning: {width}x{height}@{args.framerate:g} Mono8 = "
                 f"{rate / 1e6:.0f} MB/s exceeds GigE (~115 MB/s); "
                 "expect a lower effective fps or dropped frames")

    # Hardware timestamps: GigE cameras count ticks of an on-camera clock.
    try:
        tick_hz = float(camera.GevTimestampTickFrequency.GetValue())
    except Exception:
        tick_hz = 1e9  # ace 2 default: 1 GHz (nanosecond ticks)

    # Enough queued buffers to ride out short stalls of the consumer pipe.
    _set(camera, "MaxNumBuffer", 64)

    stop = {"flag": False}

    def _on_sigint(signum, frame):  # finish the clip cleanly, like rpicam
        stop["flag"] = True

    signal.signal(signal.SIGINT, _on_sigint)
    signal.signal(signal.SIGTERM, _on_sigint)

    out = sys.stdout.buffer if args.output == "-" else open(args.output, "wb")
    pts = open(args.save_pts, "w", encoding="utf-8") if args.save_pts else None
    if pts:
        pts.write("# timecode format v2\n")

    frames = failed = 0
    first_ts: int | None = None
    deadline = (time.monotonic() + args.timeout / 1000.0) if args.timeout > 0 else None
    rc = 0

    camera.StartGrabbing(pylon.GrabStrategy_OneByOne)
    try:
        while camera.IsGrabbing() and not stop["flag"]:
            if deadline is not None and time.monotonic() >= deadline:
                break
            result = camera.RetrieveResult(5000, pylon.TimeoutHandling_Return)
            if not result.IsValid():
                _log("grab timeout: no frame within 5s (link down? fps too high?)")
                result.Release()
                continue
            try:
                if not result.GrabSucceeded():
                    failed += 1
                    _log(f"grab failed: {result.GetErrorDescription()}")
                    continue
                ts = result.GetTimeStamp()
                if first_ts is None:
                    first_ts = ts
                out.write(result.GetBuffer())
                if pts:
                    pts.write(f"{(ts - first_ts) / tick_hz * 1000.0:.3f}\n")
                frames += 1
            finally:
                result.Release()
    except BrokenPipeError:
        _log("output pipe closed by consumer; stopping")
        rc = 1
    except KeyboardInterrupt:
        pass
    finally:
        camera.StopGrabbing()
        for fh in (pts, out if out is not sys.stdout.buffer else None):
            if fh:
                try:
                    fh.close()
                except (OSError, BrokenPipeError):
                    pass
        if out is sys.stdout.buffer:
            try:
                out.flush()
            except (OSError, BrokenPipeError):
                pass

    if frames > 1 and first_ts is not None:
        _log(f"captured {frames} frames, {failed} failed grabs")
    elif frames == 0:
        _log("no frames captured")
        rc = rc or 1
    return rc


if __name__ == "__main__":
    sys.exit(main())
