"""Live focus-assist server for manual-focus C/CS-mount lenses.

Both cameras (IMX296 Global Shutter and Basler ace 2) use manual lenses whose
focus is set by turning the lens ring — there is no autofocus. On a headless Pi
reached over Tailscale there is no local display to focus against, so this
module serves a low-latency MJPEG live view over HTTP plus a live sharpness
readout: open the printed URL in a browser on your laptop and turn the lens
ring until the focus score peaks (an optional audio tone rises in pitch as
focus improves, so you can watch the lens instead of the screen).

Design notes:

* **Zero extra dependencies.** The camera pipeline emits a plain MJPEG byte
  stream (concatenated JPEGs) on stdout — rpicam-vid natively; the Basler
  backend pipes raw gray frames from ``camrig.basler`` through ffmpeg's mjpeg
  encoder. A producer thread splits the stream into frames and a stdlib
  ``ThreadingHTTPServer`` hands out the latest one. All the sharpness maths
  (variance of the Laplacian over a centre ROI) runs client-side in the
  browser, where there is CPU and a screen to spare — the Pi only muxes bytes.
* **Denoise is forced off** regardless of config: temporal/spatial denoise
  smooths high-frequency detail and would make a soft image look focused.
* Resolution defaults to the full sensor because judging sharpness needs the
  real high-frequency detail; drop it (or the framerate) with flags if the
  Tailscale link is slow.

rpicam flags are version sensitive — verify against ``rpicam-vid --help`` on the
target image if a stream fails to start.
"""

from __future__ import annotations

import logging
import shlex
import socket
import subprocess
import sys
import threading
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .config import BaslerConfig, CaptureConfig
from .record import mjpeg_qv

log = logging.getLogger("camrig.focus")

_SOI = b"\xff\xd8"  # JPEG start-of-image marker


@dataclass
class FocusConfig:
    """Parameters for a focus-assist streaming session."""

    camera: str = "rpicam"
    width: int = 1456
    height: int = 1088
    framerate: int = 15
    quality: int = 80
    port: int = 8080
    shutter_us: int = 0  # 0 = auto-expose (convenient while setting up in varied light)
    gain: float = 0.0     # 0 = auto

    @classmethod
    def from_capture(cls, cap: CaptureConfig, **overrides) -> "FocusConfig":
        """Seed from the capture config (full-sensor resolution), then override."""
        base = cls(camera=cap.camera, width=cap.width, height=cap.height)
        for key, value in overrides.items():
            if value is not None:
                setattr(base, key, value)
        return base


def build_focus_commands(
    cfg: FocusConfig, basler: BaslerConfig | None = None
) -> list[list[str]]:
    """Return the pipeline (list of argv lists) that streams MJPEG to stdout
    forever: rpicam-vid alone, or camrig.basler piped into ffmpeg.

    Pure function (no camera required) so it can be printed under --dry-run.
    """
    if cfg.camera == "basler":
        bas = basler or BaslerConfig()
        producer = [
            sys.executable, "-m", "camrig.basler",
            "--width", str(cfg.width),
            "--height", str(cfg.height),
            "--framerate", str(cfg.framerate),
            "--timeout", "0",  # run until we stop it
            "-o", "-",
        ]
        if cfg.shutter_us > 0:
            producer += ["--shutter", str(cfg.shutter_us)]
        if cfg.gain > 0:
            producer += ["--gain", str(cfg.gain)]
        if bas.serial:
            producer += ["--serial", bas.serial]
        if bas.ip:
            producer += ["--ip", bas.ip]
        if bas.packet_size > 0:
            producer += ["--packet-size", str(bas.packet_size)]
        if bas.inter_packet_delay > 0:
            producer += ["--inter-packet-delay", str(bas.inter_packet_delay)]
        ffmpeg = [
            "ffmpeg", "-hide_banner", "-loglevel", "error",
            "-f", "rawvideo", "-pix_fmt", "gray",
            "-s", f"{cfg.width}x{cfg.height}",
            "-r", str(cfg.framerate),
            "-i", "-",
            "-c:v", "mjpeg", "-q:v", str(mjpeg_qv(cfg.quality)), "-pix_fmt", "yuvj444p",
            "-flush_packets", "1",  # push each frame out promptly for low latency
            "-f", "mjpeg", "-",
        ]
        return [producer, ffmpeg]

    args = [
        "rpicam-vid",
        "--camera", "0",
        "--width", str(cfg.width),
        "--height", str(cfg.height),
        "--framerate", str(cfg.framerate),
        "--denoise", "cdn_off",  # denoise hides softness; always off for focusing
        "--codec", "mjpeg",
        "--quality", str(cfg.quality),
        "--nopreview",
        "--timeout", "0",  # run until we stop it
        "--flush",         # push each frame out promptly for low latency
        "-o", "-",
    ]
    if cfg.shutter_us > 0:
        args += ["--shutter", str(cfg.shutter_us)]
    if cfg.gain > 0:
        args += ["--gain", str(cfg.gain)]
    return [args]


