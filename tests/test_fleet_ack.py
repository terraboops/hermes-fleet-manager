#!/usr/bin/env python3
"""Tests for fleet_ack's contract-token rule.

The failure these exist to stop: a sentinel-contract payload names its OWN done token, but
fleet_ack used to arm the completion watch on a wrapper token it invented. The session emitted
the contract token, the wrapper token never appeared, and the deadline fired SENTINEL-MISSED for
work that had actually finished -- a false alarm that cost the operator a check-in every dispatch.

Run:  python3 -m unittest discover -s tests -t .
"""

import json
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'scripts'))

import fleet_ack  # noqa: E402


REAL_CONTRACT = """SENTINEL CONTRACT
Done when: the ring agentic producer's auto-advance path is authored, wired onto the live path
and tested from this session, and you name in ONE line the single thing that genuinely cannot
be done here.
Exact done token, alone on its own line:
DONE-fw-ow-1790025010
then KEEP WORKING - do not stop at the token.

Your close-out parked on item 2; keep going from there. Nothing is gated."""

CUE_ABOVE_TOKEN = """===== SENTINEL CONTRACT =====
When the migration has actually run against the deployed database,
emit EXACTLY this one line and nothing after it:
  FW6F-POLL-LANDED
Then KEEP WORKING."""

# The shape that actually false-fired on 2026-09-22: overwatch writes "DONE CRITERION:" and
# puts the bare token on the next line. No cue in CONTRACT_CUE matched, so the watch armed on
# the wrapper token while the session emitted the contract's own.
CRITERION_CUE = """SENTINEL CONTRACT
DONE CRITERION: state, in one line each, the staged change and the deviation fix you landed,
and answer honestly whether anything is left that does not need a parked gate.
DONE-ow-230922c
then KEEP WORKING - do not stop at the token"""

LABELLED_TOKEN = """DONE CRITERION: land the increment.
DONE: DONE-fw-ow-r22
then KEEP WORKING."""


class ContractToken(unittest.TestCase):
    def test_finds_the_contracts_own_token(self):
        self.assertEqual(fleet_ack.contract_token(REAL_CONTRACT), 'DONE-fw-ow-1790025010')

    def test_accepts_a_free_form_token(self):
        self.assertEqual(fleet_ack.contract_token(CUE_ABOVE_TOKEN), 'FW6F-POLL-LANDED')

    def test_done_criterion_cue_finds_the_bare_token(self):
        self.assertEqual(fleet_ack.contract_token(CRITERION_CUE), 'DONE-ow-230922c')

    def test_labelled_token_on_its_own_line(self):
        self.assertEqual(fleet_ack.contract_token(LABELLED_TOKEN), 'DONE-fw-ow-r22')

    def test_wrapper_instruction_is_not_read_as_a_contract(self):
        # fleet_ack appends this to every payload; if it counted, the wrapper token would win
        # over the contract's own and re-create the false MISSED.
        body = fleet_ack.wrap_payload(CRITERION_CUE, 'DISPATCH-wolfgang-2',
                                      'DONE-fw3d59e2af-wolfgang-1790063361894', 'DONE-ow-230922c')
        self.assertEqual(fleet_ack.contract_token(body), 'DONE-ow-230922c')

    def test_placeholder_template_yields_none(self):
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            '..', 'templates', 'sentinel-contract.txt')
        with open(path) as fh:
            tpl = fh.read()
        self.assertIsNone(fleet_ack.contract_token(tpl))

    def test_token_mentioned_without_a_cue_is_not_a_contract(self):
        body = ("Keep working; the last run emitted token DONE-fw-ow-1790023971 and moved on.\n"
                "No contract in this payload.")
        self.assertIsNone(fleet_ack.contract_token(body))


class WrapPayload(unittest.TestCase):
    def test_contract_payload_names_one_token(self):
        wrapper = 'DONE-fw3d59e2af-wolfgang-1790025128220'
        out = fleet_ack.wrap_payload(REAL_CONTRACT, 'DISPATCH-wolfgang-1', 'DONE-fw-ow-1790025010',
                                     'DONE-fw-ow-1790025010')
        self.assertIn('[DISPATCH-wolfgang-1]', out)          # delivery marker survives
        self.assertIn('DONE-fw-ow-1790025010', out)
        self.assertNotIn(wrapper, out)                        # never a second, unspoken token
        self.assertEqual(out.count('EXACTLY this one line'), 1)

    def test_plain_payload_keeps_the_wrapper_contract(self):
        out = fleet_ack.wrap_payload('do the thing', 'DISPATCH-q-2', 'DONE-fw-abc-quad-123', None)
        self.assertIn('reply with EXACTLY this token and nothing else: DONE-fw-abc-quad-123', out)


class CompletionDeadline(unittest.TestCase):
    def test_contract_payload_floors_a_short_caller_deadline(self):
        # The observed false MISSED: 181s on a contract dispatch, fired mid-work.
        self.assertEqual(fleet_ack.completion_deadline(181, 'DONE-ow-230922c'),
                         fleet_ack.MIN_CONTRACT_DEADLINE_S)

    def test_contract_payload_keeps_a_long_deadline(self):
        self.assertEqual(fleet_ack.completion_deadline(2700, 'DONE-ow-230922c'), 2700)

    def test_plain_payload_is_left_alone(self):
        self.assertEqual(fleet_ack.completion_deadline(181, None), 181)


class Delivered(unittest.TestCase):
    """Receipt = the marker is in the transcript as a received message.

    The failure these exist to stop (2026-09-26): three dispatches to a WORKING session
    reported NOT-SUBMITTED while the daemon logged ACK-OK seconds later. A busy session
    queues the paste, so the transcript holds a queue-operation/attachment record instead
    of a user turn -- receipt all the same. Reading only `role == "user"` called that a
    swallowed Enter, which is how a real one gets ignored.
    """

    def _tx(self, records):
        import tempfile
        fd, path = tempfile.mkstemp(suffix='.jsonl')
        with os.fdopen(fd, 'w') as fh:
            for r in records:
                fh.write(json.dumps(r) + '\n')
        self.addCleanup(os.unlink, path)
        return path

    def test_user_turn_is_delivery(self):
        path = self._tx([{"type": "user", "message": {"role": "user",
                                                      "content": "hi DISPATCH-w-1"}}])
        self.assertTrue(fleet_ack.delivered("s", "DISPATCH-w-1", path=path))

    def test_queued_paste_is_delivery(self):
        path = self._tx([{"type": "queue-operation", "message": {"content": "DISPATCH-w-1"}},
                         {"type": "attachment", "content": "DISPATCH-w-1 payload"}])
        self.assertTrue(fleet_ack.delivered("s", "DISPATCH-w-1", path=path))

    def test_assistant_echo_is_not_delivery(self):
        path = self._tx([{"type": "assistant",
                          "message": {"role": "assistant", "content": "DISPATCH-w-1"}}])
        self.assertFalse(fleet_ack.delivered("s", "DISPATCH-w-1", path=path))

    def test_absent_marker_is_not_delivery(self):
        path = self._tx([{"type": "user", "message": {"role": "user", "content": "other"}}])
        self.assertFalse(fleet_ack.delivered("s", "DISPATCH-w-1", path=path))

    def test_unreadable_transcript_is_unknown_not_a_lie(self):
        self.assertIsNone(fleet_ack.delivered("s", "DISPATCH-w-1", path="/nonexistent/x.jsonl"))


if __name__ == '__main__':
    unittest.main()
