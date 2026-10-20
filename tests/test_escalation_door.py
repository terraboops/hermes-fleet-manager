import ast
import json
import os
import sys
import tempfile
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ["FLEET_LEDGER_DIR"] = tempfile.mkdtemp()
import escalation as esc   # noqa: E402  (ledger dir must be set first)


def ledger(name):
    return esc.read(name)


class DoorTest(unittest.TestCase):
    def setUp(self):
        esc.LOG_DIR = tempfile.mkdtemp()

    def test_page_queues_and_records_with_its_reason(self):
        q = []
        row = esc.page(q, esc.Escalation("cc-x", esc.Reason.NUDGE_REFUSED, "stalled", rule="ladder"))
        self.assertEqual(len(q), 1)
        self.assertEqual(q[0]["reason"], "nudge_refused")
        self.assertTrue(q[0]["urgent"])
        self.assertEqual(ledger(esc.ESCALATIONS)[0]["reason"], "nudge_refused")
        self.assertFalse(row["hard"])

    def test_hard_reasons_are_marked_hard(self):
        row = esc.page([], esc.Escalation("cc-x", esc.Reason.SESSION_DEAD, "dead"))
        self.assertTrue(row["hard"])

    def test_unknown_reason_is_delivered_and_recorded_as_a_fault(self):
        q = []
        esc.page(q, esc.Escalation("cc-x", "made_up", "something broke"))
        self.assertEqual(len(q), 1, "a page with a bad reason must still be delivered")
        self.assertEqual(q[0]["reason"], "funnel_fault")
        kinds = [r.get("kind") for r in ledger(esc.ESCALATIONS)]
        self.assertIn("bad_reason", kinds)

    def test_a_ledger_that_cannot_write_does_not_take_the_page_down(self):
        esc.LOG_DIR = "/proc/nonexistent/x"
        q = []
        esc.page(q, esc.Escalation("cc-x", esc.Reason.SESSION_DEAD, "dead"))
        self.assertEqual(len(q), 1)

    def test_legacy_reason_mapping(self):
        R = esc.Reason
        self.assertEqual(esc.legacy_reason("SESSION-DEAD-CC-X: gone"), R.SESSION_DEAD)
        self.assertEqual(esc.legacy_reason("USAGE-LIMIT-CC-X: hit"), R.USAGE_LIMIT)
        self.assertEqual(esc.legacy_reason("x", "possible mid-turn wedge: no output for 61 min"), R.NO_SAFE_NUDGE)
        self.assertEqual(esc.legacy_reason("x", "idle 40 min; nudged 5x with no revival"), R.NUDGE_REFUSED)
        self.assertEqual(esc.legacy_reason("x [Production Deploy] denied"), R.CLASSIFIER_BLOCK)
        self.assertEqual(esc.legacy_reason("NEEDS-INPUT-x", "session asks for input"), R.LEGACY)

    def test_attempt_and_outcome_share_an_episode(self):
        aid = esc.record_attempt("cc-x", "cc-x@1", "STALL", "nudge", "text")
        esc.record_outcome(aid, "cc-x", "cc-x@1", "restate_done", "said done")
        rows = ledger(esc.ATTEMPTS)
        self.assertEqual({r["episode"] for r in rows}, {"cc-x@1"})
        self.assertEqual(rows[1]["of"], aid)

    def test_heartbeat_is_rate_limited(self):
        esc._HB[0] = 0.0
        self.assertTrue(esc.heartbeat(now=1000.0))
        self.assertFalse(esc.heartbeat(now=1100.0))
        self.assertTrue(esc.heartbeat(now=1700.0))


def _calls(tree, parents=()):
    """Yield (call node, enclosing function names) for every call in a module."""
    for node in ast.iter_child_nodes(tree):
        names = parents + ((node.name,) if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) else ())
        if isinstance(node, ast.Call):
            yield node, names
        for sub in _calls(node, names):
            yield sub


class NoBypassTest(unittest.TestCase):
    """Only the door may queue a page, and only the delivery function may send one."""

    def parse(self, rel):
        with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
            return ast.parse(f.read())

    def test_nothing_appends_to_the_pending_queue_except_the_door(self):
        for rel in ("fleet_watch.py", "laya_gate.py"):
            for call, fns in _calls(self.parse(rel)):
                f = call.func
                if isinstance(f, ast.Attribute) and f.attr == "append" \
                        and isinstance(f.value, ast.Name) and f.value.id == "_pending":
                    self.fail("%s:%d appends to _pending outside escalation.page" % (rel, call.lineno))

    def test_only_delivery_sends_telegram(self):
        allowed = {"deliver_batch", "send_telegram"}
        for rel in ("fleet_watch.py", "laya_gate.py", "escalation.py"):
            for call, fns in _calls(self.parse(rel)):
                f = call.func
                name = f.id if isinstance(f, ast.Name) else getattr(f, "attr", "")
                if name in ("send_telegram", "post_batch", "post_webhook") and not (set(fns) & allowed):
                    self.fail("%s:%d sends outside delivery (%s)" % (rel, call.lineno, name))

    def test_only_the_boot_report_has_its_own_telegram_sender(self):
        """fleet_boot sends a one-time boot report. Any other script that talks to the Bot API
        is a paging path the door cannot see."""
        found = set()
        for name in os.listdir(os.path.join(ROOT, "scripts")):
            if name.endswith(".py"):
                with open(os.path.join(ROOT, "scripts", name), encoding="utf-8") as f:
                    if "api.telegram.org" in f.read():
                        found.add(name)
        self.assertEqual(found, {"fleet_boot.py"})


