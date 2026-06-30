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
    profile: str = "mjpeg"
    width: int = 1456
    height: int = 1088
    framerate: int = 60
    quality: int = 95
    shutter_us: int = 2000
    gain: float = 0.0
    denoise: str = "cdn_off"
    clip_seconds: int = 300
    max_session_seconds: int = 600


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


@dataclass
class UploadConfig:
    enabled: bool = True
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
