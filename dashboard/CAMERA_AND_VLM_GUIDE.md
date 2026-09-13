# BracketBot cameras and a practical VLM integration plan

This note describes the camera system currently installed on `bracketbot-180`, how robot programs consume it, what is live right now, and the cleanest way to add a vision-language model (VLM).

## The short version

The robot has three logical camera feeds:

| Camera | Physical role | Raw capture | Published image | BBOS JPEG topic |
|---|---|---:|---:|---|
| Head | Stereo pair in one side-by-side frame | 2560×960 at 60 FPS | 2560×960 at about 30 FPS | `camera.head.jpeg` |
| Left | Left wrist monocular camera | 640×480 at 30 FPS | 640×480 | `camera.left.jpeg` |
| Right | Right wrist monocular camera | 640×480 at 30 FPS | 640×480 | `camera.right.jpeg` |

The camera daemon owns the physical V4L2 devices. Applications should **not** open `/dev/video*` themselves. They subscribe to the latest frames through BBOS shared memory using `bbos.Reader`. This lets teleoperation, logging, policies, and a VLM use the cameras without fighting over the USB devices.

For a VLM, start with `camera.head.jpeg`, split the image down the middle, and send one eye (normally the right eye) to the model. The repository already implements exactly this in `~/bbapps/inference/vlm.py`.

## Data flow

```text
USB cameras
   │  V4L2, MJPEG
   ▼
BBOS camera daemon
   ├── camera.head.jpeg   ──► VLM / policy / web viewer
   ├── camera.head.rgb    ──► stereo depth daemon
   ├── camera.left.jpeg   ──► manipulation policy / web viewer
   ├── camera.right.jpeg  ──► manipulation policy / web viewer
   └── camera.*.status    ──► health checks (streaming, FPS)

Optional depth daemon (currently disabled)
   camera.head.rgb
      └── stereo network + calibration
           ├── camera.rect    rectified left RGB
           ├── camera.depth   depth in millimetres
           └── camera.points  colored 3-D points in the robot base frame
```

## How capture and publishing work

The implementation is in:

- `/home/bracketbot/bbos/bbos/daemons/camera/daemon.py`
- `/home/bracketbot/bbos/bbos/daemons/camera/constants.py`

The daemon starts one thread per camera. Each thread:

1. Resolves the correct `/dev/video*` V4L2 node.
2. Negotiates MJPEG at the exact configured resolution and rate.
3. Uses memory-mapped V4L2 buffers to avoid unnecessary capture copies.
4. Publishes the compressed JPEG and its hardware-derived timestamp into shared memory.
5. For the head camera only, uses the native `bbjpeg` hardware decoder to publish full RGB.
6. Detects stalls or disconnection and retries the device every two seconds.

The head camera captures at 60 FPS but has `decimate = 2`, so it publishes 30 FPS. Its single 2560×960 image contains two 1280×960 eyes:

```text
0                         1280                         2560
┌──────────────────────────┬──────────────────────────┐
│         left eye         │        right eye         │  960 px
└──────────────────────────┴──────────────────────────┘
```

The head JPEG shared-memory buffer can hold up to 4 MiB. Each wrist JPEG buffer can hold up to 640×480 bytes. Always respect `jpeg_len`; the remainder of the fixed-size array is not part of the image.

The head is identified by the unique V4L2 card name `USB Camera`. The two identical, serial-less `icspring camera` wrist cameras are matched to the arm on the same USB hub. The left one requires `/dev/ttyARMLEFT`, and the right one requires `/dev/ttyARMRIGHT`. This avoids silently swapping left and right cameras when Linux renumbers video devices.

## Reading a camera correctly

The smallest useful JPEG reader is:

```python
import time
from bbos import Reader

with Reader("camera.head.jpeg", keeptime=False) as cam:
    deadline = time.monotonic() + 5.0
    while not cam.ready():
        if time.monotonic() > deadline:
            raise RuntimeError("No fresh head-camera frame")
        time.sleep(0.01)

    n = int(cam.data["jpeg_len"])
    jpeg_bytes = bytes(cam.data["jpeg"][:n])
    timestamp = cam.data["timestamp"]
```

`ready()` tells you whether a new record has arrived. Use a timeout rather than spinning forever, because the shared-memory file can remain present even when its physical camera is disconnected.

To decode and select the right head-camera eye:

```python
import cv2
import numpy as np

stereo_bgr = cv2.imdecode(
    np.frombuffer(jpeg_bytes, np.uint8), cv2.IMREAD_COLOR
)
if stereo_bgr is None:
    raise RuntimeError("Bad camera JPEG")

right_eye_bgr = stereo_bgr[:, stereo_bgr.shape[1] // 2:]
```

OpenCV decodes into BGR. Convert with `cv2.cvtColor(image, cv2.COLOR_BGR2RGB)` if the VLM library accepts an RGB array. If its API accepts JPEG bytes, resize and JPEG-encode once, then send the bytes directly.

## The existing VLM implementation

The robot already has a substantial implementation under `~/bbapps/inference`:

