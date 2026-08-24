"""Configuration loading with built-in defaults.

The deployed config lives at /etc/camrig/config.toml (override via CAMRIG_CONFIG).
Parsing uses the stdlib tomllib (Python >=3.11), so there is no third-party
dependency for config. Unknown keys are ignored; missing keys fall back to the
defaults defined here, so a partial config file is always valid.
"""

from __future__ import annotations

import json
import os
import tomllib
from dataclasses import dataclass, field, fields, is_dataclass
from pathlib import Path
from typing import Any

DEFAULT_CONFIG_PATH = Path(os.environ.get("CAMRIG_CONFIG", "/etc/camrig/config.toml"))


@dataclass
class CaptureConfig:
    # Camera backend: "rpicam" (Pi Global Shutter IMX296) or "basler"
    # (Basler ace 2 mono over GigE; transport settings live in [basler]).
    camera: str = "rpicam"
    profile: str = "mjpeg"
    width: int = 1456
    height: int = 1088
    framerate: int = 60
    quality: int = 95
    shutter_us: int = 2000
    # rpicam: analogue gain multiplier. basler: sensor gain in dB. 0 = auto.
    gain: float = 0.0
    # When shutter_us/gain are left at 0 (auto), converge auto-exposure/gain
    # briefly at the start of each clip, then hold the converged value fixed
    # for the rest of it -- steady exposure without the AE algorithm hunting
    # mid-recording. No effect if both shutter_us and gain are manual.
    auto_lock: bool = False
    # Max time to let auto-exposure/gain converge before locking: basler
    # polls ExposureAuto/GainAuto for this long, rpicam runs a discarded
    # warm-up capture of this length.
    auto_lock_warmup_ms: int = 1000
    denoise: str = "cdn_off"
    clip_seconds: int = 300
    max_session_seconds: int = 600


@dataclass
class BaslerConfig:
    """GigE transport/device settings for the Basler backend (camera = "basler")."""

    # Device selection; both empty = first Basler GigE camera found.
    serial: str = ""
    ip: str = ""
    # GevSCPSPacketSize (bytes). 1500 works on any link; 8192/9000 needs a
    # jumbo-frame MTU on the camera NIC and cuts per-packet overhead.
    packet_size: int = 1500
    # GevSCPD (ticks) between packets; raise if frames drop while other
    # traffic shares the link. 0 = as fast as the wire allows.
    inter_packet_delay: int = 0
    # Mono8 is what the mjpeg/ffv1 pipelines expect; other formats (e.g.
    # Mono12p) are only meaningful with profile = "raw".
    pixel_format: str = "Mono8"


@dataclass
class PostprocessConfig:
    enabled: bool = True
    preview_width: int = 728
    preview_fps: int = 30
    preview_crf: int = 28
    motion_width: int = 728
    motion_threshold: int = 12
    nice: int = 10
    # Insect-vs-plant discriminators (camrig.motion tracks: straightness = net
    # displacement / path length, chronic = how persistently active a blob's
    # cells stayed). Not applied by the analysis itself yet -- for now these
    # only drive the camrig.motion_view filter sliders, tuned there and
    # persisted here so they survive between sessions. 0.0/1.0 = no filtering.
    min_straightness: float = 0.0
    max_chronic: float = 1.0


@dataclass
class ScheduleConfig:
    start_hour: int = 5
    stop_hour: int = 22
    interval_min: int = 30


@dataclass
class StorageConfig:
    nvme_mount: str = "/mnt/nvme"
    sd_fallback_dir: str = "/home/spaia/recordings"
    keep_days: int = 2
    min_free_gb: int = 10
    # Delete a clip's local files as soon as it is marked uploaded, instead of
    # holding them under the keep_days / min_free_gb retention rules.
    delete_after_upload: bool = False


@dataclass
class UploadConfig:
    enabled: bool = True
    # Upload each clip right after its postprocess (False = nightly/boot only).
    immediate: bool = True
    # Ship the full-res video itself. False = sidecars only (preview, motion,
    # pts, json); the full-res clip then lives only on the device until pruned.
    full_res: bool = True
    rclone_remote: str = "r2"
    bucket: str = "spaia-cam"


@dataclass
class PowerConfig:
    wake_hour: int = 5


@dataclass
class LedConfig:
    # Flash the onboard activity LED a few times before each recording
    # starts (scheduled, triggered, or startup), as a visible "about to
    # record" cue. No-op if the LED sysfs path isn't found (e.g. off-Pi).
    enabled: bool = True
    flashes: int = 3
    on_ms: int = 150
    off_ms: int = 150


@dataclass
class StartupConfig:
    # Record one extra clip shortly after the supervisor starts, in addition
    # to the normal schedule — confirms the rig is capturing without waiting
    # for the next :00/:30 slot. Keep the delay generous: boot already stacks
    # Wi-Fi association, NVMe enumeration, and cam-boot's NTP sync/catch-up
    # upload, and starting the camera (CSI power-up + sustained NVMe writes)
    # too soon on top of that has caused undervoltage hangs in practice.
    record: bool = False
    delay_seconds: int = 150


