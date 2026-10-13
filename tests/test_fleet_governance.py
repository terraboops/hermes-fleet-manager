#!/usr/bin/env python3
"""Tests for scripts/fleet_governance.py: authority narrowing, escalation lanes,
gate self-approval, reviewer independence, spot checks.

Run:  python3 -m unittest discover -s tests -t .
"""
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'scripts'))

import fleet_contract as fc  # noqa: E402
import fleet_governance as gv  # noqa: E402

ACC = [{"check": "tests pass", "positive": ["green"], "negative": ["forged refused"]}]


def levels():
    top = gv.Level("chief", top=True)
    mgr = gv.Level("manager", parent=top)
    impl = gv.Level("implementer", parent=mgr)
    return top, mgr, impl


class AuthorityNarrowing(unittest.TestCase):
    def setUp(self):
        self.parent = gv.Grant.of({"read", "write"}, ["/repo/a", "/repo/b"])

    def test_subset_is_granted(self):
        req = gv.Grant.of({"read"}, ["/repo/a/sub"])
        self.assertEqual(gv.grant_child(self.parent, req), req)

    def test_extra_permission_refused(self):
        with self.assertRaises(gv.WidenedAuthority):
            gv.grant_child(self.parent, gv.Grant.of({"read", "deploy"}, ["/repo/a"]))

    def test_wider_boundary_refused(self):
        with self.assertRaises(gv.WidenedAuthority):
            gv.grant_child(self.parent, gv.Grant.of({"read"}, ["/repo"]))

    def test_lookalike_prefix_refused(self):
        with self.assertRaises(gv.WidenedAuthority):
            gv.grant_child(self.parent, gv.Grant.of({"read"}, ["/repo/abc"]))

    def test_traversal_refused(self):
        with self.assertRaises(gv.GovernanceError):
            gv.Grant.of({"read"}, ["/repo/a/../../etc"])

    def test_empty_grant_refused(self):
        with self.assertRaises(gv.GovernanceError):
            gv.grant_child(self.parent, gv.Grant.of(set(), ["/repo/a"]))

    def test_effective_is_intersection_even_if_record_is_wide(self):
        wide_child = gv.Grant.of({"read", "write", "deploy"}, ["/repo", "/etc"])
        eff = gv.effective_grant([self.parent, wide_child])
        self.assertEqual(eff.permissions, frozenset({"read", "write"}))
        self.assertEqual(eff.boundary, ("/repo/a", "/repo/b"))
        self.assertFalse(gv.authorized([self.parent, wide_child], "deploy", "/repo/a"))
        self.assertFalse(gv.authorized([self.parent, wide_child], "read", "/etc/passwd"))
        self.assertTrue(gv.authorized([self.parent, wide_child], "write", "/repo/a/x.py"))

    def test_three_levels_only_narrow(self):
        mid = gv.grant_child(self.parent, gv.Grant.of({"read", "write"}, ["/repo/a"]))
        with self.assertRaises(gv.WidenedAuthority):
            gv.grant_child(mid, gv.Grant.of({"read"}, ["/repo/b"]))


