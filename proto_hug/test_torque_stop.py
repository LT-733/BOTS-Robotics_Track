"""Offline regression tests. No BBOS writers, serial ports, or motors."""
import unittest
import json
import sys
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np
import tryout

CFG = SimpleNamespace(dof=8, kt=[0.204, 2.50069575, 2.50069575, 2.50069575, .204, .204, .204, .204],
                      hard_current_limit=[13.2, 1.96, 1.96, 1.96, 13.2, 13.2, 13.2, 13.2])


def state(t=100.0, joint=2, torque=0.0):
    data = np.zeros(1, dtype=[('timestamp', 'datetime64[ns]'), ('current', 'f8', 8),
                              ('pos', 'f8', 8), ('vel', 'f8', 8)])[0]
    data['timestamp'] = np.datetime64(int(t*1e9), 'ns')
    data['current'][joint] = torque/CFG.kt[joint]
    return data


class Reader:
    # Deliberately models BBOS: .data is frozen until .ready() refreshes it.
    def __init__(self, samples):
        self.queue = list(samples)
        self.data = None
        self.calls = 0

    def ready(self):
        self.calls += 1
        if not self.queue:
            return False
        self.data = self.queue.pop(0)
        return True


class StopTests(unittest.TestCase):
    def guard(self, left, right):
        self.now = 100.0
        readers = [Reader(left), Reader(right)]
        return tryout.TorqueGuard(readers, [CFG, CFG], clock=lambda: self.now, wall=lambda: self.now)

    def test_refresh_exposes_surge_and_latches(self):
        guard = self.guard([state(), state(torque=1.31), state()], [state()]*3)
        self.assertTrue(guard.poll())
        with self.assertRaisesRegex(tryout.Cancelled, 'left J2'):
            guard.poll()
        with self.assertRaises(tryout.Cancelled):
            guard.poll()

    def test_right_refresh_is_not_short_circuited(self):
        guard = self.guard([state()], [state(), state(torque=-1.31)])
        guard.poll()
        with self.assertRaisesRegex(tryout.Cancelled, 'right J2'):
            guard.poll()
        self.assertEqual(guard.readers[1].calls, 2)

    def test_first_normal_peak_allowed_but_no_surge_exemption(self):
        guard = self.guard([state(torque=1.0), state(torque=1.5)], [state()]*2)
        guard.poll()
        with self.assertRaises(tryout.Cancelled):
            guard.poll()

    def test_lift_is_not_compared_to_arm_torque_ceiling(self):
        guard = self.guard([state(joint=0, torque=2.0)], [state()])
        self.assertTrue(guard.poll())

    def test_lift_hardware_limit_still_applies(self):
        guard = self.guard([state(joint=0, torque=3.0)], [state()])
        with self.assertRaisesRegex(tryout.Cancelled, 'current limit'):
            guard.poll()

    def test_exact_boundary(self):
        guard = self.guard([state(joint=4, torque=1.30)], [state()])
        with self.assertRaises(tryout.Cancelled):
            guard.poll()

    def test_stale_stream_cancels(self):
        guard = self.guard([state()], [state()])
        guard.poll()
        self.now += 0.101
        with self.assertRaisesRegex(tryout.Cancelled, 'stopped updating'):
            guard.poll()

    def test_old_timestamp_rejected_even_if_reader_says_ready(self):
        guard = self.guard([state(t=99.0)], [state()])
        with self.assertRaisesRegex(tryout.Cancelled, 'timestamp'):
            guard.poll()

    def test_nan_rejected(self):
        guard = self.guard([state(torque=float('nan'))], [state()])
        with self.assertRaisesRegex(tryout.Cancelled, 'invalid current'):
            guard.poll()

    def test_waits_are_monitored_no_position_after_surge(self):
        guard = self.guard([state(), state(torque=1.31)], [state()]*2)
        guard.poll()
        controls = [{}, {}]
        with self.assertRaises(tryout.Cancelled):
            tryout.playback([{'t': 10.0, 'left': [0.0]*8}], controls, guard)
        self.assertEqual(controls, [{}, {}])

    def test_off_attempted_for_both_sides_if_one_writer_fails(self):
        class Bad:
            def __setitem__(self, key, value):
                raise OSError('mock write failure')
        good = {}
        with patch('tryout.time.sleep'), patch('builtins.print'):
            tryout.publish_off([Bad(), good])
        self.assertFalse(np.any(good['enable']))

    def test_main_cancellation_disables_both_and_closes_topics(self):
        writes, closed = [], []
        class FakeWriter:
            def __init__(self, name, *args, **kwargs):
                self.name = name
            def __setitem__(self, key, value):
                writes.append((self.name, key, np.array(value).copy()))
            def __exit__(self, *args):
                closed.append(self.name)
                return True
        class FakeReader(Reader):
            def __init__(self, name, **kwargs):
                super().__init__([state()]*10)
            def __exit__(self, *args):
                return True
        class FakeGuard:
            def __init__(self, *args):
                self.samples = [state(), state()]
            def poll(self):
                return True
        fake = SimpleNamespace(Config=lambda name: CFG, Reader=FakeReader,
                               Writer=FakeWriter, Type=lambda name: name)
        recording = SimpleNamespace(read_text=lambda: json.dumps({'frames': [{'t': 0, 'left': [0]*8}]}))
        with patch.dict(sys.modules, bbos=fake), patch.object(sys, 'argv', ['tryout.py']), \
             patch.object(tryout, 'TorqueGuard', FakeGuard), patch.object(tryout, 'RECORDING', recording), \
             patch.object(tryout, 'watch_until'), patch.object(tryout, 'wait_for_enable_feedback'), \
             patch.object(tryout, 'playback', side_effect=tryout.Cancelled('surge')), \
             patch('tryout.time.sleep'), patch('builtins.print'):
            with self.assertRaisesRegex(tryout.Cancelled, 'surge'):
                tryout.main()
        for side in ('left', 'right'):
            topic = f'arm_{side}.torque'
            commands = [v for n, k, v in writes if n == topic and k == 'enable']
            self.assertTrue(any(np.any(v) for v in commands))
            self.assertTrue(all(not np.any(v) for v in commands[-3:]))
            self.assertIn(topic, closed)
            self.assertIn(f'arm_{side}.ctrl', closed)

    def startup_guard(self, resume=(.6, .7), torque=0.0):
        self.now = 100.0
        owner = self
        class PausingReader:
            def __init__(self, resume_at):
                self.resume_at = resume_at
                self.published = 100.0
                self.data = state()
            def ready(self):
                if owner.now < self.resume_at or owner.now-self.published < .015:
                    return False
                self.published = owner.now
                self.data = state(t=owner.now, torque=torque)
                return True
        return tryout.TorqueGuard([PausingReader(100+r) for r in resume], [CFG, CFG],
                                 clock=lambda: owner.now, wall=lambda: owner.now)

    def advance(self, seconds):
        self.now += seconds

    def test_startup_feedback_gap_waits_for_both_streams(self):
        guard = self.startup_guard()
        with patch('tryout.time.sleep', side_effect=self.advance):
            tryout.wait_for_enable_feedback(guard)
        self.assertGreaterEqual(self.now, 100.73)
        self.assertLess(self.now, 101.0)
        self.assertTrue(all(n >= 3 for n in guard.counts))

    def test_startup_never_proceeds_with_one_missing_arm(self):
        guard = self.startup_guard(resume=(.6, 99.0))
        with patch('tryout.time.sleep', side_effect=self.advance):
            with self.assertRaises(tryout.Cancelled):
                tryout.wait_for_enable_feedback(guard)
        self.assertLess(self.now, 102.01)

    def test_startup_surge_still_cancels_on_first_fresh_sample(self):
        guard = self.startup_guard(torque=1.4)
        with patch('tryout.time.sleep', side_effect=self.advance):
            with self.assertRaisesRegex(tryout.Cancelled, '1.30 Nm'):
                tryout.wait_for_enable_feedback(guard)
        self.assertLess(self.now, 100.62)

    def test_watchdog_returns_to_100ms_after_startup(self):
        guard = self.startup_guard()
        with patch('tryout.time.sleep', side_effect=self.advance):
            tryout.wait_for_enable_feedback(guard)
        for reader in guard.readers:
            reader.resume_at = 1000.0
        self.advance(.101)
        with self.assertRaisesRegex(tryout.Cancelled, 'stopped updating'):
            guard.poll()


if __name__ == '__main__':
    unittest.main()
