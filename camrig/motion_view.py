"""Interactive motion-track viewer, served on the Pi (like camrig.focus).

An on-demand debug tool for tuning the insect-vs-plant discriminators
(``straightness``/``chronic``, see ``camrig.motion``) against real footage
without re-encoding anything: it serves the clip's existing ``.preview.mp4``
and ``.motion.json`` as-is, and draws trails/blobs client-side on a `<canvas>`
layered over the `<video>`. Threshold sliders filter which tracks are drawn
live, in the browser; changes are persisted into ``config.toml``
(``[postprocess] min_straightness`` / ``max_chronic``) so they survive
between sessions -- open a URL printed at start-up, over Tailscale.

    camrig motion-view clip.mkv

Requires the clip's sidecars (``camrig postprocess <clip>`` first if missing).
"""

from __future__ import annotations

import json
import logging
import re
import socket
import subprocess
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .config import Config, set_config_value
from .motion_debug import load_motion, motion_path
from .postprocess import preview_path

log = logging.getLogger("camrig.motion_view")

_RANGE_RE = re.compile(r"bytes=(\d*)-(\d*)")


def _local_urls(port: int) -> list[str]:
    """Best-effort list of URLs to reach this server (incl. Tailscale IP)."""
    urls: list[str] = []
    try:
        out = subprocess.run(
            ["tailscale", "ip", "-4"], capture_output=True, text=True, timeout=3
        )
        for line in out.stdout.split():
            if line.strip():
                urls.append(f"http://{line.strip()}:{port}/")
    except (OSError, subprocess.SubprocessError):
        pass
    try:
        host = socket.gethostname()
        urls.append(f"http://{host}:{port}/  (or {host}.<tailnet>.ts.net)")
    except OSError:
        pass
    return urls


def _page(clip_name: str, fps: float, min_straightness: float, max_chronic: float) -> str:
    return _PAGE_TEMPLATE \
        .replace("__CLIP__", clip_name) \
        .replace("__FPS__", repr(fps)) \
        .replace("__MIN_STRAIGHTNESS__", repr(min_straightness)) \
        .replace("__MAX_CHRONIC__", repr(max_chronic))