class EscalationLanes(unittest.TestCase):
    def test_gate_from_implementer_reaches_human_through_manager(self):
        top, mgr, impl = levels()
        act, where = impl.route(gv.Event(gv.CLASSIFIER_BLOCK, "impl1", id="g1"), hour=12)
        self.assertEqual((act, where), (gv.DELIVER, gv.HUMAN))
        self.assertEqual(len(top.gates), 1)
        self.assertEqual(mgr.gates, [])

    def test_choice_ui_is_never_absorbed(self):
        top, mgr, impl = levels()
        self.assertEqual(mgr.route(gv.Event(gv.CHOICE_UI, "impl1", id="c1"), 12)[0], gv.DELIVER)

    def test_manager_cannot_approve_own_teams_gate(self):
        top, mgr, impl = levels()
        impl.route(gv.Event(gv.CLASSIFIER_BLOCK, "impl1", id="g1"), 12)
        for who in ("manager", "impl1", "chief", "agent"):
            with self.assertRaises(gv.SelfApprovalError):
                top.approve(who, "g1", "allow")
        self.assertIsNone(top.gates[0].resolved_by)

    def test_human_can_resolve_once(self):
        top, mgr, impl = levels()
        impl.route(gv.Event(gv.CHOICE_UI, "impl1", id="c1"), 12)
        top.approve(gv.HUMAN, "c1", "option 2")
        self.assertEqual(top.gates[0].answer, "option 2")
        with self.assertRaises(gv.GovernanceError):
            top.approve(gv.HUMAN, "c1", "option 1")

    def test_gate_in_quiet_hours_is_held_not_resolved(self):
        top, mgr, impl = levels()
        act, _ = impl.route(gv.Event(gv.CLASSIFIER_BLOCK, "impl1", id="g1"), hour=2)
        self.assertEqual(act, gv.HOLD)
        self.assertIsNone(top.gates[0].resolved_by)

    def test_receipt_absorbed_below_dropped_at_top(self):
        top, mgr, impl = levels()
        self.assertEqual(impl.route(gv.Event(gv.RECEIPT, "i"), 12)[0], gv.ABSORB)
        self.assertEqual(top.route(gv.Event(gv.RECEIPT, "i"), 12)[0], gv.DROP)
        self.assertEqual(top.delivered, [])

    def test_blocked_passes_up_from_below(self):
        top, mgr, impl = levels()
        self.assertEqual(impl.route(gv.Event(gv.BLOCKED, "i"), 12)[0], gv.DELIVER)

    def test_quiet_hours_only_apply_at_top(self):
        top, mgr, impl = levels()
        # Receipts absorbed below at 02:00 with no hold; no quiet-hours state below.
        self.assertEqual(mgr.route(gv.Event(gv.RECEIPT, "i"), hour=2)[0], gv.ABSORB)
        self.assertEqual(mgr.held, [])
        self.assertEqual(impl.held, [])
        # Same news is held only once it reaches the top.
        self.assertEqual(impl.route(gv.Event(gv.BLOCKED, "i"), hour=2)[0], gv.HOLD)
        self.assertEqual(len(top.held), 1)
        self.assertEqual(mgr.held, [])

    def test_held_released_after_quiet_hours(self):
        top, mgr, impl = levels()
        impl.route(gv.Event(gv.BLOCKED, "i"), hour=2)
        self.assertEqual(top.release_held(hour=3), [])
        self.assertEqual(len(top.release_held(hour=6)), 1)

    def test_stopped_rule_only_at_top(self):
        top, mgr, impl = levels()
        short = gv.Event(gv.STOPPED, "i", stopped_seconds=300)
        long_ = gv.Event(gv.STOPPED, "i", stopped_seconds=601)
        self.assertEqual(impl.route(short, 12)[0], gv.DROP)
        self.assertEqual(impl.route(long_, 12)[0], gv.DELIVER)

    def test_orphan_level_refuses(self):
        orphan = gv.Level("orphan")
        with self.assertRaises(gv.GovernanceError):
            orphan.route(gv.Event(gv.BLOCKED, "i"), 12)

    def test_quiet_hour_boundaries(self):
        self.assertTrue(gv.in_quiet_hours(23))
        self.assertTrue(gv.in_quiet_hours(4))
        self.assertFalse(gv.in_quiet_hours(5))
        self.assertFalse(gv.in_quiet_hours(22))


class ReviewIndependence(unittest.TestCase):
    P = gv.Candidate("s-prod", "model-a")

    def test_never_picks_producer(self):
        got = gv.pick_reviewer(self.P, [self.P, gv.Candidate("s-prod", "model-b"),
                                        gv.Candidate("s-rev", "model-a")])
        self.assertEqual(got.session, "s-rev")

    def test_prefers_different_model(self):
        got = gv.pick_reviewer(self.P, [gv.Candidate("s1", "model-a"),
                                        gv.Candidate("s2", "model-b")])
        self.assertEqual(got.session, "s2")

    def test_same_model_fallback_is_still_different_session(self):
        got = gv.pick_reviewer(self.P, [gv.Candidate("s1", "model-a")])
        self.assertNotEqual(got.session, self.P.session)

    def test_only_producer_available_refuses(self):
        with self.assertRaises(gv.ReviewerNotIndependent):
            gv.pick_reviewer(self.P, [self.P])

    def test_producer_verdict_refused(self):
        rg = gv.ReviewGate("c1", "s-prod", "DONE-g1-i1-1")
        with self.assertRaises(gv.ReviewerNotIndependent):
            rg.submit(gv.Verdict("c1", "s-prod", True))
        self.assertIsNone(rg.done_token())

    def test_rejection_blocks_done_token(self):
        rg = gv.ReviewGate("c1", "s-prod", "DONE-g1-i1-1")
        rg.submit(gv.Verdict("c1", "s-rev", False, "negative case missing"))
        self.assertTrue(rg.rejected)
        self.assertIsNone(rg.done_token())

    def test_no_verdict_no_token(self):
        self.assertIsNone(gv.ReviewGate("c1", "s-prod", "DONE-g1-i1-1").done_token())

    def test_pass_releases_token(self):
        rg = gv.ReviewGate("c1", "s-prod", "DONE-g1-i1-1")
        rg.submit(gv.Verdict("c1", "s-rev", True))
        self.assertEqual(rg.done_token(), "DONE-g1-i1-1")

    def test_rejection_can_be_overturned_only_by_new_independent_pass(self):
        rg = gv.ReviewGate("c1", "s-prod", "DONE-g1-i1-1")
        rg.submit(gv.Verdict("c1", "s-rev", False))
        with self.assertRaises(gv.ReviewerNotIndependent):
            rg.submit(gv.Verdict("c1", "s-prod", True))
        self.assertIsNone(rg.done_token())

    def test_wrong_contract_verdict_refused(self):
        rg = gv.ReviewGate("c1", "s-prod", "DONE-g1-i1-1")
        with self.assertRaises(gv.GovernanceError):
            rg.submit(gv.Verdict("c2", "s-rev", True))


