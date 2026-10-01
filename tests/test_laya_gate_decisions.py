#!/usr/bin/env python3
"""Tests for the gate's second and third decisions: what to DO, and may it be affirmed.

The escalation decision answers only whether the operator is interrupted. These two answer
what the fleet actually does next, and the failure they exist to stop is an uncalibrated
model quietly changing that behaviour:

  * a lane-1 action must never reach the model at all. Asking a small model whether
    `git push --force` is safe is a way of losing the argument, so the destructive, money,
    credential and classifier-block cases are asserted with the model STUBBED TO RAISE.
  * when the model is unsure, below threshold, or unavailable, the answer must be the
    STANDING policy for that state, not the model's guess. IDLE still gets nudged.
  * the affirmation decision returns a risk SCORE, and an unanswered question is maximum
    risk, never consent.

Run:  python3 -m unittest discover -s tests -t .
"""

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))

import laya_gate  # noqa: E402


class _Stub:
    """A predictor whose probabilities the test dictates, or that raises."""

    def __init__(self, probs=None, raises=None):
        self.probs, self.raises = probs, raises

    def predict(self, state, question):
        if self.raises:
            raise self.raises
        return {"answers": {"action": {"probabilities": self.probs}}}


class _Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix='layagate_')
        self._saved = (laya_gate.DECISIONS_FILE, laya_gate.load_model,
                       laya_gate.situation, laya_gate._recent_response)
        laya_gate.DECISIONS_FILE = os.path.join(self._tmp, 'decisions.jsonl')
        laya_gate.situation = lambda *a, **k: 'stub situation'
        laya_gate._recent_response = {}

    def tearDown(self):
        (laya_gate.DECISIONS_FILE, laya_gate.load_model,
         laya_gate.situation, laya_gate._recent_response) = self._saved

    def _rows(self):
        if not os.path.exists(laya_gate.DECISIONS_FILE):
            return []
        return [l for l in open(laya_gate.DECISIONS_FILE) if l.strip()]


class ResponseLane1(_Base):
    """The states and shapes where the action is not a judgement call."""

    def test_a_working_session_is_never_nudged(self):
        for state in ('WORKING', 'QUEUED'):
            v = laya_gate.respond('cc-x', 'state changed', state, log=False)
            self.assertEqual(v['lane'], 1, state)
            self.assertEqual(v['response'], 'let it continue', state)
            self.assertTrue(v['enforced'], state)

    def test_a_dead_session_is_escalated_never_nudged(self):
        v = laya_gate.respond('cc-x', 'state changed', 'DEAD', log=False)
        self.assertEqual((v['lane'], v['response']), (1, 'escalate to the operator'))

    def test_the_sessions_own_choice_ui_is_the_operators(self):
        v = laya_gate.respond('cc-x', 'picker on screen', 'NEEDS-INPUT', choice_ui=True, log=False)
        self.assertEqual((v['lane'], v['response']), (1, 'escalate to the operator'))

    def test_a_classifier_block_never_reaches_the_model(self):
        laya_gate.load_model = lambda: _Stub(raises=AssertionError('model must not be consulted'))
        for probe in ('denied by the Claude Code auto mode classifier: [Production Deploy]',
                      'blocked: [Git Destructive]'):
            v = laya_gate.respond('cc-x', probe, 'IDLE', log=False)
            self.assertEqual((v['lane'], v['response']), (1, 'escalate to the operator'), probe)


class ResponseLane3(_Base):
    """The model's answer, and what happens when it is not good enough to act on."""

    def test_a_confident_answer_is_taken(self):
        laya_gate.load_model = lambda: _Stub({'challenge its completion claim': 0.9,
                                              'let it continue': 0.1})
        v = laya_gate.respond('cc-x', 'session says it is done', 'IDLE', log=False)
        self.assertEqual(v['response'], 'challenge its completion claim')
        self.assertEqual(v['lane'], 3)

    def test_below_threshold_falls_back_to_the_standing_policy(self):
        laya_gate.load_model = lambda: _Stub({'let it continue': 0.29,
                                              'escalate to the operator': 0.28})
        v = laya_gate.respond('cc-x', 'state changed', 'IDLE', log=False)
        # An unsure model must not stop the nudge the policy would have sent.
        self.assertEqual(v['response'], 'nudge it with one concrete next increment')
        self.assertIn('standing policy', v['rule'])
        self.assertEqual(v['runner_up'], 'escalate to the operator')

    def test_an_unavailable_model_falls_back_to_the_standing_policy(self):
        laya_gate.load_model = lambda: _Stub(raises=RuntimeError('mlx exploded'))
        for state, expected in (('IDLE', 'nudge it with one concrete next increment'),
                                ('NEEDS-INPUT', 'escalate to the operator'),
                                ('WORKING', 'let it continue')):
            v = laya_gate.respond('cc-x', 'state changed', state, log=False)
            self.assertEqual(v['response'], expected, state)

    def test_a_repeated_response_is_not_repeated(self):
        laya_gate.load_model = lambda: _Stub({'nudge it with one concrete next increment': 0.95})
        first = laya_gate.respond('cc-x', 'state changed', 'IDLE', log=False)
        second = laya_gate.respond('cc-x', 'state changed', 'IDLE', log=False)
        self.assertEqual(first['response'], 'nudge it with one concrete next increment')
        self.assertEqual(second['lane'], 2)
        self.assertEqual(second['response'], 'let it continue')


