#!/usr/bin/env python3
"""Tests for fleet_ack's contract-token rule.

The failure these exist to stop: a sentinel-contract payload names its OWN done token, but
fleet_ack used to arm the completion watch on a wrapper token it invented. The session emitted
the contract token, the wrapper token never appeared, and the deadline fired SENTINEL-MISSED for
work that had actually finished -- a false alarm that cost the operator a check-in every dispatch.

Run:  python3 -m unittest discover -s tests -t .
"""

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


class ContractToken(unittest.TestCase):
    def test_finds_the_contracts_own_token(self):
        self.assertEqual(fleet_ack.contract_token(REAL_CONTRACT), 'DONE-fw-ow-1790025010')

    def test_accepts_a_free_form_token(self):
        self.assertEqual(fleet_ack.contract_token(CUE_ABOVE_TOKEN), 'FW6F-POLL-LANDED')

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


if __name__ == '__main__':
    unittest.main()