- `vlm.py` reads `camera.head.jpeg`, selects the right eye, shrinks its long edge to 768 pixels, JPEG-encodes at quality 85, and calls a VLM.
- `vlm_inference.py` connects the VLM selector to the remote manipulation policy.
- `prompt.md` tells the VLM how to choose a task.
- `task_manifest.json` supplies the exact policy instructions the VLM may select.
- `bracketbot_adapter.py` supplies normalized arm state plus all three JPEG feeds to the manipulation policy.

Supported VLM backends in the current code are:

| Provider | Configured model | Connection |
|---|---|---|
| Google | `gemini-flash-lite-latest` | Google GenAI API |
| OpenAI | `gpt-4o-mini` | OpenAI API |
| Overshoot | `Qwen/Qwen3.6-27B-FP8` | OpenAI-compatible API |
| Local | `Qwen3.5-35B-A3B-FP8` | OpenAI-compatible vLLM server on the lab Spark |

The current VLM is a **task selector**, not a motor controller. Every `vlm_interval` (two seconds by default), it looks at the latest right-eye frame and returns structured JSON containing a task index, a reason, and whether a gripper is holding the bowl. A different task must be selected repeatedly for `switch_streak` cycles before the system switches. The robot homes between episodes, and a 25-second default limit forces a re-home if an episode gets stuck.

That separation is a good design:

```text
VLM (slow, semantic, uncertain) ──► trained task instruction
                                      │
                                      ▼
Manipulation policy (30 Hz) ──────► joint targets
                                      │
                                      ▼
Robot safety/homing layer ─────────► arm hardware
```

Do not put cloud-VLM responses directly into motor positions. Network latency, malformed output, and visual ambiguity make that unsafe and too slow. Let the VLM choose a bounded skill or goal; let the existing policy/control loop execute it at its normal rate.

## Recommended way to add your VLM

Use the existing `grab_right_eye()` and controller structure first. Replace only the provider/model call if your VLM differs.

1. Subscribe to `camera.head.jpeg` with one long-lived `Reader`.
2. Read only fresh frames and enforce a timeout.
3. Crop one eye. A single eye is enough for semantic scene understanding and halves redundant input.
4. Resize to roughly 768 pixels on the long edge. This lowers upload time and image-token cost while retaining useful scene detail.
5. Ask for a strict, small output schema such as `task_index`, `confidence`, `reason`, and `holding`.
6. Validate the returned task against a fixed allowlist.
7. Debounce decisions across several calls.
8. Send the selected text instruction to the existing policy; keep homing, timeouts, and stop behavior outside the VLM.
9. Log frame timestamps, VLM latency, raw decisions, accepted decisions, and policy transitions.

For richer manipulation later, add the wrist cameras as separate images. The head camera provides scene context; the wrist view helps with occlusion and close-up grasp state. Keep the camera identity explicit in the prompt because the wrist images have different viewpoints.

Depth should be treated as numeric perception rather than pasted into a VLM prompt. A useful hybrid is to compute object distance or collision clearance from `camera.depth`/`camera.points`, then include that compact result in the VLM context or use it as a hard controller constraint.

## Current live state observed on 2026-09-13

| Feed | Observed state | Notes |
|---|---|---|
| Head | Healthy, about 29.99 FPS | JPEG decoded as 2560×960; roughly 380 KiB in the sampled scene |
| Left wrist | Down, 0 FPS | Camera daemon reports `/dev/ttyARMLEFT` missing, so it refuses to guess which wrist camera to use |
| Right wrist | Live, about 20 FPS | JPEG decoded as 640×480; configured for 30 FPS but observed below that rate |
| Depth | Daemon not running | Its daemon directory contains `.disabled`; shared-memory topic files still exist and may contain stale records |

The key lesson is that the existence of `/dev/shm/camera.*` does not prove that a sensor is live. Check `camera.<name>.status` and freshness of timestamps before inference. The camera daemon deliberately keeps writers alive across device loss.

## Useful existing commands

View all three camera streams in a browser:

```bash
cd ~/bbapps
uv run examples/view_camera.py
```

Then open `http://bracketbot-180.local:8004/` from a machine that can reach the robot.

Check stereo calibration (the camera daemon must be running):

```bash
uv run ~/bbapps/depth_calibration_check.py
```

Run the existing VLM selector after supplying the policy server, checkpoint, and provider credentials:

```bash
cd ~/bbapps/inference
POLICY_SERVER=<host:port> \
CHECKPOINT=<checkpoint-path> \
uv run vlm_inference.py --provider google
```

For the local lab model, set `BB_VLM_URL` to the OpenAI-compatible vLLM endpoint and use `--provider local`.

## Files worth reading next

- `~/bbapps/examples/view_camera.py` — compact example that exposes all JPEG topics as MJPEG web streams.
- `~/bbapps/inference/vlm.py` — current image preprocessing, model calls, validation, debounce, and homing logic.
- `~/bbapps/inference/vlm_inference.py` — CLI and policy/VLM wiring.
- `~/bbapps/inference/bracketbot_adapter.py` — camera and joint observations passed to the learned policy.
- `/home/bracketbot/bbos/bbos/daemons/camera/daemon.py` — physical capture, recovery, timestamps, and shared-memory publication.
- `/home/bracketbot/bbos/bbos/daemons/depth/daemon.py` — optional stereo depth and point-cloud pipeline.

