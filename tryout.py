# /// script
# dependencies = [
#   "bbos",
#   "numpy<2",
# ]
# [tool.uv.sources]
# bbos = { path = "/home/bracketbot/bbos", editable = true }
# ///
import json
import time
import threading
import numpy as np
from pathlib import Path
from bbos import Reader, Writer, Type, Config
from collections import deque

# control whether to record baseline torque and write to hug-torque.json
DRY_RUN = False
RUNNING_TORQUE_SPIKES = 9

RECORDING = Path(__file__).parent / "hug.json"

LEFT_CFG = Config("arm_left")
RIGHT_CFG = Config("arm_right")
DOF = LEFT_CFG.dof

baseline_torque : list = []
with open("hug-torque.json", 'r') as file:
    baseline_torque = json.load(file)

baseline_torque_np: np.array = np.array(baseline_torque)

left_spike_window = deque(maxlen=10)
right_spike_window = deque(maxlen=10)

def main():
    with open(RECORDING) as f:
        data = json.load(f)

    frames = data["frames"]
    if not frames:
        print("No frames in recording.")
        return

    print(f"Loaded '{data.get('name', 'hug_final')}' — {len(frames)} frames, {frames[-1]['t']:.1f}s")

    torque_baseline = []
    left_active = True
    right_active = True

    with (
        Reader("arm_left.state", keeptime=False) as r_left,
        Reader("arm_right.state", keeptime=False) as r_right,
        Writer("arm_left.ctrl", Type("arm_ctrl"), keeptime=False) as w_left,
        Writer("arm_right.ctrl", Type("arm_ctrl"), keeptime=False) as w_right,
        Writer("arm_left.torque", Type("arm_torque"), keeptime=False) as w_torque_left,
        Writer("arm_right.torque", Type("arm_torque"), keeptime=False) as w_torque_right,
    ):
        # Snap to first frame position before enabling torque
        w_torque_left['enable'] = np.zeros(DOF, dtype=np.bool_)
        w_torque_right['enable'] = np.zeros(DOF, dtype=np.bool_)
        time.sleep(0.1)

        w_left['pos'] = np.zeros(DOF, dtype=np.float32)
        w_right['pos'] = np.zeros(DOF, dtype=np.float32)
        time.sleep(0.1)

        w_torque_left['enable'] = np.ones(DOF, dtype=np.bool_)
        w_torque_right['enable'] = np.ones(DOF, dtype=np.bool_)
        time.sleep(0.1)

        print("Playing...")
        t0 = time.time()
        for i in range(len(frames)):
            target = t0 + frames[i]["t"]
            now = time.time()
            if target > now:
                time.sleep(target - now)
            if left_active:
                w_left['pos'] = np.array(frames[i]["left"], dtype=np.float32)
            if right_active:
                w_right['pos'] = -np.array(frames[i]["left"], dtype=np.float32)

            torque_left = -1
            torque_right = -1
            if r_left.ready():
                torque_left = float(np.average(r_left.data['torque'][1:6]))
            if r_right.ready():
                torque_right = float(np.average(r_right.data['torque'][1:6]))
            if DRY_RUN:
                torque_baseline.append([torque_left, torque_right])
            else:
                if i >= 750 and \
                        baseline_torque_np[i][0] != -1 and baseline_torque_np[i][1] != -1 \
                        and torque_left != -1 and torque_right != -1:

                    left_spike = abs((baseline_torque_np[i][0] - torque_left) / baseline_torque_np[i][0]) > 0.15
                    right_spike = abs((baseline_torque_np[i][1] - torque_right) / baseline_torque_np[i][1]) > 0.15

                    left_spike_window.append(left_spike)
                    right_spike_window.append(right_spike)
                    if sum(left_spike_window) > RUNNING_TORQUE_SPIKES:
                        left_active = False
                    if sum(right_spike_window) > RUNNING_TORQUE_SPIKES:
                        right_active = False

        # for frame in frames:
        #     target = t0 + frame["t"]
        #     now = time.time()
        #     if target > now:
        #         time.sleep(target - now)
        #     w_left['pos'] = np.array(frame["left"], dtype=np.float32)
        #     w_right['pos'] = -np.array(frame["left"], dtype=np.float32)

        #     torque_left = -1
        #     torque_right = -1
        #     if r_left.ready():
        #         torque_left = float(np.average(r_left.data['torque'][1:6]))
        #     if r_right.ready():
        #         torque_right = float(np.average(r_left.data['torque'][1:6]))
        #     if DRY_RUN:
        #         torque_baseline.append([torque_left, torque_right])

        # get the good hugging position (true to person's shape)
        # do not copy left/right since it might not be symmetric

        while not r_left.ready():
            time.sleep(0.01)
        hug_left = r_left.data['pos'].copy()

        while not r_right.ready():
            time.sleep(0.01)
        hug_right = r_right.data['pos'].copy()

        if r_left.ready():
            hug_left = r_left.data['pos']
        if r_right.ready():
            hug_right = r_right.data['pos']
        time.sleep(0.1)

        for i in range(0, 1200):
            hug_left[0] += 0.0025
            hug_right[0] -= 0.0025
            w_left['pos'] = np.array(hug_left, dtype=np.float32)
            w_right['pos'] = np.array(hug_right, dtype=np.float32)

            time.sleep(0.005)

        w_torque_left['enable'] = np.zeros(DOF, dtype=np.bool_)
        w_torque_right['enable'] = np.zeros(DOF, dtype=np.bool_)
        time.sleep(0.1)

        w_left['pos'] = np.zeros(DOF, dtype=np.float32)
        w_right['pos'] = np.zeros(DOF, dtype=np.float32)
        time.sleep(0.1)

        w_torque_left['enable'] = np.ones(DOF, dtype=np.bool_)
        w_torque_right['enable'] = np.ones(DOF, dtype=np.bool_)
        time.sleep(0.1)

        # Disable torque when done
        w_torque_left['enable'] = np.zeros(DOF, dtype=np.bool_)
        w_torque_right['enable'] = np.zeros(DOF, dtype=np.bool_)
        print("Done.")

    if DRY_RUN:
        open("hug-torque.json", "w").write(json.dumps(torque_baseline))

if __name__ == "__main__":
    main()