class FrameBuffer:
    """Single-slot latest-frame buffer shared between producer and HTTP threads."""

    def __init__(self) -> None:
        self._cond = threading.Condition()
        self._frame: bytes | None = None
        self._seq = 0
        self._closed = False

    def publish(self, frame: bytes) -> None:
        with self._cond:
            self._frame = frame
            self._seq += 1
            self._cond.notify_all()

    def close(self) -> None:
        with self._cond:
            self._closed = True
            self._cond.notify_all()

    def wait_newer(self, last_seq: int, timeout: float = 5.0) -> tuple[int, bytes | None]:
        """Block until a frame newer than ``last_seq`` (or timeout/close)."""
        with self._cond:
            if self._seq <= last_seq and not self._closed:
                self._cond.wait(timeout)
            return self._seq, self._frame


def _split_mjpeg(stdout, buffer: FrameBuffer) -> None:
    """Read rpicam-vid stdout and publish each complete JPEG frame.

    Frames are delimited by the SOI marker: a frame is the bytes from one SOI up
    to (but not including) the next. Splitting on SOI rather than hunting for the
    EOI avoids false boundaries from FFD9 bytes inside entropy-coded data.
    """
    buf = bytearray()
    try:
        while True:
            chunk = stdout.read(65536)
            if not chunk:
                break
            buf += chunk
            while True:
                start = buf.find(_SOI)
                if start < 0:
                    break
                nxt = buf.find(_SOI, start + 2)
                if nxt < 0:
                    if start > 0:  # discard leading garbage before the first SOI
                        del buf[:start]
                    break
                buffer.publish(bytes(buf[start:nxt]))
                del buf[:nxt]
    finally:
        buffer.close()


