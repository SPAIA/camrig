"""Send each clip's insect count to the SPAIA server as one "reading".

One clip = one counting window. After postprocess writes a clip's
``.motion.json``, ``build_reading`` runs the same filter pipeline as
``camrig.scoring`` (stitching, the scalar ``[postprocess]`` thresholds, then
the directional-burst filter) and turns the surviving tracks into the
reading body:

    {"deviceId", "spotId", "insectCount", "trails", "startTime", "endTime"}

Readings are never sent straight from the capture path. ``enqueue`` writes
the body to ``<base>/.readings/pending/<clip stem>.json`` and ``flush`` sends
everything queued, oldest first. The server identifies a reading by
deviceId + startTime and replaces on resend, so retries are safe as long as
startTime never changes -- which is why the body is frozen to disk at
enqueue time rather than rebuilt per attempt. Outcomes per response:

* 2xx -- sent; queue file deleted.
* 400/401/404 (any other 4xx) -- resending as-is can't help; the file moves
  to ``.readings/failed/`` with the server's response alongside it, for a
  human to look at.
* 5xx / network error -- left queued; ``flush`` stops there (the server or
  link is down, the rest would fail too) and the caller backs off.
"""

from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .config import Config
from .filters import FilterThresholds
from .motion_debug import load_motion
from .pts import FrameClock, load_frame_times
from .scoring import _compute_survival

log = logging.getLogger("camrig.readings")

QUEUE_DIR = ".readings"
TMP_SUFFIX = ".tmp"
REQUEST_TIMEOUT_SECONDS = 30

# flush() outcomes
SENT, RETRY, EMPTY = "sent", "retry", "empty"


def pending_dir(base: Path) -> Path:
    return base / QUEUE_DIR / "pending"


def failed_dir(base: Path) -> Path:
    return base / QUEUE_DIR / "failed"


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _clip_start(video: Path) -> datetime | None:
    """Capture start from the clip's metadata sidecar (``record.write_metadata``)."""
    meta_path = video.with_suffix(".json")
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        return datetime.fromisoformat(meta["started_at_utc"])
    except (OSError, ValueError, KeyError) as exc:
        log.error("No usable started_at_utc in %s: %s", meta_path, exc)
        return None


def _trail(track: dict, raw_tracks: list[dict], motion: dict, clock: FrameClock,
           start: datetime) -> dict:
    """One surviving (possibly stitched) track as a JSON trail.

    ``path`` is ``[seconds since clip start, x, y]`` per window, x/y
    normalised to 0..1 of the motion frame so they don't depend on
    ``postprocess.motion_width``. Times come from each raw member's own
    windows, so the gaps between stitched fragments show up as gaps in t.
    """
    windows = motion["windows"]
    width, height = motion["width"], motion["height"]
    path = []
    for raw_i in track["members"]:
        member = raw_tracks[raw_i]
        for k, (x, y) in enumerate(member["path"]):
            win = windows[member["w0"] + k]
            t = clock.time(win["f"] + win["n_frames"] / 2)
            path.append([round(t, 3), round(x / width, 4), round(y / height, 4)])
    t0, t1 = path[0][0], path[0][0] + track["duration_seconds"]
    return {
        "startTime": _iso(start + timedelta(seconds=t0)),
        "endTime": _iso(start + timedelta(seconds=t1)),
        "durationSeconds": track["duration_seconds"],
        "straightness": track["straightness"],
        "chronic": track["chronic"],
        "footprintRatio": track["footprint_ratio"],
        "stepRatio": track["step_ratio"],
        "meanArea": track["mean_area"],
        "fragments": len(track["members"]),
        "path": path,
    }


def build_reading(cfg: Config, video: Path) -> dict | None:
    """The reading body for one postprocessed clip, or None (logged) if its
    sidecars are missing/unusable.
    """
    motion = load_motion(video)
    start = _clip_start(video)
    if motion is None or start is None:
        return None
    framerate = motion.get("framerate", cfg.capture.framerate)
    pts_path = video.with_suffix(".pts")
    clock = (FrameClock.from_pts(load_frame_times(pts_path)) if pts_path.exists()
             else FrameClock.constant(framerate))

    thresholds = FilterThresholds.from_postprocess(cfg.postprocess)
    stitched, candidate_ids, burst_ids = _compute_survival(motion, thresholds, clock)
    survivors = sorted(candidate_ids - burst_ids)
    trails = [_trail(stitched.tracks[gi], motion["tracks"], motion, clock, start)
              for gi in survivors]
    trails.sort(key=lambda t: t["startTime"])

    end = start + timedelta(seconds=clock.time(motion["frame_count"]))
    return {
        "deviceId": cfg.readings.device_id or cfg.cloud.device_id,
        "spotId": cfg.readings.spot_id,
        "insectCount": len(trails),
        "trails": trails,
        "startTime": _iso(start),
        "endTime": _iso(end),
    }


def enqueue(cfg: Config, base: Path, video: Path) -> bool:
    """Build ``video``'s reading and queue it for ``flush``. Re-enqueueing a
    clip (e.g. after ``camrig postprocess --force``) overwrites its queued
    body; the server then replaces the earlier copy too.
    """
    reading = build_reading(cfg, video)
    if reading is None:
        return False
    queue = pending_dir(base)
    queue.mkdir(parents=True, exist_ok=True)
    dest = queue / f"{video.stem}.json"
    tmp = dest.with_name(dest.name + TMP_SUFFIX)
    tmp.write_text(json.dumps(reading), encoding="utf-8")
    os.replace(tmp, dest)
    log.info("Queued reading for %s: %d insect(s)", video.name, reading["insectCount"])
    return True


def _post(cfg: Config, body: bytes) -> tuple[int | None, str]:
    """POST one reading. Returns (HTTP status, response text); status None =
    network error (text is the error).
    """
    req = urllib.request.Request(
        cfg.readings.url, data=body, method="POST",
        headers={
            "Authorization": f"Bearer {cfg.readings.token}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT_SECONDS) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")
    except (urllib.error.URLError, OSError) as exc:
        return None, str(exc)


def flush(cfg: Config, base: Path) -> str:
    """Send every queued reading, oldest first. Returns SENT if the queue
    drained (rejected readings count -- they're moved aside), RETRY if a
    server/network error left readings queued, EMPTY if there was nothing
    to send.
    """
    queue = pending_dir(base)
    files = sorted(queue.glob("*.json"), key=lambda p: p.stat().st_mtime) if queue.is_dir() else []
    if not files:
        return EMPTY
    for path in files:
        status, text = _post(cfg, path.read_bytes())
        if status is not None and 200 <= status < 300:
            log.info("Sent reading %s (%s)", path.stem, text.strip())
            path.unlink(missing_ok=True)
        elif status is not None and 400 <= status < 500:
            log.error("Server rejected reading %s (HTTP %s): %s; moved to %s",
                      path.stem, status, text.strip(), failed_dir(base))
            failed = failed_dir(base)
            failed.mkdir(parents=True, exist_ok=True)
            os.replace(path, failed / path.name)
            (failed / f"{path.stem}.response.txt").write_text(f"HTTP {status}\n{text}\n",
                                                              encoding="utf-8")
        else:
            log.warning("Reading %s not sent (%s); %d left queued",
                        path.stem, f"HTTP {status}" if status else text,
                        sum(1 for p in files if p.exists()))
            return RETRY
    return SENT
