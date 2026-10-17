import datetime
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ["FLEET_LEDGER_DIR"] = tempfile.mkdtemp()
import escalation   # noqa: E402
import fleet_watch  # noqa: E402
import ladder       # noqa: E402
import outcomes     # noqa: E402

S = "cc-t"
T0 = datetime.datetime(2026, 10, 16, 12, 0).timestamp()


def iso(ts):
    return datetime.datetime.fromtimestamp(ts, datetime.timezone.utc).isoformat().replace("+00:00", "Z")


def asst(ts, text="", tool=False):
    blocks = []
    if text:
        blocks.append({"type": "text", "text": text})
    if tool:
        blocks.append({"type": "tool_use", "name": "Bash", "input": {}})
    return {"timestamp": iso(ts), "type": "assistant", "message": {"role": "assistant", "content": blocks}}


def user(ts, text):
    return {"timestamp": iso(ts), "type": "user", "message": {"role": "user", "content": text}}


def write(rows):
    fd, path = tempfile.mkstemp(suffix=".jsonl")
    with os.fdopen(fd, "w") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")
    return path


class OutcomeTest(unittest.TestCase):
    def test_tool_call_is_progress(self):
        p = write([user(T0 + 1, "nudge"), asst(T0 + 5, "on it", tool=True)])
        self.assertEqual(outcomes.evaluate(p, T0)["outcome"], "progress")

    def test_token_is_progress(self):
        p = write([asst(T0 + 5, "all set\nDONE-x-cc-t-123")])
        self.assertEqual(outcomes.evaluate(p, T0)["outcome"], "progress")

    def test_our_own_paste_is_not_a_response(self):
        p = write([user(T0 + 1, "you have been quiet 15 min")])
        self.assertEqual(outcomes.evaluate(p, T0)["outcome"], "silent")

    def test_restating_done_is_not_progress(self):
        p = write([user(T0 + 1, "nudge"), asst(T0 + 5, "The work is done and live. Nothing left to do.")])
        self.assertEqual(outcomes.evaluate(p, T0)["outcome"], "restate_done")

    def test_a_refusal(self):
        p = write([asst(T0 + 5, "I cannot continue with this.")])
        self.assertEqual(outcomes.evaluate(p, T0)["outcome"], "refusal")

    def test_an_answer_that_did_nothing(self):
        p = write([asst(T0 + 5, "Sure, noted.")])
        self.assertEqual(outcomes.evaluate(p, T0)["outcome"], "ack_only")

    def test_turns_before_the_nudge_do_not_count(self):
        p = write([asst(T0 - 100, "did a lot", tool=True), user(T0 + 1, "nudge")])
        self.assertEqual(outcomes.evaluate(p, T0)["outcome"], "silent")


class LadderTextTest(unittest.TestCase):
    def test_rungs_escalate_in_kind(self):
        with mock.patch.object(ladder, "_focus", return_value="ROLE: ship the thing\nNEXT: run the suite\n"):
            t1, g1 = ladder.rung_text(S, 1, 15)
            t2, _ = ladder.rung_text(S, 2, 30, "restate_done")
            t3, g3 = ladder.rung_text(S, 3, 45, "ack_only")
        self.assertIn("quiet 15 min", t1)
        self.assertIn("next step on record: run the suite", t1)
        self.assertTrue(g1)
        self.assertIn("evidence", t2)
        self.assertIn("ship the thing", t3)
        self.assertTrue(g3)

    def test_no_authorization_language_in_any_rung(self):
        for r in (1, 2, 3, 4):
            for o in (None, "restate_done", "ack_only"):
                t, _ = ladder.rung_text(S, r, 20, o)
                self.assertNotRegex(t.lower(), r"authori[sz]|approved|go ahead and (deploy|merge|push)")

    def test_ungrounded_without_a_next_line(self):
        with mock.patch.object(ladder, "_focus", return_value="ROLE: x\n"):
            self.assertFalse(ladder.rung_text(S, 1, 15)[1])


class WouldPageTest(unittest.TestCase):
    def test_busy_and_silent_long_is_a_wedge(self):
        self.assertEqual(ladder.would_page("WORKING", 4000, {}), "no_safe_nudge")
        self.assertIsNone(ladder.would_page("WORKING", 600, {}))

    def test_two_undelivered_dispatches(self):
        self.assertEqual(ladder.would_page("IDLE", 900, {"failed_dispatches": 2}), "no_safe_nudge")

    def test_ladder_exhausted_without_progress(self):
        n = {"nudge_count": 5, "nudge_max": 5, "last_outcome": "restate_done"}
        self.assertEqual(ladder.would_page("IDLE", 900, n), "nudge_refused")

    def test_exhausted_but_the_last_answer_was_progress_does_not_page(self):
        n = {"nudge_count": 5, "nudge_max": 5, "last_outcome": "progress"}
        self.assertIsNone(ladder.would_page("IDLE", 900, n))


class EpisodeContinuityTest(unittest.TestCase):
    """A landed nudge grows the transcript, which used to start a new episode every time."""

    def setUp(self):
        escalation.LOG_DIR = tempfile.mkdtemp()
        fleet_watch._nudges.clear()

    def ep(self, began):
        return {"episode_id": "%s@%d" % (S, began), "began": float(began), "nudge_count": 2,
                "first_nudge_at": T0, "last_nudge_at": int(T0 + 1000), "last_outcome": None,
                "failed_dispatches": 0, "last_attempt": "a1", "outcome_logged": False}

    def test_a_restating_session_stays_in_one_episode(self):
        fleet_watch._nudges[S] = self.ep(T0)
        p = write([user(T0 + 1001, "nudge"), asst(T0 + 1010, "It is done. Nothing left to do.")])
        got = fleet_watch._episode_for(S, 900, T0 + 3000, path=p)   # a "new" stop, 100s later
        self.assertEqual(got["nudge_count"], 2)
        self.assertEqual(got["last_outcome"], "restate_done")
        self.assertEqual(got["episode_id"], "%s@%d" % (S, T0))
        rows = escalation.read(escalation.ATTEMPTS)
        self.assertEqual([r["outcome"] for r in rows if r.get("kind") == "outcome"], ["restate_done"])

    def test_progress_starts_a_fresh_episode(self):
        fleet_watch._nudges[S] = self.ep(T0)
        p = write([asst(T0 + 1010, "running it", tool=True)])
        got = fleet_watch._episode_for(S, 900, T0 + 3000, path=p)
        self.assertEqual(got["nudge_count"], 0)

    def test_without_a_transcript_the_old_rule_holds(self):
        fleet_watch._nudges[S] = self.ep(T0)
        got = fleet_watch._episode_for(S, 900, T0 + 3000, path=None)
        self.assertEqual(got["nudge_count"], 0)


class SessionStateTest(unittest.TestCase):
    def test_state_is_the_bare_word_not_the_fingerprint(self):
        fake = mock.Mock()
        fake.fingerprint.return_value = "IDLE|fef50c19ff39|"
        with mock.patch.dict(sys.modules, {"fleet_state": fake}):
            self.assertEqual(fleet_watch._session_state(S), "IDLE")
        fake.fingerprint.return_value = "WORKING|abc|hb=1"
        with mock.patch.dict(sys.modules, {"fleet_state": fake}):
            self.assertEqual(fleet_watch._session_state(S), "WORKING")
        fake.fingerprint.return_value = None
        with mock.patch.dict(sys.modules, {"fleet_state": fake}):
            self.assertIsNone(fleet_watch._session_state(S))


if __name__ == "__main__":
    unittest.main()