_PAGE = """<!doctype html>
<html><head><meta charset="utf-8"><meta name="viewport"
content="width=device-width,initial-scale=1">
<title>camrig focus</title>
<style>
  :root{color-scheme:dark}
  body{margin:0;background:#0b0d10;color:#e6e9ef;
    font:14px/1.4 -apple-system,Segoe UI,Roboto,sans-serif}
  header{display:flex;gap:1rem;align-items:center;flex-wrap:wrap;
    padding:.6rem .9rem;background:#12161c;border-bottom:1px solid #222}
  h1{font-size:15px;margin:0;font-weight:600;letter-spacing:.3px}
  .score{font-variant-numeric:tabular-nums;font-weight:700;font-size:22px;
    min-width:5.5ch;text-align:right}
  .peak{color:#8b93a1;font-size:12px;font-variant-numeric:tabular-nums}
  .bar{flex:1;min-width:120px;height:12px;border-radius:6px;background:#1c222b;
    overflow:hidden}
  .bar>i{display:block;height:100%;width:0;
    background:linear-gradient(90deg,#e0533d,#e6c34a,#3ddc84);transition:width .08s}
  .wrap{position:relative;max-width:100%;margin:0 auto}
  canvas#view{display:block;width:100%;max-width:1100px;margin:0 auto;background:#000}
  .roi{position:absolute;border:1px solid rgba(61,220,132,.9);
    box-shadow:0 0 0 9999px rgba(0,0,0,.28);pointer-events:none}
  label{font-size:12px;color:#aeb6c2;display:flex;gap:.35rem;align-items:center}
  input[type=range]{width:110px}
  button{background:#232a34;color:#e6e9ef;border:1px solid #333;border-radius:6px;
    padding:.35rem .6rem;cursor:pointer;font-size:12px}
  button.on{background:#1f7a46;border-color:#2b9a58}
  .hint{padding:.5rem .9rem;color:#8b93a1;font-size:12px}
</style></head>
<body>
<header>
  <h1>camrig focus</h1>
  <div class="bar"><i id="bar"></i></div>
  <div class="score" id="score">--</div>
  <div class="peak">peak <span id="peak">--</span></div>
  <button id="reset">reset peak</button>
  <button id="beep">audio: off</button>
  <label>ROI <input type="range" id="roi" min="10" max="90" value="40"></label>
  <span class="peak" id="fps">-- fps</span>
</header>
<div class="wrap"><canvas id="view"></canvas><div class="roi" id="roibox"></div></div>
<div class="hint">Turn the lens ring until the score peaks. Sharper = higher.
The bar is relative to the best value seen since the last reset.</div>
<script>
const view=document.getElementById('view'), vctx=view.getContext('2d');
const mcv=document.createElement('canvas'), mctx=mcv.getContext('2d',{willReadFrequently:true});
const scoreEl=document.getElementById('score'), peakEl=document.getElementById('peak');
const barEl=document.getElementById('bar'), fpsEl=document.getElementById('fps');
const roi=document.getElementById('roi'), roibox=document.getElementById('roibox');
let peak=0, frames=0, lastFps=performance.now();

// Optional audio-pitch feedback so you can watch the lens, not the screen.
let audio=null, osc=null, gain=null;
document.getElementById('beep').onclick=(e)=>{
  if(!audio){
    audio=new (window.AudioContext||window.webkitAudioContext)();
    osc=audio.createOscillator(); gain=audio.createGain();
    gain.gain.value=0.05; osc.type='sine'; osc.connect(gain); gain.connect(audio.destination);
    osc.start(); e.target.classList.add('on'); e.target.textContent='audio: on';
  } else { osc.stop(); audio.close(); audio=null;
    e.target.classList.remove('on'); e.target.textContent='audio: off'; }
};
document.getElementById('reset').onclick=()=>{peak=0;};

function placeRoiBox(){
  const r=view.getBoundingClientRect(), f=roi.value/100, w=r.width*f, h=r.height*f;
  roibox.style.left=(view.offsetLeft+(r.width-w)/2)+'px';
  roibox.style.top =(view.offsetTop +(r.height-h)/2)+'px';
  roibox.style.width=w+'px'; roibox.style.height=h+'px';
}
roi.oninput=placeRoiBox; window.onresize=placeRoiBox;

function sharpness(iw,ih){
  // Variance of the Laplacian over the centre ROI, sampled at native pixels.
  const f=roi.value/100, rw=Math.max(8,Math.round(iw*f)), rh=Math.max(8,Math.round(ih*f));
  const sx=Math.round((iw-rw)/2), sy=Math.round((ih-rh)/2);
  mcv.width=rw; mcv.height=rh;
  mctx.drawImage(img, sx,sy,rw,rh, 0,0,rw,rh);
  const d=mctx.getImageData(0,0,rw,rh).data;
  const g=new Float32Array(rw*rh);
  for(let i=0,p=0;i<g.length;i++,p+=4) g[i]=0.299*d[p]+0.587*d[p+1]+0.114*d[p+2];
  let sum=0,sq=0,n=0;
  for(let y=1;y<rh-1;y++){for(let x=1;x<rw-1;x++){
    const i=y*rw+x;
    const l=4*g[i]-g[i-1]-g[i+1]-g[i-rw]-g[i+rw];
    sum+=l; sq+=l*l; n++;
  }}
  return n? sq/n-(sum/n)*(sum/n) : 0;
}

const img=new Image();
img.onload=()=>{
  const iw=img.naturalWidth, ih=img.naturalHeight;
  if(view.width!==iw){view.width=iw; view.height=ih; placeRoiBox();}
  vctx.drawImage(img,0,0);
  const s=sharpness(iw,ih);
  if(s>peak) peak=s;
  scoreEl.textContent=Math.round(s);
  peakEl.textContent=Math.round(peak);
  barEl.style.width=(peak? Math.min(100,100*s/peak):0)+'%';
  if(osc){ const t=peak? s/peak:0; osc.frequency.value=220+880*t*t; }
  frames++;
  const now=performance.now();
  if(now-lastFps>500){fpsEl.textContent=(frames*1000/(now-lastFps)).toFixed(0)+' fps';
    frames=0; lastFps=now;}
  requestAnimationFrame(tick);
};
img.onerror=()=>setTimeout(tick,500);
function tick(){ img.src='/frame.jpg?t='+performance.now(); }
tick();
</script>
</body></html>
"""


