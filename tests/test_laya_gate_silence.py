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


def stall(seconds, session="CC-TEST"):
    """The daemon's stall text, which carries how long the session has actually been quiet."""
    return (f"STALL-{session}: no new transcript output for {int(seconds)}s "
            f"(alive, not producing) - check")


class ClassifyBase(unittest.TestCase):
    def setUp(self):
        laya_gate._stall_alerted.clear()
        laya_gate._recent.clear()
        laya_gate._history.clear()
        self.clock = [1_800_000_000.0]
        self.patches = [
            mock.patch.object(laya_gate, "_newest_message", return_value=""),
            mock.patch.object(laya_gate, "in_quiet_hours", return_value=False),
            mock.patch.object(laya_gate, "hold"),
            mock.patch.object(laya_gate.time, "time", side_effect=lambda: self.clock[0]),
        ]
        for p in self.patches:
            p.start()
        self.addCleanup(lambda: [p.stop() for p in self.patches])

    def classify(self, text, expecting=False, session="cc-test"):
        return laya_gate.classify(session, "stall", text, context="", expecting=expecting)

    def replay_stop(self, until, every=300, first=360, session="cc-test"):
        """What the daemon sends for one stop: a stall at 360s, then one every 300s, each carrying
        the real quiet time, with the wall clock advancing to match. Returns the verdicts."""
        start = self.clock[0]
        out, quiet = [], first
        while quiet <= until:
            self.clock[0] = start + quiet
            out.append(self.classify(stall(quiet, session.upper()), session=session))
            quiet += every
        return out


class TestBoundedSilence(ClassifyBase):
    def test_the_first_stall_is_silent(self):
        v = self.classify(STALL)
        self.assertEqual(v["lane"], 2)
        self.assertFalse(v["escalate"])
        self.assertEqual(v["rule"], "quiet session with nothing in flight")

    def test_the_window_is_ten_minutes(self):
        self.assertEqual(laya_gate.SILENCE_ALERT_AFTER, 600.0)

    def test_a_session_that_stays_stopped_is_reported_once_it_passes_the_window(self):
        # The daemon's real sequence. Before, it sent one stall per stop and the gate swallowed
        # it, so this session was never reported however long it stayed stopped.
        vs = self.replay_stop(until=660)
        self.assertEqual([v["escalate"] for v in vs], [False, True])
        self.assertEqual(vs[1]["lane"], 1)
        self.assertIn("still stopped 11 min", vs[1]["rule"])

    def test_just_under_the_window_is_still_silent(self):
        v = self.classify(stall(laya_gate.SILENCE_ALERT_AFTER - 30))
        self.assertFalse(v["escalate"], v)

    def test_an_unacknowledged_stop_is_reminded_with_backoff_not_every_repeat(self):
        vs = self.replay_stop(until=360 + 300 * 40)          # about 3h20m stopped
        told = [v["stopped_s"] // 60 for v in vs if v["escalate"]]
        # Told at 11 min, then reminders 30, 60 and 120 min apart.
        self.assertEqual(told, [11, 41, 101, 221][:len(told)])
        self.assertEqual(len(told), 3, told)
        self.assertTrue(all(v["rule"].startswith(("quiet session", "already told", "still stopped"))
                            for v in vs))

    def test_the_first_report_comes_through_quiet_hours_but_reminders_wait(self):
        with mock.patch.object(laya_gate, "in_quiet_hours", return_value=True):
            vs = self.replay_stop(until=360 + 300 * 7)       # 41 min: one report, one reminder due
        escalated = [v for v in vs if v["escalate"]]
        held = [v for v in vs if v.get("held")]
        self.assertEqual(len(escalated), 1)
        self.assertEqual(len(held), 1)
        laya_gate.hold.assert_called_once()

    def test_a_new_stop_after_resuming_starts_fresh(self):
        # The field failure: a stop was stamped, the session resumed and worked with no event the
        # gate saw, stopped again, and its first stall read "still stopped 30 min" (2026-10-03).
        self.replay_stop(until=660)                           # first stop, reported
        self.clock[0] += 3 * 3600                             # resumed and worked for hours
        v = self.classify(stall(360))                         # the new stop's first stall
        self.assertFalse(v["escalate"], v)
        self.assertEqual(v["stopped_s"], 360)

    def test_a_long_new_stop_is_reported_as_new_not_as_a_reminder(self):
        self.replay_stop(until=660)
        self.clock[0] += 3 * 3600
        vs = self.replay_stop(until=660)
        self.assertTrue(vs[-1]["escalate"])
        self.assertNotIn("reminder", vs[-1]["rule"])

    def test_stops_are_per_session(self):
        self.replay_stop(until=660, session="cc-other")
        v = self.classify(stall(360))
        self.assertFalse(v["escalate"], v)

    def test_a_stall_with_no_duration_in_it_stays_silent(self):
        v = self.classify("STALL-CC-TEST: alive, not producing")
        self.assertFalse(v["escalate"], v)

    def test_work_in_flight_is_not_this_rule(self):
        v = self.classify(stall(5000), expecting=True)
        self.assertNotIn("still stopped", v.get("rule") or "")


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
