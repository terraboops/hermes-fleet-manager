#!/usr/bin/env python3
"""Tests for scripts/fleet_hier.py: event routing, persistence, daemon hook, flat path.

Run:  python3 -m unittest discover -s tests -t .
"""
import json
import os
import re
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, '..', 'scripts'))
sys.path.insert(0, os.path.join(HERE, '..'))

TMP = tempfile.mkdtemp()
os.environ["FLEET_HIER_DIR"] = os.path.join(TMP, "h")
os.environ["FLEET_HIER_REGISTRY"] = os.path.join(TMP, "h", "hierarchy.json")

import fleet_governance as fg  # noqa: E402
import fleet_hier as fh  # noqa: E402
import fleet_manager as fm  # noqa: E402

BLOCK = "NEEDS-INPUT-I1: blocked by the auto-mode classifier [Production Deploy] deploy to prod"


def reg():
    r = fm.Registry(fh.REGISTRY_PATH)
    r.nodes = {}
    r.register("mgr", fm.MANAGER, goal="g1")
    r.register("imp", fm.IMPLEMENTER, parent_session="mgr", goal="g1")
    return r


class Routing(unittest.TestCase):
    def setUp(self):
        os.makedirs(fh.BASE, exist_ok=True)
        for f in ("gates.json", "events.jsonl"):
            p = os.path.join(fh.BASE, f)
            if os.path.exists(p):
                os.unlink(p)
        self.reg = reg()

    def test_flat_session_is_untouched(self):
        self.assertIsNone(fh.route_event("flat-session", BLOCK, hour=12, registry=self.reg))
        self.assertFalse(os.path.exists(os.path.join(fh.BASE, "events.jsonl")))

    def test_classifier_block_rises_to_the_human(self):
        r = fh.route_event("imp", BLOCK, hour=12, registry=self.reg, queue=False)
        self.assertEqual(r["action"], fg.DELIVER)
        self.assertEqual(r["trail"], ["imp", "mgr", "chief"])
        gates = fh._load_gates()
        self.assertEqual(len(gates), 1)
        self.assertIsNone(gates[0].resolved_by)

    def test_classifier_block_at_night_is_held_not_resolved(self):
        r = fh.route_event("imp", BLOCK, hour=2, registry=self.reg, queue=False)
        self.assertEqual(r["action"], fg.HOLD)
        self.assertIsNone(fh._load_gates()[0].resolved_by)

    def test_repeat_block_does_not_stack_gates(self):
        fh.route_event("imp", BLOCK, hour=12, registry=self.reg, queue=False)
        r = fh.route_event("imp", BLOCK, hour=12, registry=self.reg, queue=False)
        self.assertEqual(r["action"], fg.HOLD)
        self.assertEqual(len(fh._load_gates()), 1)

    def test_only_the_human_resolves(self):
        r = fh.route_event("imp", BLOCK, hour=12, registry=self.reg, queue=False)
        for who in ("imp", "mgr", "chief", "reviewer"):
            self.assertEqual(fh.main(["approve", r["id"], "--as", who, "--answer", "yes"]), 1)
            self.assertIsNone(fh._load_gates()[0].resolved_by)
        self.assertEqual(fh.main(["approve", r["id"], "--as", "human", "--answer", "yes"]), 0)
        self.assertEqual(fh._load_gates()[0].resolved_by, "human")

    def test_receipt_is_dropped_and_stall_under_ten_minutes_is_not_news(self):
        r = fh.route_event("imp", "ACK-OK imp abc", hour=12, registry=self.reg, queue=False)
        self.assertEqual(r["action"], fg.ABSORB)
        r = fh.route_event("mgr", "STALL-MGR: no new transcript output for 300s (alive)",
                           hour=12, registry=self.reg, queue=False)
        self.assertIn(r["action"], (fg.UP, fg.DROP, fg.ABSORB))
        self.assertNotEqual(r["action"], fg.DELIVER)
        r = fh.route_event("mgr", "STALL-MGR: no new transcript output for 900s (alive)",
                           hour=12, registry=self.reg, queue=False)
        self.assertEqual(r["action"], fg.DELIVER)

    def test_unclassified_text_has_no_hierarchy_meaning(self):
        self.assertIsNone(fh.route_event("imp", "hello", hour=12, registry=self.reg))

    def test_routing_error_never_swallows(self):
        class Boom(object):
            nodes = property(lambda s: (_ for _ in ()).throw(RuntimeError("x")))
        self.assertIsNone(fh.route_event("imp", BLOCK, hour=12, registry=Boom()))

    def test_queue_writes_a_deliver_line_once(self):
        fh.route_event("imp", BLOCK, hour=12, registry=self.reg)
        self.assertEqual(len(fh.read_new_events("t1")), 1)
        self.assertEqual(fh.read_new_events("t1"), [])


class Held(unittest.TestCase):
    def test_held_escalation_survives_until_quiet_hours_end(self):
        from unittest import mock
        r = reg()
        for f in ("events.jsonl", "held.json", "offset-h1"):
            p = os.path.join(fh.BASE, f)
            if os.path.exists(p):
                os.unlink(p)
        fh.route_event("mgr", "HIER-CHILD-BLOCKED g1: x", hour=2, registry=r, force_kind=fg.NEEDS_INPUT)
        with mock.patch.object(fh.time, "localtime", return_value=mock.Mock(tm_hour=2)):
            self.assertEqual(fh.read_new_events("h1"), [])
        with mock.patch.object(fh.time, "localtime", return_value=mock.Mock(tm_hour=9)):
            got = fh.read_new_events("h1")
        self.assertEqual(len(got), 1)
        self.assertIn("HIER-CHILD-BLOCKED", got[0][1])
        self.assertEqual(fh.read_new_events("h1"), [])


class Parity(unittest.TestCase):
    def test_block_pattern_matches_the_live_gate(self):
        src = open(os.path.join(HERE, "..", "laya_gate.py")).read()
        m = re.search(r'r"(\\\[Production Deploy\\\][^"]*)"', src)
        self.assertIsNotNone(m)
        self.assertEqual(m.group(1), fh.CLASSIFIER_BLOCK_RE.pattern)


class Persistence(unittest.TestCase):
    def test_goal_round_trips(self):
        import fleet_contract as fc
        r = reg()
        own = fc.new_contract("g1", ["m1"], "goal", ["/repo"], [
            {"check": "c", "positive": ["p"], "negative": ["n"]}], deadline=9e9,
            owner_session="mgr")
        eff = dict(dispatch=lambda *a: None, send=lambda *a: None, state_of=lambda s: "idle",
                   last_growth=lambda s: 0, transcript_of=lambda s: None, verify=lambda c, rp: True)
        m = fm.Manager(session="mgr", registry=r, own=own, budget=fm.Budget(3), **eff)
        c = m.assign("imp2", "sub", ["/repo/a"], [
            {"check": "c", "positive": ["p"], "negative": ["n"]}], 9e9)
        fh.save_goal("g1", m, "rev")
        m2, rv = fh.load_goal("g1", (eff, []))
        self.assertEqual(rv, "rev")
        self.assertEqual(m2.budget.spent, 1)
        self.assertEqual(m2.ledger.children[c.contract_id], c)
        self.assertEqual(m2._next, 2)


if __name__ == "__main__":
    unittest.main()
