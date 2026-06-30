"""Recording storage: pick the fastest writable base, and prune by retention.

Prefers an NVMe mountpoint (PCIe HAT) for the sustained write bandwidth that
high-fidelity capture needs; falls back to an SD-card directory when the NVMe
mount is absent or not writable.
"""

from __future__ import annotations

import logging
import os
import shutil
import time
from datetime import date, datetime
from pathlib import Path

from .config import Config

log = logging.getLogger("camrig.storage")

# Sidecar/companion suffixes that belong to a clip and move/prune with it.
CLIP_SUFFIXES = (".mkv", ".raw")
SIDECAR_SUFFIXES = (".pts", ".json")
# Marker written next to a clip once rclone has confirmed it uploaded.
UPLOADED_MARKER = ".uploaded"


def _is_writable_mount(path: Path) -> bool:
    """True if path is a mountpoint (NVMe mounted) and we can write into it."""
    try:
        if not path.is_dir() or not os.path.ismount(path):
            return False
        return os.access(path, os.W_OK)
    except OSError:
        return False


def select_base_dir(cfg: Config) -> Path:
    """Return the recordings base dir, preferring NVMe, falling back to SD.

    The returned directory is created if needed. The recordings live under
    ``<base>/recordings``.
    """
    nvme = Path(cfg.storage.nvme_mount)
    if _is_writable_mount(nvme):
        base = nvme / "recordings"
        log.info("Using NVMe storage at %s", base)
    else:
        base = Path(cfg.storage.sd_fallback_dir)
        log.warning(
            "NVMe mount %s unavailable; falling back to SD storage at %s",
            nvme,
            base,
        )
    base.mkdir(parents=True, exist_ok=True)
    return base


def day_dir(base: Path, day: date | None = None) -> Path:
    """Return (and create) the YYYY-MM-DD directory for the given day."""
    day = day or datetime.now().astimezone().date()
    d = base / day.isoformat()
    d.mkdir(parents=True, exist_ok=True)
    return d


def free_gib(path: Path) -> float:
    """Free space at path in GiB."""
    usage = shutil.disk_usage(path)
    return usage.free / (1024**3)


def _clip_companions(clip: Path) -> list[Path]:
    """All files associated with a clip (sidecars + uploaded marker)."""
    companions = [clip]
    for suffix in SIDECAR_SUFFIXES:
        companions.append(clip.with_suffix(suffix))
    companions.append(clip.with_name(clip.name + UPLOADED_MARKER))
    return [p for p in companions if p.exists()]


def mark_uploaded(clip: Path) -> None:
    """Record that a clip has been uploaded, so retention may later prune it."""
    marker = clip.with_name(clip.name + UPLOADED_MARKER)
    marker.write_text(f"{time.time():.0f}\n", encoding="utf-8")


def is_uploaded(clip: Path) -> bool:
    return clip.with_name(clip.name + UPLOADED_MARKER).exists()


def iter_clips(base: Path):
    """Yield every clip file (mkv/raw) under the recordings tree, oldest first."""
    clips = [
        p
        for p in base.rglob("*")
        if p.is_file() and p.suffix in CLIP_SUFFIXES
    ]
    clips.sort(key=lambda p: p.stat().st_mtime)
    return clips


def prune(cfg: Config, base: Path) -> int:
    """Free space by deleting old, already-uploaded clips.

    A clip is eligible only if it has been uploaded AND is older than
    ``keep_days``. Pruning stops once free space is above ``min_free_gb``.
    Returns the number of clips removed.
    """
    keep_seconds = cfg.storage.keep_days * 86400
    now = time.time()
    removed = 0

    for clip in iter_clips(base):
        if free_gib(base) >= cfg.storage.min_free_gb:
            break
        if not is_uploaded(clip):
            continue
        if now - clip.stat().st_mtime < keep_seconds:
            continue
        for companion in _clip_companions(clip):
            try:
                companion.unlink()
            except OSError as exc:
                log.warning("Could not delete %s: %s", companion, exc)
        removed += 1
        log.info("Pruned uploaded clip %s", clip.name)

    # Tidy up now-empty day directories.
    for d in sorted(base.glob("*"), reverse=True):
        if d.is_dir() and not any(d.iterdir()):
            d.rmdir()

    return removed
