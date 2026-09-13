#!/usr/bin/env python3
"""Live Py-Feat v2 expression dashboard for BracketBot's head camera."""

import argparse
import asyncio
import contextlib
import json
import socket
import threading
import time
from dataclasses import dataclass

import cv2
import numpy as np
import torch
import uvicorn
from bbos import Reader
from fastapi import FastAPI, Response
from fastapi.responses import HTMLResponse, StreamingResponse
from feat import Detectorv2

CAMERA_TOPIC = "camera.head.jpeg"
DISPLAY_FPS = 15
INFERENCE_INTERVAL_S = 0.75
INFERENCE_WIDTH = 640
EMOTION_NAMES = ("Neutral", "Happy", "Sad", "Surprise", "Fear", "Disgust", "Anger")


@dataclass
class SharedState:
    lock: threading.Lock
    raw_frame: np.ndarray | None = None
    raw_sequence: int = 0
    rendered_jpeg: bytes | None = None
    camera_timestamp_ns: int = 0
    result: dict | None = None


state = SharedState(lock=threading.Lock())
stop_event = threading.Event()
app = FastAPI(title="BracketBot Py-Feat Expression Camera")


def resize_for_inference(image: np.ndarray) -> np.ndarray:
    h, w = image.shape[:2]
    if w <= INFERENCE_WIDTH:
        return image.copy()
    scale = INFERENCE_WIDTH / w
    return cv2.resize(image, (INFERENCE_WIDTH, round(h * scale)), interpolation=cv2.INTER_AREA)