class _Handler(BaseHTTPRequestHandler):
    buffer: FrameBuffer  # set on the server instance, read via self.server

    def log_message(self, *args) -> None:  # quiet; the app logs what it needs
        pass

    def do_GET(self) -> None:  # noqa: N802 (stdlib naming)
        path = self.path.split("?", 1)[0]
        buffer: FrameBuffer = self.server.buffer  # type: ignore[attr-defined]
        if path == "/":
            body = _PAGE.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if path == "/frame.jpg":
            self._serve_latest_frame(buffer)
            return
        self.send_error(404)

    def _serve_latest_frame(self, buffer: FrameBuffer) -> None:
        # Long-poll: wait for a fresh frame so the browser advances one per request.
        last = int(self.headers.get("X-Last-Seq", "0") or "0")
        seq, frame = buffer.wait_newer(last, timeout=5.0)
        if frame is None:
            self.send_error(503, "no frames yet")
            return
        try:
            self.send_response(200)
            self.send_header("Content-Type", "image/jpeg")
            self.send_header("Content-Length", str(len(frame)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Seq", str(seq))
            self.end_headers()
            self.wfile.write(frame)
        except (BrokenPipeError, ConnectionResetError):
            pass


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


def run(
    cfg: FocusConfig, *, basler: BaslerConfig | None = None, dry_run: bool = False
) -> int:
    """Start the camera stream and serve the focus-assist page until Ctrl-C."""
    commands = build_focus_commands(cfg, basler)
    rendered = " | ".join(shlex.join(cmd) for cmd in commands)
    log.info("Focus stream: %s", rendered)
    if dry_run:
        print(rendered)
        return 0

    buffer = FrameBuffer()
    procs: list[subprocess.Popen] = []
    stdin = None
    for command in commands:
        proc = subprocess.Popen(command, stdin=stdin, stdout=subprocess.PIPE)
        if stdin is not None:
            stdin.close()  # let the producer see SIGPIPE if the consumer dies
        stdin = proc.stdout
        procs.append(proc)
    reader = threading.Thread(
        target=_split_mjpeg, args=(procs[-1].stdout, buffer), daemon=True
    )
    reader.start()

    server = ThreadingHTTPServer(("0.0.0.0", cfg.port), _Handler)
    server.buffer = buffer  # type: ignore[attr-defined]
    server.daemon_threads = True

    print(f"\ncamrig focus — {cfg.camera} {cfg.width}x{cfg.height}@{cfg.framerate} q{cfg.quality}")
    print("Open in a browser on your tailnet, turn the lens ring to peak the score:")
    for url in _local_urls(cfg.port):
        print(f"  {url}")
    print("Ctrl-C to stop.\n")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.shutdown()
        for proc in procs:  # producer first, so consumers see EOF and drain
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
        buffer.close()
    return 0
