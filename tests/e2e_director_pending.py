#!/usr/bin/env python3
"""Failing-first e2e scenarios for the director layer (depth 3).

Design: docs/director-layer.md. Run explicitly:

    python3 tests/e2e_director_pending.py

The file name does not match `test*.py` on purpose: `unittest discover` must stay green
while the layer is unbuilt. When the layer lands, rename this file to
tests/test_e2e_director.py and change the depth-cap scenario in test_e2e_hierarchy.py
(depth three becomes depth four).

Every scenario is expected to FAIL today. A scenario that passes before the build tests
nothing and gets rewritten.
"""
import os
import shutil
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, '..', 'scripts'))
sys.path.insert(0, os.path.join(HERE, '..'))
sys.path.insert(0, HERE)

import fleet_contract as fc  # noqa: E402
import fleet_governance as fg  # noqa: E402
import fleet_manager as fm  # noqa: E402
from fleet_chief import ChiefOfStaff  # noqa: E402
from test_e2e_hierarchy import ACC, Session  # noqa: E402

BOUNDARY = ("/repo/src", "/repo/tests")


class DirFleet(object):
    """One chief, one department, a director. Effects are scripted."""

    def __init__(self, dept_units=4, mgr_units=3, hour=12):
        self.dir = tempfile.mkdtemp(prefix="e2e-dir-")
        self.t = 1000.0
        self.sessions, self.states, self.sent, self.escalated = {}, {}, [], []
        self.mgr_units = mgr_units
        self.cos = ChiefOfStaff(hour, registry=fm.Registry(max_depth=3))
        self.reg = self.cos.registry
        self.director = self.cos.take_department(
            "g41", "ship the widget", BOUNDARY, ACC, self.t + 3600, dept_units,
            "dir-d1", self.effects(), clock=lambda: self.t)

    def close(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def effects(self):
        return dict(dispatch=self._dispatch, send=lambda s, x: self.sent.append((s, x)),
                    state_of=lambda s: self.states.get(s, "working"),
                    last_growth=lambda s: self.t, transcript_of=self.transcript_of,
                    verify=lambda c, r: True, escalate=self._escalate)

    def transcript_of(self, s):
        sess = self.sessions.get(s)
        return sess.path if sess else None

    def _dispatch(self, session, directive):
        if session not in self.sessions:
            self.sessions[session] = Session(os.path.join(self.dir, session + ".jsonl"))
        self.sessions[session].user(directive)

    def _escalate(self, kind, text):
        self.escalated.append((kind, text))

    def manager(self, session, boundary=BOUNDARY, goal="sub goal"):
        return self.director.assign_manager(
            session, goal, boundary, ACC, self.t + 3000, self.mgr_units, self.effects())


class Base(unittest.TestCase):
    units = 4

    def setUp(self):
        self.f = DirFleet(dept_units=self.units)

    def tearDown(self):
        self.f.close()


class Depth(Base):
    def test_director_role_exists_and_cap_is_three(self):
        self.assertEqual(fm.ROLE_DEPTH[fm.DIRECTOR], 1)
        self.assertEqual(self.f.reg.nodes["dir-d1"]["depth"], 1)
        m = self.f.manager("mgr-m1")
        self.assertEqual(self.f.reg.nodes["mgr-m1"]["depth"], 2)
        c = m.assign("impl-1", "part", BOUNDARY, ACC, self.f.t + 99)
        self.assertEqual(self.f.reg.nodes["impl-1"]["depth"], 3)
        self.assertEqual(c.chain, ("d1", "m1", "i1"))

    def test_depth_four_refused_everywhere(self):
        m = self.f.manager("mgr-m1")
        m.assign("impl-1", "part", BOUNDARY, ACC, self.f.t + 99)
        with self.assertRaises(fm.DepthCapExceeded):
            self.f.reg.register("impl-1-kid", fm.IMPLEMENTER, parent_session="impl-1")
        with self.assertRaises(fm.DepthCapExceeded):
            self.f.reg.register("mgr-deep", fm.MANAGER, parent_session="impl-1")
        with self.assertRaises(fm.DepthCapExceeded):
            self.f.reg.register("dir-d2", fm.DIRECTOR, parent_session="dir-d1")
        with self.assertRaises(fc.ContractError):
            fc.new_contract("g41", ["d1", "m1", "i1", "x1"], "g", BOUNDARY, ACC, 9,
                            max_depth=3)

    def test_manager_to_manager_dispatch_refused_director_allowed(self):
        m = self.f.manager("mgr-m1")
        with self.assertRaises(fm.HierarchyError):
            m.assign_manager("mgr-peer", "x", BOUNDARY, ACC, self.f.t + 9, 1,
                             self.f.effects())
        self.assertNotIn("mgr-peer", self.f.reg.nodes)
        self.assertIn("mgr-m1", self.f.reg.nodes)     # the director's own dispatch worked

    def test_default_registry_cap_stays_two(self):
        reg = fm.Registry()
        self.assertEqual(reg.max_depth, 2)
        with self.assertRaises(fm.HierarchyError):
            reg.register("d", fm.DIRECTOR)    # no director unless the cap of 3 is opted in


class DepartmentBudget(Base):
    def test_department_cap_binds_before_manager_cap(self):
        a = self.f.manager("mgr-a", boundary=("/repo/src",))
        b = self.f.manager("mgr-b", boundary=("/repo/tests",))
        self.assertIs(a.budget.parent, self.f.director.budget)   # 3 + 3 > 4: shared pool
        for i in range(3):
            a.assign("a-%d" % i, "p", ("/repo/src",), ACC, self.f.t + 99)
        b.assign("b-0", "p", ("/repo/tests",), ACC, self.f.t + 99)
        self.assertEqual(self.f.director.budget.spent, 4)
        with self.assertRaises(fm.BudgetExhausted) as cm:
            b.assign("b-1", "p", ("/repo/tests",), ACC, self.f.t + 99)
        self.assertIn("department", str(cm.exception))
        self.assertNotIn("b-1", self.f.reg.nodes)                # nothing half-created
        self.assertNotIn("b-1", self.f.sessions)
        self.assertEqual(b.budget.spent, 1)                      # no partial roll-up

    def test_failed_spend_rolls_nothing_up(self):
        a = self.f.manager("mgr-a", boundary=("/repo/src",))
        with self.assertRaises(fm.BudgetExhausted):
            a.budget.spend(99)
        self.assertEqual(self.f.director.budget.spent, 0)

    def test_only_the_human_raises_a_cap(self):
        a = self.f.manager("mgr-a", boundary=("/repo/src",))
        with self.assertRaises(fm.HierarchyError):
            a.budget.raise_cap(10, principal="mgr-a")
        with self.assertRaises(fm.HierarchyError):
            self.f.director.budget.raise_cap(10, principal="dir-d1")
        self.f.director.budget.raise_cap(10, principal=fg.HUMAN)
        self.assertEqual(self.f.director.budget.units, 10)


class Rulings(Base):
    def setUp(self):
        Base.setUp(self)
        import fleet_rulings
        self.fr = fleet_rulings
        self.store = fleet_rulings.RulingStore(clock=lambda: self.f.t)
        self.f.director.rulings = self.store

    def _add(self, topic="naming:scripts-prefix", decision="use fleet_ prefix", ttl=100):
        return self.store.add(fg.HUMAN, topic, decision, expires=self.f.t + ttl)

    def test_applied_and_cited_with_audit_trail(self):
        r = self._add()
        out = self.f.director.rule("naming:scripts-prefix", asked_by="mgr-m1")
        self.assertEqual((out.decision, out.ruling_id), ("use fleet_ prefix", r.ruling_id))
        self.assertEqual(self.f.escalated, [])                   # the human was not asked
        entry = self.store.audit[-1]
        self.assertEqual((entry["ruling_id"], entry["applied_by"], entry["asked_by"]),
                         (r.ruling_id, "dir-d1", "mgr-m1"))
        self.assertIn(r.ruling_id, out.cite())

    def test_expired_ruling_is_not_applied_and_goes_to_the_human(self):
        self._add(ttl=10)
        self.f.t += 11
        self.assertIsNone(self.f.director.rule("naming:scripts-prefix", asked_by="mgr-m1"))
        self.assertEqual([k for k, _ in self.f.escalated], ["needs-ruling"])
        self.assertEqual(self.store.audit[-1]["result"], "expired")

    def test_no_matching_ruling_escalates_never_guesses(self):
        self.assertIsNone(self.f.director.rule("unknown:topic", asked_by="mgr-m1"))
        self.assertEqual([k for k, _ in self.f.escalated], ["needs-ruling"])

    def test_conflicting_rulings_escalate_not_resolved_by_director(self):
        self._add(decision="use fleet_ prefix")
        self._add(decision="use hfm_ prefix")
        self.assertIsNone(self.f.director.rule("naming:scripts-prefix", asked_by="mgr-m1"))
        self.assertEqual([k for k, _ in self.f.escalated], ["ruling-conflict"])
        self.assertEqual(self.store.audit[-1]["result"], "conflict")

    def test_agent_cannot_write_edit_or_extend_a_ruling(self):
        r = self._add()
        for who in ("dir-d1", "mgr-m1", "chief"):
            with self.assertRaises(self.fr.RulingError):
                self.store.add(who, "t", "d", expires=self.f.t + 9)
            with self.assertRaises(self.fr.RulingError):
                self.store.edit(who, r.ruling_id, decision="x")
            with self.assertRaises(self.fr.RulingError):
                self.store.extend(who, r.ruling_id, self.f.t + 999)
        self.assertEqual(self.store.get(r.ruling_id).decision, "use fleet_ prefix")

    def test_ruling_without_expiry_is_refused(self):
        with self.assertRaises(self.fr.RulingError):
            self.store.add(fg.HUMAN, "t", "d", expires=None)

    def test_a_ruling_can_never_resolve_a_gate(self):
        for kind in fg.GATE_KINDS:
            with self.assertRaises(self.fr.RulingError):
                self.store.add(fg.HUMAN, "gate:" + kind, "approve", expires=self.f.t + 9)
        ev = fg.Event(fg.CLASSIFIER_BLOCK, "impl-1", "deploy --prod blocked", id="ev1")
        with self.assertRaises(self.fr.RulingError):
            self.f.director.rule_event(ev)


class Arbitration(Base):
    units = 8

    def test_overlapping_boundaries_refused_until_arbitrated(self):
        self.f.manager("mgr-a", boundary=("/repo/src",))
        with self.assertRaises(fm.BoundaryOverlap) as cm:
            self.f.manager("mgr-b", boundary=("/repo/src/core", "/repo/tests"))
        self.assertIn("/repo/src/core", str(cm.exception))
        self.assertNotIn("mgr-b", self.f.reg.nodes)

    def test_arbitration_gives_the_overlap_to_one_manager_and_records_it(self):
        self.f.manager("mgr-a", boundary=("/repo/src",))
        out = self.f.director.arbitrate(
            "mgr-b", boundary=("/repo/src/core", "/repo/tests"), owner="mgr-a",
            reason="core is the refactor", goal="sub goal", acceptance=ACC,
            deadline=self.f.t + 3000, units=2, effects=self.f.effects())
        self.assertEqual(out["owner"], "mgr-a")
        self.assertEqual(tuple(self.f.director.boundary_of("mgr-b")), ("/repo/tests",))
        self.assertEqual(self.f.director.arbitrations[-1]["overlap"], ["/repo/src/core"])

    def test_no_owner_named_goes_to_the_human(self):
        self.f.manager("mgr-a", boundary=("/repo/src",))
        with self.assertRaises(fm.BoundaryOverlap):
            self.f.director.arbitrate("mgr-b", boundary=("/repo/src",), owner=None,
                                      reason="", goal="g", acceptance=ACC,
                                      deadline=self.f.t + 9, units=1,
                                      effects=self.f.effects())
        self.assertEqual([k for k, _ in self.f.escalated], ["boundary-conflict"])

    def test_manager_cannot_arbitrate_for_itself(self):
        a = self.f.manager("mgr-a", boundary=("/repo/src",))
        with self.assertRaises(fm.HierarchyError):
            a.arbitrate("mgr-b", boundary=("/repo/src",), owner="mgr-a", reason="mine")


class Reviewers(Base):
    units = 8

    def setUp(self):
        Base.setUp(self)
        self.a = self.f.manager("mgr-a", boundary=("/repo/src",))
        self.b = self.f.manager("mgr-b", boundary=("/repo/tests",))
        self.a.assign("a-1", "p", ("/repo/src",), ACC, self.f.t + 99)
        self.b.assign("b-1", "p", ("/repo/tests",), ACC, self.f.t + 99)
        self.pool = self.f.director.reviewer_pool
        for s in ("rev-1", "rev-2"):
            self.pool.add(s, model="m-x")

    def test_one_pool_serves_both_managers(self):
        self.assertIn(self.pool.pick("a-1"), ("rev-1", "rev-2"))
        self.assertIn(self.pool.pick("b-1"), ("rev-1", "rev-2"))

    def test_producer_and_its_whole_chain_are_never_reviewers(self):
        for s in ("a-1", "mgr-a", "dir-d1"):
            self.pool.add(s, model="m-y")
        for _ in range(20):
            self.assertNotIn(self.pool.pick("a-1"), ("a-1", "mgr-a", "dir-d1"))

    def test_other_managers_subtree_is_independent_of_the_producer(self):
        self.assertTrue(self.pool.is_independent("b-1", producer="a-1"))
        self.assertFalse(self.pool.is_independent("mgr-a", producer="a-1"))
        self.assertFalse(self.pool.is_independent("dir-d1", producer="a-1"))

    def test_no_independent_candidate_means_no_review_not_a_self_review(self):
        self.pool.members.clear()
        self.pool.add("a-1", model="m-x")
        self.pool.add("mgr-a", model="m-x")
        with self.assertRaises(fg.ReviewerNotIndependent):
            self.pool.pick("a-1")

    def test_ledger_refuses_a_verdict_from_the_managers_own_chain(self):
        cid = next(iter(self.a.ledger.children))
        child = self.a.ledger.children[cid]
        verify = self.f.director.verify_for(self.a)
        with self.assertRaises(fg.ReviewerNotIndependent):
            verify.as_reviewer("mgr-a")(child, None)


class GatesAndQuietHours(Base):
    units = 8

    def setUp(self):
        Base.setUp(self)
        self.m = self.f.manager("mgr-a", boundary=("/repo/src",))
        self.m.assign("a-1", "p", ("/repo/src",), ACC, self.f.t + 99)
        self.impl = fg.Level("a-1", parent=self.m.level)

    def test_level_chain_is_implementer_manager_director_chief(self):
        self.assertIs(self.m.level.parent, self.f.director.level)
        self.assertIs(self.f.director.level.parent, self.f.cos.level)
        self.assertTrue(self.f.cos.level.top)
        self.assertFalse(self.f.director.level.top)

    def test_classifier_block_crosses_three_levels_unchanged_and_once(self):
        ev = fg.Event(fg.CLASSIFIER_BLOCK, "a-1", "deploy --prod blocked", id="ev1")
        self.assertEqual(self.impl.route(ev, 12), (fg.DELIVER, fg.HUMAN))
        for lvl in (self.impl, self.m.level, self.f.director.level):
            self.assertEqual((lvl.gates, lvl.delivered, lvl.held), ([], [], []))
        self.assertEqual(len(self.f.cos.level.gates), 1)
        self.assertIs(self.f.cos.level.gates[0].event, ev)
        self.assertEqual(len(self.f.cos.level.delivered), 1)

    def test_nobody_in_the_chain_can_approve_it(self):
        ev = fg.Event(fg.CHOICE_UI, "a-1", "pick a branch", id="ev2")
        self.impl.route(ev, 12)
        for who in ("a-1", "mgr-a", "dir-d1", "chief", "rev-1"):
            with self.assertRaises(fg.SelfApprovalError):
                self.f.cos.level.approve(who, "ev2", "yes")
        self.f.cos.level.approve(fg.HUMAN, "ev2", "main")

    def test_director_never_applies_quiet_hours(self):
        ev = fg.Event(fg.CLASSIFIER_BLOCK, "a-1", "blocked", id="ev3")
        self.assertEqual(self.impl.route(ev, 2)[0], fg.HOLD)
        self.assertEqual(self.f.director.level.held, [])         # held only at the top
        self.assertEqual(len(self.f.cos.level.held), 1)
        self.assertEqual(len(self.f.cos.level.release_held(9)), 1)

    def test_stopped_threshold_lives_only_at_the_top(self):
        short = fg.Event(fg.STOPPED, "a-1", "idle 5 min", stopped_seconds=300)
        long_ = fg.Event(fg.STOPPED, "a-1", "idle 15 min", stopped_seconds=900)
        self.assertEqual(self.impl.route(short, 12)[0], fg.DROP)
        self.assertEqual(self.f.director.level.seen, [short])    # passed up, not filtered
        self.assertEqual(self.impl.route(long_, 12)[0], fg.DELIVER)

    def test_receipts_absorbed_at_the_first_level(self):
        ev = fg.Event(fg.RECEIPT, "a-1", "ACK-OK")
        self.assertEqual(self.impl.route(ev, 12)[0], fg.ABSORB)
        self.assertEqual(self.f.director.level.delivered, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
