"""Interactive motion-track viewer, served on the Pi (like camrig.focus).

An on-demand debug tool for tuning the insect-vs-plant discriminators
(``straightness``/``chronic``, see ``camrig.motion``) against real footage
without re-encoding anything: it serves the clip's existing ``.preview.mp4``
and ``.motion.json`` as-is, and draws trails/blobs client-side on a `<canvas>`
layered over the `<video>`. Threshold sliders filter which tracks are drawn
live, in the browser; changes are persisted into ``config.toml``
(``[postprocess] min_straightness`` / ``max_chronic``) so they survive
between sessions -- open a URL printed at start-up, over Tailscale. Trail
length/thickness match ``[postprocess] trail_seconds`` and
``camrig.motion_debug``'s rendered look, so this preview and the mp4 agree.

Click a trail to label its track ground-truth (insect/other/unsure, or the
``i``/``o``/``u`` shortcuts) -- see ``camrig.labels`` for the sidecar this
writes. Labelling against real footage gives an objective way to check
whether a threshold change actually helps, instead of eyeballing it.

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

from . import trim as trim_mod
from .config import Config, set_config_value
from .labels import LABELS, append_label, load_labels, remap_labels
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


def _page(clip_name: str, fps: float, min_straightness: float, max_chronic: float,
          trail_seconds: float) -> str:
    return _PAGE_TEMPLATE \
        .replace("__CLIP__", clip_name) \
        .replace("__FPS__", repr(fps)) \
        .replace("__MIN_STRAIGHTNESS__", repr(min_straightness)) \
        .replace("__MAX_CHRONIC__", repr(max_chronic)) \
        .replace("__TRAIL_SECONDS__", repr(trail_seconds))


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
  canvas#overlay{position:absolute;inset:0;width:100%;height:100%;cursor:pointer}
  .modal{position:fixed;inset:0;background:rgba(0,0,0,.55);display:flex;
    align-items:center;justify-content:center;z-index:10}
  .modal.hidden{display:none}
  .modalBox{background:#12161c;border:1px solid #333;border-radius:10px;
    padding:1rem 1.2rem;min-width:220px;box-shadow:0 8px 30px rgba(0,0,0,.5)}
  .modalBox h2{font-size:14px;margin:0 0 .6rem;font-weight:600}
  .modalStats{display:flex;flex-direction:column;gap:.2rem;font-size:12px;
    color:#aeb6c2;margin-bottom:.8rem}
  .modalButtons{display:flex;gap:.5rem}
  .modalButtons button{flex:1}
  kbd{background:#232a34;border:1px solid #333;border-radius:4px;padding:0 .3rem;
    font-size:11px;margin-left:.3rem}
  .modalHint{margin-top:.6rem;font-size:11px;color:#8b93a1;text-align:center}
  .transport{display:flex;gap:.5rem;align-items:center;flex-wrap:wrap;
    padding:.6rem .9rem;background:#12161c;border-top:1px solid #222}
  .transport input[type=range]{flex:1;width:auto;min-width:120px}
  .seekWrap{position:relative;flex:1;display:flex;align-items:center;min-width:120px}
  .seekWrap input[type=range]{width:100%}
  .cutmarks{position:absolute;left:0;right:0;top:50%;height:5px;margin-top:-2px;
    pointer-events:none}
  .cutmarks span{position:absolute;top:0;height:100%;background:#db4437;
    opacity:.85;border-radius:2px}
  .cuttools{display:flex;gap:.5rem;align-items:center;flex-wrap:wrap;
    padding:.5rem .9rem;background:#12161c;border-top:1px solid #222}
  .cutlist{display:flex;gap:.4rem;flex-wrap:wrap;flex:1}
  .cutchip{background:#232a34;border:1px solid #db4437;border-radius:12px;
    padding:.15rem .5rem;font-size:11px;display:flex;gap:.4rem;align-items:center}
  .cutchip button{background:none;border:none;color:#db4437;padding:0;
    font-size:13px;cursor:pointer;line-height:1}
  #cutStatus{font-size:11px;color:#8b93a1}
  button{background:#232a34;color:#e6e9ef;border:1px solid #333;border-radius:6px;
    padding:.35rem .6rem;cursor:pointer;font-size:12px}
  #time{font-variant-numeric:tabular-nums;font-size:12px;color:#aeb6c2;white-space:nowrap}
  .hint{padding:.5rem .9rem;color:#8b93a1;font-size:12px}
</style></head>
<body>
<header>
  <h1>camrig motion-view</h1>
  <label><input type="checkbox" id="trails" checked> trails<kbd>t</kbd></label>
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
<div id="labelModal" class="modal hidden">
  <div class="modalBox">
    <h2>Label track <span id="lblTrackId"></span></h2>
    <div class="modalStats">
      <div>straightness <b id="lblStraightness"></b></div>
      <div>chronic <b id="lblChronic"></b></div>
      <div>duration <b id="lblDuration"></b></div>
    </div>
    <div class="modalButtons">
      <button data-label="insect" style="border-left:3px solid #0f9d58">Insect<kbd>i</kbd></button>
      <button data-label="other" style="border-left:3px solid #db4437">Other<kbd>o</kbd></button>
      <button data-label="unsure" style="border-left:3px solid #f4b400">Unsure<kbd>u</kbd></button>
    </div>
    <div class="modalHint">Esc to cancel</div>
  </div>
</div>
<div class="transport">
  <button id="playpause">Play</button>
  <button id="back10">&laquo;10</button>
  <button id="back1">&lsaquo;1</button>
  <div class="seekWrap">
    <input type="range" id="seek" min="0" max="1000" value="0" step="1">
    <div class="cutmarks" id="cutMarks"></div>
  </div>
  <button id="fwd1">1&rsaquo;</button>
  <button id="fwd10">10&raquo;</button>
  <span id="time">--</span>
</div>
<div class="cuttools">
  <button id="cutIn">Mark cut-in<kbd>[</kbd></button>
  <button id="cutOut">Mark cut-out<kbd>]</kbd></button>
  <div class="cutlist" id="cutList"></div>
  <button id="applyCuts" disabled>Apply cuts</button>
  <span id="cutStatus"></span>
</div>
<div class="hint">
  Left/Right arrow: step 1 frame. Shift+Left/Right: step 10. Space: play/pause.
  T: toggle trails. Grey boxes are every raw per-window detection; coloured
  trails are linked tracks passing the threshold sliders, fading out after a
  few frames. Labelled tracks are coloured by label:
  <span style="color:#0f9d58">insect</span>,
  <span style="color:#db4437">other</span>,
  <span style="color:#f4b400">unsure</span>.
  <br>Mark cut-in/cut-out (<kbd>[</kbd>/<kbd>]</kbd>) to queue a section of the
  raw clip for deletion, then Apply -- this permanently removes it from the
  clip on disk (lossless, but not undoable) and drops any label caught inside
  it. Re-run <code>camrig postprocess --force</code> afterwards.
</div>
<script>
const FPS = __FPS__;
const TRAIL_SECONDS = __TRAIL_SECONDS__;
let minStraightness = __MIN_STRAIGHTNESS__;
let maxChronic = __MAX_CHRONIC__;
let showTrails = true;

const video = document.getElementById('v');
const canvas = document.getElementById('overlay');
const ctx = canvas.getContext('2d');
const seek = document.getElementById('seek');
const timeEl = document.getElementById('time');
const PALETTE = ['#4285f4','#ab47bc','#00acc1','#ff5722','#9e9d24'];
const LABEL_COLORS = {insect: '#0f9d58', other: '#db4437', unsure: '#f4b400'};

let motion = null, frameWindows = [], windowFrames = 6;
let drawnTracks = [];  // this frame's {ti, track, pts} that passed the filter, for click-to-label
const labeledTracks = new Map();  // source_track -> label, from saved labels.jsonl

fetch('/motion.json').then(r => r.json()).then(m => {
  motion = m;
  windowFrames = (m.params && m.params.window) || 6;
  canvas.width = m.width;
  canvas.height = m.height;
  m.windows.forEach((w, wi) => { for (let i = 0; i < w.n_frames; i++) frameWindows.push(wi); });
  document.getElementById('trackCount').textContent = m.tracks.length + ' tracks';
  requestAnimationFrame(loop);
});

fetch('/labels').then(r => r.json()).then(rows => {
  rows.forEach(r => labeledTracks.set(r.source_track, r.label));
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
  drawnTracks = [];
  if (!showTrails) return;
  const trailWindows = Math.max(1, Math.round(TRAIL_SECONDS * FPS / windowFrames));
  motion.tracks.forEach((t, ti) => {
    if (t.straightness < minStraightness || t.chronic > maxChronic) return;
    const relIdx = wIdx - t.w0;
    if (relIdx < 0 || relIdx > t.n - 1) return;
    const idxEnd = relIdx;
    const idxStart = Math.max(0, idxEnd - trailWindows + 1);
    const pts = t.path.slice(idxStart, idxEnd + 1);
    const label = labeledTracks.get(ti);
    const color = LABEL_COLORS[label] || PALETTE[ti % PALETTE.length];
    ctx.lineWidth = 2;
    for (let i = 1; i < pts.length; i++) {
      ctx.strokeStyle = color;
      ctx.globalAlpha = i / (pts.length - 1);
      ctx.beginPath();
      ctx.moveTo(pts[i - 1][0], pts[i - 1][1]);
      ctx.lineTo(pts[i][0], pts[i][1]);
      ctx.stroke();
    }
    ctx.globalAlpha = 1;
    const [cx, cy] = pts[pts.length - 1];
    ctx.fillStyle = color;
    ctx.beginPath();
    ctx.arc(cx, cy, 3, 0, Math.PI * 2);
    ctx.fill();
    if (labeledTracks.has(ti)) {
      ctx.strokeStyle = '#ffffff';
      ctx.lineWidth = 1.5;
      ctx.beginPath();
      ctx.arc(cx, cy, 6, 0, Math.PI * 2);
      ctx.stroke();
    }
    drawnTracks.push({ti, track: t, pts});
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
function currentFrame() {
  return Math.round(video.currentTime * FPS);
}

let cutStart = null;  // frame index waiting for its cut-out, or null
let pendingCuts = [];  // [{startFrame, endFrame}], oldest first
const cutInBtn = document.getElementById('cutIn');
const cutOutBtn = document.getElementById('cutOut');
const cutListEl = document.getElementById('cutList');
const applyCutsBtn = document.getElementById('applyCuts');
const cutStatus = document.getElementById('cutStatus');
const cutMarks = document.getElementById('cutMarks');

function renderCuts() {
  cutListEl.innerHTML = '';
  pendingCuts.forEach((c, i) => {
    const chip = document.createElement('span');
    chip.className = 'cutchip';
    chip.textContent = fmtTime(c.startFrame / FPS) + '–' + fmtTime(c.endFrame / FPS);
    const rm = document.createElement('button');
    rm.textContent = '✕';
    rm.title = 'remove this cut';
    rm.onclick = () => { pendingCuts.splice(i, 1); renderCuts(); };
    chip.appendChild(rm);
    cutListEl.appendChild(chip);
  });
  applyCutsBtn.disabled = pendingCuts.length === 0;
  applyCutsBtn.textContent = pendingCuts.length ? `Apply ${pendingCuts.length} cut(s)` : 'Apply cuts';
  cutMarks.innerHTML = '';
  const max = parseFloat(seek.max) || 1;
  pendingCuts.forEach(c => {
    const span = document.createElement('span');
    span.style.left = (100 * c.startFrame / max) + '%';
    span.style.width = Math.max(0.3, 100 * (c.endFrame - c.startFrame) / max) + '%';
    cutMarks.appendChild(span);
  });
}

function markCutIn() {
  cutStart = currentFrame();
  cutStatus.textContent = 'cut-in @ f' + cutStart + ' -- now mark cut-out';
}

function markCutOut() {
  if (cutStart === null) { cutStatus.textContent = 'mark cut-in first'; return; }
  const end = currentFrame();
  if (end <= cutStart) { cutStatus.textContent = 'cut-out must be after cut-in'; return; }
  pendingCuts.push({startFrame: cutStart, endFrame: end});
  cutStart = null;
  cutStatus.textContent = '';
  renderCuts();
}

cutInBtn.onclick = markCutIn;
cutOutBtn.onclick = markCutOut;

applyCutsBtn.onclick = () => {
  if (!pendingCuts.length) return;
  const totalFrames = pendingCuts.reduce((s, c) => s + (c.endFrame - c.startFrame), 0);
  const ok = confirm(
    `Permanently delete ${pendingCuts.length} section(s), ` +
    `${(totalFrames / FPS).toFixed(1)}s total, from the raw clip on disk? This cannot be undone.`
  );
  if (!ok) return;
  applyCutsBtn.disabled = true;
  cutStatus.textContent = 'applying...';
  fetch('/trim', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({cuts: pendingCuts.map(c => [c.startFrame, c.endFrame])}),
  }).then(r => r.json()).then(j => {
    if (j.ok) {
      pendingCuts = [];
      cutStart = null;
      renderCuts();
      cutStatus.textContent = 'cut applied';
      let msg = `Cut applied: ${j.frames_before} -> ${j.frames_after} frames.`;
      if (j.labels_dropped) msg += ` ${j.labels_dropped} label(s) dropped (fell inside a cut).`;
      msg += ' Run `camrig postprocess <clip> --force`, then reload this page.';
      alert(msg);
    } else {
      cutStatus.textContent = 'failed: ' + j.error;
      applyCutsBtn.disabled = false;
    }
  }).catch(() => { cutStatus.textContent = 'apply failed'; applyCutsBtn.disabled = false; });
};

document.getElementById('back10').onclick = () => step(-10);
document.getElementById('back1').onclick = () => step(-1);
document.getElementById('fwd1').onclick = () => step(1);
document.getElementById('fwd10').onclick = () => step(10);
document.getElementById('playpause').onclick = () => { video.paused ? video.play() : video.pause(); };
video.addEventListener('play', () => { document.getElementById('playpause').textContent = 'Pause'; });
video.addEventListener('pause', () => { document.getElementById('playpause').textContent = 'Play'; });

const modal = document.getElementById('labelModal');
const lblTrackId = document.getElementById('lblTrackId');
const lblStraightness = document.getElementById('lblStraightness');
const lblChronic = document.getElementById('lblChronic');
const lblDuration = document.getElementById('lblDuration');
let selected = null;  // {ti, track, pts} of the track pending a label

canvas.addEventListener('click', (e) => {
  if (!drawnTracks.length) return;
  const rect = canvas.getBoundingClientRect();
  const scaleX = canvas.width / rect.width;
  const scaleY = canvas.height / rect.height;
  const cx = (e.clientX - rect.left) * scaleX;
  const cy = (e.clientY - rect.top) * scaleY;
  const hitRadius = 14;
  let best = null, bestDist = hitRadius;
  for (const dt of drawnTracks) {
    for (const [px, py] of dt.pts) {
      const d = Math.hypot(px - cx, py - cy);
      if (d < bestDist) { bestDist = d; best = dt; }
    }
  }
  if (best) openLabelModal(best);
});

function openLabelModal(dt) {
  selected = dt;
  video.pause();
  lblTrackId.textContent = dt.ti;
  lblStraightness.textContent = dt.track.straightness.toFixed(2);
  lblChronic.textContent = dt.track.chronic.toFixed(2);
  lblDuration.textContent = (dt.track.n * windowFrames / FPS).toFixed(2) + 's';
  modal.classList.remove('hidden');
}

function closeModal() {
  modal.classList.add('hidden');
  selected = null;
}

function saveLabel(label) {
  if (!selected) return;
  const t = selected.track;
  const path = t.path.map((p, i) => {
    const w = motion.windows[t.w0 + i];
    const time = (w.f + w.n_frames / 2) / FPS;
    return [
      Math.round(p[0] / motion.width * 1000) / 1000,
      Math.round(p[1] / motion.height * 1000) / 1000,
      Math.round(time * 1000) / 1000,
    ];
  });
  const record = {
    label,
    source_track: selected.ti,
    source_analysis: motion.analysis || 'blob-track-v1',
    t0: path[0][2],
    t1: path[path.length - 1][2],
    path,
  };
  fetch('/label', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(record),
  }).then(r => r.json()).then(j => {
    if (j.ok) {
      labeledTracks.set(selected.ti, label);
      closeModal();
    } else {
      alert('save failed: ' + j.error);
    }
  }).catch(() => alert('save failed'));
}

document.querySelectorAll('#labelModal button[data-label]').forEach(btn => {
  btn.addEventListener('click', () => saveLabel(btn.dataset.label));
});

window.addEventListener('keydown', (e) => {
  if (!modal.classList.contains('hidden')) {
    if (e.key === 'Escape') { closeModal(); e.preventDefault(); }
    else if (e.key === 'i' || e.key === 'I') { saveLabel('insect'); e.preventDefault(); }
    else if (e.key === 'o' || e.key === 'O') { saveLabel('other'); e.preventDefault(); }
    else if (e.key === 'u' || e.key === 'U') { saveLabel('unsure'); e.preventDefault(); }
    return;
  }
  // Arrow keys and space have a native meaning on a focused range/checkbox
  // input (nudge the slider, toggle the box) -- defer to that rather than
  // also stepping the video, so tabbing/clicking into a slider doesn't
  // double-handle those two keys. Every other shortcut (t, [, ]) is generic:
  // it fires no matter what's focused, so clicking a slider never silently
  // disables it until you click back into the video.
  const onFormControl = e.target.tagName === 'INPUT';
  if (e.key === 'ArrowLeft') { if (onFormControl) return; step(e.shiftKey ? -10 : -1); e.preventDefault(); }
  else if (e.key === 'ArrowRight') { if (onFormControl) return; step(e.shiftKey ? 10 : 1); e.preventDefault(); }
  else if (e.key === ' ') { if (onFormControl) return; video.paused ? video.play() : video.pause(); e.preventDefault(); }
  else if (e.key === 't' || e.key === 'T') { toggleTrails(); e.preventDefault(); }
  else if (e.key === '[') { markCutIn(); e.preventDefault(); }
  else if (e.key === ']') { markCutOut(); e.preventDefault(); }
});

const trailsCheckbox = document.getElementById('trails');
function toggleTrails() {
  showTrails = !showTrails;
  trailsCheckbox.checked = showTrails;
}
trailsCheckbox.addEventListener('change', (e) => {
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
        elif path == "/labels":
            self._json_response(200, load_labels(self.video))
        else:
            self.send_error(404)

    def do_POST(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0]
        if path == "/thresholds":
            self._save_thresholds()
        elif path == "/label":
            self._save_label()
        elif path == "/trim":
            self._save_trim()
        else:
            self.send_error(404)

    def _serve_page(self) -> None:
        cfg = self.cfg
        body = _page(
            self.video.name, cfg.capture.framerate,
            cfg.postprocess.min_straightness, cfg.postprocess.max_chronic,
            cfg.postprocess.trail_seconds,
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

    def _save_label(self) -> None:
        length = int(self.headers.get("Content-Length", 0))
        try:
            record = json.loads(self.rfile.read(length) or b"{}")
            if record.get("label") not in LABELS:
                raise ValueError(f"label must be one of {LABELS}")
            source_track = int(record["source_track"])
            source_analysis = str(record["source_analysis"])
            t0 = float(record["t0"])
            t1 = float(record["t1"])
            path = record["path"]
            if not isinstance(path, list) or not path:
                raise ValueError("path must be a non-empty list")
            for pt in path:
                x, y, _t = pt
                if not (0.0 <= x <= 1.0 and 0.0 <= y <= 1.0):
                    raise ValueError("path x/y must be normalised to 0..1")
        except (ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
            self._json_response(400, {"ok": False, "error": str(exc)})
            return

        try:
            append_label(self.video, {
                "label": record["label"],
                "source_track": source_track,
                "source_analysis": source_analysis,
                "t0": t0,
                "t1": t1,
                "path": path,
            })
        except OSError as exc:
            log.warning("Could not persist label to %s: %s", self.video, exc)
            self._json_response(200, {"ok": False, "error": f"could not write sidecar: {exc}"})
            return

        self._json_response(200, {"ok": True})

    def _save_trim(self) -> None:
        length = int(self.headers.get("Content-Length", 0))
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
            raw_cuts = body["cuts"]
            if not isinstance(raw_cuts, list) or not raw_cuts:
                raise ValueError("cuts must be a non-empty list")
            cuts = [(int(c[0]), int(c[1])) for c in raw_cuts]
        except (ValueError, KeyError, TypeError, IndexError, json.JSONDecodeError) as exc:
            self._json_response(400, {"ok": False, "error": str(exc)})
            return

        try:
            result = trim_mod.apply_cuts(self.video, self.cfg.capture.framerate, cuts)
        except (ValueError, RuntimeError) as exc:
            log.warning("Trim failed for %s: %s", self.video, exc)
            self._json_response(200, {"ok": False, "error": str(exc)})
            return

        _, dropped = remap_labels(self.video, self.cfg.capture.framerate, result.frame_map)
        self._json_response(200, {
            "ok": True,
            "frames_before": result.frames_before,
            "frames_after": result.frames_after,
            "labels_dropped": len(dropped),
        })

    def _json_response(self, status: int, payload: dict | list) -> None:
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