@dataclass
class CloudConfig:
    worker_ws_url: str = "wss://your-worker.example.workers.dev/device"
    device_id: str = "pi-rig-01"
    device_token_file: str = "/etc/camrig/device_token"


@dataclass
class CaptiveConfig:
    # Fallback Wi-Fi AP + captive-portal focus page, started by cam-boot.service
    # when there's no internet after boot (fresh deployment, wrong Wi-Fi creds,
    # out of range) so a phone can join and reach the focus page directly —
    # see "Focusing the lens" in README.md.
    enabled: bool = True
    ssid: str = "camrig-setup"
    # Empty = open network. Set a passphrase (8-63 chars) for WPA2-PSK.
    psk: str = ""
    ap_ip: str = "10.42.0.1"
    port: int = 8080
    # Idle timeout (minutes): torn down this long after the last request, so
    # it doesn't sit broadcasting/powered for a rig meant to run unattended.
    timeout_minutes: int = 15


@dataclass
class Config:
    capture: CaptureConfig = field(default_factory=CaptureConfig)
    basler: BaslerConfig = field(default_factory=BaslerConfig)
    postprocess: PostprocessConfig = field(default_factory=PostprocessConfig)
    schedule: ScheduleConfig = field(default_factory=ScheduleConfig)
    storage: StorageConfig = field(default_factory=StorageConfig)
    upload: UploadConfig = field(default_factory=UploadConfig)
    power: PowerConfig = field(default_factory=PowerConfig)
    led: LedConfig = field(default_factory=LedConfig)
    startup: StartupConfig = field(default_factory=StartupConfig)
    cloud: CloudConfig = field(default_factory=CloudConfig)
    captive: CaptiveConfig = field(default_factory=CaptiveConfig)

    def device_token(self) -> str | None:
        """Read the device bearer token, or None if the file is absent."""
        path = Path(self.cloud.device_token_file)
        try:
            return path.read_text(encoding="utf-8").strip()
        except OSError:
            return None


def _apply_section(section_obj: Any, data: dict[str, Any]) -> None:
    """Overlay a TOML table onto a dataclass instance, keeping known keys only."""
    known = {f.name for f in fields(section_obj)}
    for key, value in data.items():
        if key in known:
            setattr(section_obj, key, value)


def load_config(path: str | os.PathLike[str] | None = None) -> Config:
    """Load configuration, falling back to defaults for any missing file/keys."""
    cfg = Config()
    config_path = Path(path) if path is not None else DEFAULT_CONFIG_PATH
    try:
        raw = tomllib.loads(config_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return cfg
    for section_name, section_data in raw.items():
        section_obj = getattr(cfg, section_name, None)
        if is_dataclass(section_obj) and isinstance(section_data, dict):
            _apply_section(section_obj, section_data)
    return cfg


def set_config_value(path: str | os.PathLike[str], section: str, key: str, value: str) -> None:
    """Set ``key = value`` inside ``[section]`` of the TOML file at ``path``.

    A targeted line-level patch, not a full TOML round-trip: it updates an
    existing ``key = ...`` line in place, or appends the key to the section
    (creating the section if needed), leaving every other line -- including
    comments -- untouched. Only safe for config files (like this project's)
    that don't put comments on the same line as a value; a general TOML
    writer would need a library (tomlkit) this project doesn't otherwise need.
    """
    path = Path(path)
    try:
        lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    except FileNotFoundError:
        lines = []

    header = f"[{section}]"
    key_line = f"{key} = {value}\n"
    section_start: int | None = None
    section_end = len(lines)
    for i, line in enumerate(lines):
        stripped = line.strip()
        if section_start is not None and stripped.startswith("[") and stripped.endswith("]"):
            section_end = i
            break
        if stripped == header:
            section_start = i

    if section_start is None:
        if lines and not lines[-1].endswith("\n"):
            lines.append("\n")
        lines.append(f"\n{header}\n{key_line}")
    else:
        for i in range(section_start + 1, section_end):
            if lines[i].split("=", 1)[0].strip() == key:
                lines[i] = key_line
                break
        else:
            lines.insert(section_end, key_line)

    path.write_text("".join(lines), encoding="utf-8")


def format_toml_scalar(value: Any, type_name: str) -> str:
    """Render a Python value as a TOML scalar literal, for ``set_config_value``.

    ``type_name`` is a dataclass field's ``.type`` (a string, since every
    config dataclass module uses ``from __future__ import annotations``):
    "bool", "int", "float", or "str".
    """
    if type_name == "bool":
        return "true" if value else "false"
    if type_name == "int":
        return str(int(value))
    if type_name == "float":
        return repr(float(value))
    return json.dumps(str(value))
