"""Outbound WebSocket client to the Cloudflare Worker.

The Pi is the client: it dials the Worker's device endpoint and holds the socket
open, so there is no inbound exposure / port-forwarding / tunnel. A button click
on the public page reaches the Worker -> Durable Object -> down this socket as a
``start_session`` command. Status and session lifecycle events flow back up.

Messages are newline-free JSON objects with a ``type`` field. See
docs/worker-brief.md for the full contract. Authentication is a bearer token sent
as a header on the upgrade request and validated by the Worker.

This module deliberately contains no count handling: bug counts are stored in
Cloudflare only. The Pi just supplies the authoritative session start time used
to align those counts to frames.
"""

from __future__ import annotations

import asyncio
import json
import logging

import websockets

from .config import Config

log = logging.getLogger("camrig.cloudlink")

# Reconnect backoff bounds (seconds).
_BACKOFF_MIN = 1.0
_BACKOFF_MAX = 30.0


class CloudLink:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self._supervisor = None
        self._ws = None
        self._send_lock = asyncio.Lock()

    def bind(self, supervisor) -> None:
        self._supervisor = supervisor

    # ----- outbound -----------------------------------------------------

    async def send_event(self, event: dict) -> None:
        """Send a JSON event to the Worker if connected (best-effort)."""
        ws = self._ws
        if ws is None:
            return
        try:
            async with self._send_lock:
                await ws.send(json.dumps(event))
        except websockets.WebSocketException:
            log.debug("send dropped; socket closing")

    # ----- inbound ------------------------------------------------------

    async def _handle(self, raw: str) -> None:
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            log.warning("Ignoring non-JSON message")
            return

        mtype = msg.get("type")
        sup = self._supervisor
        if sup is None:
            return

        if mtype == "start_session":
            result = await sup.start_session(msg.get("session_id", ""))
            await self.send_event(result)
        elif mtype == "stop_session":
            result = await sup.stop_session(msg.get("session_id", ""))
            await self.send_event(result)
        elif mtype == "get_status":
            await self.send_event(sup.status())
        elif mtype == "ping":
            await self.send_event({"type": "pong"})
        else:
            log.debug("Unhandled message type: %s", mtype)

    # ----- connection lifecycle ----------------------------------------

    async def run(self) -> None:
        """Maintain the connection forever, reconnecting with backoff."""
        url = self.cfg.cloud.worker_ws_url
        token = self.cfg.device_token()
        if not token:
            log.error("No device token (%s); cloudlink disabled",
                      self.cfg.cloud.device_token_file)
            return
        headers = {"Authorization": f"Bearer {token}"}
        backoff = _BACKOFF_MIN

        while True:
            try:
                log.info("Connecting to Worker %s", url)
                async with websockets.connect(
                    url, additional_headers=headers,
                    ping_interval=20, ping_timeout=20, close_timeout=5,
                ) as ws:
                    self._ws = ws
                    backoff = _BACKOFF_MIN
                    await self._on_connect()
                    async for raw in ws:
                        await self._handle(raw)
            except (OSError, websockets.WebSocketException) as exc:
                log.warning("Cloudlink disconnected: %s; retrying in %.0fs", exc, backoff)
            finally:
                self._ws = None

            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, _BACKOFF_MAX)

    async def _on_connect(self) -> None:
        from . import __version__
        await self.send_event({
            "type": "hello",
            "device_id": self.cfg.cloud.device_id,
            "fw_version": __version__,
            "capabilities": {
                "profile": self.cfg.capture.profile,
                "max_session_seconds": self.cfg.capture.max_session_seconds,
            },
        })
        if self._supervisor is not None:
            await self.send_event(self._supervisor.status())
