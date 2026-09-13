# /// script
# dependencies = [
#   "bbos",
#   "numpy<2",
# ]
# [tool.uv.sources]
# bbos = { path = "/home/bracketbot/bbos", editable = true }
# ///
"""Run proto_hug/main.py and record possible contact from arm motor feedback.

This file only reads arm state. It does not send motor commands; main.py remains
the program that moves the robot.
"""

import argparse
import csv
import json
import signal
import subprocess
import sys
import time
from pathlib import Path

import numpy as np
from bbos import Reader


HERE = Path(__file__).resolve().parent
MAIN = HERE / "main.py"
DEFAULT_CSV = HERE / "feel_data.csv"
DEFAULT_EVENTS = HERE / "feel_events.json"


def snapshot(reader):
    if not reader.ready():
        return None
    state = reader.data
    result = {}
    for field in ("pos", "vel", "current", "torque", "temp"):
        if field in state.dtype.names:
            result[field] = np.asarray(state[field], dtype=float).copy()
    return result if "current" in result else None


def wait_for_sample(left, right, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        lstate, rstate = snapshot(left), snapshot(right)
        if lstate is not None and rstate is not None:
            return lstate, rstate
        time.sleep(0.02)
    raise RuntimeError("No fresh arm state received within 5 seconds")


def main():
    parser = argparse.ArgumentParser(
        description="Run main.py and log possible contact from motor current."
    )
    parser.add_argument("--threshold-amps", type=float, default=0.35,
                        help="current rise above baseline required per motor")
    parser.add_argument("--hold-ms", type=float, default=120.0,
                        help="rise must persist this long before an event")
    parser.add_argument("--baseline-seconds", type=float, default=2.0)
    parser.add_argument("--rate-hz", type=float, default=50.0)
    parser.add_argument("--csv", type=Path, default=DEFAULT_CSV)
    parser.add_argument("--events", type=Path, default=DEFAULT_EVENTS)
    parser.add_argument("--monitor-only", action="store_true",
                        help="log an already-running main.py instead of launching it")
    args = parser.parse_args()

    if args.threshold_amps <= 0 or args.hold_ms <= 0 or args.rate_hz <= 0:
        parser.error("threshold, hold time, and sample rate must be positive")

    period = 1.0 / args.rate_hz
    needed = max(1, int(round(args.hold_ms / 1000.0 * args.rate_hz)))
    child = None
    events = []

    with Reader("arm_left.state", keeptime=False) as left, Reader(
        "arm_right.state", keeptime=False
    ) as right:
        wait_for_sample(left, right)
        print(f"Measuring resting current for {args.baseline_seconds:.1f}s...")
        baseline_left, baseline_right = [], []
        deadline = time.monotonic() + args.baseline_seconds
        while time.monotonic() < deadline:
            lstate, rstate = wait_for_sample(left, right)
            baseline_left.append(lstate["current"])
            baseline_right.append(rstate["current"])
            time.sleep(period)

        left_zero = np.median(np.stack(baseline_left), axis=0)
        right_zero = np.median(np.stack(baseline_right), axis=0)
        print("Baseline ready.")

        if not args.monitor_only:
            print("Starting main.py...")
            child = subprocess.Popen([sys.executable, str(MAIN)], cwd=HERE)

        args.csv.parent.mkdir(parents=True, exist_ok=True)
        started = time.monotonic()
        streak = {"left": np.zeros(len(left_zero), dtype=int),
                  "right": np.zeros(len(right_zero), dtype=int)}
        active = {"left": np.zeros(len(left_zero), dtype=bool),
                  "right": np.zeros(len(right_zero), dtype=bool)}

        fields = ["time_s", "side", "motor", "current_a", "baseline_a",
                  "delta_a", "possible_contact"]
        try:
            with args.csv.open("w", newline="") as output:
                writer = csv.DictWriter(output, fieldnames=fields)
                writer.writeheader()
                while child is None or child.poll() is None:
                    loop_at = time.monotonic()
                    lstate, rstate = wait_for_sample(left, right)
                    elapsed = loop_at - started
                    for side, state, zero in (
                        ("left", lstate, left_zero),
                        ("right", rstate, right_zero),
                    ):
                        current = state["current"]
                        delta = np.abs(current - zero)
                        above = delta >= args.threshold_amps
                        streak[side] = np.where(above, streak[side] + 1, 0)
                        detected = streak[side] >= needed
                        new_events = detected & ~active[side]
                        for motor in np.flatnonzero(new_events):
                            event = {
                                "time_s": round(elapsed, 4),
                                "side": side,
                                "motor": int(motor),
                                "current_a": float(current[motor]),
                                "baseline_a": float(zero[motor]),
                                "delta_a": float(delta[motor]),
                            }
                            events.append(event)
                            print("POSSIBLE CONTACT:", json.dumps(event))
                        active[side] = detected
                        for motor in range(len(current)):
                            writer.writerow({
                                "time_s": f"{elapsed:.4f}",
                                "side": side,
                                "motor": motor,
                                "current_a": f"{current[motor]:.6f}",
                                "baseline_a": f"{zero[motor]:.6f}",
                                "delta_a": f"{delta[motor]:.6f}",
                                "possible_contact": int(detected[motor]),
                            })
                    output.flush()
                    time.sleep(max(0.0, period - (time.monotonic() - loop_at)))
        except KeyboardInterrupt:
            print("Stopping logger...")
            if child is not None and child.poll() is None:
                child.send_signal(signal.SIGINT)
                try:
                    child.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    child.terminate()
        finally:
            args.events.write_text(json.dumps({
                "threshold_amps": args.threshold_amps,
                "hold_ms": args.hold_ms,
                "events": events,
            }, indent=2) + "\n")

    code = child.returncode if child is not None else 0
    print(f"Saved samples to {args.csv}")
    print(f"Saved {len(events)} possible-contact events to {args.events}")
    raise SystemExit(code or 0)


if __name__ == "__main__":
    main()
