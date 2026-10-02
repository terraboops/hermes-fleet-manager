#!/usr/bin/env python3
"""Tests for the parked-on-the-operator rule.

The failure this exists to stop: a session that STOPPED while its own last word was that it waits
on a person reads, in the event text, exactly like a session that finished. Every rule the gate had
answered from the event text alone, so the park was held, and the operator learned nothing while
the session sat. Measured 2026-10-01: 5 distinct episodes (58 raw stall and expiry events).

Run:  python3 -m unittest discover -s tests -t .
"""

import os
import sys
import tempfile
import time
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

import laya_gate  # noqa: E402

STALL = "STALL-CC-W-WOLFGANG-1C2B: no new transcript output for 600s (alive, not producing) — check"
SENTINEL = ("SENTINEL-MISSED-CC-W-WOLFGANG-1C2B: expected DONE-ow-1 by 1790893883 — check in on "
            "this session (re-estimate ETA + re-arm the watch)")

PARKED_MSGS = [
    "both items now wait only on a person's approval past their agents' flags",
    "J9 hasn't moved; it's still parked with you",
    "the same line is in your résumé, which I didn't touch because that decision is still parked "
    "with you",
    "I cannot proceed without your approval on the schema change",
    "the migration is awaiting your decision before it runs",
    "one human-only step left: the approval on the deploy",
]

MACHINE_MSGS = [
    "the deploy is still running; I'll read the result when it lands",
    "waiting on CI to finish the test job before I push",
    "the two-client walk is still running in the background",
    "I finished the sweep; nothing is outstanding",
    "DONE-ow-1 — work complete, nothing outstanding and nothing waiting on a person",
    "no new transcript output while the migration runs",
]


class OperatorBlocked(unittest.TestCase):
    def test_real_park_phrasings_are_recognised(self):
        for msg in PARKED_MSGS:
            with self.subTest(msg=msg[:40]):
                self.assertIsNotNone(laya_gate.operator_blocked(msg))

    def test_waiting_on_a_machine_is_not_a_park(self):
        for msg in MACHINE_MSGS:
            with self.subTest(msg=msg[:40]):
                self.assertIsNone(laya_gate.operator_blocked(msg))

    def test_a_negated_dependency_is_not_a_park(self):
        # The first false positive this rule produced: the regex matched the OPPOSITE statement.
        self.assertIsNone(laya_gate.operator_blocked("nothing waiting on a person"))
        self.assertIsNone(laya_gate.operator_blocked("the session is no longer waiting on you"))

    def test_an_earlier_negative_in_the_same_clause_does_not_hide_a_park(self):
        # "I didn't touch it because that decision is still parked with you" IS a park. A wide
        # negation window suppressed it and cost one row.
        self.assertIsNotNone(laya_gate.operator_blocked(
            "which I didn't touch because that decision is still parked with you"))


class IsStop(unittest.TestCase):
    def test_a_suffixed_family_still_counts_as_a_stop(self):
        # The daemon's family carries its own suffix, so equality against "SENTINEL" never fired.
        self.assertTrue(laya_gate._is_stop("SENTINEL-MISSED"))
        self.assertTrue(laya_gate._is_stop("STALL"))
        self.assertTrue(laya_gate._is_stop("WATCH-EXPIRED"))

    def test_a_non_stop_family_does_not(self):
        self.assertFalse(laya_gate._is_stop("ACK-MISSED"))
        self.assertFalse(laya_gate._is_stop("VERSION-BEHIND"))