def read_right_eye(reader: Reader) -> tuple[np.ndarray, int] | None:
    if not reader.ready():
        return None
    n = int(reader.data["jpeg_len"])
    if n <= 0:
        return None
    stereo = cv2.imdecode(np.asarray(reader.data["jpeg"][:n]), cv2.IMREAD_COLOR)
    if stereo is None or stereo.shape[1] < 2:
        return None
    right = np.ascontiguousarray(stereo[:, stereo.shape[1] // 2:])
    timestamp_ns = int(reader.data["timestamp"].view("i8"))
    return resize_for_inference(right), timestamp_ns


def draw_result(frame: np.ndarray, result: dict | None) -> bytes | None:
    image = frame.copy()
    if result and result.get("faces"):
        for face in result["faces"]:
            x, y, w, h = (int(v) for v in face["box"])
            label = f'{face["emotion"]} {face["confidence"] * 100:.0f}%'
            cv2.rectangle(image, (x, y), (x + w, y + h), (60, 230, 170), 3)
            (tw, th), _ = cv2.getTextSize(label, cv2.FONT_HERSHEY_SIMPLEX, 0.65, 2)
            top = max(0, y - th - 14)
            cv2.rectangle(image, (x, top), (x + tw + 14, y), (60, 230, 170), -1)
            cv2.putText(image, label, (x + 7, y - 7), cv2.FONT_HERSHEY_SIMPLEX,
                        0.65, (8, 15, 18), 2, cv2.LINE_AA)
    ok, encoded = cv2.imencode(".jpg", image, [int(cv2.IMWRITE_JPEG_QUALITY), 82])
    return encoded.tobytes() if ok else None


def camera_loop() -> None:
    with Reader(CAMERA_TOPIC, keeptime=False) as reader:
        while not stop_event.is_set():
            item = read_right_eye(reader)
            if item is None:
                stop_event.wait(0.005)
                continue
            frame, timestamp_ns = item
            with state.lock:
                state.raw_frame = frame
                state.raw_sequence += 1
                state.camera_timestamp_ns = timestamp_ns
                result = state.result
            rendered = draw_result(frame, result)
            if rendered:
                with state.lock:
                    state.rendered_jpeg = rendered


def fex_to_result(fex, sequence: int, timestamp_ns: int, latency_s: float) -> dict:
    faces = []
    emotion_columns = list(fex.emotion_columns or EMOTION_NAMES)
    for _, row in fex.iterrows():
        box = [float(row.get("FaceRectX", np.nan)), float(row.get("FaceRectY", np.nan)),
               float(row.get("FaceRectWidth", np.nan)), float(row.get("FaceRectHeight", np.nan))]
        # Detectorv2 deliberately emits one all-NaN placeholder row when a
        # frame contains no detections. It is metadata, not a face.
        if not np.isfinite(box).all() or box[2] <= 0 or box[3] <= 0:
            continue
        scores = {name: max(0.0, min(1.0, float(row[name]))) for name in emotion_columns
                  if name in row and np.isfinite(row[name])}
        emotion = max(scores, key=scores.get) if scores else "Unknown"
        faces.append({"emotion": emotion, "confidence": scores.get(emotion, 0.0),
                      "scores": scores, "box": box})
    faces.sort(key=lambda face: face["box"][2] * face["box"][3], reverse=True)
    return {
        "valid": bool(faces),
        "faces": faces,
        "people": len(faces),
        "primary_emotion": faces[0]["emotion"] if faces else None,
        "confidence": faces[0]["confidence"] if faces else None,
        "scores": faces[0]["scores"] if faces else {name: 0.0 for name in EMOTION_NAMES},
        "source_ns": timestamp_ns,
        "frame_sequence": sequence,
        "latency_ms": round(latency_s * 1000, 1),
        "model": "Py-Feat Detectorv2",
        "reason": "face_detected" if faces else "no_face_detected",
    }


def inference_loop(detector: Detectorv2) -> None:
    last_sequence = -1
    while not stop_event.is_set():
        cycle_start = time.monotonic()
        with state.lock:
            frame = None if state.raw_frame is None else state.raw_frame.copy()
            sequence = state.raw_sequence
            timestamp_ns = state.camera_timestamp_ns
        if frame is not None and sequence != last_sequence:
            try:
                rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                tensor = torch.from_numpy(rgb).permute(2, 0, 1).unsqueeze(0)
                started = time.monotonic()
                with torch.inference_mode():
                    fex = detector.detect(tensor, data_type="tensor", batch_size=1,
                                          progress_bar=False)
                result = fex_to_result(fex, sequence, timestamp_ns,
                                       time.monotonic() - started)
            except Exception as exc:
                result = {
                    "valid": False, "faces": [], "people": 0,
                    "primary_emotion": None, "confidence": None,
                    "scores": {name: 0.0 for name in EMOTION_NAMES},
                    "source_ns": timestamp_ns, "frame_sequence": sequence,
                    "latency_ms": None, "model": "Py-Feat Detectorv2",
                    "reason": f"inference_error: {type(exc).__name__}: {exc}",
                }
            with state.lock:
                state.result = result
            last_sequence = sequence
            print(json.dumps(result, separators=(",", ":")), flush=True)
        elapsed = time.monotonic() - cycle_start
        stop_event.wait(max(0.02, INFERENCE_INTERVAL_S - elapsed))


async def mjpeg_stream():
    last = None
    while True:
        with state.lock:
            jpeg = state.rendered_jpeg
        if jpeg is not None and jpeg is not last:
            last = jpeg
            yield b"--frame\r\nContent-Type: image/jpeg\r\n\r\n" + jpeg + b"\r\n"
        await asyncio.sleep(1 / DISPLAY_FPS)


@app.get("/head/stream")
async def stream():
    return StreamingResponse(mjpeg_stream(), media_type="multipart/x-mixed-replace; boundary=frame",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.get("/head/frame")
async def frame():
    with state.lock:
        jpeg = state.rendered_jpeg
    return Response(jpeg, media_type="image/jpeg") if jpeg else Response(status_code=503)


@app.get("/expression")
async def expression():
    with state.lock:
        result = state.result
    if result is None:
        result = {"valid": False, "faces": [], "people": 0, "primary_emotion": None,
                  "confidence": None, "scores": {name: 0.0 for name in EMOTION_NAMES},
                  "reason": "model_loading_or_waiting_for_camera"}
    return Response(json.dumps(result), media_type="application/json",
                    headers={"Cache-Control": "no-store"})


@app.get("/health")
async def health():
    with state.lock:
        camera_ok = state.raw_frame is not None
        result = state.result
    return {"ok": camera_ok and result is not None, "camera": camera_ok,
            "inference": result is not None, "topic": CAMERA_TOPIC}


@app.get("/", response_class=HTMLResponse)
async def index():
    return HTMLResponse("""<!doctype html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>BracketBot Expression Camera</title><style>
:root{color-scheme:dark;--bg:#071013;--card:#0d1b20;--line:#1d343c;--mint:#3ce6aa;--text:#edf8f5;--muted:#8fa8a2}
*{box-sizing:border-box}body{margin:0;background:radial-gradient(circle at 85% 0,#14313a 0,transparent 38%),var(--bg);color:var(--text);font:15px Inter,system-ui,sans-serif}
main{max-width:1180px;margin:auto;padding:28px}.top{display:flex;align-items:end;justify-content:space-between;margin-bottom:18px}h1{font-size:28px;margin:0}.sub{color:var(--muted);margin-top:5px}.status{color:var(--mint)}
.grid{display:grid;grid-template-columns:minmax(0,1.65fr) minmax(300px,.85fr);gap:18px}.card{background:rgba(13,27,32,.92);border:1px solid var(--line);border-radius:18px;padding:18px;box-shadow:0 18px 60px #0005}
.video{padding:10px;overflow:hidden}.video img{width:100%;display:block;border-radius:12px;aspect-ratio:4/3;object-fit:cover;background:#020506}
.emotion{text-align:center;padding:20px 10px 24px}.label{color:var(--muted);font-size:12px;letter-spacing:.14em;text-transform:uppercase}.primary{font-size:46px;font-weight:750;margin:8px 0 2px}.confidence{font-size:18px;color:var(--mint)}
.bars{margin-top:12px}.row{display:grid;grid-template-columns:76px 1fr 46px;gap:9px;align-items:center;margin:9px 0}.track{height:9px;background:#1a2b30;border-radius:9px;overflow:hidden}.fill{height:100%;width:0;background:linear-gradient(90deg,#25bd8b,var(--mint));transition:width .25s}.pct{text-align:right;color:var(--muted);font-variant-numeric:tabular-nums}
.meta{display:grid;grid-template-columns:repeat(3,1fr);gap:10px;margin-top:18px}.metric{background:#091418;border:1px solid var(--line);border-radius:12px;padding:12px}.metric b{display:block;font-size:20px;margin-top:4px}.note{color:var(--muted);font-size:12px;margin-top:14px}
@media(max-width:800px){.grid{grid-template-columns:1fr}.top{align-items:start;flex-direction:column;gap:8px}}
</style></head><body><main>
<div class="top"><div><h1>Expression Camera</h1><div class="sub">Py-Feat v2 · BracketBot right-eye view</div></div><div id="status" class="status">Starting…</div></div>
<div class="grid"><section class="card video"><img src="/head/stream" alt="Live camera"></section>
<aside class="card"><div class="emotion"><div class="label">Primary expression</div><div id="primary" class="primary">—</div><div id="confidence" class="confidence">Waiting for a face</div></div><div id="bars" class="bars"></div>
<div class="meta"><div class="metric"><span class="label">Faces</span><b id="faces">0</b></div><div class="metric"><span class="label">Latency</span><b id="latency">—</b></div><div class="metric"><span class="label">Rate</span><b id="rate">—</b></div></div>
<div class="note">Scores are model estimates of visible facial expression, not a measurement of a person's internal emotional state.</div></aside></div>
</main><script>
const names=['Neutral','Happy','Sad','Surprise','Fear','Disgust','Anger'];
const bars=document.getElementById('bars');for(const n of names){bars.insertAdjacentHTML('beforeend',`<div class="row"><span>${n}</span><div class="track"><div class="fill" id="b-${n}"></div></div><span class="pct" id="p-${n}">0%</span></div>`)}
let priorSeq=null,priorTime=performance.now();async function poll(){try{const r=await fetch('/expression',{cache:'no-store'}),d=await r.json();const valid=d.valid;
document.getElementById('status').textContent=d.reason==='face_detected'?'● Live':'● '+String(d.reason||'waiting').replaceAll('_',' ');
document.getElementById('primary').textContent=valid?d.primary_emotion:'No face';document.getElementById('confidence').textContent=valid?Math.round(d.confidence*100)+'% confidence':'Look toward the robot';document.getElementById('faces').textContent=d.people||0;document.getElementById('latency').textContent=d.latency_ms?Math.round(d.latency_ms)+' ms':'—';
if(priorSeq!==null&&d.frame_sequence!==priorSeq){const now=performance.now();document.getElementById('rate').textContent=(1000/(now-priorTime)).toFixed(1)+' Hz';priorTime=now}priorSeq=d.frame_sequence;
for(const n of names){const v=Number((d.scores||{})[n]||0);document.getElementById('b-'+n).style.width=(v*100)+'%';document.getElementById('p-'+n).textContent=Math.round(v*100)+'%'}}catch(e){document.getElementById('status').textContent='● Disconnected'}setTimeout(poll,250)}poll();
</script></body></html>""")


def wait_for_frame(timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with state.lock:
            if state.raw_frame is not None:
                return True
        time.sleep(0.05)
    return False


def main() -> int:
    parser = argparse.ArgumentParser(description="Live Py-Feat v2 expression camera")
    parser.add_argument("--port", type=int, default=9002)
    parser.add_argument("--once", action="store_true", help="analyze one frame and exit")
    args = parser.parse_args()

    camera_thread = threading.Thread(target=camera_loop, name="camera", daemon=True)
    camera_thread.start()
    if not wait_for_frame():
        raise RuntimeError(f"No frames from {CAMERA_TOPIC} in 10 seconds")

    print("[+] Loading Py-Feat Detectorv2 (first run downloads model weights)...", flush=True)
    detector = Detectorv2(device="cpu", identity_model=None)
    print("[+] Py-Feat Detectorv2 ready", flush=True)
    infer_thread = threading.Thread(target=inference_loop, args=(detector,), name="pyfeat", daemon=True)
    infer_thread.start()

    if args.once:
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            with state.lock:
                if state.result is not None:
                    stop_event.set()
                    infer_thread.join(timeout=10)
                    camera_thread.join(timeout=3)
                    return 0 if not str(state.result.get("reason", "")).startswith("inference_error") else 1
            time.sleep(0.1)
        raise RuntimeError("Py-Feat produced no result in 120 seconds")

    host = socket.gethostname()
    print(f"[+] Dashboard: http://{host}.local:{args.port}/", flush=True)
    try:
        uvicorn.run(app, host="0.0.0.0", port=args.port, log_level="warning",
                    access_log=False, timeout_graceful_shutdown=1)
    finally:
        stop_event.set()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
