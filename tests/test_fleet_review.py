#!/usr/bin/env python3
"""Tests for fleet_review: the transcript reviewer and the tiered verifier."""
import json
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts"))

import fleet_contract as fc  # noqa: E402
import fleet_governance as fg  # noqa: E402
import fleet_review as fr  # noqa: E402

ACC = [{"check": "suite green", "positive": ["pass"], "negative": ["fail"]}]


def w(path, obj):
    with open(path, "a") as fh:
        fh.write(json.dumps(obj) + "\n")


def asst(path, *blocks):
    w(path, {"type": "assistant", "message": {"role": "assistant", "content": list(blocks)}})


def text(t):
    return {"type": "text", "text": t}


def use(i, name, **inp):
    return {"type": "tool_use", "id": i, "name": name, "input": inp}


def result(path, i, body, err=False):
    w(path, {"type": "user", "message": {"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": i, "content": body, "is_error": err}]}})


class Fleet(object):
    """N children under one manager, transcripts as real-shaped JSONL."""

    def __init__(self, n=5, **vkw):
        self.dir = tempfile.mkdtemp(prefix="rev-")
        self.root = fc.new_contract("g1", ["m1"], "goal", ["/r"], ACC, 1e9, "mgr")
        self.kids = []
        for i in range(1, n + 1):
            self.kids.append(fc.new_contract("g1", ["m1", "i%d" % i], "part", ["/r"], ACC, 1e9,
                                             "impl-%d" % i, parent=self.root))
        self.verify = fr.build_verify("rev", lambda c: c.owner_session, self.path,
                                      lambda: [k.contract_id for k in self.kids], **vkw)
        self.ledger = fc.Ledger(self.root, self.path, self.verify)
        for k in self.kids:
            self.ledger.add_child(k)
            open(self.path(k.owner_session), "a").close()
            w(self.path(k.owner_session), {"type": "user", "message": {
                "role": "user", "content": "do it\n" + k.done_token}})
            self.ledger.confirm_delivery(k.contract_id, k.done_token)

    def path(self, s):
        return os.path.join(self.dir, s + ".jsonl")

    def work(self, k, ok=True, edit_after=False, run_tests=True, done=True):
        p = self.path(k.owner_session)
        if run_tests:
            asst(p, use("t", "Bash", command="python3 -m pytest -q"))
            result(p, "t", "5 passed" if ok else "Exit code 1\n2 failed", err=not ok)
        if edit_after:
            asst(p, use("e", "Edit", file_path="/r/a.py"))
            result(p, "e", "edited")
        if done:
            asst(p, text("finished\n%s\n" % k.done_token))
        return fc.Report(k.contract_id, "transcript:" + p, "pass", "done")

    def close(self):
        shutil.rmtree(self.dir, ignore_errors=True)


class Base(unittest.TestCase):
    def fleet(self, **kw):
        f = Fleet(**kw)
        self.addCleanup(f.close)
        return f


