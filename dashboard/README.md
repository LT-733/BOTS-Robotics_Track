# Proton Microexpress expression camera

This standalone app reads BracketBot's existing `camera.head.jpeg` shared-memory topic and analyzes the right-eye image with Py-Feat 2's `Detectorv2`. It does not open a physical camera or modify `utwat/pose.py`.

## Run

```bash
cd ~/bbapps/proton_microexpress
.venv/bin/python expression_camera.py
```

Open `http://100.66.141.212:9002/`. The `.local` hostname may not resolve over Tailscale, so the robot's Tailscale IP is the reliable address. The live view continues independently of the slower expression inference. The JSON API is at `/expression`, a still frame is at `/head/frame`, and health state is at `/health`.

The dashboard normally runs as a user service and survives SSH disconnects:

```bash
systemctl --user start bracketbot-expression
systemctl --user stop bracketbot-expression
systemctl --user restart bracketbot-expression
systemctl --user status bracketbot-expression
journalctl --user -u bracketbot-expression -f
```

## Bouncer dashboard with distance and expression

The existing Bouncer dashboard is duplicated in `dashboard/`. Its triangulation header shows `Distance` and `Expression` side by side. Open:

```text
http://100.66.141.212:8000/
```

This copy also runs persistently:

```bash
systemctl --user restart bracketbot-bouncer-expression
systemctl --user status bracketbot-bouncer-expression
```

For one diagnostic inference:

```bash
.venv/bin/python expression_camera.py --once
```

## Isolated installation

Py-Feat 2.1.3 requires Python 3.11 or newer. This robot's control environment uses Python 3.10, so the app has its own Python 3.11 virtual environment. To rebuild it:

```bash
uv python install 3.11
uv venv --python 3.11 --clear .venv
uv pip install --python .venv/bin/python --torch-backend cpu \
  'py-feat==2.1.3' fastapi uvicorn opencv-python-headless \
  --editable /home/bracketbot/bbos
```

The first launch downloads Py-Feat model weights from Hugging Face. Later launches use the local cache.
