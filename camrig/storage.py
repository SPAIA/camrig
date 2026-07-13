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
SIDECAR_SUFFIXES = (".pts", ".json", ".preview.mp4", ".motion.json")
# Marker written next to a clip once rclone has confirmed it uploaded.
UPLOADED_MARKER = ".uploaded"
# In-progress staging suffix (capture and postprocess outputs).
PART_SUFFIX = ".part"
# A .part family untouched for this long is orphaned: an active capture or
# postprocess keeps its staging files' mtimes fresh.
PARTIAL_GRACE_SECONDS = 600


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


def sweep_partials(
    base: Path,
    *,
    grace_seconds: int = PARTIAL_GRACE_SECONDS,
    dry_run: bool = False,
) -> int:
    """Recover or remove stale ``*.part`` staging files. Returns families touched.

    A crash or power cut mid-capture leaves a clip staged as ``.part`` —
    invisible to upload, postprocess and prune, so it would leak disk forever.
    A family with video, pts and json all present is salvaged by renaming into
    place (a truncated intra-only clip is still analysable); incomplete
    leftovers (including orphaned postprocess outputs, which regenerate) are
    deleted. Families touched within ``grace_seconds`` are skipped: they may
    belong to a capture or postprocess that is still running.
    """
    families: dict[Path, list[Path]] = {}
    for part in base.rglob(f"*{PART_SUFFIX}"):
        if not part.is_file():
            continue
        final = part.with_name(part.name[: -len(PART_SUFFIX)])
        # Group by clip stem: clip.mkv/.pts/.json share a key; postprocess
        # outputs (clip.preview.mp4, clip.motion.json) form their own groups.
        families.setdefault(final.with_suffix(""), []).append(part)

    now = time.time()
    touched = 0
    for stem, parts in sorted(families.items()):
        if now - max(p.stat().st_mtime for p in parts) < grace_seconds:
            log.info("Leaving %s.*%s: recently written", stem.name, PART_SUFFIX)
            continue
        # Keyed by the *final* suffix (.mkv/.pts/.json once .part is stripped).
        by_suffix: dict[str, tuple[Path, Path]] = {}
        for part in parts:
            final = part.with_name(part.name[: -len(PART_SUFFIX)])
            by_suffix[final.suffix] = (part, final)
        video = next(
            (by_suffix[s] for s in CLIP_SUFFIXES if s in by_suffix), None
        )
        complete = video and ".pts" in by_suffix and ".json" in by_suffix
        if complete:
            if dry_run:
                print(f"would salvage {stem.name}.*")
            else:
                # Sidecars first, video last: never discoverable half-renamed.
                for suffix in (".pts", ".json", *CLIP_SUFFIXES):
                    if suffix in by_suffix:
                        part, final = by_suffix[suffix]
                        part.replace(final)
                log.warning("Salvaged interrupted clip %s", video[1].name)
        else:
            for part in parts:
                if dry_run:
                    print(f"would delete {part}")
                else:
                    part.unlink(missing_ok=True)
            log.warning(
                "Deleted %d incomplete staging file(s) for %s", len(parts), stem.name
            )
        touched += 1
    return touched


def prune(cfg: Config, base: Path) -> int:
    """Free space by deleting old, already-uploaded clips.

    A clip is eligible only if it has been uploaded AND is older than
    ``keep_days``. Pruning stops once free space is above ``min_free_gb``.
    With ``delete_after_upload`` both limits are bypassed: every uploaded clip
    is deleted immediately. Returns the number of clips removed.
    """
    immediate = cfg.storage.delete_after_upload
    keep_seconds = cfg.storage.keep_days * 86400
    now = time.time()
    removed = 0

    for clip in iter_clips(base):
        if not immediate and free_gib(base) >= cfg.storage.min_free_gb:
            break
        if not is_uploaded(clip):
            continue
        if not immediate and now - clip.stat().st_mtime < keep_seconds:
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