class TranscriptReviewerTests(Base):
    def review(self, f, k, rep):
        return fr.TranscriptReviewer(f.path).review(k, rep)

    def test_clean_run_passes(self):
        f = self.fleet(n=1)
        k = f.kids[0]
        self.assertEqual(self.review(f, k, f.work(k)).outcome, fr.PASS)

    def test_report_says_pass_but_last_run_failed(self):
        f = self.fleet(n=1)
        k = f.kids[0]
        r = self.review(f, k, f.work(k, ok=False))
        self.assertEqual(r.outcome, fr.FAIL)
        self.assertIn("failed", r.note)

    def test_early_failure_then_later_pass_is_fine(self):
        f = self.fleet(n=1)
        k = f.kids[0]
        f.work(k, ok=False, done=False)
        self.assertEqual(self.review(f, k, f.work(k)).outcome, fr.PASS)

    def test_pass_then_failure_is_a_fail(self):
        f = self.fleet(n=1)
        k = f.kids[0]
        f.work(k, ok=True, done=False)
        self.assertEqual(self.review(f, k, f.work(k, ok=False)).outcome, fr.FAIL)

    def test_edit_after_last_run_is_stale_evidence(self):
        f = self.fleet(n=1)
        k = f.kids[0]
        r = self.review(f, k, f.work(k, edit_after=True))
        self.assertEqual(r.outcome, fr.FAIL)
        self.assertIn("edited after", r.note)

    def test_no_test_run_is_inconclusive_not_pass(self):
        f = self.fleet(n=1)
        k = f.kids[0]
        self.assertEqual(self.review(f, k, f.work(k, run_tests=False)).outcome, fr.INCONCLUSIVE)

    def test_non_test_bash_is_not_evidence(self):
        f = self.fleet(n=1)
        k = f.kids[0]
        p = f.path(k.owner_session)
        asst(p, use("x", "Bash", command="echo all tests pass"))
        result(p, "x", "all tests pass")
        self.assertEqual(self.review(f, k, f.work(k, run_tests=False)).outcome, fr.INCONCLUSIVE)

    def test_pointer_to_another_transcript_fails(self):
        f = self.fleet(n=2)
        a, b = f.kids
        f.work(b)
        rep = f.work(a, ok=False)
        lie = fc.Report(a.contract_id, "transcript:" + f.path(b.owner_session), "pass")
        self.assertEqual(self.review(f, a, lie).outcome, fr.FAIL)
        self.assertIn("pointer", self.review(f, a, lie).note)
        self.assertEqual(self.review(f, a, rep).outcome, fr.FAIL)

    def test_token_not_emitted_fails(self):
        f = self.fleet(n=1)
        k = f.kids[0]
        self.assertEqual(self.review(f, k, f.work(k, done=False)).outcome, fr.FAIL)

    def test_missing_transcript_inconclusive(self):
        f = self.fleet(n=1)
        k = f.kids[0]
        os.remove(f.path(k.owner_session))
        rep = fc.Report(k.contract_id, "transcript:" + f.path(k.owner_session), "pass")
        self.assertEqual(self.review(f, k, rep).outcome, fr.INCONCLUSIVE)


class TieredPolicyTests(Base):
    def test_contradiction_rejected_even_when_unsampled(self):
        calls = []
        f = self.fleet(n=5, deep=lambda c, r: calls.append(c) or True, rate=0.0, seed=1)
        liar = f.kids[4]
        sample = f.verify.sample()
        liar = next(k for k in f.kids if k.contract_id not in sample)   # guaranteed unsampled
        ok, why = f.ledger.receive(liar.contract_id, liar.done_token, f.work(liar, ok=False))
        self.assertEqual((ok, why), (True, "failed acceptance"))
        self.assertEqual(calls, [])                      # rejected on tier 1 alone

    def test_lying_child_caught_on_every_seed_where_sampling_missed_it(self):
        sampling_missed, tiered_missed = 0, 0
        for seed in range(50):
            f = self.fleet(n=5, rate=0.2, seed=seed, deep=lambda c, r: True)
            liar = f.kids[2]
            if liar.contract_id not in fg.spot_check([k.contract_id for k in f.kids], 0.2, seed):
                sampling_missed += 1
            f.ledger.receive(liar.contract_id, liar.done_token, f.work(liar, ok=False))
            if f.ledger.state[liar.contract_id]["verified"]:
                tiered_missed += 1
        self.assertGreater(sampling_missed, 30)          # sampling alone: a coin the liar wins
        self.assertEqual(tiered_missed, 0)

    def test_pass_unsampled_skips_deep(self):
        calls = []
        f = self.fleet(n=5, deep=lambda c, r: calls.append(c.contract_id) or True,
                       rate=0.2, seed=3)
        sample = set(f.verify.sample())
        for k in f.kids:
            f.ledger.receive(k.contract_id, k.done_token, f.work(k))
        self.assertEqual(set(calls), sample)
        self.assertLess(len(calls), 5)
        self.assertIsNotNone(f.ledger.done_token())

    def test_sampled_pass_deep_can_still_reject(self):
        f = self.fleet(n=3, deep=lambda c, r: False, rate=1.0, seed=1)
        k = f.kids[0]
        f.ledger.receive(k.contract_id, k.done_token, f.work(k))
        self.assertFalse(f.ledger.state[k.contract_id]["verified"])

    def test_inconclusive_without_deep_is_rejected(self):
        f = self.fleet(n=1)
        k = f.kids[0]
        f.ledger.receive(k.contract_id, k.done_token, f.work(k, run_tests=False))
        self.assertFalse(f.ledger.state[k.contract_id]["verified"])
        self.assertIsNone(f.ledger.done_token())

    def test_inconclusive_with_deep_is_decided_by_deep_even_unsampled(self):
        f = self.fleet(n=5, rate=0.0, seed=1, deep=lambda c, r: True)
        k = next(x for x in f.kids if x.contract_id not in f.verify.sample())
        f.ledger.receive(k.contract_id, k.done_token, f.work(k, run_tests=False))
        self.assertTrue(f.ledger.state[k.contract_id]["verified"])
        f2 = self.fleet(n=5, rate=0.0, seed=1, deep=lambda c, r: False)
        k2 = next(x for x in f2.kids if x.contract_id not in f2.verify.sample())
        f2.ledger.receive(k2.contract_id, k2.done_token, f2.work(k2, run_tests=False))
        self.assertFalse(f2.ledger.state[k2.contract_id]["verified"])

    def test_every_verdict_passes_through_a_review_gate(self):
        f = self.fleet(n=2)
        a, b = f.kids
        f.ledger.receive(a.contract_id, a.done_token, f.work(a))
        f.ledger.receive(b.contract_id, b.done_token, f.work(b, ok=False))
        ga, gb = f.verify.gates[a.contract_id], f.verify.gates[b.contract_id]
        self.assertEqual(ga.done_token(), a.done_token)
        self.assertTrue(gb.rejected)
        self.assertIsNone(gb.done_token())
        self.assertEqual(ga.verdict.reviewer, "rev")


