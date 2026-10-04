#!/usr/bin/env python3
"""The daemon's side of the escalation gate: the stall it sends, and keeping the gate alive.

Two field failures (2026-10-03 review):

- A stop was reported to the gate ONCE, at 360s, which is under the operator's ten-minute bound,
  so the gate swallowed it and no second report ever came: 210 stops swallowed, a session that
  stayed stopped was never reported at all. A stop is now repeated until the transcript grows.
- The self-heal killed a gate with a broken model and then hit its own throttle before starting
  the replacement; it only checked at startup, before the lazily loaded model had failed; and it
  SIGTERMed every process connected to the port, not just the listener.

Nothing here touches a real process: subprocess, os.kill and the health probe are all mocked.

Run:  python3 -m unittest discover -s tests -t .
"""

import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))

import fleet_watch  # noqa: E402

W, R = fleet_watch.STALL_WINDOW, fleet_watch.STALL_REPEAT


class StallStep(unittest.TestCase):
    def replay(self, seconds, alive=True, every=30):
        """Scan every `every` seconds for `seconds` with no growth; return the quiet times sent."""
        state, _ = fleet_watch.stall_step({}, 0, grew=True, alive=alive)
        sent = []
        for now in range(every, seconds + 1, every):
            state, q = fleet_watch.stall_step(state, now, grew=False, alive=alive)
            if q is not None:
                sent.append(q)
        return sent, state

    def test_a_stop_is_repeated_until_the_transcript_grows(self):
        sent, _ = self.replay(W + 3 * R)
        self.assertEqual(sent, [W, W + R, W + 2 * R, W + 3 * R])

    def test_each_report_carries_the_real_quiet_time(self):
        # The gate's ten-minute bound reads this number; it must not be the fixed window.
        sent, _ = self.replay(W + R)
        self.assertEqual(sent, [W, W + R])

    def test_growth_is_the_acknowledgement(self):
        _, state = self.replay(W + R)
        state, q = fleet_watch.stall_step(state, W + R + 30, grew=True, alive=True)
        self.assertIsNone(q)
        self.assertFalse(state["flagged"])
        state, q = fleet_watch.stall_step(state, W + R + 60, grew=False, alive=True)
        self.assertIsNone(q, "a fresh stop must wait the full window again")

    def test_a_dead_session_is_not_a_stall(self):
        sent, _ = self.replay(W + 2 * R, alive=False)
        self.assertEqual(sent, [])

    def test_upgrading_does_not_burst_every_long_stopped_session(self):
        old = {"since": 0, "flagged": True}                 # written by the once-per-stop daemon
        state, q = fleet_watch.stall_step(old, 10_000, grew=False, alive=True)
        self.assertIsNone(q)
        state, q = fleet_watch.stall_step(state, 10_000 + R, grew=False, alive=True)
        self.assertEqual(q, 10_000 + R)


class GateBase(unittest.TestCase):
    def setUp(self):
        fleet_watch._GATE.update(checked_at=0.0, bad_since=None, loading_since=None,
                                 alerted=False)
        fleet_watch._GATE_SPAWNED_AT[0] = 0.0
        self.run = mock.patch.object(fleet_watch.subprocess, "run").start()
        self.popen = mock.patch.object(fleet_watch.subprocess, "Popen").start()
        self.kill = mock.patch.object(fleet_watch.os, "kill").start()
        mock.patch.object(fleet_watch.time, "sleep").start()
        self.addCleanup(mock.patch.stopall)

    def health(self, h):
        return mock.patch.object(fleet_watch, "_gate_health", return_value=h)

    def launchd(self, owns):
        return mock.patch.object(fleet_watch, "_launchd_owns_gate", return_value=owns)


OK = {"ok": True, "model_loaded": True, "model_error": None}
BROKEN = {"ok": True, "model_loaded": False, "model_error": "ModuleNotFoundError: laya_mlx"}
LOADING = {"ok": True, "model_loaded": False, "model_loading": True, "model_error": None}