if __name__ == "__main__":
    unittest.main()


class RepeatSuppressionTest(unittest.TestCase):
    """A page means something is broken that only the operator can fix. The same ask again is not."""

    def setUp(self):
        esc.LOG_DIR = tempfile.mkdtemp()
        self._mode = esc.mode
        esc.mode = lambda: "enforce"

    def tearDown(self):
        esc.mode = self._mode

    def ask(self, text, reason=esc.Reason.CLARIFICATION_UNGROUNDED, session="cc-x"):
        return esc.Escalation(session, reason, text, rule="r")

    def test_the_same_ask_pages_once_then_is_ledgered_as_a_repeat(self):
        q = []
        esc.page(q, self.ask("No change. Still blocked on a read-only key."), now=1000)
        esc.page(q, self.ask("No change. Still blocked on a read-only key."), now=1600)
        self.assertEqual(len(q), 1)
        kinds = [r.get("kind") for r in ledger(esc.ESCALATIONS)]
        self.assertIn("suppressed_repeat", kinds)

    def test_rephrased_status_of_the_same_blocker_is_one_ask(self):
        q = []
        for i, t in enumerate((
                "Still idle; I need the next Vibes brief or a go-ahead for taste-rust-brief.md.",
                "No change: I'm idle and need the next Vibes brief or a go-ahead for taste-rust-brief.md.",
                "Nothing has changed. Idle, blocked on the next Vibes brief or a go-ahead for taste-rust-brief.md.")):
            esc.page(q, self.ask(t), now=1000 + i * 600)
        self.assertEqual(len(q), 1)

    def test_a_genuinely_new_blocker_in_the_same_session_pages(self):
        q = []
        esc.page(q, self.ask("Still blocked on a read-only Linear API key for the consistency check."), now=1000)
        esc.page(q, self.ask("Production database migration 0042 failed on the staging replica and rolled back."), now=1100)
        self.assertEqual(len(q), 2)

    def test_similarity_does_not_merge_different_things_that_share_filler(self):
        self.assertFalse(esc.similar("Still blocked on the GitHub billing fix.",
                                     "Still blocked on the eBay marketplace deletion endpoint."))
        self.assertTrue(esc.similar("Still blocked on the GitHub billing fix and the Google OAuth client.",
                                    "Nothing has changed: waiting on the Google OAuth client and the GitHub billing fix."))

    def test_counts_and_times_do_not_make_a_new_ask(self):
        q = []
        esc.page(q, self.ask("No change at 10:02. 3 items still waiting on the key (abc1234)."), now=1000)
        esc.page(q, self.ask("No change at 10:41. 2 items still waiting on the key (def5678)."), now=2000)
        self.assertEqual(len(q), 1)

    def test_a_different_ask_from_the_same_session_pages(self):
        q = []
        esc.page(q, self.ask("Still blocked on a read-only Linear key."), now=1000)
        esc.page(q, self.ask("Which database should the notebooks use, sqlite or postgres?"), now=1100)
        self.assertEqual(len(q), 2)

    def test_the_same_text_from_another_session_pages(self):
        q = []
        esc.page(q, self.ask("Still blocked on a key.", session="cc-a"), now=1000)
        esc.page(q, self.ask("Still blocked on a key.", session="cc-b"), now=1100)
        self.assertEqual(len(q), 2)

    def test_real_breakage_always_pages_even_when_identical(self):
        q = []
        for r in (esc.Reason.SESSION_DEAD, esc.Reason.USAGE_LIMIT, esc.Reason.CLASSIFIER_BLOCK, esc.Reason.FUNNEL_FAULT):
            esc.page(q, self.ask("it is down", reason=r), now=1000)
            esc.page(q, self.ask("it is down", reason=r), now=1100)
        self.assertEqual(len(q), 8)

    def test_the_window_expires(self):
        q = []
        esc.page(q, self.ask("Still blocked on a key."), now=1000)
        esc.page(q, self.ask("Still blocked on a key."), now=1000 + esc.REPEAT_WINDOW + 5)
        self.assertEqual(len(q), 2)

    def test_memory_survives_a_restart(self):
        q = []
        esc.page(q, self.ask("Still blocked on a key."), now=1000)
        esc._HB[0] = 0.0                          # nothing in memory carries over; state is the file
        esc.page(q, self.ask("Still blocked on a key."), now=1200)
        self.assertEqual(len(q), 1)

    def test_shadow_mode_never_suppresses(self):
        esc.mode = lambda: "shadow"
        q = []
        esc.page(q, self.ask("Still blocked on a key."), now=1000)
        esc.page(q, self.ask("Still blocked on a key."), now=1100)
        self.assertEqual(len(q), 2)

    def test_a_memory_that_cannot_be_written_pages_rather_than_goes_silent(self):
        esc.LOG_DIR = "/proc/nonexistent/x"
        q = []
        esc.page(q, self.ask("Still blocked on a key."), now=1000)
        esc.page(q, self.ask("Still blocked on a key."), now=1100)
        self.assertEqual(len(q), 2)
