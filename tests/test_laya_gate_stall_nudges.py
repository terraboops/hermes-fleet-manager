#!/usr/bin/env python3
"""STALL logic gate: escalate only when nudges were sent and silence outlived them.

Run:  python3 -m unittest tests.test_laya_gate_stall_nudges
"""
import os
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))

import laya_gate  # noqa: E402

S = "cc-w-demo-1"
WINDOW = laya_gate.NUDGE_SILENCE_WINDOW
COUNT = laya_gate.NUDGE_ESCALATE_COUNT


def stall(stopped=900):
    return f"STALL-{S.upper()}: no new transcript output for {stopped}s (alive, not producing)"


class StallNudgeGate(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.mkdtemp(prefix="stallgate_")
        for name, val in (("HELD_FILE", os.path.join(tmp, "held.jsonl")),
                          ("DECISIONS_FILE", os.path.join(tmp, "decisions.jsonl"))):
            p = mock.patch.object(laya_gate, name, val)
            p.start()
            self.addCleanup(p.stop)
        p = mock.patch.object(laya_gate, "in_quiet_hours", return_value=False)
        self.quiet = p.start()
        self.addCleanup(p.stop)
        laya_gate._stall_alerted.clear()

    def nudges(self, count, silent_for):
        now = time.time()
        return {"episode_id": f"{S}@1", "nudge_count": count,
                "first_nudge_at": now - silent_for - 600 if count else None,
                "last_nudge_at": now - silent_for if count else None}

    def run_gate(self, state, nudges, stopped=900):
        return laya_gate.classify(S, "", stall(stopped), state=state, nudges=nudges)

    def test_defaults(self):
        self.assertEqual(COUNT, 2)
        self.assertEqual(WINDOW, 1800)

    def test_no_nudges_sent(self):
        v = self.run_gate("IDLE", self.nudges(0, 0))
        self.assertFalse(v["escalate"])
        self.assertEqual(v["nudge_count"], 0)

    def test_one_nudge(self):
        self.assertFalse(self.run_gate("IDLE", self.nudges(1, WINDOW + 600))["escalate"])

    def test_two_nudges_short_silence(self):
        v = self.run_gate("IDLE", self.nudges(2, WINDOW - 60))
        self.assertFalse(v["escalate"])
        self.assertEqual(v["nudge_count"], 2)

    def test_two_nudges_long_silence_escalates(self):
        n = self.nudges(2, WINDOW + 5)
        v = self.run_gate("IDLE", n)
        self.assertTrue(v["escalate"])
        self.assertEqual(v["nudge_count"], 2)
        self.assertEqual(v["last_nudge_at"], n["last_nudge_at"])

    def test_boundary_is_inclusive(self):
        fixed = time.time()
        n = {"episode_id": "e", "nudge_count": COUNT, "first_nudge_at": fixed - 4000,
             "last_nudge_at": fixed - WINDOW}
        with mock.patch.object(laya_gate.time, "time", return_value=fixed):
            self.assertTrue(self.run_gate("IDLE", n)["escalate"])

    def test_thresholds_are_configurable(self):
        with mock.patch.object(laya_gate, "NUDGE_ESCALATE_COUNT", 3):
            self.assertFalse(self.run_gate("IDLE", self.nudges(2, WINDOW + 5))["escalate"])
            self.assertTrue(self.run_gate("IDLE", self.nudges(3, WINDOW + 5))["escalate"])
        laya_gate._stall_alerted.clear()
        with mock.patch.object(laya_gate, "NUDGE_SILENCE_WINDOW", 60):
            self.assertTrue(self.run_gate("IDLE", self.nudges(2, 61))["escalate"])

    def test_busy_states_ignore_stale_nudges(self):
        for state in ("WORKING", "QUEUED"):
            laya_gate._stall_alerted.clear()
            v = self.run_gate(state, self.nudges(5, WINDOW * 4))
            self.assertFalse(v["escalate"], state)

    def test_quiet_hours_hold(self):
        self.quiet.return_value = True
        v = self.run_gate("IDLE", self.nudges(2, WINDOW + 5))
        self.assertFalse(v["escalate"])
        self.assertTrue(v.get("held"))
        self.assertTrue(os.path.exists(laya_gate.HELD_FILE))

    def test_absent_state_is_legacy(self):
        self.assertFalse(self.run_gate("IDLE", None, stopped=900)["escalate"])
        v = self.run_gate("IDLE", None, stopped=int(laya_gate.IDLE_WEDGE_AFTER) + 60)
        self.assertTrue(v["escalate"])
        self.assertIsNone(v["nudge_count"])

    def test_legacy_pair_still_accepted(self):
        self.assertTrue(self.run_gate("IDLE", [2, WINDOW + 1])["escalate"])
        self.assertFalse(self.run_gate("IDLE", [2, 10])["escalate"])


class DecisionRow(unittest.TestCase):
    def test_row_carries_nudge_fields(self):
        import fleet_watch
        rows = []
        n = {"episode_id": "e", "nudge_count": 2, "first_nudge_at": 1, "last_nudge_at": 5}

        class R:
            def __enter__(s): return s
            def __exit__(s, *a): return False
            def read(s): return b'{"escalate": false}'
        with mock.patch.object(fleet_watch.urllib.request, "urlopen", return_value=R()), \
             mock.patch.object(fleet_watch, "_append_decision", side_effect=rows.append):
            fleet_watch.gate_verdict(S, "", "x", state="IDLE", nudges=n)
            fleet_watch.gate_verdict(S, "", "x", state="WORKING", nudges=None)
        self.assertEqual((rows[0]["state"], rows[0]["nudge_count"], rows[0]["last_nudge_at"]),
                         ("IDLE", 2, 5))
        self.assertEqual((rows[1]["nudge_count"], rows[1]["last_nudge_at"]), (None, None))


if __name__ == "__main__":
    unittest.main()
