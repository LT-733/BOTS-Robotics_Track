#!/usr/bin/env python3
"""Drive BracketBot's LEDs from perception distance and hug lifecycle."""
import json
import os
import sys
import time
from pathlib import Path
from urllib.request import urlopen

import numpy as np

BBOS_DIR = Path("/home/bracketbot/bbos")
if str(BBOS_DIR) not in sys.path:
    sys.path.insert(0, str(BBOS_DIR))

from bbos import Type, Writer

DISTANCE_URL = "http://127.0.0.1:9000/distance"
STATUS_PATH = Path(__file__).with_name("led_status.json")
MIN_DISTANCE_M = 0.20
MAX_DISTANCE_M = 0.35
COLORS = {
    "no_person": (255, 255, 255),
    "too_far": (255, 0, 0),
    "ready": (90, 255, 120),
    "hugging": (0, 255, 0),
    "complete": (0, 255, 0),
}
HUG_SCRIPTS = ("proto_hug/tryout.py", "proto_hug/sigma.py", "proto_hug/charles.py", "proto_hug/leon.py")


def hug_running():
    own_pid = os.getpid()
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit() or int(entry.name) == own_pid:
            continue
        try:
            command = (entry / "cmdline").read_bytes().replace(b"\0", b" ").decode(errors="ignore")
        except (FileNotFoundError, PermissionError, ProcessLookupError):
            continue
        if any(script in command for script in HUG_SCRIPTS):
            return True
    return False


def read_distance():
    try:
        with urlopen(DISTANCE_URL, timeout=1.0) as response:
            return json.load(response)
    except Exception:
        return {"valid": False, "people": 0, "distance_m": None}


def choose_mode(distance, running, completed):
    if running:
        return "hugging"
    if not distance.get("valid") or int(distance.get("people") or 0) < 1:
        return "no_person"
    if completed:
        return "complete"
    value = distance.get("distance_m")
    if not isinstance(value, (int, float)) or not MIN_DISTANCE_M <= value <= MAX_DISTANCE_M:
        return "too_far"
    return "ready"


def write_status(mode, distance):
    data = {
        "mode": mode,
        "rgb": COLORS[mode],
        "period_ms": 500 if mode == "hugging" else 0,
        "distance_m": distance.get("distance_m"),
        "people": int(distance.get("people") or 0),
        "range_m": [MIN_DISTANCE_M, MAX_DISTANCE_M],
        "updated_ns": time.time_ns(),
    }
    temporary = STATUS_PATH.with_suffix(".tmp")
    temporary.write_text(json.dumps(data))
    temporary.replace(STATUS_PATH)


def self_test():
    cases = [
        ({"valid": False, "people": 0}, False, False, "no_person"),
        ({"valid": True, "people": 1, "distance_m": 0.50}, False, False, "too_far"),
        ({"valid": True, "people": 1, "distance_m": 0.25}, False, False, "ready"),
        ({"valid": True, "people": 1, "distance_m": 0.50}, True, False, "hugging"),
        ({"valid": True, "people": 1, "distance_m": 0.50}, False, True, "complete"),
    ]
    for distance, running, completed, expected in cases:
        assert choose_mode(distance, running, completed) == expected
    print("LED state tests passed")


def main():
    completed = False
    was_running = False
    with Writer("led.ctrl", Type("led_ctrl"), keeptime=False) as writer:
        while True:
            distance = read_distance()
            running = hug_running()
            if was_running and not running:
                completed = True
            if not distance.get("valid") or int(distance.get("people") or 0) < 1:
                completed = False
            mode = choose_mode(distance, running, completed)
            with writer.buf() as command:
                command["rgb"] = np.array(COLORS[mode], dtype=np.uint8)
                command["brightness"] = np.int16(-1)
                command["period_ms"] = np.uint16(500 if mode == "hugging" else 0)
            write_status(mode, distance)
            was_running = running
            time.sleep(0.5)


if __name__ == "__main__":
    if "--self-test" in sys.argv:
        self_test()
    else:
        main()
