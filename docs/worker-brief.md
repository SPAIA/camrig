# Cloudflare Worker brief — camrig remote trigger

This is the contract the Pi (`camrig/cloudlink.py`) already implements. You add a
**Durable Object** to your existing Worker and wire up the routes below. Nothing
here requires inbound access to the Pi: the Pi dials *out* to the Worker and holds
the socket open, so a button click on the public page reaches the device through
the Worker → Durable Object → the held WebSocket.

## Topology

```
Public page (Cloudflare Pages)
   │  POST /api/trigger  (Turnstile token)
   ▼
Worker (routing + Turnstile verify + rate limit)
   │  stub.fetch()  — keyed by device_id
   ▼
Durable Object  "DeviceRoom"  (one instance per device_id)
   │  holds the Pi WebSocket (use the hibernation API)
   │  holds page subscriber sockets (status fan-out)
   ▼
Raspberry Pi  (outbound WebSocket client)
```

One DO instance per rig: `env.DEVICE_ROOM.idFromName(device_id)`.

## Secrets / bindings

| Binding | Purpose |
|---|---|
| `DEVICE_TOKEN` (secret) | Bearer token the Pi must present on the device WS upgrade. |
| `TURNSTILE_SECRET` (secret) | Server-side Turnstile siteverify. |
| `DEVICE_ROOM` (Durable Object namespace) | The per-device coordinator. |
| `DB` (D1) | Stores bug-count events (counts are Cloudflare-only). |
| `STATE` (KV, optional) | Cache of last-known device state for cheap status reads. |

## Routes (Worker)

### `GET /device`  — Pi connects here (WebSocket upgrade)
1. Require header `Authorization: Bearer <DEVICE_TOKEN>`; reject `401` otherwise.
2. Read `device_id` (query param or derive from token); get the DO stub by name.
3. Forward the upgrade to the DO, which accepts and **retains** the socket
   (hibernatable). The Pi sends `hello` + `status` immediately on connect.

### `POST /api/trigger`  — public page starts a recording
Body: `{ "turnstile_token": "...", "device_id": "pi-rig-01" }`
1. **Verify Turnstile** via `https://challenges.cloudflare.com/turnstile/v0/siteverify`
   with `TURNSTILE_SECRET` + the client IP (`CF-Connecting-IP`). Reject `403` on fail.
2. **Rate limit**: per-IP (e.g. ≤1 trigger / 10s) and a global per-device minimum
   interval (e.g. ≥15s between sessions). Use a counter in the DO or KV/Rate
   Limiting binding. Reject `429` when exceeded.
3. Generate a `session_id` (UUID).
4. Call the DO: if the device socket is absent → `503 {"error":"device_offline"}`;
   if already recording → `409 {"error":"already_recording"}`.
5. The DO sends `start_session` down the Pi socket and returns `{ session_id }`.
   Respond `202 { "session_id", "state": "starting" }`.

### `POST /api/stop`  — stop the current session
Body: `{ "session_id": "...", "device_id": "..." }` → DO sends `stop_session`.
(Optionally also Turnstile-gate this.)

### `POST /api/count`  — log a bug count (Cloudflare-only)
Body: `{ "session_id": "...", "delta": 1 }`
Insert into D1 with the **server** UTC timestamp; do **not** forward to the Pi:

```sql
CREATE TABLE IF NOT EXISTS counts (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  session_id TEXT NOT NULL,
  delta      INTEGER NOT NULL DEFAULT 1,
  ts_utc     TEXT NOT NULL          -- new Date().toISOString()
);
CREATE TABLE IF NOT EXISTS sessions (
  session_id      TEXT PRIMARY KEY,
  device_id       TEXT NOT NULL,
  clip            TEXT,
  started_at_utc  TEXT,             -- authoritative, from the Pi
  ended_at_utc    TEXT
);
```

### `GET /api/subscribe`  — page live status (WebSocket; SSE/poll fallback)
Upgrade to the DO; it adds the socket to its subscriber set and pushes device
`status` / `session_started` / `session_stopped` as they arrive from the Pi.
A `GET /api/status` JSON endpoint reading KV is an acceptable polling fallback.

## Durable Object responsibilities (`DeviceRoom`)

- Accept and hold the **device** socket (one at a time; replace stale on reconnect).
- Maintain the **subscriber** socket set; broadcast device events to them.
- Track in-memory state: `online`, `recording`, `session_id`, `started_at_utc`.
- On `start_session`/`stop_session` requests, forward to the device socket; reject
  if offline / already recording.
- On device `session_started`: persist `started_at_utc` + `clip` into D1
  `sessions` (this is the anchor for count alignment). On `session_stopped`:
  set `ended_at_utc`. Mirror last state into KV if you use the polling fallback.

## Message schemas

### Pi → Worker/DO (received by the DO from the device socket)
```jsonc
{ "type": "hello", "device_id": "pi-rig-01", "fw_version": "0.1.0",
  "capabilities": { "profile": "mjpeg", "max_session_seconds": 600 } }

{ "type": "status", "state": "idle|recording", "session_id": null,
  "trigger": null, "started_at_utc": null, "clip": null,
  "profile": "mjpeg", "disk_free_gb": 812.3 }

{ "type": "session_started", "session_id": "...", "trigger": "triggered",
  "started_at_utc": "2026-06-30T09:15:02.481+00:00", "clip": "clip_20260630_101502.mkv" }

{ "type": "session_stopped", "session_id": "...",
  "ended_at_utc": "2026-06-30T09:20:02.7+00:00", "clip": "clip_...mkv", "rc": 0 }

{ "type": "accepted", "session_id": "..." }
{ "type": "error", "code": "already_recording|not_recording", "session_id": "..." }
{ "type": "pong" }
```

### Worker/DO → Pi (sent down the device socket)
```jsonc
{ "type": "start_session", "session_id": "<uuid>", "requested_at": "<iso8601>" }
{ "type": "stop_session",  "session_id": "<uuid>" }
{ "type": "get_status" }
{ "type": "ping" }
```

The Pi ignores unknown message types. It auto-reconnects with backoff and
WebSocket ping/pong keepalive, so transient Worker restarts are harmless.

## Count → frame alignment (do this in your analysis tooling)

Counts live in Cloudflare; video + per-frame PTS live on the Pi / in R2. The
session's authoritative start comes from the Pi (`session_started.started_at_utc`,
stored in D1 `sessions`). For a count at `ts_utc`:

```
offset_seconds = ts_utc − sessions.started_at_utc
frame_index    = first i where clip.pts[i] − clip.pts[0] ≥ offset_seconds
```

The `.pts` sidecar (microseconds, one per line, written by `rpicam --save-pts`)
gives exact per-frame capture times. Because the Pi is NTP-synced at boot and the
Worker timestamps counts in UTC, the two clocks agree to well within a frame at
60 fps.

## Abuse / safety notes
- Turnstile + rate limit live entirely on the Worker (this doc). The Pi adds its
  own safety: it refuses a second concurrent session and auto-stops at
  `max_session_seconds`.
- Reject triggers when the device is offline rather than queueing them — a stale
  queued start firing minutes later would surprise an on-site operator.
