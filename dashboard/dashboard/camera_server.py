#!/usr/bin/env python3
import json
import sys
import threading
import time
from pathlib import Path
from typing import Generator, Optional

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from pydantic import BaseModel
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import numpy as np
import uvicorn

ROOT = Path(__file__).resolve().parent
LED_STATUS_PATH = ROOT.parent / "led_status.json"
BBOS_DIR = Path("/home/bracketbot/bbos")

if str(BBOS_DIR) not in sys.path:
    sys.path.insert(0, str(BBOS_DIR))

from bbos import Config, Reader


REMOTE_BASE = "http://127.0.0.1:8004"
POSE_BASE = "http://127.0.0.1:9000"
EXPRESSION_BASE = "http://127.0.0.1:9002"
API_URL = "http://100.66.141.212:8000"
BACKEND_START_URL = "http://100.66.141.212:8001/api/start"

LATEST = {
    "ready": False,
    "message": "Waiting for BBOS arm state",
    "left": None,
    "right": None,
}

BUTTON_STATES = {
    "left_anomaly": 0,
    "right_anomaly": 0,
    "fail": 0,
    "pass": 0,
}

LATEST_LOCK = threading.Lock()

app = FastAPI(title="BracketBot Camera Dashboard")


class ButtonPress(BaseModel):
    value: int = 1


def timestamp_ns(state):
    return int(np.asarray(state["timestamp"]).view("i8"))


def read_arm_state(reader, config):
    if not reader.ready():
        return None

    state = reader.data
    positions = np.asarray(state["pos"], dtype=float)

    if positions.shape != (8,) or not np.all(np.isfinite(positions)):
        raise ValueError("Invalid eight-motor position array")

    # BBOS state is in motor-turn coordinates.
    # Convert to the URDF joint coordinates expected by FK.
    urdf_positions = config.q2urdf(positions.copy())[:7]

    # FK returns Cartesian position in meters and xyzw orientation.
    xyz, _orientation = config.ik.fk(urdf_positions.tolist())

    xyz = np.asarray(xyz, dtype=float)

    if xyz.shape != (3,) or not np.all(np.isfinite(xyz)):
        raise ValueError("Invalid Cartesian position from FK")

    sample_timestamp_ns = timestamp_ns(state)
    age_ms = max(
        0.0,
        (time.time_ns() - sample_timestamp_ns) / 1_000_000,
    )

    return {
        "x_m": float(xyz[0]),
        "y_m": float(xyz[1]),
        "z_m": float(xyz[2]),
        "age_ms": float(age_ms),
        "timestamp_ns": sample_timestamp_ns,
    }


def telemetry_sampler():
    global LATEST

    configs = {
        "left": Config("arm_left"),
        "right": Config("arm_right"),
    }

    with Reader("arm_left.state", keeptime=False) as left_reader, \
         Reader("arm_right.state", keeptime=False) as right_reader:

        while True:
            try:
                left = read_arm_state(left_reader, configs["left"])
                right = read_arm_state(right_reader, configs["right"])

                ready = (
                    left is not None
                    and right is not None
                    and left["age_ms"] <= 500
                    and right["age_ms"] <= 500
                )

                snapshot = {
                    "ready": ready,
                    "message": "LIVE" if ready else "Waiting for fresh BBOS data",
                    "left": left,
                    "right": right,
                }

                with LATEST_LOCK:
                    LATEST = snapshot

            except Exception as exc:
                with LATEST_LOCK:
                    LATEST = {
                        "ready": False,
                        "message": str(exc),
                        "left": None,
                        "right": None,
                    }

            time.sleep(0.05)


def record_button(button_name: str, payload: Optional[ButtonPress]):
    if button_name not in BUTTON_STATES:
        raise HTTPException(status_code=404, detail="Unknown button")

    value = 1 if payload is None else payload.value

    if value != 1:
        raise HTTPException(
            status_code=400,
            detail="Button value must be 1",
        )

    with LATEST_LOCK:
        BUTTON_STATES[button_name] = 1

    return {
        "button": button_name,
        "value": 1,
    }


@app.post("/api/left-anomaly")
def left_anomaly(payload: Optional[ButtonPress] = None):
    return record_button("left_anomaly", payload)


@app.post("/api/right-anomaly")
def right_anomaly(payload: Optional[ButtonPress] = None):
    return record_button("right_anomaly", payload)


@app.post("/api/fail")
def fail(payload: Optional[ButtonPress] = None):
    return record_button("fail", payload)


@app.post("/api/pass")
def pass_button(payload: Optional[ButtonPress] = None):
    return record_button("pass", payload)


