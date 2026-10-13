#!/usr/bin/env python3
"""End-to-end scenarios for the manager-over-implementers hierarchy.

One command runs everything and prints a table of each negative case and what was
observed:

    python3 tests/test_e2e_hierarchy.py

Also picked up by:  python3 -m unittest discover -s tests -t .

What is real: fleet_contract (contracts, tokens, ledger, transcript proofs),
fleet_governance (levels, gates, review) and fleet_manager (registry, budget, Manager).
All of them read and write real JSONL transcript files in a temp dir; delivery and
emission are proven from those files, never from a flag.

What is simulated: the implementer sessions (a scripted `Session` writes transcript
records the way a Claude session would) and the clock. The chief-of-staff entry point is
the production one, scripts/fleet_chief.py.
"""
import json
import os
import shutil
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, '..', 'scripts'))
sys.path.insert(0, os.path.join(HERE, '..'))

from fleet_chief import ChiefOfStaff  # noqa: E402
import fleet_contract as fc  # noqa: E402
import fleet_governance as fg  # noqa: E402
import fleet_manager as fm  # noqa: E402
import fleet_review as fr  # noqa: E402

ACC = [{"check": "suite green", "positive": ["all tests pass"],
        "negative": ["forged token refused"]}]
BOUNDARY = ("/repo/src", "/repo/tests")
OBSERVED = []   # (case, outcome) rows, printed at the end of a run


def observe(case, outcome):
    OBSERVED.append((case, outcome))


# ---------------------------------------------------------------- simulated sessions

class Session(object):
    """A scripted session. Writes the same record shapes a real transcript carries."""

    def __init__(self, path):
        self.path = path
        open(path, "a").close()

    def _w(self, obj):
        with open(self.path, "a") as fh:
            fh.write(json.dumps(obj) + "\n")

    def user(self, text):
        self._w({"type": "user", "message": {"role": "user", "content": text}})

    def queued(self, text):
        self._w({"type": "queue-operation", "operation": "enqueue", "content": text})

    def say(self, text):
        self._w({"type": "assistant", "message": {"role": "assistant",
                 "content": [{"type": "text", "text": text}]}})

    def tool_use(self, name, **inp):
        self._n = getattr(self, "_n", 0) + 1
        tid = "tu%d" % self._n
        self._w({"type": "assistant", "message": {"role": "assistant", "content": [
            {"type": "tool_use", "id": tid, "name": name, "input": inp}]}})
        return tid

    def tool_result(self, text, tid=None, is_error=False):
        blk = {"type": "tool_result", "content": text, "is_error": is_error}
        if tid is not None:
            blk["tool_use_id"] = tid
        self._w({"type": "user", "message": {"role": "user", "content": [blk]}})

    def acceptance_run(self, exit_code):
        """A real-shaped test run: a Bash tool_use and its tool_result."""
        tid = self.tool_use("Bash", command="python3 -m pytest -q")
        if exit_code:
            self.tool_result("Exit code %d\n2 failed, 3 passed" % exit_code, tid, True)
        else:
            self.tool_result("5 passed in 0.1s", tid)


def last_acceptance_exit(path):
    """Ground truth: the exit code of the last test-run tool_result, or None."""
    code = None
    with open(path) as fh:
        for ln in fh:
            try:
                obj = json.loads(ln)
            except ValueError:
                continue
            content = (obj.get("message") or {}).get("content")
            if not isinstance(content, list):
                continue
            for b in content:
                if isinstance(b, dict) and b.get("type") == "tool_result":
                    c = b.get("content", "")
                    if isinstance(c, str) and c.startswith("Exit code "):
                        code = int(c.split()[2])
                    elif isinstance(c, str) and " passed" in c:
                        code = 0
    return code


# ---------------------------------------------------------------- the fixture fleet

