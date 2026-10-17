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
