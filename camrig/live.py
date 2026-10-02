"""Live motion-trail viewer for the rpicam backends (Camera Module 3 first).

Like ``camrig focus`` this serves a browser page over HTTP (reach it over
Tailscale), but on top of the live image it draws the motion trails that
``camrig debug-motion`` renders offline -- as they happen, so you can watch
what the tracker makes of the scene while setting up a site.

Pipeline::

    rpicam-vid --codec yuv420 -o -          (full camera frame rate)
        |
    camrig.live (this process)
        |-- Y plane, every frame --> motion.MotionAccumulator / TrackLinker
        |                            --> /events (server-sent events, JSON)
        '-- YUV, every Nth frame --> ffmpeg -c:v mjpeg --> /frame.jpg

Design notes:

* **Same analysis as offline.** Detection and linking are the very classes
  the batch ``camrig.motion.analyse`` runs (``MotionAccumulator``,
  ``TrackLinker``, ``track_metrics``), with the same ``[postprocess]``
  threshold and filter settings, so a trail shown here is one the offline
  pipeline would also find. What can't carry over live:

  - ``chronic`` is clip-global offline (fraction of *all* windows a cell
    was active). Live there is no whole clip, so it's an exponential moving
    average of per-cell activity over ``chronic_seconds``. Swaying
    vegetation is therefore only suppressed once it has been swaying for
    about that long.
  - ``duration_seconds`` is the open track's age so far, so a
    ``min_duration_seconds`` filter makes trails appear once they're old
    enough rather than not at all. There is no fragment stitching.
  - Burst filtering (``burst_min_tracks``) isn't applied.

  Tracks that haven't (yet) passed the filters are still sent, flagged, so
  the page can show them dimmed on request.
* **The Pi only does the motion maths.** Trails are drawn client-side on a
  canvas, so nothing is re-encoded with overlays; ffmpeg only JPEG-encodes
  the clean frames, at a reduced ``view_fps``.
* **Frame geometry.** rpicam-vid writes its YUV420 buffers as-is, so a row
  stride or plane height padded for hardware alignment would break the
  fixed ``w*h*3/2``-byte framing. Width is therefore kept a multiple of 128
  (so the half-width chroma rows are a multiple of 64) and height a multiple
  of 16. Motion runs on the Y plane block-averaged down to roughly
  ``[postprocess] motion_width``, the size its pixel thresholds were tuned
  at.
* **Focus.** On ``rpicam-af``, continuous autofocus hunting would light up
  the whole frame as motion, so the lens is pinned: to
  ``[capture] lens_position`` when set, otherwise autofocused once at start
  and then held.

rpicam flags are version sensitive -- verify against ``rpicam-vid --help`` on
the target image if the stream fails to start.
"""

from __future__ import annotations

import json
import logging
import shlex
import subprocess
import threading
import time
from collections import deque
from dataclasses import asdict, dataclass

import numpy as np

from .config import Config, PostprocessConfig
from .focus import FrameBuffer, _Handler, _local_urls, _split_mjpeg, stop_stream
from .motion import MotionAccumulator, TrackLinker, track_metrics
from .motion_debug import passes_thresholds
from .record import mjpeg_qv, rpicam_camera_index

log = logging.getLogger("camrig.live")

_POLL_SECONDS = 5
# Frames per motion window: camrig.motion's default, which postprocess also uses.
_WINDOW = 6
# Sensor aspect ratios, used when [capture] is configured for a different camera.
_SENSOR_ASPECT = {"rpicam-af": 4608 / 2592, "rpicam": 1456 / 1088}


def live_dims(width: int, aspect: float) -> tuple[int, int]:
    """Stream size near ``width`` x ``width/aspect`` with no YUV420 padding (see module notes)."""
    w = max(128, round(width / 128) * 128)
    h = max(16, round(w / aspect / 16) * 16)
    return w, h


