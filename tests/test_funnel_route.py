import datetime
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "scripts"))
FUNNEL = tempfile.mkdtemp()
FOCUS = tempfile.mkdtemp()
os.environ["FLEET_FUNNEL_DIR"] = FUNNEL
os.environ["LAYA_GATE_FOCUS_DIR"] = FOCUS
import clarify, escalation, funnel, ladder, obligations   # noqa: E402

clarify.FUNNEL_DIR = FUNNEL
clarify.FOCUS_DIR = FOCUS
ladder.FOCUS_DIR = FOCUS
S = "cc-t"
T0 = datetime.datetime(2026, 10, 16, 12, 0).timestamp()

FOCUS_TEXT = """ROLE: the shop and the fall fair.
RULING IN FORCE (2026-09-26): LEAVE THE FUNDRAISER OFF. The campaign stays DRAFT, do not publish the campaign.
- SETTLED (do not re-raise): the deploy cache wrinkle was fixed with a content hashed bundle.
- The vendor contract renewal wording is hers to settle. RAISED 2026-10-04, awaiting her one-line call, do not re-raise.
- some ordinary note about work.
"""


def setup_focus(text=FOCUS_TEXT):
    with open(os.path.join(FOCUS, S + "_focus.md"), "w") as f:
        f.write(text)


def iso(ts):
    return datetime.datetime.fromtimestamp(ts, datetime.timezone.utc).isoformat().replace("+00:00", "Z")


class Base(unittest.TestCase):
    def setUp(self):
        escalation.LOG_DIR = tempfile.mkdtemp()
        for f in ("answered.json", "mode", "page-requests.jsonl"):
            try:
                os.remove(os.path.join(FUNNEL, f))
            except OSError:
                pass
        funnel._PAGED.clear()
        setup_focus()
        self.sent = []

    def dispatch(self, sn, text):
        self.sent.append(text)
        return True


class ClarifyTest(Base):
    def test_a_question_a_registered_decision_covers_is_answered_with_that_line(self):
        r = clarify.resolve(S, "Should I publish the campaign now or keep the campaign as DRAFT?", self.dispatch, T0)
        self.assertEqual(r[0], "answered")
        self.assertEqual(r[3]["cite"], "focus:L2")
        self.assertIn("LEAVE THE FUNDRAISER OFF", self.sent[0])
        self.assertIn("emit NEEDS-INPUT", self.sent[0])

    def test_a_line_waiting_on_her_is_never_an_answer(self):
        r = clarify.resolve(S, "Should I settle the vendor contract renewal wording myself or wait?", self.dispatch, T0)
        self.assertEqual(r[0], "page")
        self.assertEqual(r[1], "clarification_ungrounded")
        self.assertEqual(self.sent, [])

    def test_a_line_that_names_the_operator_as_the_one_to_act_is_pending(self):
        setup_focus("SETTLED (do not re-raise): the vendor invoice dispute needs Pat to approve the wording.\n")
        q = "Should I send the vendor invoice dispute wording or wait for approval?"
        with mock.patch.dict(os.environ, {"FLEET_OPERATOR_NAMES": ""}):
            self.assertIsNotNone(clarify.ground(S, q))           # unknown name: looks decided
        with mock.patch.dict(os.environ, {"FLEET_OPERATOR_NAMES": "Pat"}):
            self.assertIsNone(clarify.ground(S, q))              # named: it is waiting on her

    def test_an_unrelated_question_is_ungrounded(self):
        r = clarify.resolve(S, "Which color should the new banner use for the harvest festival?", self.dispatch, T0)
        self.assertEqual(r[1], "clarification_ungrounded")

    def test_a_gated_question_is_never_answered(self):
        r = clarify.resolve(S, "Run git push --force to publish the campaign draft bundle?", self.dispatch, T0)
        self.assertEqual((r[0], r[1]), ("page", "gated_action"))
        self.assertEqual(self.sent, [])

    def test_asking_again_after_an_answer_is_a_refusal(self):
        q = "Should I publish the campaign now or keep the campaign as DRAFT?"
        self.assertEqual(clarify.resolve(S, q, self.dispatch, T0)[0], "answered")
        r = clarify.resolve(S, q, self.dispatch, T0 + 600)
        self.assertEqual((r[0], r[1]), ("page", "nudge_refused"))
        self.assertEqual(len(self.sent), 1)

    def test_an_answer_that_cannot_be_delivered_pages(self):
        r = clarify.resolve(S, "Should I publish the campaign now or keep the campaign as DRAFT?",
                            lambda s, t: False, T0)
        self.assertEqual((r[0], r[1]), ("page", "no_safe_nudge"))

    def test_cite_must_name_a_real_line_and_quote_it(self):
        self.assertTrue(clarify.cite_ok(S, "focus:L3", "SETTLED (do not re-raise): the deploy cache wrinkle"))
        self.assertFalse(clarify.cite_ok(S, "focus:L3", "ship it to production"))
        self.assertFalse(clarify.cite_ok(S, "focus:L99", "anything"))
        self.assertFalse(clarify.cite_ok(S, "inbound:1", "anything"))