class IndependenceTests(Base):
    def test_reviewer_equal_to_producer_refuses(self):
        f = self.fleet(n=1)
        f.verify.reviewer_session = "impl-1"
        k = f.kids[0]
        with self.assertRaises(fg.ReviewerNotIndependent):
            f.ledger.receive(k.contract_id, k.done_token, f.work(k))

    def test_same_model_refused_by_default(self):
        f = self.fleet(n=1, models=("opus", lambda c: "opus"))
        k = f.kids[0]
        with self.assertRaises(fg.ReviewerNotIndependent):
            f.ledger.receive(k.contract_id, k.done_token, f.work(k))
        self.assertIsNone(f.ledger.state[k.contract_id]["token"])

    def test_different_model_accepted(self):
        f = self.fleet(n=1, models=("opus", lambda c: "sonnet"))
        k = f.kids[0]
        f.ledger.receive(k.contract_id, k.done_token, f.work(k))
        self.assertTrue(f.ledger.state[k.contract_id]["verified"])

    def test_same_model_allowed_is_logged(self):
        f = self.fleet(n=1, models=("opus", lambda c: "opus"), allow_same_model=True)
        k = f.kids[0]
        f.ledger.receive(k.contract_id, k.done_token, f.work(k))
        self.assertTrue(f.ledger.state[k.contract_id]["verified"])
        self.assertTrue(any(e[1] == "same-model" for e in f.verify.log))

    def test_make_verify_model_check(self):
        c = fc.new_contract("g1", ["m1"], "g", ["/r"], ACC, 1e9, "impl")
        v = fg.make_verify("rev", lambda c: "impl", lambda c, r: True,
                           models=("m", lambda c: "m"))
        with self.assertRaises(fg.ReviewerNotIndependent):
            v(c, None)
        v = fg.make_verify("rev", lambda c: "impl", lambda c, r: True,
                           models=("m", lambda c: "n"))
        self.assertTrue(v(c, None))
        v = fg.make_verify("rev", lambda c: "impl", lambda c, r: True,
                           models=("m", lambda c: "m"), allow_same_model=True)
        self.assertTrue(v(c, None))


class LedgerLabelTests(Base):
    def reason(self, f, offered_by, token):
        p = f.path(offered_by.owner_session)
        asst(p, text(token))
        return f.ledger.receive(offered_by.contract_id, token, fc.Report(
            offered_by.contract_id, "transcript:" + p, "pass"))

    def test_real_sibling_is_labelled_sibling(self):
        f = self.fleet(n=2)
        a, b = f.kids
        self.assertEqual(self.reason(f, a, b.done_token), (False, "sibling chain"))

    def test_other_manager_is_foreign(self):
        f = self.fleet(n=1)
        a = f.kids[0]
        self.assertEqual(self.reason(f, a, fc.make_token("DONE", "g1", ["m9", "i1"])),
                         (False, "foreign chain"))

    def test_other_goal_is_generic(self):
        f = self.fleet(n=1)
        a = f.kids[0]
        self.assertEqual(self.reason(f, a, fc.make_token("DONE", "g2", ["m1", "i1"])),
                         (False, "token does not belong to this contract"))

    def test_manager_own_token(self):
        f = self.fleet(n=1)
        a = f.kids[0]
        self.assertEqual(self.reason(f, a, f.root.done_token), (False, "manager's own token"))


if __name__ == "__main__":
    unittest.main()