class EnsureGate(GateBase):
    def test_a_healthy_gate_is_left_alone(self):
        with self.health(OK):
            self.assertEqual(fleet_watch.ensure_gate(), "ok")
        self.run.assert_not_called()
        self.popen.assert_not_called()

    def test_a_broken_gate_under_launchd_is_restarted_by_launchd(self):
        self.run.return_value = mock.Mock(returncode=0, stdout="", stderr="")
        with self.health(BROKEN), self.launchd(True):
            self.assertEqual(fleet_watch.ensure_gate(), "bad")
        cmd = self.run.call_args[0][0]
        self.assertEqual(cmd[:3], ["launchctl", "kickstart", "-k"])
        self.popen.assert_not_called()          # one owner: never a second copy
        self.kill.assert_not_called()

    def test_a_broken_gate_without_launchd_is_replaced_in_the_same_call(self):
        # The bug: the throttle stamp was set before the kill, so the spawn guard right after it
        # always returned, and the replacement was never started.
        self.run.return_value = mock.Mock(returncode=0, stdout="4242\n", stderr="")
        with self.health(BROKEN), self.launchd(False):
            fleet_watch.ensure_gate()
        self.kill.assert_called_once_with(4242, 15)
        self.popen.assert_called_once()

    def test_only_the_listener_is_stopped(self):
        self.run.return_value = mock.Mock(returncode=0, stdout="", stderr="")
        with self.health(BROKEN), self.launchd(False):
            fleet_watch.ensure_gate()
        lsof = self.run.call_args[0][0]
        self.assertIn("-sTCP:LISTEN", lsof)

    def test_restarts_are_throttled(self):
        self.run.return_value = mock.Mock(returncode=0, stdout="", stderr="")
        with self.health(BROKEN), self.launchd(True):
            fleet_watch.ensure_gate()
            fleet_watch.ensure_gate()
        kicks = [c for c in self.run.call_args_list if c[0][0][:2] == ["launchctl", "kickstart"]]
        self.assertEqual(len(kicks), 1)

    def test_a_loading_gate_gets_its_grace_period(self):
        with self.health(LOADING):
            self.assertEqual(fleet_watch.ensure_gate(), "loading")
        self.run.assert_not_called()

    def test_a_gate_stuck_loading_is_restarted(self):
        self.run.return_value = mock.Mock(returncode=0, stdout="", stderr="")
        with self.health(LOADING), self.launchd(True):
            fleet_watch.ensure_gate()
            fleet_watch._GATE["loading_since"] -= fleet_watch.GATE_LOAD_GRACE + 1
            self.assertEqual(fleet_watch.ensure_gate(), "bad")
        self.assertTrue(any(c[0][0][:2] == ["launchctl", "kickstart"]
                            for c in self.run.call_args_list))

    def test_a_silent_gate_is_restarted(self):
        self.run.return_value = mock.Mock(returncode=0, stdout="", stderr="")
        with self.health(None), self.launchd(True):
            self.assertEqual(fleet_watch.ensure_gate(), "bad")


class GateWatch(GateBase):
    def tick(self, now, h):
        with self.health(h), self.launchd(True):
            return fleet_watch.gate_watch(now=now)

    def test_polls_on_an_interval_not_every_scan(self):
        with mock.patch.object(fleet_watch, "ensure_gate", return_value="ok") as eg:
            fleet_watch.gate_watch(now=1000)
            fleet_watch.gate_watch(now=1001)
            fleet_watch.gate_watch(now=1000 + fleet_watch.GATE_CHECK_EVERY)
        self.assertEqual(eg.call_count, 2)

    def test_an_outage_restart_cannot_fix_reaches_her_once(self):
        self.run.return_value = mock.Mock(returncode=0, stdout="", stderr="")
        step = fleet_watch.GATE_CHECK_EVERY
        events = []
        for i in range(int(fleet_watch.GATE_ALERT_AFTER / step) + 5):
            events += self.tick(1000 + i * step, BROKEN)
        self.assertEqual(len(events), 1, events)
        self.assertTrue(events[0]["urgent"])
        self.assertTrue(events[0]["match"].startswith("GATE-DOWN"))

    def test_a_short_blip_is_not_reported(self):
        self.run.return_value = mock.Mock(returncode=0, stdout="", stderr="")
        step = fleet_watch.GATE_CHECK_EVERY
        events = self.tick(1000, BROKEN) + self.tick(1000 + step, OK)
        self.assertEqual(events, [])

    def test_recovery_after_an_alert_is_reported(self):
        self.run.return_value = mock.Mock(returncode=0, stdout="", stderr="")
        step = fleet_watch.GATE_CHECK_EVERY
        t = 1000
        for _ in range(int(fleet_watch.GATE_ALERT_AFTER / step) + 2):
            self.tick(t, BROKEN)
            t += step
        events = self.tick(t, OK)
        self.assertEqual([e["match"][:14] for e in events], ["GATE-RECOVERED"])
        self.assertFalse(events[0]["urgent"])


if __name__ == "__main__":
    unittest.main()
