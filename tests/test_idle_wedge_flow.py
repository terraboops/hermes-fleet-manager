#!/usr/bin/env python3
"""End to end: one IDLE stop, from daemon nudges to gate escalation, on an injectable clock.

The daemon half (fleet_watch._maybe_nudge, gate_verdict) and the gate half (laya_gate.classify)
run for real; only the clock, the tmux dispatch, and the HTTP hop (replaced by a JSON round
trip into classify) are faked.

Run:  python3 -m unittest tests.test_idle_wedge_flow
"""
import io
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))

import fleet_watch  # noqa: E402
import laya_gate  # noqa: E402

S = "cc-w-demo-1"
T0 = 1_800_000_000          # the moment the transcript stopped growing
FIRST_STALL = 360           # the daemon's first STALL event
INTERVAL = fleet_watch.NUDGE_INTERVAL
WINDOW = laya_gate.NUDGE_SILENCE_WINDOW


class Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class Flow(unittest.TestCase):
    def setUp(self):
        self.now = T0
        self.quiet = False
        self.rows, self.sent = [], []
        tmp = tempfile.mkdtemp(prefix="wedgeflow_")
        laya_gate._stall_alerted.clear()
        fleet_watch._nudges.clear()

        def fake_urlopen(req, timeout=None):
            body = json.loads(req.data)                      # the real JSON round trip
            v = laya_gate.classify(body["session"], body["kind"], body["text"],
                                   context=body["context"], expecting=body["expecting"],
                                   state=body["state"], nudges=body["nudges"])
            return Resp(json.dumps(v).encode())

        for p in (
            mock.patch("time.time", side_effect=lambda: self.now),
            mock.patch.object(fleet_watch, "_dispatch_nudge",
                              side_effect=lambda sn, t: self.sent.append(self.now) or True),
            mock.patch.object(fleet_watch, "_append_decision", side_effect=self.rows.append),
            mock.patch.object(fleet_watch, "_in_quiet_hours", side_effect=lambda: self.quiet),
            mock.patch.object(laya_gate, "in_quiet_hours", side_effect=lambda *a, **k: self.quiet),
            mock.patch.object(laya_gate, "HELD_FILE", os.path.join(tmp, "held.jsonl")),
            mock.patch.object(laya_gate, "DECISIONS_FILE", os.path.join(tmp, "dec.jsonl")),
            mock.patch.object(fleet_watch.urllib.request, "urlopen", side_effect=fake_urlopen),
        ):
            p.start()
            self.addCleanup(p.stop)

    def tick(self, at, state="IDLE"):
        """One daemon pass at T0+at: the STALL event, nudge if due, gate verdict."""
        self.now = T0 + at
        stopped = at
        match = f"STALL-{S.upper()}: no new transcript output for {stopped}s (alive, not producing)"
        nudges = fleet_watch._maybe_nudge(S, stopped, state=state, now=self.now)
        return fleet_watch.gate_verdict(S, "", match, state=state, nudges=nudges)

    def nudge_rows(self):
        return [r for r in self.rows if r.get("kind") == "nudge"]

    def gate_rows(self):
        return [r for r in self.rows if r.get("kind") != "nudge"]

    def test_full_episode_sequence(self):
        with mock.patch.object(fleet_watch, "NUDGE_MAX", 3):
            # Before the first stall interval: nothing to say, nothing sent.
            v = self.tick(FIRST_STALL)
            self.assertEqual(len(self.nudge_rows()), 1)      # nudge 1 on the first stall
            self.assertFalse(v["escalate"])
            v = self.tick(FIRST_STALL + INTERVAL)            # nudge 2
            self.assertEqual(len(self.nudge_rows()), 2)
            self.assertFalse(v["escalate"])
            # Two nudges are not enough evidence while the cap allows a third.
            v = self.tick(FIRST_STALL + 2 * INTERVAL - 1)
            self.assertEqual(len(self.nudge_rows()), 2)
            self.assertFalse(v["escalate"])
            v = self.tick(FIRST_STALL + 2 * INTERVAL)        # nudge 3: the cap
            self.assertEqual(len(self.nudge_rows()), 3)
            last = T0 + FIRST_STALL + 2 * INTERVAL
            # Silence after the last nudge, not yet long enough: no wedge.
            v = self.tick(FIRST_STALL + 2 * INTERVAL + int(WINDOW) - 60)
            self.assertFalse(v["escalate"])
            self.assertEqual(len(self.nudge_rows()), 3)      # the cap held
            # The silence window runs out: escalate.
            v = self.tick(FIRST_STALL + 2 * INTERVAL + int(WINDOW))
            self.assertTrue(v["escalate"], v)
            self.assertEqual(v["lane"], 1)
            self.assertEqual(v["nudges"], 3)
            # Same stop, later: reported once.
            v = self.tick(FIRST_STALL + 2 * INTERVAL + int(WINDOW) + 120)
            self.assertFalse(v["escalate"])

        # The decision-row sequence: nudge, gate, nudge, gate, ... the last gate row escalates.
        seq = [(r["kind"], (r["verdict"].get("nudge") if r["kind"] == "nudge" else r["verdict"]["escalate"]))
               for r in self.rows]
        self.assertEqual(seq, [
            ("nudge", 1), ("", False),
            ("nudge", 2), ("", False),
            ("", False),
            ("nudge", 3), ("", False),
            ("", False),
            ("", True),
            ("", False),
        ])
        gr = self.gate_rows()
        self.assertEqual([r["nudge_count"] for r in gr], [1, 2, 2, 3, 3, 3, 3])
        self.assertEqual(gr[-2]["last_nudge_at"], last)
        self.assertTrue(all(r["state"] == "IDLE" for r in self.rows))
        # All nudges belong to one episode.
        self.assertEqual(len({r["verdict"]["episode_id"] for r in self.nudge_rows()}), 1)

    def test_resume_working_mid_episode_resets(self):
        with mock.patch.object(fleet_watch, "NUDGE_MAX", 3):
            self.tick(FIRST_STALL)
            self.assertEqual(len(self.nudge_rows()), 1)
            # The session starts working: no nudge, no escalation, episode gone.
            n = len(self.sent)
            v = self.tick(FIRST_STALL + INTERVAL, state="WORKING")
            self.assertEqual(len(self.sent), n)
            self.assertFalse(v["escalate"])
            self.assertNotIn(S, fleet_watch._nudges)
            # A later stop is a new episode with a clean count.
            global T0
            self.now = T0 + 5000
            ep_old = self.nudge_rows()[0]["verdict"]["episode_id"]
            st = fleet_watch._maybe_nudge(S, 400, state="IDLE", now=self.now)
            self.assertEqual(st["nudge_count"], 1)
            self.assertNotEqual(st["episode_id"], ep_old)

    def test_resume_before_window_never_escalates(self):
        with mock.patch.object(fleet_watch, "NUDGE_MAX", 3):
            for at in (FIRST_STALL, FIRST_STALL + INTERVAL, FIRST_STALL + 2 * INTERVAL):
                self.tick(at)
            v = self.tick(FIRST_STALL + 2 * INTERVAL + 600, state="WORKING")
            self.assertFalse(v["escalate"])
            self.assertEqual(len(self.nudge_rows()), 3)

    def test_cap_is_respected_over_a_long_stop(self):
        with mock.patch.object(fleet_watch, "NUDGE_MAX", 3):
            for at in range(FIRST_STALL, FIRST_STALL + 6 * 3600, 60):
                self.tick(at)
        self.assertEqual(len(self.nudge_rows()), 3)
        self.assertEqual(len(self.sent), 3)
        self.assertEqual(sum(1 for r in self.gate_rows() if r["verdict"].get("escalate")), 1)

    def test_quiet_hours_hold_nudges_and_escalation(self):
        with mock.patch.object(fleet_watch, "NUDGE_MAX", 3):
            self.quiet = True
            for at in (FIRST_STALL, FIRST_STALL + INTERVAL, FIRST_STALL + 4000):
                v = self.tick(at)
                self.assertFalse(v["escalate"])
            self.assertEqual(self.sent, [])                  # no nudge in quiet hours
            self.assertEqual(self.nudge_rows(), [])
            # Morning: nudges resume (wedge fallback is held while quiet, reported after).
            self.quiet = False
            self.tick(FIRST_STALL + 4100)
            self.assertEqual(len(self.nudge_rows()), 1)

    def test_quiet_hours_hold_escalation_after_nudges(self):
        with mock.patch.object(fleet_watch, "NUDGE_MAX", 2):
            self.tick(FIRST_STALL)
            self.tick(FIRST_STALL + INTERVAL)
            self.quiet = True
            v = self.tick(FIRST_STALL + INTERVAL + int(WINDOW) + 10)
            self.assertFalse(v["escalate"])
            self.assertTrue(v.get("held"))
            self.quiet = False
            v = self.tick(FIRST_STALL + INTERVAL + int(WINDOW) + 20)
            self.assertTrue(v["escalate"])

    def test_nudge_that_never_lands_cannot_swallow_a_stall(self):
        with mock.patch.object(fleet_watch, "_dispatch_nudge", return_value=False):
            v = self.tick(FIRST_STALL)
            self.assertFalse(v["escalate"])
            v = self.tick(int(laya_gate.IDLE_WEDGE_AFTER) + 5)
        self.assertTrue(v["escalate"])
        self.assertEqual(v["nudges"], 0)

    def test_wedge_fallback_is_derived_not_an_hour(self):
        self.assertNotEqual(laya_gate.IDLE_WEDGE_AFTER, 3600)
        self.assertEqual(laya_gate.IDLE_WEDGE_AFTER,
                         360 + laya_gate.NUDGE_ESCALATE_COUNT * 600 + laya_gate.NUDGE_SILENCE_WINDOW)

    def test_cron_agents_do_not_nudge_by_default(self):
        sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'scripts'))
        import fleet_overwatch
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("FLEET_OVERWATCH_NUDGES", None)
            self.assertFalse(fleet_overwatch._cron_nudges_enabled())
        self.assertIn("do NOT nudge", fleet_overwatch.IDLE_RULE_DAEMON)
        tpl = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..',
                                'templates', 'overwatch-prompt.md')).read()
        self.assertIn("{{IDLE_RULE}}", tpl)
        self.assertNotIn("IDLE -> NUDGE", tpl)


if __name__ == "__main__":
    unittest.main()
