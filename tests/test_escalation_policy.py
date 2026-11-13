"""Escalation policy: reversible asks are answered and logged, hard floor and classifier blocks page,
dead/limit/crash/stall are news, thresholds come from policy.yaml, weekly counts are honest."""
import os
import sys
import tempfile
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "scripts"))
FUNNEL = tempfile.mkdtemp()
FOCUS = tempfile.mkdtemp()
LOGS = tempfile.mkdtemp()
os.environ["FLEET_FUNNEL_DIR"] = FUNNEL
os.environ["LAYA_GATE_FOCUS_DIR"] = FOCUS
os.environ["FLEET_DECISIONS_LOG"] = os.path.join(LOGS, "decisions.md")
os.environ["FLEET_POLICY"] = os.path.join(FUNNEL, "policy.yaml")
import clarify, escalation, funnel, policy   # noqa: E402

clarify.FUNNEL_DIR = FUNNEL
clarify.FOCUS_DIR = FOCUS
S = "cc-t"


class Base(unittest.TestCase):
    def setUp(self):
        escalation.LOG_DIR = tempfile.mkdtemp()
        for f in ("answered.json", "mode", "policy.yaml"):
            try:
                os.remove(os.path.join(FUNNEL, f))
            except OSError:
                pass
        try:
            os.remove(os.environ["FLEET_DECISIONS_LOG"])
        except OSError:
            pass
        funnel._PAGED.clear()
        self.sent = []

    def dispatch(self, sn, text):
        self.sent.append(text)
        return True

    def log(self):
        try:
            with open(os.environ["FLEET_DECISIONS_LOG"]) as f:
                return f.read()
        except OSError:
            return ""


class ReversibleAsk(Base):
    def test_may_i_continue_is_answered_and_logged(self):
        r = clarify.resolve(S, "The tests pass and the branch is clean. May I continue with the next module?",
                            self.dispatch, time.time())
        self.assertEqual(r[0], "answered")
        self.assertEqual(r[3]["cite"], "rule:continue-reversible")
        self.assertIn("keep every step reversible", self.sent[0])
        line = self.log()
        self.assertIn("fleet answered cc-t", line)
        self.assertIn("Undo:", line)

    def test_a_choice_with_a_recommendation_is_answered_and_logged(self):
        r = clarify.resolve(S, "Should I use option A (cache the result) or option B (recompute)? Recommendation: option A, it is cheaper.",
                            self.dispatch, time.time())
        self.assertEqual(r[0], "answered")
        self.assertIn("decisions", os.path.basename(os.environ["FLEET_DECISIONS_LOG"]))
        self.assertIn("option A", self.log())

    def test_continue_that_names_a_hard_floor_step_pages_and_logs_nothing(self):
        for ask in ("May I continue and delete the old production database?",
                    "May I proceed and force-push main?",
                    "Shall I continue? I need your credential for the deploy key."):
            r = clarify.resolve(S, ask, self.dispatch, time.time())
            self.assertEqual(r[0], "page", ask)
        self.assertEqual(self.sent, [])
        self.assertEqual(self.log(), "")

    def test_a_real_two_way_choice_is_answered_and_an_outward_one_in_the_same_blob_pages(self):
        q = "should its fix wait for #728, or should I fold skew protection into this work?"
        r = clarify.resolve(S, q, self.dispatch, time.time())
        self.assertEqual(r[0], "answered")
        self.assertIn("rule:safest-option", r[3]["cite"])
        self.assertIn("fold skew protection", self.log())
        sent_before = len(self.sent)
        r = clarify.resolve(S, "may I post the drafted comment on #728? " + q, self.dispatch, time.time())
        self.assertEqual(r[0], "page")
        self.assertEqual(len(self.sent), sent_before)

    def test_a_status_line_that_says_can_move_on_is_not_an_ask(self):
        r = clarify.resolve(S, "Nothing has changed, and I have no work I can move on my own. The cost watch runs on its own schedule.",
                            self.dispatch, time.time())
        self.assertEqual(r[0], "silent")           # status, not answered and not paged
        self.assertEqual(self.sent, [])

    def test_an_undeliverable_answer_pages_and_is_not_logged(self):
        r = clarify.resolve(S, "May I continue with the next module?", lambda *a: False, time.time())
        self.assertEqual(r[0], "page")
        self.assertEqual(self.log(), "")


class ClassifierBlockStillPages(Base):
    def test_classifier_block_is_legacy_hard_and_pages(self):
        a = funnel.route(S, "[Production Deploy] blocked: deploy to prod", dispatch=self.dispatch)
        self.assertEqual(a.kind, "legacy")
        self.assertTrue(funnel.is_hard("x [Security Weaken] y"))
        self.assertEqual(escalation.legacy_reason("x", "the session was blocked by the auto-mode classifier"),
                         escalation.Reason.CLASSIFIER_BLOCK)
        self.assertIn(escalation.Reason.CLASSIFIER_BLOCK, escalation.HARD)