class SpotCheck(unittest.TestCase):
    def test_nonempty_set_always_samples_at_least_one(self):
        self.assertEqual(len(gv.spot_check(["a", "b", "c"], rate=0.0, seed=1)), 1)

    def test_empty_samples_nothing(self):
        self.assertEqual(gv.spot_check([], seed=1), [])

    def test_rate_one_checks_all(self):
        self.assertEqual(gv.spot_check(["a", "b", "c"], rate=1.0, seed=1), ["a", "b", "c"])

    def test_deterministic_with_seed_and_subset(self):
        ids = ["c%d" % i for i in range(10)]
        a = gv.spot_check(ids, rate=0.3, seed=7)
        self.assertEqual(a, gv.spot_check(ids, rate=0.3, seed=7))
        self.assertEqual(len(a), 3)
        self.assertTrue(set(a) <= set(ids))

    def test_bad_rate_refused(self):
        with self.assertRaises(gv.GovernanceError):
            gv.spot_check(["a"], rate=1.5)


def _line(role, text):
    content = text if role == "user" else [{"type": "text", "text": text}]
    return {"type": role, "message": {"role": role, "content": content}}


class LedgerIntegration(unittest.TestCase):
    """The reviewer plugs into fleet_contract.Ledger; a rejection blocks the manager."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.mgr = fc.new_contract("g1", ["m1"], "goal", ["/repo"], ACC, 9e9,
                                   owner_session="s-mgr")
        self.kid = fc.new_contract("g1", ["m1", "i1"], "part", ["/repo/a"], ACC, 9e9,
                                   owner_session="s-impl", parent=self.mgr)
        self.path = os.path.join(self.tmp.name, "impl.jsonl")
        with open(self.path, "w") as fh:
            fh.write(json.dumps(_line("user", "do it ACK-1")) + "\n")
            fh.write(json.dumps(_line("assistant", self.kid.done_token)) + "\n")

    def ledger(self, verify):
        led = fc.Ledger(self.mgr, lambda s: self.path, verify)
        led.add_child(self.kid)
        led.confirm_delivery(self.kid.contract_id, "ACK-1")
        return led

    def report(self):
        return fc.Report(self.kid.contract_id, "artifact:x", "pass")

    def test_reviewer_rejection_blocks_manager_done(self):
        verify = gv.make_verify("s-rev", lambda c: c.owner_session, lambda c, r: False)
        led = self.ledger(verify)
        led.receive(self.kid.contract_id, self.kid.done_token, self.report())
        self.assertIsNone(led.done_token())
        self.assertEqual(led.blocked_token(), self.mgr.blocked_token)

    def test_independent_pass_lets_manager_finish(self):
        verify = gv.make_verify("s-rev", lambda c: c.owner_session, lambda c, r: True)
        led = self.ledger(verify)
        led.receive(self.kid.contract_id, self.kid.done_token, self.report())
        self.assertEqual(led.done_token(), self.mgr.done_token)

    def test_producer_as_reviewer_cannot_verify(self):
        verify = gv.make_verify("s-impl", lambda c: c.owner_session, lambda c, r: True)
        led = self.ledger(verify)
        with self.assertRaises(gv.ReviewerNotIndependent):
            led.receive(self.kid.contract_id, self.kid.done_token, self.report())
        self.assertIsNone(led.done_token())


if __name__ == "__main__":
    unittest.main()
