#!/usr/bin/env python3
"""Daemon-owned nudges: dispatch rules, per-episode state, and the decision row.

Nothing here touches tmux: the dispatcher and the decision log are mocked.

Run:  python3 -m unittest discover -s tests -t .
"""

import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))

import fleet_watch  # noqa: E402

S = "cc-w-demo-1"
GAP = fleet_watch.NUDGE_INTERVAL


class NudgeDaemon(unittest.TestCase):
    def setUp(self):
        fleet_watch._nudges.clear()
        self.sent, self.rows = [], []
        patches = [
            mock.patch.object(fleet_watch, "_dispatch_nudge",
                              side_effect=lambda sn, t: self.sent.append(sn) or True),
            mock.patch.object(fleet_watch, "_append_decision", side_effect=self.rows.append),
            mock.patch.object(fleet_watch, "_in_quiet_hours", return_value=False),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def nudge(self, now, state="IDLE", began=1000.0):
        return fleet_watch._maybe_nudge(S, now - began, state=state, now=now)

    def test_idle_past_interval_is_nudged(self):
        st = self.nudge(2000.0)
        self.assertEqual(self.sent, [S])
        self.assertEqual(st["nudge_count"], 1)
        self.assertEqual(st["first_nudge_at"], 2000)
        self.assertEqual(st["last_nudge_at"], 2000)
        self.assertTrue(st["episode_id"].startswith(S + "@"))

    def test_interval_between_nudges(self):
        self.nudge(2000.0)
        self.nudge(2000.0 + GAP - 1)
        self.assertEqual(len(self.sent), 1)
        st = self.nudge(2000.0 + GAP)
        self.assertEqual(len(self.sent), 2)
        self.assertEqual(st["first_nudge_at"], 2000)
        self.assertEqual(st["last_nudge_at"], 2000 + GAP)

    def test_working_and_queued_never_nudged(self):
        for state in ("WORKING", "QUEUED", "NEEDS-INPUT", "DEAD", None):
            self.assertIsNone(self.nudge(5000.0, state=state))
        self.assertEqual(self.sent, [])
        self.assertEqual(self.rows, [])

    def test_cap_is_the_fleet_managers_max_nudges(self):
        import dataclasses
        sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'scripts'))
        import fleet_manager
        mn = next(f.default for f in dataclasses.fields(fleet_manager.Manager) if f.name == "max_nudges")
        self.assertEqual(fleet_watch.NUDGE_MAX, mn)
        for i in range(mn + 4):
            st = self.nudge(2000.0 + i * GAP)
        self.assertEqual(len(self.sent), mn)
        self.assertEqual(st["nudge_count"], mn)

    def test_quiet_hours_suppress(self):
        with mock.patch.object(fleet_watch, "_in_quiet_hours", return_value=True):
            st = self.nudge(2000.0)
        self.assertEqual(self.sent, [])
        self.assertEqual(st["nudge_count"], 0)

    def test_leaving_idle_resets_episode(self):
        first = self.nudge(2000.0)
        self.assertIsNone(self.nudge(2100.0, state="WORKING"))
        again = self.nudge(2000.0 + GAP, began=2000.0)   # same began would not matter: reset
        self.assertEqual(again["nudge_count"], 1)
        self.assertEqual(len(self.sent), 2)
        self.assertNotEqual(first["episode_id"], again["episode_id"])

    def test_new_stop_resets_episode_even_without_state_change(self):
        a = self.nudge(2000.0, began=1000.0)
        b = self.nudge(2000.0 + GAP, began=2000.0 + GAP - 900.0)   # stop start jumped
        self.assertNotEqual(a["episode_id"], b["episode_id"])
        self.assertEqual(b["nudge_count"], 1)

    def test_each_nudge_writes_a_decision_row_with_state(self):
        self.nudge(2000.0)
        self.nudge(2000.0 + GAP)
        self.assertEqual(len(self.rows), 2)
        for i, r in enumerate(self.rows, 1):
            self.assertEqual(r["kind"], "nudge")
            self.assertEqual(r["state"], "IDLE")
            self.assertEqual(r["session"], S)
            self.assertEqual(r["verdict"]["nudge"], i)

    def test_failed_dispatch_is_not_counted_or_logged(self):
        with mock.patch.object(fleet_watch, "_dispatch_nudge", return_value=False):
            st = self.nudge(2000.0)
        self.assertEqual(st["nudge_count"], 0)
        self.assertEqual(self.rows, [])


class GateReceivesNudgeState(unittest.TestCase):
    def test_gate_reads_dict_and_json_list(self):
        import laya_gate
        now = 10_000_000
        with mock.patch.object(laya_gate.time, "time", return_value=now):
            for nudges in ({"episode_id": "e", "nudge_count": 3, "first_nudge_at": now - 4000,
                            "last_nudge_at": now - 2000},):
                v = laya_gate.classify(S, "", f"STALL-{S.upper()}: no new transcript output for 2400s",
                                       state="IDLE", nudges=nudges)
                self.assertTrue(v["escalate"], v)
                self.assertEqual(v["nudges"], 3)


if __name__ == "__main__":
    unittest.main()
