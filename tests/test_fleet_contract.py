#!/usr/bin/env python3
"""Tests for scripts/fleet_contract.py: tokens, namespacing, parsing, and the negative
cases the hierarchy depends on. Transcripts are fixture jsonl files, so delivery and
emission are judged on what the file records, not on what a caller asserts.

Run:  python3 -m unittest discover -s tests -t .
"""
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'scripts'))

import fleet_ack  # noqa: E402
import fleet_contract as fc  # noqa: E402

ACC = [{"check": "tests pass", "positive": ["suite green"], "negative": ["forged token refused"]}]


def user(text):
    return {"type": "user", "message": {"role": "user", "content": text}}


def asst(text):
    return {"type": "assistant",
            "message": {"role": "assistant", "content": [{"type": "text", "text": text}]}}


def queued(text):
    return {"type": "queue-operation", "operation": "enqueue", "content": text}


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.paths = {}
        self.manager = fc.new_contract("g41", ["m2"], "ship the thing", ["/repo"], ACC,
                                       deadline=9e9, owner_session="mgr")
        self.a = self._child(["m2", "i3"], "sess-a")
        self.b = self._child(["m2", "i4"], "sess-b")
        self.verdicts = {}
        self.ledger = fc.Ledger(self.manager, self.paths.get,
                                lambda c, r: self.verdicts.get(c.contract_id, True))
        self.ledger.add_child(self.a)
        self.ledger.add_child(self.b)

    def _child(self, chain, sess):
        return fc.new_contract("g41", chain, "part", ["/repo/src"], ACC, 9e9,
                               owner_session=sess, parent=self.manager)

    def write(self, sess, records):
        p = os.path.join(self.tmp.name, sess + ".jsonl")
        with open(p, "w") as fh:
            for r in records:
                fh.write(json.dumps(r) + "\n")
        self.paths[sess] = p

    def report(self, c, verdict="pass"):
        return fc.Report(c.contract_id, "transcript:/t/%s.jsonl" % c.owner_session, verdict, "ok")

    def deliver(self, c, extra=()):
        marker = "ACK-" + c.owner_session
        self.write(c.owner_session, [user("go " + marker)] + list(extra))
        self.assertTrue(self.ledger.confirm_delivery(c.contract_id, marker))


class TokenTests(unittest.TestCase):
    def test_generate_and_parse_roundtrip(self):
        t = fc.make_token("DONE", "g41", ["m2", "i3"], 1)
        self.assertEqual(t, "DONE-g41-m2-i3-1")
        self.assertEqual(fc.parse_token(t), ("DONE", "g41", ("m2", "i3"), 1))

    def test_kinds_differ(self):
        self.assertNotEqual(fc.make_token("DONE", "g1", ["m1"]), fc.make_token("BLOCKED", "g1", ["m1"]))

    def test_chain_distinguishes_levels(self):
        self.assertNotEqual(fc.make_token("DONE", "g1", ["m1"]), fc.make_token("DONE", "g1", ["m1", "i1"]))
        self.assertNotEqual(fc.parse_token("DONE-g1-m1-1")[2], fc.parse_token("DONE-g1-m1-i1-1")[2])

    def test_malformed_rejected(self):
        for bad in ["", "DONE", "DONE-g1-1", "done-g1-m1-1", "DONE-g1-m1-x", "DONE-g1-M1-1",
                    "FOO-g1-m1-1", "DONE-g1-m1-1 extra", None]:
            self.assertIsNone(fc.parse_token(bad), bad)

    def test_bad_inputs_raise(self):
        for args in [("FOO", "g1", ["m1"], 1), ("DONE", "G1", ["m1"], 1), ("DONE", "g1", [], 1),
                     ("DONE", "g1", ["m-1"], 1), ("DONE", "g1", ["m1"], 0)]:
            with self.assertRaises(fc.ContractError):
                fc.make_token(*args)

    def test_chain_prefix(self):
        self.assertTrue(fc.chain_prefix(("m2",), ("m2", "i3")))
        self.assertFalse(fc.chain_prefix(("m2",), ("m9", "i3")))


