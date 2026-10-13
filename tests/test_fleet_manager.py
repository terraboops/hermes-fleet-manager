#!/usr/bin/env python3
"""Tests for scripts/fleet_manager.py: a manager running one goal with 2-3 implementers
against fixture transcripts, overwatch issued by the manager, and the refusals.

Run:  python3 -m unittest discover -s tests -t .
"""
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, '..', 'scripts'))
sys.path.insert(0, os.path.join(HERE, '..'))

import fleet_contract as fc  # noqa: E402
import fleet_governance as fg  # noqa: E402
import fleet_manager as fm  # noqa: E402

ACC = [{"check": "tests pass", "positive": ["suite green"], "negative": ["forged token refused"]}]
BOUNDARY = ("/repo/src", "/repo/tests")


def user(text):
    return {"type": "user", "message": {"role": "user", "content": text}}


def asst(text):
    return {"type": "assistant",
            "message": {"role": "assistant", "content": [{"type": "text", "text": text}]}}


class Fleet:
    """Fixture world: sessions with a state, a growth time and a transcript file."""

    def __init__(self, tmp):
        self.tmp = tmp
        self.now = 10000.0
        self.state = {}
        self.growth = {}
        self.sent = []
        self.escalations = []
        self.passing = set()
        self.registry = fm.Registry()
        self.registry.register("mgr", fm.MANAGER, goal="g1")

    def path(self, s):
        return os.path.join(self.tmp, s + ".jsonl")

    def write(self, s, *records):
        with open(self.path(s), "a") as fh:
            for r in records:
                fh.write(json.dumps(r) + "\n")

    def dispatch(self, s, directive):
        self.state[s] = "working"
        self.growth[s] = self.now
        self.write(s, user(directive))   # a real user turn in the child

    def manager(self, units=3, **kw):
        own = fc.new_contract("g1", ["m1"], "ship the widget", BOUNDARY, ACC,
                              deadline=self.now + 10000, owner_session="mgr")
        check = lambda child, report: child.contract_id in self.passing
        verify = fg.make_verify("reviewer", lambda c: c.owner_session, check)
        return fm.Manager(
            session="mgr", registry=self.registry, own=own, budget=fm.Budget(units),
            dispatch=self.dispatch, send=lambda s, t: self.sent.append((s, t)),
            state_of=lambda s: self.state[s], last_growth=lambda s: self.growth[s],
            transcript_of=lambda s: self.path(s), verify=verify,
            escalate=lambda k, t: self.escalations.append((k, t)),
            clock=lambda: self.now, **kw)

    def assign(self, m, session, deadline=None, boundary=("/repo/src",)):
        return m.assign(session, "part for " + session, boundary, ACC,
                        deadline or self.now + 5000)

    def finish(self, m, c, ok=True):
        self.write(c.owner_session, asst("work done\n" + c.done_token))
        rep = fc.Report(c.contract_id, "transcript:" + self.path(c.owner_session), "pass", "ok")
        if ok:
            self.passing.add(c.contract_id)
        return m.receive(c.contract_id, rep)