class Fleet(object):
    def __init__(self, units=5, hour=12, reviewer="reviewer-r", verify=None):
        self.dir = tempfile.mkdtemp(prefix="e2e-hier-")
        self.t = 1000.0
        self.sessions = {}
        self.states = {}
        self.growth = {}
        self.sent = []
        self.escalated = []
        self.deliver = True
        self.cos = ChiefOfStaff(hour)
        self.level = self.cos.mgr_level()
        self.reg = self.cos.registry
        self.reviewer = reviewer
        self.mgr = self.cos.take_goal(
            "g41", "ship the widget", BOUNDARY, ACC, self.t + 3600, units, "mgr-m1",
            dict(dispatch=self._dispatch, send=self._send, state_of=lambda s: self.states[s],
                 last_growth=lambda s: self.growth[s], transcript_of=self.transcript_of,
                 verify=verify or self.reviewer_verify(), escalate=self._escalate),
            clock=lambda: self.t)
        self.own = self.mgr.own

    def close(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    # injected effects
    def transcript_of(self, s):
        sess = self.sessions.get(s)
        return sess.path if sess else None

    def _session(self, name):
        if name not in self.sessions:
            self.sessions[name] = Session(os.path.join(self.dir, name + ".jsonl"))
            self.states[name] = "working"
            self.growth[name] = self.t
        return self.sessions[name]

    def _dispatch(self, session, directive):
        s = self._session(session)
        if self.deliver:
            s.user(directive)
        else:
            s.queued(directive)     # pasted but never became a turn

    def _send(self, session, text):
        self.sent.append((session, text))
        self._session(session).user(text)

    def _escalate(self, kind, text):
        self.escalated.append((kind, text))
        ev_kind = {"out-of-scope": fg.NEEDS_INPUT, "dead": fg.STOPPED,
                   "idle": fg.STOPPED}.get(kind, fg.NEEDS_INPUT)
        self.level.route(fg.Event(ev_kind, "mgr-m1", text, stopped_seconds=900),
                         self.cos.hour)

    def reviewer_verify(self):
        """The production reviewer (fleet_review): reads the transcript, not the report."""
        return fr.build_verify(self.reviewer, lambda c: c.owner_session, self.transcript_of,
                               lambda: list(self.mgr.ledger.children), rate=0.0, seed=1)

    # scenario helpers
    def assign(self, name, goal="implement part"):
        return self.mgr.assign(name, goal, BOUNDARY, ACC, self.t + 3600)

    def team(self, n=3):
        cs = [self.assign("impl-%d" % i, "part %d" % i) for i in range(1, n + 1)]
        for c in cs:
            self.mgr.confirm(c.contract_id)
        return cs

    def finish(self, c, exit_code=0, verdict="pass", token=None, pointer=None):
        s = self.sessions[c.owner_session]
        s.acceptance_run(exit_code)
        s.say("work finished\n%s\n" % (token or c.done_token))
        return fc.Report(c.contract_id, pointer or "transcript:" + s.path, verdict, "part done")


class Base(unittest.TestCase):
    def setUp(self):
        self.f = None

    def tearDown(self):
        if self.f:
            self.f.close()

    def fleet(self, **kw):
        self.f = Fleet(**kw)
        return self.f


# ---------------------------------------------------------------- 1. happy path

class HappyPath(Base):
    def test_goal_in_contracts_out_one_human_report(self):
        f = self.fleet()
        cs = f.team(3)
        # contracts dispatched: each directive is a real user turn carrying the child's own token
        for c in cs:
            self.assertTrue(fc.delivered_as_user_turn(f.transcript_of(c.owner_session),
                                                      c.done_token))
            self.assertEqual(f.reg.nodes[c.owner_session]["depth"], 2)
            self.assertEqual(f.reg.nodes[c.owner_session]["lifecycle_owner"], "mgr-m1")
        self.assertEqual(len({c.done_token for c in cs}), 3)
        self.assertIsNone(f.mgr.result()[0])               # nothing settled yet
        # children finish; manager verifies every token from the child's own transcript
        for c in cs:
            ok, why = f.mgr.receive(c.contract_id, f.finish(c))
            self.assertEqual((ok, why), (True, "verified"))
            self.assertEqual(fc.emitted_by_assistant(f.transcript_of(c.owner_session),
                                                     c.done_token), 1)
        token, up = f.mgr.result()
        self.assertEqual(token, f.own.done_token)
        self.assertEqual(up.verdict, "pass")
        self.assertLessEqual(len(up.note), fc.MAX_VERDICT_CHARS)
        # single human-facing report, pointers to ground truth that exist on disk
        text = f.cos.report("g41", token, up, f.mgr.ledger)
        self.assertEqual(len(f.cos.level.delivered), 1)
        for c in cs:
            path = f.transcript_of(c.owner_session)
            self.assertIn("transcript:" + path, text)
            self.assertTrue(os.path.exists(path))
        self.assertEqual(f.cos.level.gates, [])
        observe("happy path", "3 contracts delivered as user turns, 3 tokens verified, "
                "manager done token issued, 1 report with 3 transcript pointers")


# ---------------------------------------------------------------- 2. negative cases

class Tokens(Base):
    def test_forged_token_never_emitted(self):
        f = self.fleet()
        c = f.team(1)[0]
        # nothing emitted. Offer the real token string anyway.
        ok, why = f.mgr.ledger.receive(c.contract_id, c.done_token,
                                       fc.Report(c.contract_id, "transcript:x", "pass"))
        self.assertFalse(ok)
        self.assertIn("not emitted", why)
        self.assertEqual(f.mgr.result(), (None, None))
        observe("forged token (offered, never emitted by the child)", "refused: " + why)

    def test_forged_token_only_in_directive(self):
        f = self.fleet()
        c = f.team(1)[0]     # the token sits in the directive user turn
        self.assertTrue(fc.delivered_as_user_turn(f.transcript_of(c.owner_session),
                                                  c.done_token))
        ok, why = f.mgr.ledger.receive(c.contract_id, c.done_token,
                                       fc.Report(c.contract_id, "transcript:x", "pass"))
        self.assertFalse(ok)
        self.assertEqual(f.mgr.ledger.state[c.contract_id]["token"], None)
        observe("token only in the directive (user turn)", "refused: " + why)

    def test_cross_chain_sibling_token(self):
        f = self.fleet()
        a, b = f.team(2)
        # child A really emits B's token (a stolen or guessed sibling token)
        f.sessions[a.owner_session].acceptance_run(0)
        f.sessions[a.owner_session].say(b.done_token)
        ok, why = f.mgr.ledger.receive(
            a.contract_id, b.done_token,
            fc.Report(a.contract_id, "transcript:" + f.transcript_of(a.owner_session), "pass"))
        self.assertFalse(ok)
        self.assertEqual(why, "sibling chain")
        self.assertIsNone(f.mgr.ledger.state[a.contract_id]["token"])
        observe("cross-chain token (child A emits sibling B's token)", "refused: " + why)

    def test_foreign_goal_token(self):
        f = self.fleet()
        c = f.team(1)[0]
        foreign = fc.make_token(fc.DONE, "g99", ["m1", "i1"])
        f.sessions[c.owner_session].say(foreign)
        ok, why = f.mgr.ledger.receive(c.contract_id, foreign,
                                       fc.Report(c.contract_id, "transcript:x", "pass"))
        self.assertFalse(ok)
        observe("token from another goal", "refused: " + why)

    def test_flat_token_shape(self):
        f = self.fleet()
        c = f.team(1)[0]
        flat = "DONE-fw-ow-1790025010"
        f.sessions[c.owner_session].say(flat)
        ok, why = f.mgr.ledger.receive(c.contract_id, flat,
                                       fc.Report(c.contract_id, "transcript:x", "pass"))
        self.assertFalse(ok)
        observe("flat-protocol token offered to a nested contract", "refused: " + why)

    def test_duplicate_settle(self):
        f = self.fleet()
        c = f.team(1)[0]
        rep = f.finish(c)
        self.assertTrue(f.mgr.receive(c.contract_id, rep)[0])
        ok, why = f.mgr.receive(c.contract_id, rep)
        self.assertFalse(ok)
        observe("second result for a settled contract", "refused: " + why)


class Acceptance(Base):
    def test_child_claims_done_but_fails_acceptance(self):
        f = self.fleet()
        a, b, c = f.team(3)
        for x in (a, b):
            self.assertTrue(f.mgr.receive(x.contract_id, f.finish(x))[0])
        rep = f.finish(c, exit_code=1)       # emits token, says pass, acceptance run failed
        ok, why = f.mgr.receive(c.contract_id, rep)
        self.assertTrue(ok)                  # token accepted as an emission...
        self.assertEqual(why, "failed acceptance")   # ...but not as success
        self.assertIsNone(f.mgr.ledger.done_token())
        token, up = f.mgr.result()
        self.assertEqual(token, f.own.blocked_token)
        self.assertEqual(up.verdict, "blocked")
        observe("child claims done, acceptance exit=1", "manager done withheld; "
                "result is the manager's BLOCKED token (%s)" % why)

    def test_child_verdict_fail_with_done_token(self):
        f = self.fleet()
        c = f.team(1)[0]
        ok, why = f.mgr.receive(c.contract_id, f.finish(c, verdict="fail"))
        self.assertEqual(why, "contradicted")
        self.assertEqual(f.mgr.ledger.state[c.contract_id]["why"],
                         "child verdict fail contradicts done token")
        self.assertIsNone(f.mgr.ledger.done_token())
        observe("done token with a fail verdict", "contradicted: " + why)

    def test_child_blocked(self):
        f = self.fleet()
        a, b = f.team(2)
        self.assertTrue(f.mgr.receive(a.contract_id, f.finish(a))[0])
        s = f.sessions[b.owner_session]
        s.say("cannot proceed, missing credentials\n%s" % b.blocked_token)
        ok, why = f.mgr.receive(b.contract_id,
                                fc.Report(b.contract_id, "transcript:" + s.path, "blocked", "no creds"))
        self.assertEqual((ok, why), (True, "blocked"))
        self.assertEqual(f.mgr.ledger.blocked_children(), [b.contract_id])
        token, up = f.mgr.result()
        self.assertEqual(token, f.own.blocked_token)
        # the block was raised upward at receive time (1 delivery), then the final report
        # names the blocked child with a pointer (1 more)
        self.assertEqual(len(f.cos.level.delivered), 1)
        text = f.cos.report("g41", token, up, f.mgr.ledger)
        self.assertIn(b.contract_id, text)
        self.assertEqual(len(f.cos.level.delivered), 2)
        observe("child blocked", "manager BLOCKED token; 1 human report naming %s" % b.contract_id)

    def test_reviewer_is_the_producer(self):
        f = self.fleet(reviewer="impl-1")      # the reviewer is the session that produced it
        c = f.team(1)[0]
        with self.assertRaises(fg.ReviewerNotIndependent):
            f.mgr.receive(c.contract_id, f.finish(c))
        self.assertIsNone(f.mgr.ledger.state[c.contract_id]["verified"])
        self.assertIsNone(f.mgr.result()[0])
        with self.assertRaises(fg.ReviewerNotIndependent):
            fg.pick_reviewer(fg.Candidate("impl-1", "m"), [fg.Candidate("impl-1", "m")])
        # the standalone review gate refuses the same way, and a rejection withholds the token
        gate = fg.ReviewGate(c.contract_id, "impl-1", c.done_token)
        with self.assertRaises(fg.ReviewerNotIndependent):
            gate.submit(fg.Verdict(c.contract_id, "impl-1", True))
        self.assertIsNone(gate.done_token())
        gate.submit(fg.Verdict(c.contract_id, "reviewer-r", False, "tests fail"))
        self.assertTrue(gate.rejected)
        self.assertIsNone(gate.done_token())
        observe("reviewer is the producer", "ReviewerNotIndependent raised; contract left "
                "unsettled, no manager done token")


class Delivery(Base):
    def test_undelivered_marker(self):
        f = self.fleet()
        f.deliver = False
        c = f.assign("impl-1")
        self.assertFalse(f.mgr.confirm(c.contract_id))
        rep = f.finish(c)                      # even a perfect result
        ok, why = f.mgr.receive(c.contract_id, rep)
        self.assertFalse(ok)
        self.assertIn("not proven delivered", why)
        self.assertIsNone(f.mgr.result()[0])
        observe("undelivered marker (queued, never a user turn)", "refused: " + why)

    def test_assistant_echo_is_not_delivery(self):
        f = self.fleet()
        f.deliver = False
        c = f.assign("impl-1")
        f.sessions["impl-1"].say(c.done_token)
        self.assertFalse(f.mgr.confirm(c.contract_id))
        observe("assistant echo of the marker", "not counted as delivery")


class Overwatch(Base):
    def test_stalled_child_triggers_manager_checkin(self):
        f = self.fleet()
        a, b = f.team(2)
        f.t += 400                              # 400s quiet > stall window 360
        f.growth["impl-1"] = f.t                # impl-1 is fine
        acts = f.mgr.tick()
        self.assertEqual([(x["issuer"], x["kind"], x["session"]) for x in acts],
                         [("mgr-m1", "checkin", "impl-2")])
        self.assertEqual(len(f.sent), 1)
        self.assertEqual(f.sent[0][0], "impl-2")
        self.assertTrue(fc.delivered_as_user_turn(f.transcript_of("impl-2"), "No output for"))
        self.assertEqual(f.mgr.tick(), [])      # repeat window respected, no spam
        # the daemon stands down while the manager is alive, and takes over when it is not
        self.assertTrue(fm.daemon_defers("impl-2", f.reg, lambda m: True))
        self.assertFalse(fm.daemon_defers("impl-2", f.reg, lambda m: False))
        self.assertFalse(fm.daemon_defers("flat-session", f.reg, lambda m: True))
        observe("stalled child", "check-in issued by mgr-m1 (not the daemon), delivered as a "
                "user turn, not repeated inside the window; daemon defers only while "
                "the manager is alive")

    def test_dead_child_escalates_up(self):
        f = self.fleet()
        f.team(1)
        f.states["impl-1"] = "dead"
        acts = f.mgr.tick()
        self.assertEqual(acts[0]["kind"], "dead")
        self.assertEqual(f.sent, [])            # nothing is sent to a dead session
        # stopped over 10 minutes is news: exactly one event reaches the human
        self.assertEqual([e.kind for e in f.cos.level.delivered], [fg.STOPPED])
        observe("dead child", "manager escalated; nothing sent into the session; 1 stopped-over-10-min "
                "event reached the human")


class Limits(Base):
    def test_over_budget_goal(self):
        f = self.fleet(units=2)
        f.assign("impl-1")
        f.assign("impl-2")
        with self.assertRaises(fm.BudgetExhausted):
            f.assign("impl-3")
        self.assertNotIn("impl-3", f.reg.nodes)           # nothing half-created
        self.assertNotIn("impl-3", f.sessions)            # nothing dispatched
        self.assertEqual(f.mgr.budget.spent, 2)
        self.assertEqual(len(f.mgr.ledger.children), 2)
        observe("over-budget goal (2 units, 3rd dispatch)", "BudgetExhausted; no node, no "
                "contract, no dispatch for the 3rd")

    def test_depth_three(self):
        f = self.fleet()
        f.assign("impl-1")
        # an implementer creating a session
        with self.assertRaises(fm.DepthCapExceeded):
            f.reg.register("impl-1-child", fm.IMPLEMENTER, parent_session="impl-1")
        with self.assertRaises(fm.DepthCapExceeded):
            f.reg.register("sneaky-mgr", fm.MANAGER, parent_session="impl-1")
        # a manager that is itself at depth 2 trying to dispatch
        f.reg.register("mgr-m2", fm.MANAGER, parent_session="mgr-m1", goal="g41")
        own2 = fc.new_contract("g41", ["m1", "m2"], "sub goal", BOUNDARY, ACC, f.t + 99,
                               parent=f.own, owner_session="mgr-m2")
        m2 = fm.Manager("mgr-m2", f.reg, own2, fm.Budget(3), f._dispatch, f._send,
                        lambda s: "working", lambda s: f.t, f.transcript_of,
                        f.reviewer_verify(), clock=lambda: f.t)
        with self.assertRaises(fm.DepthCapExceeded):
            m2.assign("impl-deep", "x", BOUNDARY, ACC, f.t + 99)
        self.assertNotIn("impl-deep", f.reg.nodes)
        # and a depth-3 contract chain
        with self.assertRaises(fc.ContractError):
            fc.new_contract("g41", ["m1", "i1", "x1"], "g", BOUNDARY, ACC, f.t + 9)
        observe("depth-3 attempt", "DepthCapExceeded for implementer->session, implementer->"
                "manager and depth-2 manager->implementer; ContractError for a 3-node chain")

    def test_out_of_scope_work_flagged_not_absorbed(self):
        f = self.fleet()
        with self.assertRaises(fm.OutOfScope):
            f.mgr.intake("fix billing", ["/other/billing"])
        self.assertEqual(len(f.mgr.flagged), 1)
        self.assertEqual(f.mgr.budget.spent, 0)
        observe("out-of-scope work handed to the manager", "OutOfScope; flagged and escalated; "
                "no budget spent")

    def test_boundary_widening(self):
        f = self.fleet()
        with self.assertRaises(fc.ContractError):
            f.mgr.assign("impl-1", "g", ["/repo/src", "/etc"], ACC, f.t + 9)
        self.assertNotIn("impl-1", f.reg.nodes)
        observe("child boundary wider than the manager's", "ContractError; nothing registered")


class Escalation(Base):
    def _raise_gate(self, f, kind):
        ev = fg.Event(kind, "impl-1", "Bash(rm -rf build) blocked by classifier", id="ev1")
        implementer = fg.Level("impl-1", parent=f.level)
        return ev, implementer

    def test_classifier_block_reaches_human_untouched(self):
        f = self.fleet()
        f.team(1)
        ev, implementer = self._raise_gate(f, fg.CLASSIFIER_BLOCK)
        action, where = implementer.route(ev, hour=12)
        self.assertEqual((action, where), (fg.DELIVER, fg.HUMAN))
        # not absorbed at the implementer or the manager
        self.assertEqual(implementer.gates + f.level.gates, [])
        self.assertEqual(implementer.delivered + f.level.delivered, [])
        # exactly one pending gate at the top, still unresolved
        self.assertEqual(len(f.cos.level.gates), 1)
        gate = f.cos.level.gates[0]
        self.assertIsNone(gate.resolved_by)
        # no agent principal can resolve it, at any level
        for who in ("impl-1", "mgr-m1", "chief", "reviewer-r"):
            with self.assertRaises(fg.SelfApprovalError):
                f.cos.level.approve(who, "ev1", "yes")
        self.assertIsNone(gate.resolved_by)
        # the manager's own tick does not touch it either
        f.mgr.tick()
        self.assertIsNone(gate.resolved_by)
        # only the human resolves it
        f.cos.level.approve(fg.HUMAN, "ev1", "no")
        self.assertEqual((gate.resolved_by, gate.answer), (fg.HUMAN, "no"))
        observe("classifier block escalation (implementer -> manager -> chief of staff)",
                "delivered to the human once; absorbed by no level; agent approvals "
                "(impl, manager, chief, reviewer) raised SelfApprovalError; human resolved")

    def test_choice_ui_same_path(self):
        f = self.fleet()
        ev = fg.Event(fg.CHOICE_UI, "impl-1", "pick a branch", id="ev2")
        fg.Level("impl-1", parent=f.level).route(ev, hour=12)
        self.assertEqual(len(f.cos.level.gates), 1)
        observe("choice UI escalation", "same path as classifier block, pending for the human")

    def test_self_approval_attempt(self):
        f = self.fleet()
        ev, implementer = self._raise_gate(f, fg.CLASSIFIER_BLOCK)
        implementer.route(ev, hour=12)
        gate = f.cos.level.gates[0]
        with self.assertRaises(fg.SelfApprovalError):
            gate.resolve("impl-1", "yes")        # the raiser approving its own block
        with self.assertRaises(fg.SelfApprovalError):
            gate.resolve("mgr-m1", "yes")
        self.assertIsNone(gate.resolved_by)
        observe("self-approval attempt (raiser and its manager)", "SelfApprovalError; gate "
                "still pending")

    def test_gate_in_quiet_hours_held_not_resolved(self):
        f = self.fleet()
        ev, implementer = self._raise_gate(f, fg.CLASSIFIER_BLOCK)
        action, _ = implementer.route(ev, hour=2)
        self.assertEqual(action, fg.HOLD)
        self.assertIsNone(f.cos.level.gates[0].resolved_by)
        self.assertEqual(f.cos.level.release_held(2), [])
        self.assertEqual(len(f.cos.level.release_held(9)), 1)
        observe("classifier block at 02:00", "held, not auto-resolved; delivered at 09:00")

    def test_receipts_never_reach_the_human(self):
        f = self.fleet()
        action, _ = fg.Level("impl-1", parent=f.level).route(
            fg.Event(fg.RECEIPT, "impl-1", "ACK-OK"), hour=12)
        self.assertEqual(action, fg.ABSORB)
        self.assertEqual(f.cos.level.delivered, [])
        observe("receipt", "absorbed at the first level, never reaches the human")


# ---------------------------------------------------------------- 3. lossy summary

class LossySummary(Base):
    def test_misreported_result_is_detected_via_ground_truth(self):
        f = self.fleet()
        cs = f.team(3)
        # child 2's summary is rosy; its transcript says acceptance failed
        for c in cs:
            code = 1 if c is cs[1] else 0
            f.mgr.receive(c.contract_id, f.finish(c, exit_code=code))
        token, up = f.mgr.result()
        self.assertEqual(up.verdict, "blocked")           # manager did not trust the summary
        text = f.cos.report("g41", token, up, f.mgr.ledger)
        # the human report carries a pointer per child and each pointer reaches ground truth
        pointers = [ln.split("transcript:")[1] for ln in text.splitlines() if "transcript:" in ln]
        self.assertEqual(len(pointers), 3)
        truths = [last_acceptance_exit(p) for p in pointers]
        self.assertEqual(sorted(truths), [0, 0, 1])
        # the child's own report said pass; the pointer shows otherwise
        self.assertEqual(f.mgr.ledger.state[cs[1].contract_id]["report"].verdict, "pass")
        self.assertEqual(last_acceptance_exit(f.transcript_of(cs[1].owner_session)), 1)
        observe("lossy summary: child reports pass, transcript exit=1",
                "reviewer rejected from ground truth; report links the transcript that shows it")

    def test_pointer_to_the_wrong_transcript_is_detected(self):
        f = self.fleet()
        a, b = f.team(2)
        f.sessions[b.owner_session].acceptance_run(0)
        # a's acceptance failed, but its report points at b's clean transcript
        sa = f.sessions[a.owner_session]
        sa.acceptance_run(1)
        sa.say(a.done_token)
        rep = fc.Report(a.contract_id, "transcript:" + f.transcript_of(b.owner_session), "pass")
        ok, why = f.mgr.receive(a.contract_id, rep)
        self.assertEqual(why, "failed acceptance")
        observe("lossy summary: pointer redirected to a clean transcript",
                "reviewer refused (pointer is not the child's own transcript)")

    def test_spot_check_sampling_properties(self):
        ids = ["c1", "c2", "c3", "c4", "c5"]
        self.assertEqual(len(fg.spot_check(ids, rate=0.0)), 1)      # never empty
        self.assertEqual(fg.spot_check(ids, rate=1.0), sorted(ids))
        self.assertEqual(fg.spot_check([], rate=1.0), [])
        self.assertEqual(fg.spot_check(ids, rate=0.4, seed=7), fg.spot_check(ids, rate=0.4, seed=7))
        # With rate < 1 a lying child can be missed. Show it: some seed misses c3.
        missed = [s for s in range(50) if "c3" not in fg.spot_check(ids, rate=0.2, seed=s)]
        self.assertTrue(missed)
        observe("spot check sampling", "never empty; rate=1.0 covers all; at rate 0.2 a given "
                "child is missed on %d of 50 seeds, so sampling alone is not detection"
                % len(missed))


class UpwardEscalation(Base):
    def _block(self, f, b):
        s = f.sessions[b.owner_session]
        s.say("cannot proceed\n%s" % b.blocked_token)
        return fc.Report(b.contract_id, "transcript:" + s.path, "blocked", "no creds")

    def test_blocked_child_escalates_once(self):
        f = self.fleet()
        a, b = f.team(2)
        rep = self._block(f, b)
        self.assertEqual(f.mgr.receive(b.contract_id, rep), (True, "blocked"))
        self.assertEqual([k for k, _ in f.escalated], ["child-blocked"])
        self.assertIn(b.contract_id, f.escalated[0][1])
        self.assertEqual(f.mgr.receive(b.contract_id, rep)[0], False)   # duplicate
        self.assertEqual(len(f.escalated), 1)
        observe("child blocked, manager raises it", "1 escalation (child-blocked); duplicate receive adds none")

    def test_failed_acceptance_escalates_once(self):
        f = self.fleet()
        c = f.team(1)[0]
        f.mgr.receive(c.contract_id, f.finish(c, exit_code=1))
        self.assertEqual([k for k, _ in f.escalated], ["child-failed"])
        observe("child fails acceptance, manager raises it", "1 escalation (child-failed)")

    def test_verified_child_escalates_nothing(self):
        f = self.fleet()
        c = f.team(1)[0]
        f.mgr.receive(c.contract_id, f.finish(c))
        self.assertEqual(f.escalated, [])


class ManagerEmission(Base):
    def _done(self, f):
        cs = f.team(2)
        for c in cs:
            f.mgr.receive(c.contract_id, f.finish(c))

    def test_derived_token_not_reported_until_manager_emits(self):
        f = self.fleet()
        self._done(f)
        self.assertEqual(f.mgr.result()[0], f.own.done_token)        # derived
        tok, rep, why = f.mgr.result_proven()
        self.assertIsNone(tok)
        self.assertIn("not emitted", why)
        observe("manager token derived, never emitted", "refused: " + why)

    def test_manager_emission_accepted(self):
        f = self.fleet()
        self._done(f)
        f._session("mgr-m1").say("all children verified\n%s" % f.own.done_token)
        tok, rep, why = f.mgr.result_proven()
        self.assertEqual((tok, why), (f.own.done_token, "emitted"))
        self.assertEqual(rep.verdict, "pass")

    def test_token_quoted_by_user_or_tool_is_not_emission(self):
        f = self.fleet()
        self._done(f)
        m = f._session("mgr-m1")
        m.user(f.own.done_token)
        m.tool_result(f.own.done_token)
        self.assertIsNone(f.mgr.result_proven()[0])

    def test_blocked_manager_token_needs_emission_too(self):
        f = self.fleet()
        c = f.team(1)[0]
        f.mgr.receive(c.contract_id, f.finish(c, exit_code=1))
        self.assertIsNone(f.mgr.result_proven()[0])
        f._session("mgr-m1").say(f.own.blocked_token)
        tok, rep, _ = f.mgr.result_proven()
        self.assertEqual((tok, rep.verdict), (f.own.blocked_token, "blocked"))


def _print_table():
    print("\n%-70s | %s" % ("CASE", "OBSERVED"))
    print("-" * 140)
    for case, outcome in OBSERVED:
        print("%-70s | %s" % (case[:70], outcome))


if __name__ == "__main__":
    res = unittest.main(exit=False, verbosity=1)
    _print_table()
    sys.exit(0 if res.result.wasSuccessful() else 1)