@dataclass
class LiveConfig:
    """Parameters for a live trail-viewer session."""

    camera: str = "rpicam-af"
    width: int = 768
    height: int = 432
    framerate: int = 60
    view_fps: int = 15
    quality: int = 80
    port: int = 8080
    shutter_us: int = 0   # 0 = auto
    gain: float = 0.0     # 0 = auto
    lens_position: float = 0.0  # rpicam-af only; 0 = autofocus once at start, then hold
    denoise: str = "cdn_off"
    motion_width: int = 728
    threshold: int = 12
    # Horizon of the live chronic estimate (see module notes).
    chronic_seconds: float = 60.0
    trail_seconds: float = 4.0
    # Stop (and release the camera) after this many minutes with no browser
    # connected, same as camrig focus. 0 = never time out.
    timeout_minutes: int = 10

    @classmethod
    def from_config(cls, cfg: Config, **overrides) -> "LiveConfig":
        """Seed exposure/motion settings from the capture + postprocess config, then override.

        Exposure and denoise come from [capture] so motion behaves as it does
        on real clips; pass shutter_us=0 / gain=0 to override back to auto.
        """
        cap, pp = cfg.capture, cfg.postprocess
        base = cls(
            framerate=cap.framerate, shutter_us=cap.shutter_us, gain=cap.gain,
            lens_position=cap.lens_position, denoise=cap.denoise,
            motion_width=pp.motion_width, threshold=pp.motion_threshold,
            trail_seconds=pp.trail_seconds,
        )
        for key, value in overrides.items():
            if value is not None:
                setattr(base, key, value)
        aspect = (cap.width / cap.height if cap.camera == base.camera
                  else _SENSOR_ASPECT[base.camera])
        base.width, base.height = live_dims(base.width, aspect)
        return base

    @property
    def motion_scale(self) -> int:
        """Integer block-averaging factor from stream to motion resolution."""
        return max(1, round(self.width / self.motion_width))

    @property
    def motion_dims(self) -> tuple[int, int]:
        k = self.motion_scale
        return self.width // k, self.height // k


def build_live_commands(cfg: LiveConfig) -> tuple[list[str], list[str]]:
    """Return (camera, jpeg encoder) argv lists. Pure apart from the camera-index lookup."""
    camera = [
        "rpicam-vid",
        "--camera", rpicam_camera_index(cfg.camera),
        "--width", str(cfg.width),
        "--height", str(cfg.height),
        "--framerate", str(cfg.framerate),
        "--denoise", cfg.denoise,
        "--codec", "yuv420",
        "--nopreview",
        "--timeout", "0",  # run until we stop it
        "--flush",
        "-o", "-",
    ]
    if cfg.shutter_us > 0:
        camera += ["--shutter", str(cfg.shutter_us)]
    if cfg.gain > 0:
        camera += ["--gain", str(cfg.gain)]
    if cfg.camera == "rpicam-af":
        if cfg.lens_position > 0:
            camera += ["--autofocus-mode", "manual", "--lens-position", str(cfg.lens_position)]
        else:
            camera += ["--autofocus-mode", "auto"]  # one AF cycle at start, then hold
    encoder = [
        "ffmpeg", "-hide_banner", "-loglevel", "error",
        "-f", "rawvideo", "-pix_fmt", "yuv420p",
        "-s", f"{cfg.width}x{cfg.height}",
        "-r", str(cfg.view_fps),
        "-i", "-",
        "-c:v", "mjpeg", "-q:v", str(mjpeg_qv(cfg.quality)), "-pix_fmt", "yuvj420p",
        "-flush_packets", "1",
        "-f", "mjpeg", "-",
    ]
    return camera, encoder


def downscale(y: np.ndarray, k: int) -> np.ndarray:
    """Block-average a gray frame by an integer factor (identity for k == 1)."""
    if k == 1:
        return y
    h, w = y.shape[0] // k, y.shape[1] // k
    return y[:h * k, :w * k].reshape(h, k, w, k).mean(axis=(1, 3), dtype=np.float32)