class Inform(Base):
    def test_dead_limit_and_crash_are_phrased_as_news(self):
        for reason, text in ((escalation.Reason.SESSION_DEAD, "SESSION-DEAD-cc-t-1792000000000"),
                             (escalation.Reason.USAGE_LIMIT, "USAGE-LIMIT-cc-t-1792000000000"),
                             (escalation.Reason.CRASH, "Traceback (most recent call last): boom")):
            pending = []
            escalation.page(pending, escalation.Escalation(S, reason, text))
            msg = pending[0]["match"]
            self.assertNotIn("?", msg)
            self.assertNotIn("Recommendation", msg)
            self.assertIn("nothing to decide", msg.lower())

    def test_stall_under_the_threshold_is_not_news_and_over_it_is(self):
        n = {"episode_id": "e", "nudge_count": 5, "nudge_max": 5, "last_outcome": "restate_done"}
        a = funnel.route(S, "STALL-CC-T: quiet", state="IDLE", nudges=n, stopped=300)
        self.assertEqual(a.kind, "handled")
        b = funnel.route(S, "STALL-CC-T: quiet", state="IDLE", nudges=n, stopped=900)
        self.assertEqual(b.kind, "page")
        self.assertNotIn("?", b.esc.text)

    def test_the_threshold_and_wording_are_config(self):
        with open(os.environ["FLEET_POLICY"], "w") as f:
            f.write("stall_inform_after_s: 1800\ninform:\n  nudge_refused: '{session} is quiet ({mins} min).'\n")
        n = {"episode_id": "e", "nudge_count": 5, "nudge_max": 5, "last_outcome": "restate_done"}
        self.assertEqual(funnel.route(S, "STALL-CC-T: quiet", state="IDLE", nudges=n, stopped=900).kind, "handled")
        b = funnel.route(S, "STALL-CC-T: quiet", state="IDLE", nudges=n, stopped=2000)
        self.assertEqual(b.esc.text, "cc-t is quiet (33 min).")

    def test_a_broken_policy_file_falls_back_to_defaults(self):
        with open(os.environ["FLEET_POLICY"], "w") as f:
            f.write("stall_inform_after_s: [unclosed\n")
        self.assertEqual(policy.get("stall_inform_after_s"), 600)

    def test_a_policy_pattern_changes_the_answer(self):
        with open(os.environ["FLEET_POLICY"], "w") as f:
            f.write("continue_reply: 'Carry on.'\n")
        r = clarify.resolve(S, "May I continue with the next module?", self.dispatch, time.time())
        self.assertEqual(self.sent, ["Carry on."])


class Rows(Base):
    def test_page_rows_carry_their_class(self):
        pending = []
        escalation.page(pending, escalation.Escalation(S, escalation.Reason.SESSION_DEAD, "SESSION-DEAD-x-1792000000000"))
        escalation.page(pending, escalation.Escalation(S, escalation.Reason.CLASSIFIER_BLOCK, "[Production Deploy] blocked"))
        rows = [r for r in escalation.read(escalation.ESCALATIONS) if r.get("reason")]
        self.assertEqual([r["class"] for r in rows], ["inform", "ask"])


class StatusLoops(Base):
    VIBES = ("No change: I'm idle and need the next brief or a go-ahead for `taste-rust-brief.md`. "
             "`main` is unchanged at `b2df3fa`. Recommendation: reply in one line. Default: it stays parked.")

    def test_an_idle_status_loop_is_silent_and_sends_nothing(self):
        r = clarify.resolve(S, self.VIBES, self.dispatch, time.time())
        self.assertEqual(r[0], "silent")
        self.assertEqual(self.sent, [])
        self.assertEqual(self.log(), "")

    def test_status_text_is_not_adopted_as_a_recommendation(self):
        self.assertEqual(clarify.resolve(S, self.VIBES, self.dispatch, time.time())[0], "silent")
        # no question mark: the "Recommendation:" tail of a status line must not become an instruction
        r = clarify.resolve(S, "Deploy is blocked on the cache. Recommendation: reply in one line. Default: it stays parked.",
                            self.dispatch, time.time())
        self.assertEqual(r[0], "page")
        self.assertEqual(self.sent, [])

    def test_a_status_that_needs_a_credential_still_pages(self):
        for ask in ("Still blocked, nothing has changed. I need a read-only Linear key.",
                    "Nothing has changed: I'm still blocked on the operator. 1. A Google OAuth client. 2. Billing fixed.",
                    "I'm idle. Every open item is the owner's call: landing the fix, pushing the held changes.",
                    "I'm idle, nothing has changed. Waiting on you: your review on #526."):
            self.assertEqual(clarify.resolve(S, ask, self.dispatch, time.time())[0], "page", ask)

    def test_a_question_is_never_silenced(self):
        r = clarify.resolve(S, "I'm idle and nothing has changed. Should I delete the old branch?", self.dispatch, time.time())
        self.assertEqual(r[0], "page")

    def test_the_bare_placeholder_is_not_a_status_loop(self):
        r = clarify.resolve(S, "(the session gave no words with its request)", self.dispatch, time.time())
        self.assertEqual(r[0], "page")

    def test_route_hands_a_status_loop_back_as_handled(self):
        a = funnel.route(S, "NEEDS-INPUT-x", ask=self.VIBES, dispatch=self.dispatch)
        self.assertEqual(a.kind, "handled")
        self.assertIn("status-loop", a.note)

    def test_a_classifier_block_still_pages(self):
        a = funnel.route(S, "[Production Deploy] denied: nothing has changed", ask="", dispatch=self.dispatch)
        self.assertEqual(a.kind, "legacy")

    def test_a_no_change_restatement_waits_the_status_window(self):
        esc = lambda t: escalation.Escalation(S, escalation.Reason.CLARIFICATION_UNGROUNDED, t)
        self.assertEqual(escalation.repeat_window("Nothing has changed, still blocked on the operator"),
                         policy.get("status_repeat_window_s"))
        self.assertEqual(escalation.repeat_window("Should the deploy wait for the review"), escalation.REPEAT_WINDOW)


if __name__ == "__main__":
    unittest.main()