class DefaultResponse(unittest.TestCase):
    def test_the_standing_policy_is_reproduced(self):
        self.assertEqual(laya_gate._default_response('IDLE'),
                         'nudge it with one concrete next increment')
        self.assertEqual(laya_gate._default_response('NEEDS-INPUT'), 'escalate to the operator')
        self.assertEqual(laya_gate._default_response('DEAD'), 'escalate to the operator')
        self.assertEqual(laya_gate._default_response('WORKING'), 'let it continue')
        # An unrecognised state must not read as "nothing to do": the nudge is the safe default.
        self.assertEqual(laya_gate._default_response(None),
                         'nudge it with one concrete next increment')


class Affirmation(_Base):
    """Risk scoring, and the shapes that never get scored."""

    def test_destructive_money_and_credential_shapes_never_reach_the_model(self):
        laya_gate.load_model = lambda: _Stub(raises=AssertionError('model must not be consulted'))
        for action in ('git push --force origin main',
                       'kubectl delete pod api-7f9',
                       'rotate the production API key',
                       'charge the client card for the extra month',
                       'blocked by the classifier: [Security Weaken]'):
            v = laya_gate.affirm('cc-x', action, log=False)
            self.assertEqual(v['lane'], 1, action)
            self.assertFalse(v['affirm'], action)
            self.assertEqual(v['risk'], 1.0, action)

    def test_risk_is_one_minus_p_safe(self):
        laya_gate.load_model = lambda: _Stub({'safe to affirm': 0.95, 'escalate to the operator': 0.05})
        v = laya_gate.affirm('cc-x', 'add a regression test', log=False)
        self.assertAlmostEqual(v['risk'], 0.05, places=4)
        self.assertTrue(v['affirm'])

    def test_a_risky_score_is_not_affirmed(self):
        laya_gate.load_model = lambda: _Stub({'safe to affirm': 0.40, 'escalate to the operator': 0.60})
        v = laya_gate.affirm('cc-x', 'rewrite the deploy script', log=False)
        self.assertAlmostEqual(v['risk'], 0.60, places=4)
        self.assertFalse(v['affirm'])

    def test_an_unanswered_question_is_not_consent(self):
        laya_gate.load_model = lambda: _Stub(raises=RuntimeError('model down'))
        v = laya_gate.affirm('cc-x', 'add a regression test', log=False)
        self.assertFalse(v['affirm'])
        self.assertEqual(v['risk'], 1.0)


class Logging(_Base):
    """Both decisions are reviewable, which is the only way a threshold gets calibrated."""

    def test_decisions_are_logged_with_their_kind(self):
        laya_gate.load_model = lambda: _Stub({'let it continue': 0.9})
        laya_gate.respond('cc-x', 'state changed', 'IDLE')
        laya_gate.affirm('cc-x', 'add a test')
        rows = [__import__('json').loads(l) for l in self._rows()]
        self.assertEqual([r['kind'] for r in rows], ['respond', 'affirm'])
        self.assertTrue(all(r.get('id') for r in rows), 'every decision is addressable')

    def test_log_false_writes_nothing(self):
        laya_gate.load_model = lambda: _Stub({'let it continue': 0.9})
        laya_gate.respond('cc-x', 'state changed', 'IDLE', log=False)
        laya_gate.affirm('cc-x', 'add a test', log=False)
        self.assertEqual(self._rows(), [])

    def test_the_review_keeps_the_two_kinds_apart(self):
        import json
        laya_gate.load_model = lambda: _Stub({'let it continue': 0.9})
        laya_gate.respond('cc-x', 'state changed', 'IDLE')
        laya_gate.affirm('cc-x', 'add a test')
        # An escalation decision, written the way the daemon writes one (no `kind`).
        with open(laya_gate.DECISIONS_FILE, 'a') as f:
            f.write(json.dumps({'at': int(__import__('time').time()), 'session': 'cc-y',
                                'text': 'STALL', 'id': 'deadbeef',
                                'verdict': {'lane': 1, 'escalate': True, 'rule': 'crash'}}) + '\n')
        out = laya_gate.review(1)
        self.assertIn('decisions 1 ', out.replace('\n', ' '))
        self.assertIn('OTHER DECISIONS  (2)', out)
        self.assertIn('respond: lane split', out)
        self.assertIn('affirm: lane split', out)


if __name__ == '__main__':
    unittest.main()