class ContractTests(Base):
    def test_fields_and_derived_tokens(self):
        c = self.a
        self.assertEqual(c.done_token, "DONE-g41-m2-i3-1")
        self.assertEqual(c.blocked_token, "BLOCKED-g41-m2-i3-1")
        self.assertEqual(c.parent_contract_id, self.manager.contract_id)
        self.assertEqual(c.chain, ("m2", "i3"))
        self.assertEqual(c.depth, 2)

    def test_depth_cap(self):
        with self.assertRaises(fc.ContractError):
            fc.new_contract("g41", ["m2", "i3", "x1"], "g", ["/r"], ACC, 1)

    def test_acceptance_needs_negative_case(self):
        with self.assertRaises(fc.ContractError):
            fc.new_contract("g41", ["m2"], "g", ["/r"], [{"check": "c", "positive": ["p"], "negative": []}], 1)
        with self.assertRaises(fc.ContractError):
            fc.new_contract("g41", ["m2"], "g", ["/r"], [], 1)

    def test_boundary_must_narrow(self):
        with self.assertRaises(fc.ContractError):
            fc.new_contract("g41", ["m2", "i9"], "g", ["/elsewhere"], ACC, 1, parent=self.manager)
        with self.assertRaises(fc.ContractError):  # sibling-prefix trap
            fc.new_contract("g41", ["m2", "i9"], "g", ["/repository"], ACC, 1, parent=self.manager)

    def test_child_chain_must_extend_parent(self):
        with self.assertRaises(fc.ContractError):
            fc.new_contract("g41", ["m9", "i1"], "g", ["/repo"], ACC, 1, parent=self.manager)

    def test_duplicate_child_rejected(self):
        with self.assertRaises(fc.ContractError):
            self.ledger.add_child(self.a)


class DeliveryTests(Base):
    def test_user_turn_counts(self):
        self.deliver(self.a)
        self.assertTrue(self.ledger.state[self.a.contract_id]["delivered"])

    def test_pasted_but_not_delivered_is_not_counted(self):
        # The paste sits in the queue bookkeeping: visible, never a turn.
        self.write("sess-a", [queued("go ACK-x")])
        self.assertFalse(self.ledger.confirm_delivery(self.a.contract_id, "ACK-x"))
        self.assertFalse(fc.delivered_as_user_turn(self.paths["sess-a"], "ACK-x"))

    def test_assistant_echo_is_not_delivery(self):
        self.write("sess-a", [asst("I will say ACK-x now")])
        self.assertFalse(self.ledger.confirm_delivery(self.a.contract_id, "ACK-x"))

    def test_tool_result_is_not_delivery(self):
        rec = {"type": "user", "message": {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "1", "content": "ACK-x"}]}}
        self.write("sess-a", [rec])
        self.assertFalse(self.ledger.confirm_delivery(self.a.contract_id, "ACK-x"))

    def test_missing_or_unreadable_transcript_is_not_delivery(self):
        self.assertFalse(self.ledger.confirm_delivery(self.a.contract_id, "ACK-x"))
        self.assertFalse(fc.delivered_as_user_turn("/nonexistent", "ACK-x"))

    def test_undelivered_child_token_is_refused(self):
        self.write("sess-a", [queued("go ACK-x"), asst(self.a.done_token)])
        self.ledger.confirm_delivery(self.a.contract_id, "ACK-x")
        ok, why = self.ledger.receive(self.a.contract_id, self.a.done_token, self.report(self.a))
        self.assertFalse(ok)
        self.assertIn("delivered", why)

    def test_flat_path_unchanged(self):
        # fleet_ack still treats a queued record as receipt; the strict proof is separate.
        p = os.path.join(self.tmp.name, "flat.jsonl")
        with open(p, "w") as fh:
            fh.write(json.dumps({"type": "attachment", "attachment": "FLAT-1"}) + "\n")
            fh.write(json.dumps(user("hello FLAT-2")) + "\n")
        self.assertTrue(fleet_ack.delivered("x", "FLAT-2", path=p))
        self.assertTrue(fc.delivered_as_user_turn(p, "FLAT-2"))
        self.assertEqual(fleet_ack.contract_token("Exact done token, alone on its own line:\nDONE-fw-ow-1\n"),
                         "DONE-fw-ow-1")


class EmissionTests(Base):
    def test_token_in_directive_is_not_emission(self):
        # The contract text itself carries the done token as a USER turn.
        self.deliver(self.a, [user("when done say\n" + self.a.done_token)])
        self.assertEqual(fc.emitted_by_assistant(self.paths["sess-a"], self.a.done_token), 0)
        ok, why = self.ledger.receive(self.a.contract_id, self.a.done_token, self.report(self.a))
        self.assertFalse(ok)
        self.assertIn("not emitted", why)

    def test_token_must_be_alone_on_its_line(self):
        self.deliver(self.a, [asst("I will emit " + self.a.done_token + " later")])
        self.assertEqual(fc.emitted_by_assistant(self.paths["sess-a"], self.a.done_token), 0)

    def test_alone_on_line_counts(self):
        self.deliver(self.a, [asst("finished\n" + self.a.done_token + "\n")])
        self.assertEqual(fc.emitted_by_assistant(self.paths["sess-a"], self.a.done_token), 1)