class RouteTest(Base):
    def r(self, match, **kw):
        kw.setdefault("dispatch", self.dispatch)
        return funnel.route(S, match, now=T0, **kw)

    def test_hard_system_events_go_to_the_gate(self):
        for m in ("SESSION-DEAD-CC-T: no tmux session", "USAGE-LIMIT-CC-T: hit", "Traceback (most recent call last)",
                  "[Production Deploy] denied"):
            self.assertEqual(self.r(m).kind, "legacy", m)

    def test_events_that_are_not_one_of_the_three_cases_are_handled(self):
        for m in ("ERROR-x: something odd", "SENTINEL-MISSED-CC-T: expected DONE-a by 1", "VERSION-BEHIND-CC-T"):
            self.assertEqual(self.r(m).kind, "handled", m)

    def test_needs_input_grounded_is_handled_ungrounded_pages(self):
        a = self.r("NEEDS-INPUT-x", ask="Should I publish the campaign now or keep the campaign as DRAFT?")
        self.assertEqual(a.kind, "handled")
        b = self.r("NEEDS-INPUT-x", ask="Which color should the harvest festival banner use this year?")
        self.assertEqual(b.kind, "page")
        self.assertEqual(b.esc.reason, escalation.Reason.CLARIFICATION_UNGROUNDED)
        self.assertIn("Recommendation", b.esc.text)
        self.assertIn("Default", b.esc.text)

    def test_ladder_in_progress_is_handled(self):
        n = {"episode_id": "e", "nudge_count": 2, "nudge_max": 5, "last_outcome": "ack_only"}
        self.assertEqual(self.r("STALL-CC-T: quiet", state="IDLE", nudges=n, stopped=900).kind, "handled")

    def test_ladder_exhausted_pages_once_per_stop(self):
        n = {"episode_id": "e", "nudge_count": 5, "nudge_max": 5, "last_outcome": "restate_done"}
        a = self.r("STALL-CC-T: quiet", state="IDLE", nudges=n, stopped=900)
        self.assertEqual(a.kind, "page")
        self.assertEqual(a.esc.reason, escalation.Reason.NUDGE_REFUSED)
        self.assertIn("Recommendation", a.esc.text)
        b = self.r("STALL-CC-T: quiet", state="IDLE", nudges=n, stopped=1200)
        self.assertEqual(b.kind, "handled")

    def test_busy_and_silent_too_long_pages_as_no_safe_nudge(self):
        a = self.r("STALL-CC-T: quiet", state="WORKING", nudges=None, stopped=4000)
        self.assertEqual((a.kind, a.esc.reason), ("page", escalation.Reason.NO_SAFE_NUDGE))
        self.assertEqual(self.r("STALL-CC-T: quiet", state="WORKING", nudges=None, stopped=600).kind, "handled")

    def test_undelivered_dispatch_pages(self):
        a = self.r("ACK-MISSED-CC-T: dispatch never received")
        self.assertEqual((a.kind, a.esc.reason), ("page", escalation.Reason.NO_SAFE_NUDGE))

    def test_a_manager_owned_session_is_left_to_its_manager(self):
        n = {"episode_id": "e", "nudge_count": 5, "nudge_max": 5, "last_outcome": "refusal"}
        self.assertEqual(self.r("STALL-CC-T: q", state="IDLE", nudges=n, stopped=900, manager_owns=True).kind, "handled")

    def test_a_session_parked_at_its_finished_task_is_silent(self):
        rows = [{"timestamp": iso(T0), "type": "assistant", "message": {"role": "assistant", "content": [
            {"type": "text", "text": "All shipped.\nDONE-x-cc-t-1"}]}}]
        fd, path = tempfile.mkstemp(suffix=".jsonl")
        with os.fdopen(fd, "w") as f:
            for x in rows:
                f.write(json.dumps(x) + "\n")
        n = {"episode_id": "e", "nudge_count": 5, "nudge_max": 5, "last_outcome": "restate_done"}
        a = self.r("STALL-CC-T: q", state="IDLE", nudges=n, stopped=900, path=path)
        self.assertEqual((a.kind, a.note), ("handled", "parked at its finished task"))

    def test_an_instruction_newer_than_done_reopens_the_obligation(self):
        rows = [{"timestamp": iso(T0), "type": "assistant", "message": {"role": "assistant", "content": [
                    {"type": "text", "text": "DONE-x-cc-t-1"}]}},
                {"timestamp": iso(T0 + 60), "type": "attachment", "attachment": {
                    "type": "queued_command", "prompt": "now also do the second thing", "origin": {"kind": "human"}}}]
        fd, path = tempfile.mkstemp(suffix=".jsonl")
        with os.fdopen(fd, "w") as f:
            for x in rows:
                f.write(json.dumps(x) + "\n")
        self.assertEqual(obligations.state(path)["state"], "open")

    def test_the_fleets_own_nudge_does_not_reopen_a_finished_task(self):
        rows = [{"timestamp": iso(T0), "type": "assistant", "message": {"role": "assistant", "content": [
                    {"type": "text", "text": "DONE-x-cc-t-1"}]}},
                {"timestamp": iso(T0 + 60), "type": "attachment", "attachment": {
                    "type": "queued_command", "origin": {"kind": "human"},
                    "prompt": "cc-t: you have been quiet 15 min. Continue the work"}}]
        fd, path = tempfile.mkstemp(suffix=".jsonl")
        with os.fdopen(fd, "w") as f:
            for x in rows:
                f.write(json.dumps(x) + "\n")
        self.assertEqual(obligations.state(path)["state"], "complete")