_PAGE_TEMPLATE = """<!doctype html>
<html><head><meta charset="utf-8"><meta name="viewport"
content="width=device-width,initial-scale=1">
<title>camrig motion-view -- __CLIP__</title>
<style>
  :root{color-scheme:dark}
  body{margin:0;background:#0b0d10;color:#e6e9ef;
    font:14px/1.4 -apple-system,Segoe UI,Roboto,sans-serif}
  header{display:flex;gap:1.2rem;align-items:center;flex-wrap:wrap;
    padding:.6rem .9rem;background:#12161c;border-bottom:1px solid #222}
  h1{font-size:15px;margin:0;font-weight:600;letter-spacing:.3px;white-space:nowrap}
  label{font-size:12px;color:#aeb6c2;display:flex;gap:.4rem;align-items:center}
  input[type=range]{width:130px}
  .val{font-variant-numeric:tabular-nums;min-width:3ch;display:inline-block}
  #saveStatus{font-size:11px;color:#8b93a1;min-width:6ch}
  .wrap{position:relative;max-width:1100px;margin:0 auto;background:#000}
  video{display:block;width:100%}
  canvas#overlay{position:absolute;inset:0;width:100%;height:100%;pointer-events:none}
  .transport{display:flex;gap:.5rem;align-items:center;flex-wrap:wrap;
    padding:.6rem .9rem;background:#12161c;border-top:1px solid #222}
  .transport input[type=range]{flex:1;width:auto;min-width:120px}
  button{background:#232a34;color:#e6e9ef;border:1px solid #333;border-radius:6px;
    padding:.35rem .6rem;cursor:pointer;font-size:12px}
  #time{font-variant-numeric:tabular-nums;font-size:12px;color:#aeb6c2;white-space:nowrap}
  .hint{padding:.5rem .9rem;color:#8b93a1;font-size:12px}
</style></head>
<body>
<header>
  <h1>camrig motion-view</h1>
  <label><input type="checkbox" id="trails" checked> trails</label>
  <label>min straightness
    <input type="range" id="minStraightness" min="0" max="1" step="0.01">
    <span class="val" id="minStraightnessVal"></span>
  </label>
  <label>max chronic
    <input type="range" id="maxChronic" min="0" max="1" step="0.01">
    <span class="val" id="maxChronicVal"></span>
  </label>
  <span id="saveStatus"></span>
  <span id="trackCount" style="margin-left:auto;color:#8b93a1;font-size:12px"></span>
</header>
<div class="wrap">
  <video id="v" src="/clip.mp4" preload="auto"></video>
  <canvas id="overlay"></canvas>
</div>
<div class="transport">
  <button id="playpause">Play</button>
  <button id="back10">&laquo;10</button>
  <button id="back1">&lsaquo;1</button>
  <input type="range" id="seek" min="0" max="1000" value="0" step="1">
  <button id="fwd1">1&rsaquo;</button>
  <button id="fwd10">10&raquo;</button>
  <span id="time">--</span>
</div>
<div class="hint">
  Left/Right arrow: step 1 frame. Shift+Left/Right: step 10. Space: play/pause.
  Grey boxes are every raw per-window detection; coloured trails are linked
  tracks passing the threshold sliders, fading after a few seconds.
</div>
<script>
const FPS = __FPS__;
const TRAIL_SECONDS = 3.0;
let minStraightness = __MIN_STRAIGHTNESS__;
let maxChronic = __MAX_CHRONIC__;
let showTrails = true;

const video = document.getElementById('v');
const canvas = document.getElementById('overlay');
const ctx = canvas.getContext('2d');
const seek = document.getElementById('seek');
const timeEl = document.getElementById('time');
const PALETTE = ['#4285f4','#db4437','#f4b400','#0f9d58','#ab47bc','#00acc1','#ff5722','#9e9d24'];

let motion = null, frameWindows = [], windowFrames = 6;

fetch('/motion.json').then(r => r.json()).then(m => {
  motion = m;
  windowFrames = (m.params && m.params.window) || 6;
  canvas.width = m.width;
  canvas.height = m.height;
  m.windows.forEach((w, wi) => { for (let i = 0; i < w.n_frames; i++) frameWindows.push(wi); });
  document.getElementById('trackCount').textContent = m.tracks.length + ' tracks';
  requestAnimationFrame(loop);
});

function windowAt(t) {
  if (!frameWindows.length) return 0;
  const f = Math.max(0, Math.round(t * FPS));
  return frameWindows[Math.min(f, frameWindows.length - 1)];
}

function draw(wIdx) {
  ctx.clearRect(0, 0, canvas.width, canvas.height);
  const win = motion.windows[wIdx];
  ctx.strokeStyle = '#aaaaaa';
  ctx.lineWidth = 1;
  for (const b of win.blobs) {
    const [x, y, w, h] = b.bbox;
    ctx.strokeRect(x + 0.5, y + 0.5, w, h);
  }
  if (!showTrails) return;
  const trailWindows = Math.max(1, Math.round(TRAIL_SECONDS * FPS / windowFrames));
  motion.tracks.forEach((t, ti) => {
    if (t.straightness < minStraightness || t.chronic > maxChronic) return;
    if (wIdx < t.w0) return;
    const idxEnd = Math.min(wIdx - t.w0, t.n - 1);
    const idxStart = Math.max(0, idxEnd - trailWindows + 1);
    const pts = t.path.slice(idxStart, idxEnd + 1);
    const color = PALETTE[ti % PALETTE.length];
    if (pts.length > 1) {
      ctx.strokeStyle = color;
      ctx.lineWidth = 1;
      ctx.beginPath();
      ctx.moveTo(pts[0][0], pts[0][1]);
      for (let i = 1; i < pts.length; i++) ctx.lineTo(pts[i][0], pts[i][1]);
      ctx.stroke();
    }
    const [cx, cy] = pts[pts.length - 1];
    ctx.fillStyle = color;
    ctx.beginPath();
    ctx.arc(cx, cy, 2, 0, Math.PI * 2);
    ctx.fill();
  });
}

function fmtTime(s) {
  s = Math.max(0, s || 0);
  const m = Math.floor(s / 60);
  return m + ':' + (s - m * 60).toFixed(2).padStart(5, '0');
}

let seeking = false;
seek.addEventListener('pointerdown', () => { seeking = true; });
seek.addEventListener('pointerup', () => { seeking = false; });
seek.addEventListener('input', () => {
  video.pause();
  video.currentTime = seek.value / FPS;
});

video.addEventListener('loadedmetadata', () => {
  seek.max = Math.max(1, Math.round((video.duration || 0) * FPS) - 1);
});

function loop() {
  if (motion) {
    draw(windowAt(video.currentTime));
    if (!seeking) seek.value = Math.round(video.currentTime * FPS);
    timeEl.textContent = fmtTime(video.currentTime) + ' / ' + fmtTime(video.duration) +
      '  (f' + Math.round(video.currentTime * FPS) + ')';
  }
  requestAnimationFrame(loop);
}

function step(n) {
  video.pause();
  video.currentTime = Math.max(0, Math.min(video.duration || 0, video.currentTime + n / FPS));
}
document.getElementById('back10').onclick = () => step(-10);
document.getElementById('back1').onclick = () => step(-1);
document.getElementById('fwd1').onclick = () => step(1);
document.getElementById('fwd10').onclick = () => step(10);
document.getElementById('playpause').onclick = () => { video.paused ? video.play() : video.pause(); };
video.addEventListener('play', () => { document.getElementById('playpause').textContent = 'Pause'; });
video.addEventListener('pause', () => { document.getElementById('playpause').textContent = 'Play'; });

window.addEventListener('keydown', (e) => {
  if (e.target.tagName === 'INPUT') return;
  if (e.key === 'ArrowLeft') { step(e.shiftKey ? -10 : -1); e.preventDefault(); }
  else if (e.key === 'ArrowRight') { step(e.shiftKey ? 10 : 1); e.preventDefault(); }
  else if (e.key === ' ') { video.paused ? video.play() : video.pause(); e.preventDefault(); }
});

document.getElementById('trails').addEventListener('change', (e) => {
  showTrails = e.target.checked;
});

const minEl = document.getElementById('minStraightness');
const maxEl = document.getElementById('maxChronic');
const minVal = document.getElementById('minStraightnessVal');
const maxVal = document.getElementById('maxChronicVal');
const saveStatus = document.getElementById('saveStatus');
minEl.value = minStraightness; maxEl.value = maxChronic;
minVal.textContent = minStraightness.toFixed(2);
maxVal.textContent = maxChronic.toFixed(2);

let saveTimer = null;
function onThresholdChange() {
  minStraightness = parseFloat(minEl.value);
  maxChronic = parseFloat(maxEl.value);
  minVal.textContent = minStraightness.toFixed(2);
  maxVal.textContent = maxChronic.toFixed(2);
  saveStatus.textContent = 'saving...';
  clearTimeout(saveTimer);
  saveTimer = setTimeout(saveThresholds, 400);
}
minEl.addEventListener('input', onThresholdChange);
maxEl.addEventListener('input', onThresholdChange);

function saveThresholds() {
  fetch('/thresholds', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({min_straightness: minStraightness, max_chronic: maxChronic}),
  }).then(r => r.json()).then(j => {
    saveStatus.textContent = j.ok ? 'saved' : ('save failed: ' + j.error);
  }).catch(() => { saveStatus.textContent = 'save failed'; });
}
</script>
</body></html>
"""