class ParentGatingTests(Base):
    def finish(self, c, verdict="pass"):
        self.deliver(c, [asst(c.done_token)])
        return self.ledger.receive(c.contract_id, c.done_token, self.report(c, verdict))

    def test_missing_child_token_blocks_parent_done(self):
        self.assertEqual(self.finish(self.a), (True, "verified"))
        self.assertIsNone(self.ledger.done_token())
        self.assertEqual(self.ledger.missing(), [self.b.contract_id])

    def test_all_children_verified_releases_done(self):
        self.finish(self.a)
        self.finish(self.b)
        self.assertEqual(self.ledger.done_token(), self.manager.done_token)
        tok, rep = self.ledger.upward_report()
        self.assertEqual(tok, self.manager.done_token)
        self.assertEqual(rep.verdict, "pass")

    def test_no_children_no_done(self):
        empty = fc.Ledger(self.manager, self.paths.get, lambda c, r: True)
        self.assertIsNone(empty.done_token())

    def test_child_say_so_is_not_verification(self):
        self.verdicts[self.a.contract_id] = False  # the reviewer disagrees
        self.finish(self.a)
        self.finish(self.b)
        self.assertIsNone(self.ledger.done_token())
        self.assertEqual(self.ledger.blocked_token(), self.manager.blocked_token)

    def test_child_verdict_fail_with_done_token_is_contradiction(self):
        self.finish(self.a, verdict="fail")
        self.assertIsNone(self.ledger.done_token())
        self.assertEqual(self.ledger.state[self.a.contract_id]["verified"], False)

    def test_sibling_chain_token_rejected(self):
        # b's session emits b's token; it is offered as a's.
        self.deliver(self.a, [asst(self.b.done_token)])
        ok, why = self.ledger.receive(self.a.contract_id, self.b.done_token, self.report(self.a))
        self.assertFalse(ok)
        self.assertEqual(why, "sibling chain")
        self.assertIsNone(self.ledger.done_token())

    def test_token_from_foreign_goal_or_chain_rejected(self):
        foreign = fc.make_token("DONE", "g41", ["m9", "i3"])
        self.deliver(self.a, [asst(foreign)])
        ok, why = self.ledger.receive(self.a.contract_id, foreign, self.report(self.a))
        self.assertFalse(ok)
        self.assertEqual(why, "foreign chain")

    def test_forged_token_rejected(self):
        for forged in ["DONE-g41-m2-i3-2", "DONE-g41-m2-i3", "done-g41-m2-i3-1", "DONE-g41-m2-1"]:
            self.deliver(self.a, [asst(forged)])
            ok, _ = self.ledger.receive(self.a.contract_id, forged, self.report(self.a))
            self.assertFalse(ok, forged)
        self.assertIsNone(self.ledger.done_token())

    def test_right_token_from_wrong_session_rejected(self):
        # a's token typed into b's transcript does not settle a.
        self.deliver(self.a)
        self.deliver(self.b, [asst(self.a.done_token)])
        ok, why = self.ledger.receive(self.a.contract_id, self.a.done_token, self.report(self.a))
        self.assertFalse(ok)
        self.assertIn("not emitted", why)

    def test_duplicate_token_does_not_complete_parent(self):
        self.deliver(self.a, [asst(self.a.done_token), asst(self.a.done_token)])
        self.assertEqual(self.ledger.receive(self.a.contract_id, self.a.done_token, self.report(self.a))[0], True)
        ok, why = self.ledger.receive(self.a.contract_id, self.a.done_token, self.report(self.a))
        self.assertFalse(ok)
        self.assertIn("duplicate", why)
        # a's token can never stand in for b
        ok, _ = self.ledger.receive(self.b.contract_id, self.a.done_token, self.report(self.b))
        self.assertFalse(ok)
        self.assertIsNone(self.ledger.done_token())

    def test_report_for_other_contract_rejected(self):
        self.deliver(self.a, [asst(self.a.done_token)])
        ok, why = self.ledger.receive(self.a.contract_id, self.a.done_token, self.report(self.b))
        self.assertFalse(ok)
        self.assertIn("another contract", why)

    def test_blocked_child_blocks_parent_and_surfaces(self):
        self.deliver(self.a, [asst(self.a.blocked_token)])
        self.assertEqual(self.ledger.receive(self.a.contract_id, self.a.blocked_token,
                                             self.report(self.a, "blocked")), (True, "blocked"))
        self.assertIsNone(self.ledger.done_token())
        self.assertEqual(self.ledger.blocked_children(), [self.a.contract_id])
        tok, rep = self.ledger.upward_report()
        self.assertEqual((tok, rep.verdict), (self.manager.blocked_token, "blocked"))


class PointerTests(unittest.TestCase):
    def test_pointer_and_short_verdict_only(self):
        fc.Report("c1", "transcript:/t/x.jsonl", "pass", "ok")
        fc.Report("c1", "artifact:docs/x.md", "fail")

    def test_body_is_rejected(self):
        for kw in [dict(pointer="no pointer here"), dict(pointer="transcript:/t", note="x" * 500),
                   dict(pointer="transcript:/t", note="line one\nline two"),
                   dict(pointer="transcript:/t", verdict="great")]:
            args = dict(contract_id="c1", pointer="transcript:/t", verdict="pass")
            args.update(kw)
            with self.assertRaises(fc.ContractError):
                fc.Report(**args)


if __name__ == "__main__":
    unittest.main()