class RequestsAndModeTest(Base):
    def test_requested_pages_go_through_the_door(self):
        funnel.request_page(S, "needs her call on the wording", "replace it", "leave it")
        q = []
        self.assertEqual(funnel.drain_requests(q), 1)
        self.assertEqual(q[0]["reason"], "clarification_ungrounded")
        self.assertIn("Recommendation: replace it", q[0]["match"])
        self.assertEqual(funnel.drain_requests(q), 0)

    def test_mode_file_and_env(self):
        with mock.patch.dict(os.environ, {"FLEET_FUNNEL": ""}):
            self.assertEqual(escalation.mode(), "shadow")
            with open(os.path.join(FUNNEL, "mode"), "w") as f:
                f.write("enforce\n")
            self.assertEqual(escalation.mode(), "enforce")
            with mock.patch.dict(os.environ, {"FLEET_FUNNEL": "off"}):
                self.assertEqual(escalation.mode(), "off")
            with open(os.path.join(FUNNEL, "mode"), "w") as f:
                f.write("garbage\n")
            self.assertEqual(escalation.mode(), "shadow")

    def test_resolve_cli_refuses_a_composed_answer(self):
        import fleet_resolve
        with mock.patch.object(fleet_resolve.subprocess, "run") as run:
            rc = fleet_resolve.main(["answer", S, "--cite", "focus:L3", "--text", "go ahead and deploy to production"])
        self.assertEqual(rc, 1)
        run.assert_not_called()

    def test_resolve_cli_refuses_a_gated_quote(self):
        import fleet_resolve
        setup_focus("SETTLED (do not re-raise): cleanup uses rm -rf on the build dir every time.\n")
        with mock.patch.object(fleet_resolve.subprocess, "run") as run:
            rc = fleet_resolve.main(["answer", S, "--cite", "focus:L1", "--text", "cleanup uses rm -rf on the build dir"])
        self.assertEqual(rc, 1)
        run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
