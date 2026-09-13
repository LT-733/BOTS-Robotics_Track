"""Read-only live arm torque and end-effector force monitor."""
import csv
import json
import os
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ENV_FILE = Path("/home/bracketbot/bbos/bbos/daemons/arm_left/.devenv/bbos-env.json")
PYTHON = Path("/home/bracketbot/bbos/bbos/daemons/arm_left/.venv/bin/python")

if os.environ.get("PROTO_FORCE_ENV") != "1":
    env = os.environ.copy()
    raw = json.loads(ENV_FILE.read_text())
    values = raw.get("env", raw)
    for key in ("PATH", "LD_LIBRARY_PATH", "PYTHONPATH", "NIX_PYTHONPATH"):
        if isinstance(values.get(key), str):
            env[key] = values[key]
    env["PROTO_FORCE_ENV"] = "1"
    os.execve(str(PYTHON), [str(PYTHON), str(Path(__file__).resolve()), *sys.argv[1:]], env)

import numpy as np
import pinocchio as pin
from bbos import Config, Reader


class ForceEstimator:
    def __init__(self, arm):
        self.cfg = Config(arm)
        full = pin.buildModelFromUrdf(self.cfg.urdf_path)
        ids = [full.getJointId(n) for n in self.cfg.joint_names[:7]]
        locks = [j for j in range(1, full.njoints) if j not in ids]
        self.model = pin.buildReducedModel(full, locks, pin.neutral(full))
        self.data = self.model.createData()
        self.frame = self.model.getFrameId(self.cfg.ee_frame)
        self.zero = np.zeros(7)
        self.bias_samples = []
        self.force_bias = np.zeros(3)

    def estimate(self, state, calibrating=False):
        pos = np.array(state["pos"], dtype=float)
        vel = np.array(state["vel"], dtype=float)
        current = np.array(state["current"], dtype=float)
        q = np.asarray(self.cfg.q2urdf(pos.copy()), dtype=float)[:7]
        v = np.asarray(self.cfg.q2urdf(vel.copy()), dtype=float)[:7]
        tau_motor = np.zeros(7)
        tau_motor[1:7] = (current * np.asarray(self.cfg.kt))[1:7]
        tau_expected = pin.rnea(self.model, self.data, q, v, self.zero)
        tau_expected[0] = 0.0
        tau_residual = -(tau_motor - tau_expected)
        pin.forwardKinematics(self.model, self.data, q, v)
        pin.computeJointJacobians(self.model, self.data, q)
        pin.updateFramePlacements(self.model, self.data)
        jac = pin.getFrameJacobian(
            self.model, self.data, self.frame,
            pin.ReferenceFrame.LOCAL_WORLD_ALIGNED,
        )[:3]
        force_raw = np.linalg.solve(jac @ jac.T + 1e-3*np.eye(3), jac @ tau_residual)
        if calibrating:
            self.bias_samples.append(force_raw)
            self.force_bias = np.median(np.stack(self.bias_samples), axis=0)
        return current, tau_motor, tau_residual, force_raw-self.force_bias


def main():
    output = Path(sys.argv[1]) if len(sys.argv) > 1 else HERE / "live_force.csv"
    fields = ["time_s", "side", "fx_n", "fy_n", "fz_n", "force_n",
              "peak_joint", "peak_residual_nm", "current_a", "torque_nm", "residual_nm"]
    estimators = [ForceEstimator("arm_left"), ForceEstimator("arm_right")]
    with Reader("arm_left.state", keeptime=False) as left, Reader(
        "arm_right.state", keeptime=False
    ) as right, output.open("w", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        print("Read-only monitor. Force assumes one contact at each end effector.")
        print("Hold arms untouched for 1.5 seconds while the force bias is measured.")
        start = time.monotonic()
        next_print = start
        while True:
            now = time.monotonic()
            if not (left.ready() and right.ready()):
                time.sleep(0.02)
                continue
            calibrating = now-start < 1.5
            rows = []
            for side, state, estimator in zip(("L", "R"), (left.data, right.data), estimators):
                current, torque, residual, force = estimator.estimate(state, calibrating)
                peak = int(np.argmax(np.abs(residual[1:7]))) + 1
                row = {
                    "time_s": f"{now-start:.4f}", "side": side,
                    "fx_n": f"{force[0]:.4f}", "fy_n": f"{force[1]:.4f}",
                    "fz_n": f"{force[2]:.4f}", "force_n": f"{np.linalg.norm(force):.4f}",
                    "peak_joint": peak, "peak_residual_nm": f"{residual[peak]:.5f}",
                    "current_a": json.dumps(current.tolist()),
                    "torque_nm": json.dumps(torque.tolist()),
                    "residual_nm": json.dumps(residual.tolist()),
                }
                writer.writerow(row)
                rows.append(row)
            stream.flush()
            if now >= next_print:
                tag = "BIAS" if calibrating else "LIVE"
                print(" | ".join(
                    f"{tag} {r['time_s']}s {r['side']} force={r['force_n']}N "
                    f"xyz=({r['fx_n']},{r['fy_n']},{r['fz_n']}) "
                    f"peak=J{r['peak_joint']} {r['peak_residual_nm']}Nm "
                    f"tau={r['torque_nm']}"
                    for r in rows
                ), flush=True)
                next_print = now + 0.10
            time.sleep(0.02)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nStopped. CSV data was saved.")