@app.get("/")
@app.get("/index.html")
def index():
    return FileResponse(
        ROOT / "index.html",
        media_type="text/html",
        headers={"Cache-Control": "no-store"},
    )


@app.get("/app.js")
def app_js():
    return FileResponse(
        ROOT / "app.js",
        media_type="application/javascript",
        headers={"Cache-Control": "no-store"},
    )


@app.get("/styles.css")
def styles_css():
    return FileResponse(
        ROOT / "styles.css",
        media_type="text/css",
        headers={"Cache-Control": "no-store"},
    )


@app.get("/panda.png")
def panda():
    return FileResponse(
        ROOT / "panda.png",
        media_type="image/png",
        headers={"Cache-Control": "no-store"},
    )


@app.get("/data")
def data():
    with LATEST_LOCK:
        payload = dict(LATEST)
        payload["buttons"] = dict(BUTTON_STATES)

    try:
        payload["led"] = json.loads(LED_STATUS_PATH.read_text())
    except (OSError, ValueError):
        payload["led"] = None

    return JSONResponse(
        content=payload,
        headers={"Cache-Control": "no-store"},
    )


def proxy_json(remote_url: str):
    try:
        request = Request(
            remote_url,
            headers={"User-Agent": "BracketBotDashboard"},
        )

        with urlopen(request, timeout=2) as response:
            body = response.read()
            content_type = response.headers.get(
                "Content-Type",
                "application/json",
            )

        return Response(
            content=body,
            media_type=content_type,
            headers={"Cache-Control": "no-store"},
        )

    except Exception as exc:
        return JSONResponse(
            status_code=503,
            content={
                "valid": False,
                "distance_m": None,
                "people": 0,
                "reason": f"service_unavailable: {exc}",
            },
            headers={"Cache-Control": "no-store"},
        )


@app.get("/pose/distance")
def pose_distance():
    return proxy_json(POSE_BASE + "/distance")


@app.get("/expression")
def expression():
    return proxy_json(EXPRESSION_BASE + "/expression")


def open_stream(remote_url: str):
    try:
        request = Request(
            remote_url,
            headers={"User-Agent": "BracketBotDashboard"},
        )
        upstream = urlopen(request, timeout=5)
    except HTTPError as exc:
        raise HTTPException(
            status_code=exc.code,
            detail="Camera stream unavailable",
        ) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=503,
            detail="Camera stream unavailable",
        ) from exc

    content_type = upstream.headers.get(
        "Content-Type",
        "multipart/x-mixed-replace; boundary=frame",
    )

    def chunks() -> Generator[bytes, None, None]:
        try:
            while True:
                chunk = upstream.read(65536)

                if not chunk:
                    break

                yield chunk
        finally:
            upstream.close()

    return StreamingResponse(
        chunks(),
        headers={
            "Content-Type": content_type,
            "Cache-Control": "no-store",
            "Pragma": "no-cache",
        },
    )


@app.get("/head/stream")
def head_stream():
    return open_stream(POSE_BASE + "/head/stream")


@app.get("/raw/head/stream")
def raw_head_stream():
    return open_stream(REMOTE_BASE + "/head/stream")


@app.get("/left/stream")
def left_stream():
    return open_stream(REMOTE_BASE + "/left/stream")


@app.get("/right/stream")
def right_stream():
    return open_stream(REMOTE_BASE + "/right/stream")

@app.post("/api/start")
def start_backend():
    try:
        request = Request(
            BACKEND_START_URL,
            method="POST",
            headers={"User-Agent": "BracketBotDashboard"},
        )

        with urlopen(request, timeout=2) as response:
            body = response.read()

        try:
            content = json.loads(body)
        except (TypeError, ValueError):
            content = {"success": True}

        return JSONResponse(
            status_code=response.status,
            content=content,
        )

    except HTTPError as exc:
        return JSONResponse(
            status_code=exc.code,
            content={
                "success": False,
                "error": f"Backend returned HTTP {exc.code}",
            },
        )

    except Exception as exc:
        return JSONResponse(
            status_code=503,
            content={
                "success": False,
                "error": f"Unable to start backend: {exc}",
            },
        )


def main():
    threading.Thread(
        target=telemetry_sampler,
        daemon=True,
    ).start()

    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8010

    print(
        f"Dashboard server running on http://localhost:{port}",
        flush=True,
    )
    print(f"Remote camera source: {REMOTE_BASE}", flush=True)
    print(f"Pose source: {POSE_BASE}", flush=True)

    uvicorn.run(
        app,
        host="0.0.0.0",
        port=port,
        log_level="info",
    )


if __name__ == "__main__":
    main()