class Base(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        self.w = Fleet(self._td.name)


class EndToEnd(Base):
    def test_goal_with_three_implementers_completes(self):
        m = self.w.manager()
        cs = [self.w.assign(m, "impl%d" % i) for i in range(3)]
        self.assertEqual([c.chain for c in cs], [("m1", "i1"), ("m1", "i2"), ("m1", "i3")])
        self.assertEqual(m.budget.left, 0)
        for c in cs:
            self.assertTrue(m.confirm(c.contract_id))
        self.assertEqual(m.result(), (None, None))        # nothing finished yet
        for c in cs[:2]:
            self.assertEqual(self.w.finish(m, c), (True, "verified"))
        self.assertEqual(m.result(), (None, None))        # two of three is not done
        self.w.finish(m, cs[2])
        tok, rep = m.result()
        self.assertEqual(tok, m.own.done_token)
        self.assertEqual(rep.verdict, "pass")

    def test_two_implementers(self):
        m = self.w.manager()
        cs = [self.w.assign(m, "a"), self.w.assign(m, "b")]
        for c in cs:
            self.w.finish(m, c)
        self.assertEqual(m.result()[0], m.own.done_token)

    def test_links_recorded_and_owner_immutable(self):
        m = self.w.manager()
        self.w.assign(m, "a")
        node = self.w.registry.nodes["a"]
        self.assertEqual(node["parent_session"], "mgr")
        self.assertEqual(node["lifecycle_owner"], "mgr")
        self.assertEqual(node["depth"], 2)
        self.w.registry.register("mgr2", fm.MANAGER)
        self.w.registry.reparent("a", "mgr2")             # reporting line may move
        self.assertEqual(self.w.registry.nodes["a"]["parent_session"], "mgr2")
        with self.assertRaises(fm.OwnerImmutable) as cm:
            self.w.registry.set_lifecycle_owner("a", "mgr2")
        self.assertIn("cannot change", str(cm.exception))
        self.assertEqual(self.w.registry.nodes["a"]["lifecycle_owner"], "mgr")

    def test_registry_persists(self):
        p = os.path.join(self.w.tmp, "h.json")
        r = fm.Registry(p)
        r.register("mgr", fm.MANAGER)
        r.register("a", fm.IMPLEMENTER, parent_session="mgr")
        again = fm.Registry(p)
        self.assertEqual(again.nodes["a"]["lifecycle_owner"], "mgr")


class Refusals(Base):
    def test_budget_exhaustion_rejected_with_clear_error(self):
        m = self.w.manager(units=2)
        self.w.assign(m, "a")
        self.w.assign(m, "b")
        with self.assertRaises(fm.BudgetExhausted) as cm:
            self.w.assign(m, "c")
        self.assertIn("2 of 2 units spent", str(cm.exception))
        self.assertNotIn("c", self.w.registry.nodes)       # nothing half-created
        self.assertNotIn("c", self.w.state)                # nothing dispatched

    def test_depth_cap_rejected(self):
        # a manager nested under a manager sits at depth 2; its child would be depth 3
        self.w.registry.register("sub", fm.MANAGER, parent_session="mgr")
        self.assertEqual(self.w.registry.nodes["sub"]["depth"], 2)
        top = self.w.manager().own
        own = fc.new_contract("g1", ["m1", "m2"], "sub goal", BOUNDARY, ACC, self.w.now + 9,
                              owner_session="sub", parent=top)
        sub = fm.Manager(session="sub", registry=self.w.registry, own=own,
                         budget=fm.Budget(5), dispatch=self.w.dispatch,
                         send=lambda *a: None, state_of=lambda s: "working",
                         last_growth=lambda s: 0, transcript_of=lambda s: None,
                         verify=lambda c, r: True)
        with self.assertRaises(fm.DepthCapExceeded) as cm:
            sub.assign("deep", "g", ("/repo/src",), ACC, self.w.now + 5)
        self.assertIn("exceeds cap 2", str(cm.exception))
        self.assertNotIn("deep", self.w.registry.nodes)

    def test_implementer_cannot_create_sessions(self):
        m = self.w.manager()
        self.w.assign(m, "a")
        with self.assertRaises(fm.DepthCapExceeded) as cm:
            self.w.registry.register("kid", fm.IMPLEMENTER, parent_session="a")
        self.assertIn("may not create sessions", str(cm.exception))

    def test_boundary_wider_than_manager_refused(self):
        m = self.w.manager()
        with self.assertRaises(fc.ContractError):
            self.w.assign(m, "a", boundary=("/etc",))
        self.assertEqual(m.budget.spent, 0)

    def test_unregistered_owner_and_parent_refused(self):
        with self.assertRaises(fm.HierarchyError):
            self.w.registry.register("x", fm.IMPLEMENTER, parent_session="ghost")
        with self.assertRaises(fm.HierarchyError):
            self.w.registry.register("y", fm.IMPLEMENTER)

    def test_drift_out_of_scope_flagged_not_absorbed(self):
        m = self.w.manager()
        self.assertTrue(m.intake("add test", ["/repo/tests/x.py"]))
        with self.assertRaises(fm.OutOfScope):
            m.intake("rewrite billing", ["/repo/billing/core.py"])
        self.assertEqual(len(m.flagged), 1)
        self.assertEqual(self.w.escalations[0][0], "out-of-scope")
        self.assertEqual(m.budget.spent, 0)                # nothing was dispatched
        with self.assertRaises(fm.OutOfScope):
            m.intake("unspecified", [])                    # no paths is not in scope


class Children(Base):
    def test_blocked_child_blocks_manager(self):
        m = self.w.manager()
        a, b = self.w.assign(m, "a"), self.w.assign(m, "b")
        self.w.finish(m, a)
        self.w.write("b", asst("stuck\n" + b.blocked_token))
        rep = fc.Report(b.contract_id, "transcript:" + self.w.path("b"), "blocked", "need API key")
        self.assertEqual(m.receive(b.contract_id, rep), (True, "blocked"))
        self.assertIsNone(m.ledger.done_token())
        tok, up = m.result()
        self.assertEqual(tok, m.own.blocked_token)
        self.assertEqual(up.verdict, "blocked")
        self.assertIn(b.contract_id, up.note)

    def test_done_claim_without_acceptance_not_counted(self):
        m = self.w.manager()
        a = self.w.assign(m, "a")
        ok, why = self.w.finish(m, a, ok=False)            # token emitted, reviewer says no
        self.assertEqual((ok, why), (True, "failed acceptance"))
        self.assertEqual(m.ledger.missing(), [a.contract_id])
        self.assertIsNone(m.ledger.done_token())
        self.assertEqual(m.result()[0], m.own.blocked_token)

    def test_done_token_never_emitted_is_rejected(self):
        m = self.w.manager()
        a = self.w.assign(m, "a")
        rep = fc.Report(a.contract_id, "transcript:" + self.w.path("a"), "pass", "ok")
        ok, why = m.receive(a.contract_id, rep)            # the token is only in the directive
        self.assertFalse(ok)
        self.assertIn("not emitted", why)
        self.assertIsNone(m.result()[0])

    def test_self_review_refused(self):
        m = self.w.manager()
        a = self.w.assign(m, "a")
        m.ledger.verify = fg.make_verify("a", lambda c: c.owner_session, lambda c, r: True)
        self.w.write("a", asst(a.done_token))
        rep = fc.Report(a.contract_id, "transcript:" + self.w.path("a"), "pass", "ok")
        with self.assertRaises(fg.ReviewerNotIndependent):
            m.receive(a.contract_id, rep)

    def test_stalled_child_gets_checkin_from_manager(self):
        m = self.w.manager(stall_window=360, stall_repeat=300)
        a = self.w.assign(m, "a")
        self.w.now += 200
        self.assertEqual(m.tick(), [])                     # quiet, but not long enough
        self.w.now += 200                                  # 400s of silence, state working
        acts = m.tick()
        self.assertEqual([x["kind"] for x in acts], ["checkin"])
        self.assertEqual(acts[0]["issuer"], "mgr")
        self.assertIn(a.blocked_token, self.w.sent[0][1])
        self.assertEqual(self.w.sent[0][0], "a")
        self.w.now += 100
        self.assertEqual(m.tick(), [])                     # repeat only after stall_repeat
        self.w.now += 250
        self.assertEqual(len(m.tick()), 1)
        self.w.growth["a"] = self.w.now                    # growth is the acknowledgement
        self.w.now += 10
        self.assertEqual(m.tick(), [])

    def test_idle_child_nudged_by_manager_and_escalated_after_cap(self):
        m = self.w.manager(nudge_interval=300, max_nudges=2)
        self.w.assign(m, "a")
        self.w.state["a"] = "idle"
        self.w.now += 400
        self.assertEqual([x["kind"] for x in m.tick()], ["nudge"])
        self.w.now += 100
        self.assertEqual(m.tick(), [])                     # too soon
        self.w.now += 300
        self.assertEqual([x["kind"] for x in m.tick()], ["nudge"])
        self.w.now += 400
        self.assertEqual(m.tick(), [])                     # cap reached: stop nagging
        self.assertEqual([k for k, _ in self.w.escalations], ["idle"])
        self.w.now += 400
        m.tick()
        self.assertEqual(len(self.w.escalations), 1)       # raised once, not per tick
        self.assertTrue(all(x["issuer"] == "mgr" for x in m.issued))

    def test_dead_child_escalated_not_nudged(self):
        m = self.w.manager()
        self.w.assign(m, "a")
        self.w.state["a"] = "dead"
        acts = m.tick()
        self.assertEqual([x["kind"] for x in acts], ["dead"])
        self.assertEqual(self.w.sent, [])
        self.assertEqual(self.w.escalations[0][0], "dead")

    def test_settled_child_is_not_nudged(self):
        m = self.w.manager()
        a = self.w.assign(m, "a")
        self.w.finish(m, a)
        self.w.state["a"] = "idle"
        self.w.now += 5000
        self.assertEqual(m.tick(), [])

    def test_deadline_passed_raises_checkin(self):
        m = self.w.manager()
        self.w.assign(m, "a", deadline=self.w.now + 100)
        self.w.growth["a"] = self.w.now + 500
        self.w.now += 500
        acts = m.tick()
        self.assertEqual([x["kind"] for x in acts], ["deadline"])


class DaemonHandoff(Base):
    def test_daemon_defers_for_managed_session_with_live_manager(self):
        self.w.assign(self.w.manager(), "a")
        self.assertTrue(fm.daemon_defers("a", self.w.registry, lambda m: True))

    def test_daemon_keeps_fallback_for_unmanaged_flat_session(self):
        self.assertFalse(fm.daemon_defers("flat", self.w.registry, lambda m: True))

    def test_daemon_resumes_when_manager_dead_or_unknown(self):
        self.w.assign(self.w.manager(), "a")
        self.assertFalse(fm.daemon_defers("a", self.w.registry, lambda m: False))
        def boom(m):
            raise RuntimeError("tmux gone")
        self.assertFalse(fm.daemon_defers("a", self.w.registry, boom))

    def test_manager_session_itself_stays_with_daemon(self):
        self.assertFalse(fm.daemon_defers("mgr", self.w.registry, lambda m: True))

    def test_fleet_watch_hook_reads_registry(self):
        import fleet_watch
        self.w.registry.path = os.path.join(self.w.tmp, "hier.json")
        self.w.assign(self.w.manager(), "a")
        cfg = lambda key, default: self.w.registry.path if key == "hierarchy_file" else default
        with mock.patch.object(fleet_watch, "_cfg", cfg), \
                mock.patch.object(fleet_watch, "load_registry", lambda: {"mgr": {}}), \
                mock.patch.object(fleet_watch, "_probe_liveness", lambda s, e: (True, "ALIVE")):
            self.assertTrue(fleet_watch._manager_owns("a"))
            self.assertFalse(fleet_watch._manager_owns("flat"))
        with mock.patch.object(fleet_watch, "_cfg", cfg), \
                mock.patch.object(fleet_watch, "load_registry", lambda: {}), \
                mock.patch.object(fleet_watch, "_probe_liveness", lambda s, e: (False, "dead")):
            self.assertFalse(fleet_watch._manager_owns("a"))   # manager dead -> daemon covers
        missing = lambda key, default: os.path.join(self.w.tmp, "nope.json")
        with mock.patch.object(fleet_watch, "_cfg", missing):
            self.assertFalse(fleet_watch._manager_owns("a"))   # no registry -> daemon covers


if __name__ == "__main__":
    unittest.main()