class _Handler(BaseHTTPRequestHandler):
    # Set on the server instance (see run()).
    video: Path
    cfg: Config
    config_path: Path

    def log_message(self, *args) -> None:  # quiet; the app logs what it needs
        pass

    def do_GET(self) -> None:  # noqa: N802 (stdlib naming)
        path = self.path.split("?", 1)[0]
        if path == "/":
            self._serve_page()
        elif path == "/clip.mp4":
            self._serve_file(preview_path(self.video), "video/mp4")
        elif path == "/motion.json":
            self._serve_file(motion_path(self.video), "application/json")
        else:
            self.send_error(404)

    def do_POST(self) -> None:  # noqa: N802
        if self.path.split("?", 1)[0] != "/thresholds":
            self.send_error(404)
            return
        self._save_thresholds()

    def _serve_page(self) -> None:
        cfg = self.cfg
        body = _page(
            self.video.name, cfg.capture.framerate,
            cfg.postprocess.min_straightness, cfg.postprocess.max_chronic,
        ).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _serve_file(self, path: Path, content_type: str) -> None:
        try:
            size = path.stat().st_size
        except OSError:
            self.send_error(404)
            return

        start, end = 0, size - 1
        status = 200
        range_header = self.headers.get("Range")
        if range_header:
            match = _RANGE_RE.match(range_header)
            if not match:
                self.send_error(416)
                return
            start_s, end_s = match.groups()
            start = int(start_s) if start_s else 0
            end = min(int(end_s), size - 1) if end_s else size - 1
            if start > end or start >= size:
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{size}")
                self.end_headers()
                return
            status = 206

        length = end - start + 1
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(length))
        if status == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        try:
            with path.open("rb") as f:
                f.seek(start)
                remaining = length
                while remaining > 0:
                    chunk = f.read(min(65536, remaining))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    remaining -= len(chunk)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _save_thresholds(self) -> None:
        length = int(self.headers.get("Content-Length", 0))
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
            min_straightness = float(body["min_straightness"])
            max_chronic = float(body["max_chronic"])
            if not (0.0 <= min_straightness <= 1.0 and 0.0 <= max_chronic <= 1.0):
                raise ValueError("thresholds must be within 0..1")
        except (ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
            self._json_response(400, {"ok": False, "error": str(exc)})
            return

        try:
            set_config_value(self.config_path, "postprocess",
                             "min_straightness", f"{min_straightness:.3f}")
            set_config_value(self.config_path, "postprocess",
                             "max_chronic", f"{max_chronic:.3f}")
        except OSError as exc:
            log.warning("Could not persist thresholds to %s: %s", self.config_path, exc)
            self._json_response(200, {"ok": False, "error": f"could not write config: {exc}"})
            return

        self.cfg.postprocess.min_straightness = min_straightness
        self.cfg.postprocess.max_chronic = max_chronic
        self._json_response(200, {"ok": True})

    def _json_response(self, status: int, payload: dict) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def run(cfg: Config, video: Path, config_path: Path, *, port: int = 8090) -> int:
    """Serve the motion-track viewer for one clip until Ctrl-C."""
    if load_motion(video) is None:
        return 1
    if not preview_path(video).exists():
        log.error("Missing %s; run `camrig postprocess %s` first", preview_path(video), video)
        return 1

    server = ThreadingHTTPServer(("0.0.0.0", port), _Handler)
    server.daemon_threads = True
    _Handler.video = video
    _Handler.cfg = cfg
    _Handler.config_path = config_path

    print(f"\ncamrig motion-view -- {video.name}")
    print("Open in a browser on your tailnet:")
    for url in _local_urls(port):
        print(f"  {url}")
    print("Ctrl-C to stop.\n")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.shutdown()
    return 0
