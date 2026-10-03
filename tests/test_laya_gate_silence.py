"""The gate's own limits, asked of the operator and answered 2026-10-03.

Three of her numbers are load-bearing here:

- a session must not stay stopped more than TEN MINUTES before it reaches her, so the silence on a
  stall is bounded rather than open-ended;
- a swallowed event that mattered costs TEN TIMES an unnecessary interruption, so the thresholds
  are tuned against 10.0/0.5 and not the checkpoint's 3.0/0.5;
- anything touching a client-visible surface must reach her whatever the gate thinks.
"""
import os
import sys
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import laya_gate  # noqa: E402

STALL = "STALL-CC-TEST: no new transcript output for 360s (alive, not producing) - check"


class ClassifyBase(unittest.TestCase):
    def setUp(self):
        laya_gate._stall_since.clear()
        laya_gate._recent.clear()
        laya_gate._history.clear()
        self.patches = [
            mock.patch.object(laya_gate, "_newest_message", return_value=""),
            mock.patch.object(laya_gate, "in_quiet_hours", return_value=False),
        ]
        for p in self.patches:
            p.start()
        self.addCleanup(lambda: [p.stop() for p in self.patches])

    def classify(self, text, expecting=False, session="cc-test"):
        return laya_gate.classify(session, "stall", text, context="", expecting=expecting)


class TestBoundedSilence(ClassifyBase):
    def test_the_first_stall_is_silent(self):
        v = self.classify(STALL)
        self.assertEqual(v["lane"], 2)
        self.assertFalse(v["escalate"])
        self.assertEqual(v["rule"], "quiet session with nothing in flight")

    def test_a_stall_that_persists_past_the_window_escalates(self):
        laya_gate._stall_since["cc-test"] = time.time() - (laya_gate.SILENCE_ALERT_AFTER + 60)
        v = self.classify(STALL)
        self.assertEqual(v["lane"], 1)
        self.assertTrue(v["escalate"], v)
        self.assertIn("still stopped", v["rule"])

    def test_the_window_is_ten_minutes(self):
        self.assertEqual(laya_gate.SILENCE_ALERT_AFTER, 600.0)

    def test_just_under_the_window_is_still_silent(self):
        laya_gate._stall_since["cc-test"] = time.time() - (laya_gate.SILENCE_ALERT_AFTER - 30)
        v = self.classify(STALL)
        self.assertFalse(v["escalate"], v)

    def test_a_persisting_stop_comes_through_quiet_hours(self):
        laya_gate._stall_since["cc-test"] = time.time() - (laya_gate.SILENCE_ALERT_AFTER + 600)
        with mock.patch.object(laya_gate, "in_quiet_hours", return_value=True):
            v = self.classify(STALL)
        self.assertTrue(v["escalate"], v)
        self.assertFalse(v.get("held"))

    def test_producing_output_resets_the_stop_clock(self):
        laya_gate._stall_since["cc-test"] = time.time() - (laya_gate.SILENCE_ALERT_AFTER + 600)
        self.classify("DONE-fw1234", session="cc-test")
        self.assertNotIn("cc-test", laya_gate._stall_since)
        v = self.classify(STALL)
        self.assertFalse(v["escalate"], v)

    def test_a_stop_clock_is_per_session(self):
        laya_gate._stall_since["cc-other"] = time.time() - (laya_gate.SILENCE_ALERT_AFTER + 600)
        v = self.classify(STALL, session="cc-test")
        self.assertFalse(v["escalate"], v)


class TestAlwaysTell(ClassifyBase):
    def test_a_client_visible_surface_always_reaches_her(self):
        for text in ("I am posting in a client channel now",
                     "that is a client-visible change",
                     "this one is client-facing"):
            v = self.classify(text)
            self.assertTrue(v["escalate"], (text, v))
            self.assertEqual(v["rule"], "client-visible surface", text)

    def test_a_client_visible_surface_comes_through_quiet_hours(self):
        with mock.patch.object(laya_gate, "in_quiet_hours", return_value=True):
            v = self.classify("posting in a client channel")
        self.assertTrue(v["escalate"], v)

    def test_money_and_production_blocks_still_reach_her(self):
        for text, why in (("we will spend $200 on this", "money"),
                          ("[Security Weaken] refused", "the session was blocked by the auto-mode classifier"),
                          ("[Production Deploy] blocked", "the session was blocked by the auto-mode classifier")):
            v = self.classify(text)
            self.assertTrue(v["escalate"], (text, v))
            self.assertEqual(v["rule"], why, text)

    def test_a_dead_session_reaches_her(self):
        v = self.classify("SESSION-DEAD-CC-TEST: no tmux session")
        self.assertTrue(v["escalate"], v)

    def test_an_ordinary_event_does_not_match_the_always_tell_rules(self):
        v = self.classify("state changed WORKING -> IDLE")
        self.assertNotIn(v["rule"], ("client-visible surface", "money"))


class TestCostModelIsHers(unittest.TestCase):
    def test_the_scorer_costs_a_swallowed_event_ten_times_a_spare_interruption(self):
        sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "evals"))
        import score_escalation
        self.assertEqual(score_escalation.COST_MISS, 10.0)
        self.assertEqual(score_escalation.COST_SPARE, 0.5)

    def test_the_calibrator_uses_the_same_asymmetry(self):
        sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "evals"))
        import calibrate_laya
        self.assertEqual(calibrate_laya.COST_WRONG_ACT, 10.0)


if __name__ == "__main__":
    unittest.main()