class ClassifyParked(unittest.TestCase):
    def setUp(self):
        laya_gate.PARKED_RULE = True
        laya_gate._recent.clear()
        self._real_quiet = laya_gate.in_quiet_hours
        laya_gate.in_quiet_hours = lambda now=None: False
        self._real_newest = laya_gate._newest_message
        # Redirect the held log. A test that exercises the quiet-hours path must not write to the
        # production held file: the first version of this file did, and the morning digest then
        # reported a phantom held event (found 2026-10-01 when a held record appeared at the exact
        # minute the suite ran).
        self._real_held = laya_gate.HELD_FILE
        self._tmp_held = tempfile.NamedTemporaryFile(delete=False, suffix=".held.jsonl")
        self._tmp_held.close()
        laya_gate.HELD_FILE = self._tmp_held.name

    def tearDown(self):
        laya_gate.in_quiet_hours = self._real_quiet
        laya_gate._newest_message = self._real_newest
        laya_gate.HELD_FILE = self._real_held
        try:
            os.unlink(self._tmp_held.name)
        except OSError:
            pass
        laya_gate._recent.clear()

    def _with_message(self, msg):
        laya_gate._newest_message = lambda session, m=msg: m

    def test_a_stall_on_a_parked_session_escalates(self):
        self._with_message("J9 hasn't moved; it's still parked with you")
        v = laya_gate.classify("cc-w-wolfgang-1c2b", "", STALL)
        self.assertTrue(v["escalate"], v)
        self.assertEqual(v["lane"], 1)
        self.assertIn("parked on the operator", v["rule"])

    def test_a_sentinel_miss_on_a_parked_session_escalates(self):
        # SENTINEL-MISSED is lane 2 by event text; the session's own state has to override that.
        self._with_message("both items now wait only on a person's approval")
        v = laya_gate.classify("cc-w-wolfgang-1c2b", "", SENTINEL)
        self.assertTrue(v["escalate"], v)

    def test_a_stall_on_a_session_waiting_on_a_machine_stays_held(self):
        self._with_message("waiting on CI to finish the test job before I push")
        v = laya_gate.classify("cc-w-wolfgang-1c2b", "", STALL)
        self.assertFalse(v["escalate"], v)

    def test_a_park_is_reported_once_per_episode(self):
        self._with_message("J9 hasn't moved; it's still parked with you")
        first = laya_gate.classify("cc-w-wolfgang-1c2b", "", STALL)
        second = laya_gate.classify("cc-w-wolfgang-1c2b", "", STALL)
        self.assertTrue(first["escalate"])
        self.assertFalse(second["escalate"], second)
        self.assertIn("already told her", second["rule"])

    def test_a_park_during_quiet_hours_is_held_until_morning(self):
        laya_gate.in_quiet_hours = lambda now=None: True
        self._with_message("J9 hasn't moved; it's still parked with you")
        v = laya_gate.classify("cc-w-wolfgang-1c2b", "", STALL)
        self.assertFalse(v["escalate"], v)
        self.assertIn("held until morning", v["rule"])

    def test_the_rule_can_be_switched_off(self):
        laya_gate.PARKED_RULE = False
        self._with_message("J9 hasn't moved; it's still parked with you")
        v = laya_gate.classify("cc-w-wolfgang-1c2b", "", STALL)
        self.assertFalse(v["escalate"], v)
        laya_gate.PARKED_RULE = True

    def test_the_repeat_window_expires(self):
        self._with_message("J9 hasn't moved; it's still parked with you")
        laya_gate.classify("cc-w-wolfgang-1c2b", "", STALL)
        laya_gate._recent[("cc-w-wolfgang-1c2b", "PARKED")] = (
            time.time() - laya_gate.PARKED_REPEAT_WINDOW - 1)
        v = laya_gate.classify("cc-w-wolfgang-1c2b", "", STALL)
        self.assertTrue(v["escalate"], v)


class ModelLoadFailure(unittest.TestCase):
    """A gate that cannot load its model must say WHY, and name its interpreter.

    27 response decisions over 15 hours fell back to the standing policy because a gate started on
    the wrong interpreter answered /health perfectly while being unable to import laya-mlx. The
    exception TYPE alone did not say which module or which python, so the class was invisible.
    """

    def test_a_failed_load_records_the_module_and_the_interpreter(self):
        class Blocker:
            def find_spec(self, name, path=None, target=None):
                if name == "laya_mlx":
                    raise ImportError(f"No module named '{name}'")
                return None

        saved_model, saved_err = laya_gate._model, laya_gate._model_error
        blocker = Blocker()
        laya_gate._model = None
        laya_gate._model_error = None
        sys.meta_path.insert(0, blocker)
        try:
            with self.assertRaises(ImportError):
                laya_gate.load_model()
            recorded = laya_gate._model_error
        finally:
            sys.meta_path.remove(blocker)
            laya_gate._model, laya_gate._model_error = saved_model, saved_err

        self.assertIsNotNone(recorded)
        self.assertIn("laya_mlx", recorded)
        self.assertIn("interpreter=", recorded)

    def test_a_successful_load_clears_a_previous_error(self):
        saved_model, saved_err = laya_gate._model, laya_gate._model_error
        laya_gate._model = object()          # pretend it loaded
        laya_gate._model_error = "stale"
        try:
            self.assertIsNotNone(laya_gate._model_error)
        finally:
            laya_gate._model, laya_gate._model_error = saved_model, saved_err


if __name__ == "__main__":
    unittest.main()