class LiveTracker:
    """Streaming counterpart of ``camrig.motion.analyse`` plus the trail filters.

    ``push`` takes one gray frame at motion resolution and, each time a window
    completes, returns the update message the page draws from.
    """

    def __init__(self, width: int, height: int, cfg: LiveConfig, pp: PostprocessConfig,
                 *, window: int = _WINDOW, cell: int = 8, max_link_dist: float = 80.0,
                 max_accel: float = 40.0, min_track_len: int = 3) -> None:
        self.acc = MotionAccumulator(width, height, cfg.threshold, window=window, cell=cell)
        self.linker = TrackLinker(max_link_dist, max_accel)
        self.pp = pp
        self.min_track_len = min_track_len
        self.window_seconds = window / cfg.framerate
        self.trail_windows = max(1, round(cfg.trail_seconds / self.window_seconds))
        self.decay = min(1.0, self.window_seconds / cfg.chronic_seconds)
        self.cell_activity = np.zeros((height // cell, width // cell), dtype=np.float32)
        self.w_idx = 0

    def push(self, frame: np.ndarray) -> dict | None:
        _, win = self.acc.push(frame)
        return None if win is None else self._on_window(win["blobs"])

    def _on_window(self, blobs: list[dict]) -> dict:
        active = np.zeros(self.cell_activity.shape, dtype=bool)
        for blob in blobs:
            for c in blob["_cells"]:
                active[c] = True
        self.cell_activity *= 1.0 - self.decay
        self.cell_activity[active] += self.decay
        for blob in blobs:
            cells = blob.pop("_cells")
            blob["chronic"] = round(
                float(sum(self.cell_activity[c] for c in cells)) / len(cells), 3)

        w_idx = self.w_idx
        self.w_idx += 1
        self.linker.step(w_idx, blobs)

        tracks = []
        for track in self.linker.open:
            path = track["path"]
            if len(path) < 2:
                continue
            ok = False
            if len(path) >= self.min_track_len:
                metrics = track_metrics(path)
                metrics["duration_seconds"] = len(path) * self.window_seconds
                ok = passes_thresholds(metrics, self.pp)
            tail = path[-self.trail_windows:]
            tracks.append({"id": track["id"], "w0": tail[0][0],
                           "pts": [blob["c"] for _, blob in tail], "ok": ok})
        return {"w": w_idx, "blobs": [blob["bbox"] for blob in blobs], "tracks": tracks}


class EventLog:
    """Ring of recent update messages shared by the motion thread and /events clients.

    Each client tracks its own sequence number, so a client that falls a
    little behind still receives every update rather than only the latest.
    """

    def __init__(self, maxlen: int = 50) -> None:
        self._cond = threading.Condition()
        self._items: deque[bytes] = deque(maxlen=maxlen)
        self._seq = 0
        self._closed = False

    @property
    def seq(self) -> int:
        return self._seq

    @property
    def closed(self) -> bool:
        return self._closed

    def append(self, item: bytes) -> None:
        with self._cond:
            self._items.append(item)
            self._seq += 1
            self._cond.notify_all()

    def close(self) -> None:
        with self._cond:
            self._closed = True
            self._cond.notify_all()

    def since(self, seq: int, timeout: float = 15.0) -> tuple[int, list[bytes]]:
        """Block until messages newer than ``seq`` exist (or timeout/close); return them."""
        with self._cond:
            if self._seq <= seq and not self._closed:
                self._cond.wait(timeout)
            first = self._seq - len(self._items) + 1  # seq of the oldest item held
            skip = max(0, seq + 1 - first)
            return self._seq, list(self._items)[skip:]


class LiveSession:
    """Camera + encoder processes and the threads joining them."""

    def __init__(self, cfg: LiveConfig, pp: PostprocessConfig) -> None:
        self.cfg = cfg
        self.pp = pp
        self.jpegs = FrameBuffer()
        self.events = EventLog()
        self._raw = FrameBuffer()  # every Nth YUV frame, waiting for the encoder
        self.procs: list[subprocess.Popen] = []
        self.motion_fps = 0.0

    def start(self) -> None:
        camera_cmd, encoder_cmd = build_live_commands(self.cfg)
        log.info("Live stream: %s | camrig.live | %s",
                 shlex.join(camera_cmd), shlex.join(encoder_cmd))
        camera = subprocess.Popen(camera_cmd, stdout=subprocess.PIPE)
        encoder = subprocess.Popen(encoder_cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE)
        self.procs = [camera, encoder]
        threading.Thread(target=_split_mjpeg, args=(encoder.stdout, self.jpegs),
                         daemon=True).start()
        threading.Thread(target=self._feed_encoder, args=(encoder.stdin,), daemon=True).start()
        threading.Thread(target=self._process, args=(camera.stdout,), daemon=True).start()

    def stop(self) -> None:
        stop_stream(self.procs, self.jpegs)
        self._raw.close()
        self.events.close()

    def _feed_encoder(self, stdin) -> None:
        # Its own thread with a latest-only slot, so a slow JPEG encode drops
        # view frames instead of stalling the motion loop.
        seq = 0
        try:
            while True:
                new_seq, data = self._raw.wait_newer(seq, timeout=1.0)
                if new_seq == seq:
                    if self._raw.closed:
                        break
                    continue
                seq = new_seq
                stdin.write(data)
                stdin.flush()
        except (BrokenPipeError, ValueError, OSError):
            pass
        finally:
            try:
                stdin.close()
            except OSError:
                pass

    def _process(self, stdout) -> None:
        cfg = self.cfg
        w, h = cfg.width, cfg.height
        frame_bytes = w * h * 3 // 2
        k = cfg.motion_scale
        mw, mh = cfg.motion_dims
        tracker = LiveTracker(mw, mh, cfg, self.pp)
        every = max(1, round(cfg.framerate / cfg.view_fps))
        n = counted = 0
        t0 = time.monotonic()
        try:
            while True:
                data = stdout.read(frame_bytes)
                if len(data) < frame_bytes:
                    break
                if n % every == 0:
                    self._raw.publish(data)
                n += 1
                y = np.frombuffer(data, dtype=np.uint8, count=w * h).reshape(h, w)
                msg = tracker.push(downscale(y, k))
                counted += 1
                now = time.monotonic()
                if now - t0 >= 1.0:
                    self.motion_fps = counted / (now - t0)
                    counted, t0 = 0, now
                if msg is not None:
                    msg["fps"] = round(self.motion_fps, 1)
                    self.events.append(json.dumps(msg, separators=(",", ":")).encode())
        finally:
            self._raw.close()
            self.events.close()


_PAGE = """<!doctype html>
<html><head><meta charset="utf-8"><meta name="viewport"
content="width=device-width,initial-scale=1">
<title>camrig live</title>
<style>
  :root{color-scheme:dark}
  body{margin:0;background:#0b0d10;color:#e6e9ef;
    font:14px/1.4 -apple-system,Segoe UI,Roboto,sans-serif}
  header{display:flex;gap:1rem;align-items:center;flex-wrap:wrap;
    padding:.6rem .9rem;background:#12161c;border-bottom:1px solid #222}
  h1{font-size:15px;margin:0;font-weight:600;letter-spacing:.3px}
  .stat{color:#8b93a1;font-size:12px;font-variant-numeric:tabular-nums}
  label{font-size:12px;color:#aeb6c2;display:flex;gap:.35rem;align-items:center}
  canvas#view{display:block;width:100%;max-width:1100px;margin:0 auto;background:#000}
  .hint{padding:.5rem .9rem;color:#8b93a1;font-size:12px}
</style></head>
<body>
<header>
  <h1>camrig live</h1>
  <span class="stat" id="stats">connecting…</span>
  <label><input type="checkbox" id="blobs"> blobs</label>
  <label><input type="checkbox" id="rejected"> rejected tracks</label>
</header>
<canvas id="view"></canvas>
<div class="hint">Coloured trails passed the [postprocess] track filters; enable
"rejected tracks" to also see (grey) candidates that haven't. Chronic is a
__CHRONIC__ s running estimate here, so swaying plants take about that long to be
suppressed. Bursts aren't filtered live.</div>
<script>
const META=__META__;
const PALETTE=['244,133,66','219,68,55','244,180,0','15,157,88',
               '171,71,188','0,172,193','255,112,67','158,157,36'];
const view=document.getElementById('view'), ctx=view.getContext('2d');
const statsEl=document.getElementById('stats');
const showBlobs=document.getElementById('blobs'), showRejected=document.getElementById('rejected');
view.width=META.width; view.height=META.height;
const tracks=new Map();
let curW=0, blobs=[], frame=null, motionFps=0, frames=0, viewFps=0, lastFps=performance.now();

function draw(){
  if(frame) ctx.drawImage(frame,0,0,view.width,view.height);
  else { ctx.fillStyle='#000'; ctx.fillRect(0,0,view.width,view.height); }
  const s=view.width/META.motionWidth, lw=Math.max(1,s);
  ctx.lineCap='round'; ctx.lineJoin='round';
  if(showBlobs.checked){
    ctx.strokeStyle='rgba(170,170,170,.8)'; ctx.lineWidth=1;
    for(const [x,y,w,h] of blobs) ctx.strokeRect(x*s,y*s,w*s,h*s);
  }
  for(const t of tracks.values()){
    if(!t.ok && !showRejected.checked) continue;
    const col=t.ok? PALETTE[t.id%PALETTE.length] : '140,140,150';
    ctx.lineWidth=(t.ok?2.5:1.2)*lw;
    for(let i=1;i<t.pts.length;i++){
      const a=1-(curW-(t.w0+i))*META.windowSeconds/META.trailSeconds;
      if(a<=0) continue;
      ctx.strokeStyle='rgba('+col+','+a.toFixed(2)+')';
      ctx.beginPath();
      ctx.moveTo(t.pts[i-1][0]*s,t.pts[i-1][1]*s); ctx.lineTo(t.pts[i][0]*s,t.pts[i][1]*s);
      ctx.stroke();
    }
    if(t.w0+t.pts.length-1===curW){  // still open: mark its head
      const [x,y]=t.pts[t.pts.length-1];
      ctx.fillStyle='rgb('+col+')'; ctx.beginPath(); ctx.arc(x*s,y*s,3*lw,0,7); ctx.fill();
    }
  }
}

function stats(){
  let ok=0; for(const t of tracks.values()) if(t.ok && t.w0+t.pts.length-1===curW) ok++;
  statsEl.textContent='camera '+META.framerate+' fps · motion '+motionFps.toFixed(0)+
    ' fps · view '+viewFps.toFixed(0)+' fps · '+ok+' live track'+(ok===1?'':'s');
}

const es=new EventSource('/events');
es.onmessage=(e)=>{
  const m=JSON.parse(e.data);
  curW=m.w; blobs=m.blobs; motionFps=m.fps;
  for(const t of m.tracks) tracks.set(t.id,t);
  for(const [id,t] of tracks)
    if((curW-(t.w0+t.pts.length-1))*META.windowSeconds>META.trailSeconds) tracks.delete(id);
  stats(); draw();
};
es.onerror=()=>{ statsEl.textContent='disconnected – retrying…'; };
showBlobs.onchange=showRejected.onchange=draw;

const sleep=(ms)=>new Promise(r=>setTimeout(r,ms));
(async function frames_loop(){
  let lastSeq=0;
  for(;;){
    try{
      const r=await fetch('/frame.jpg',{headers:{'X-Last-Seq':String(lastSeq)},cache:'no-store'});
      if(!r.ok){ await sleep(500); continue; }
      lastSeq=+(r.headers.get('X-Seq')||0);
      const bmp=await createImageBitmap(await r.blob());
      if(frame) frame.close();
      frame=bmp; frames++; draw();
      const now=performance.now();
      if(now-lastFps>1000){ viewFps=frames*1000/(now-lastFps); frames=0; lastFps=now; stats(); }
    }catch(e){ await sleep(500); }
  }
})();
</script>
</body></html>
"""


def render_live_page(cfg: LiveConfig) -> bytes:
    mw, mh = cfg.motion_dims
    meta = {
        "width": cfg.width, "height": cfg.height, "framerate": cfg.framerate,
        "motionWidth": mw, "motionHeight": mh,
        "windowSeconds": _WINDOW / cfg.framerate, "trailSeconds": cfg.trail_seconds,
    }
    return (_PAGE.replace("__META__", json.dumps(meta))
            .replace("__CHRONIC__", f"{cfg.chronic_seconds:g}").encode("utf-8"))


class _LiveHandler(_Handler):
    """Reuses camrig focus's frame long-poll; adds the page and /events."""

    def do_GET(self) -> None:  # noqa: N802 (stdlib naming)
        self.server.last_request = time.monotonic()  # type: ignore[attr-defined]
        path = self.path.split("?", 1)[0]
        session: LiveSession = self.server.session  # type: ignore[attr-defined]
        if path == "/frame.jpg":
            self._serve_latest_frame(session.jpegs)
        elif path == "/events":
            self._serve_events(session.events)
        elif path == "/":
            self._send_html(self.server.page)  # type: ignore[attr-defined]
        else:
            self.send_error(404)

    def do_POST(self) -> None:  # noqa: N802 (stdlib naming)
        self.send_error(404)

    def _serve_events(self, events: EventLog) -> None:
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            seq = events.seq
            while True:
                seq, items = events.since(seq)
                if not items and events.closed:
                    break
                chunk = b"".join(b"data: " + item + b"\n\n" for item in items)
                self.wfile.write(chunk or b": keepalive\n\n")
                self.wfile.flush()
                # An open page counts as activity for the idle timeout.
                self.server.last_request = time.monotonic()  # type: ignore[attr-defined]
        except (BrokenPipeError, ConnectionResetError):
            pass


def run(cfg: LiveConfig, pp: PostprocessConfig, *, dry_run: bool = False) -> int:
    """Start the camera + motion pipeline and serve the live page until Ctrl-C."""
    if dry_run:
        camera_cmd, encoder_cmd = build_live_commands(cfg)
        print(shlex.join(camera_cmd))
        print(f"  | camrig.live (motion on Y plane at {'x'.join(map(str, cfg.motion_dims))}, "
              f"every {max(1, round(cfg.framerate / cfg.view_fps))}th frame on to:)")
        print(f"  | {shlex.join(encoder_cmd)}")
        print(json.dumps(asdict(cfg)))
        return 0

    from http.server import ThreadingHTTPServer

    session = LiveSession(cfg, pp)
    session.start()
    server = ThreadingHTTPServer(("0.0.0.0", cfg.port), _LiveHandler)
    server.session = session  # type: ignore[attr-defined]
    server.page = render_live_page(cfg)  # type: ignore[attr-defined]
    server.last_request = time.monotonic()  # type: ignore[attr-defined]
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()

    mw, mh = cfg.motion_dims
    print(f"\ncamrig live — {cfg.camera} {cfg.width}x{cfg.height}@{cfg.framerate} "
          f"(motion {mw}x{mh}, view {cfg.view_fps} fps)")
    print("Open in a browser on your tailnet:")
    for url in _local_urls(cfg.port):
        print(f"  {url}")
    if cfg.timeout_minutes > 0:
        print(f"Idle timeout: {cfg.timeout_minutes} min with no page open. Ctrl-C to stop now.\n")
    else:
        print("Ctrl-C to stop.\n")

    timeout_s = cfg.timeout_minutes * 60
    rc = 0
    try:
        while cfg.timeout_minutes <= 0 or time.monotonic() - server.last_request < timeout_s:  # type: ignore[attr-defined]
            if session.procs[0].poll() is not None:
                log.error("rpicam-vid exited (rc=%s); camera busy or unavailable?",
                          session.procs[0].returncode)
                rc = 1
                break
            time.sleep(_POLL_SECONDS)
        else:
            log.info("Idle timeout (%d min); stopping camrig live", cfg.timeout_minutes)
    except KeyboardInterrupt:
        pass
    finally:
        server.shutdown()
        session.stop()
    return rc
