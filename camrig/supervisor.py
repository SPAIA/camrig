"""The capture supervisor: single owner of the camera.

Runs as a long-lived asyncio service (cam-supervisor.service). It:

* fires a scheduled 5-minute clip on each :00/:30 boundary within the active
  window, and
* serves on-demand "sessions" requested over the Cloudflare WebSocket
  (cloudlink), where a remote user triggers recording and a human counts bugs.

Only one rpicam process may use the camera at a time, so a single asyncio lock
serialises everything. Priority policy:

* A manual (triggered) start preempts an in-progress scheduled clip.
* Scheduled slots that land during an active manual session are skipped (logged).
* Manual sessions auto-stop at capture.max_session_seconds as a safety ceiling.

The supervisor reports the authoritative NTP-synced session start/stop times up
the socket so the Worker can anchor Cloudflare-stored bug counts to video frames.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from .config import Config
from . import record, storage

log = logging.getLogger("camrig.supervisor")


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class SessionState:
    session_id: str
    trigger: str  # "scheduled" | "triggered"
    started_at_utc: str
    clip_name: str


class Supervisor:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.base = storage.select_base_dir(cfg)
        self._camera_lock = asyncio.Lock()
        self._active: SessionState | None = None
        self._recording: record.Recording | None = None
        self._manual_active = False
        self._auto_stop_task: asyncio.Task | None = None
        # Set by run(); cloudlink uses it to push status/session events.
        self.on_event = None  # type: ignore[assignment]

    # ----- status -------------------------------------------------------

    def status(self) -> dict:
        s = self._active
        return {
            "type": "status",
            "state": "recording" if s else "idle",
            "session_id": s.session_id if s else None,
            "trigger": s.trigger if s else None,
            "started_at_utc": s.started_at_utc if s else None,
            "clip": s.clip_name if s else None,
            "profile": self.cfg.capture.profile,
            "disk_free_gb": round(storage.free_gib(self.base), 1),
        }

    async def _emit(self, event: dict) -> None:
        if self.on_event is not None:
            try:
                await self.on_event(event)
            except Exception:  # never let reporting break capture
                log.exception("event emit failed")

    # ----- capture primitives ------------------------------------------

    async def _run_capture(
        self, *, trigger: str, session_id: str | None, duration_seconds: int | None
    ) -> None:
        """Hold the camera lock and run one capture to completion/stop."""
        async with self._camera_lock:
            started_at = datetime.now().astimezone()
            day_dir = storage.day_dir(self.base, started_at.date())
            paths = record.clip_paths(day_dir, self.cfg.capture.profile, started_at)
            duration = duration_seconds or self.cfg.capture.clip_seconds
            commands = record.build_commands(
                self.cfg.capture, paths, int(duration * 1000)
            )
            record.write_metadata(
                paths, self.cfg.capture,
                trigger=trigger, started_at=started_at, session_id=session_id,
            )

            self._active = SessionState(
                session_id=session_id or f"sched-{started_at:%Y%m%d_%H%M%S}",
                trigger=trigger,
                started_at_utc=started_at.astimezone(timezone.utc).isoformat(),
                clip_name=paths.video.name,
            )
            self._recording = record.Recording(commands, paths)
            log.info("Starting %s capture: %s", trigger, record.describe_commands(commands))
            self._recording.start()
            await self._emit({
                "type": "session_started",
                "session_id": self._active.session_id,
                "trigger": trigger,
                "started_at_utc": self._active.started_at_utc,
                "clip": paths.video.name,
            })

            # Wait for the pipeline to finish in a worker thread.
            rc = await asyncio.to_thread(self._recording.wait)
            ended_at = _utcnow_iso()
            log.info("Capture finished rc=%s clip=%s", rc, paths.video.name)
            session = self._active
            self._active = None
            self._recording = None

        await self._emit({
            "type": "session_stopped",
            "session_id": session.session_id if session else session_id,
            "ended_at_utc": ended_at,
            "clip": paths.video.name,
            "rc": rc,
        })

    # ----- manual (triggered) sessions ---------------------------------

    async def start_session(self, session_id: str) -> dict:
        """Begin a human-triggered recording. Preempts a scheduled clip."""
        if self._manual_active:
            return {"type": "error", "code": "already_recording", "session_id": session_id}

        self._manual_active = True
        # Preempt an in-progress scheduled clip so the manual session owns the camera.
        if self._recording is not None and self._active and self._active.trigger == "scheduled":
            log.info("Preempting scheduled clip for manual session %s", session_id)
            await asyncio.to_thread(self._recording.stop)

        asyncio.create_task(self._manual_session(session_id))
        return {"type": "accepted", "session_id": session_id}

    async def _manual_session(self, session_id: str) -> None:
        self._auto_stop_task = asyncio.create_task(self._auto_stop(session_id))
        try:
            await self._run_capture(
                trigger="triggered",
                session_id=session_id,
                duration_seconds=self.cfg.capture.max_session_seconds,
            )
        finally:
            self._manual_active = False
            if self._auto_stop_task:
                self._auto_stop_task.cancel()
                self._auto_stop_task = None

    async def _auto_stop(self, session_id: str) -> None:
        await asyncio.sleep(self.cfg.capture.max_session_seconds)
        log.warning("Auto-stopping session %s at max length", session_id)
        await self.stop_session(session_id)

    async def stop_session(self, session_id: str) -> dict:
        if self._recording is not None and self._manual_active:
            await asyncio.to_thread(self._recording.stop)
            return {"type": "accepted", "session_id": session_id}
        return {"type": "error", "code": "not_recording", "session_id": session_id}

    # ----- scheduler ----------------------------------------------------

    def _in_window(self, now: datetime) -> bool:
        return self.cfg.schedule.start_hour <= now.hour < self.cfg.schedule.stop_hour

    def _seconds_to_next_slot(self, now: datetime) -> float:
        interval = self.cfg.schedule.interval_min
        minute_block = (now.minute // interval + 1) * interval
        nxt = now.replace(second=0, microsecond=0)
        if minute_block >= 60:
            nxt = nxt.replace(minute=0) + timedelta(hours=1)
        else:
            nxt = nxt.replace(minute=minute_block)
        return max(1.0, (nxt - now).total_seconds())

    async def _scheduler_loop(self) -> None:
        while True:
            now = datetime.now().astimezone()
            await asyncio.sleep(self._seconds_to_next_slot(now))
            now = datetime.now().astimezone()
            if not self._in_window(now):
                continue
            if self._manual_active:
                log.info("Skipping scheduled clip; manual session active")
                continue
            try:
                await self._run_capture(
                    trigger="scheduled", session_id=None, duration_seconds=None
                )
            except Exception:
                log.exception("Scheduled capture failed")

    async def run(self, cloudlink=None) -> None:
        """Run scheduler + (optional) Cloudflare link until cancelled."""
        if cloudlink is not None:
            cloudlink.bind(self)
            self.on_event = cloudlink.send_event
        tasks = [asyncio.create_task(self._scheduler_loop())]
        if cloudlink is not None:
            tasks.append(asyncio.create_task(cloudlink.run()))
        log.info("Supervisor running (storage=%s)", self.base)
        await asyncio.gather(*tasks)
