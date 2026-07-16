"""Configuration loading with built-in defaults.

The deployed config lives at /etc/camrig/config.toml (override via CAMRIG_CONFIG).
Parsing uses the stdlib tomllib (Python >=3.11), so there is no third-party
dependency for config. Unknown keys are ignored; missing keys fall back to the
defaults defined here, so a partial config file is always valid.
"""

from __future__ import annotations

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
    motion_detector: str = "blob-track-v1"
    # Detector knobs, keyed by detector id: {"blob-track-v1": {"bg_alpha": 0.02}}.
    # Omitted knobs fall back to the detector's own defaults, so {} means stock.
    # Validated at load against the target detector's signature.
    motion_params: dict[str, dict[str, Any]] = field(default_factory=dict)
    nice: int = 10

    def active_motion_params(self) -> dict[str, Any]:
        """Configured knob overrides for the selected detector."""
        return self.motion_params.get(self.motion_detector, {})


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
class CloudConfig:
    worker_ws_url: str = "wss://your-worker.example.workers.dev/device"
    device_id: str = "pi-rig-01"
    device_token_file: str = "/etc/camrig/device_token"


@dataclass
class Config:
    capture: CaptureConfig = field(default_factory=CaptureConfig)
    basler: BaslerConfig = field(default_factory=BaslerConfig)
    postprocess: PostprocessConfig = field(default_factory=PostprocessConfig)
    schedule: ScheduleConfig = field(default_factory=ScheduleConfig)
    storage: StorageConfig = field(default_factory=StorageConfig)
    upload: UploadConfig = field(default_factory=UploadConfig)
    power: PowerConfig = field(default_factory=PowerConfig)
    cloud: CloudConfig = field(default_factory=CloudConfig)

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


def _validate_postprocess(pp: PostprocessConfig, raw: dict[str, Any]) -> None:
    """Reject an unusable motion detector/knob set before any clip is decoded.

    Unknown keys are ignored everywhere else, but a mistyped detector or knob
    would otherwise surface only after ffmpeg had decoded a whole clip, once per
    clip, forever — and the retry it triggers can never succeed.
    """
    # motion_threshold moved into [postprocess.motion_params.<detector>] as
    # `threshold`. Ignoring it silently would quietly revert a tuned value.
    if "motion_threshold" in raw.get("postprocess", {}):
        raise ValueError(
            "[postprocess] motion_threshold has moved: set it as `threshold` "
            f'under [postprocess.motion_params."{pp.motion_detector}"] instead.'
        )

    from . import motion  # local: keeps numpy off the import path of every CLI

    motion.detector_fn(pp.motion_detector)  # raises naming the alternatives
    for name, params in pp.motion_params.items():
        if not isinstance(params, dict):
            raise ValueError(
                f"[postprocess.motion_params.{name}] must be a table of knobs"
            )
        motion.detector_fn(name)
        motion.resolve_params(name, params)


def load_config(path: str | os.PathLike[str] | None = None) -> Config:
    """Load configuration, falling back to defaults for any missing file/keys.

    Raises ValueError if the motion detector or its knobs are unusable.
    """
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
    _validate_postprocess(cfg.postprocess, raw)
    return cfg
