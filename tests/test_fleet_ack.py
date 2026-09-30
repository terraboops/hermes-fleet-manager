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

    def test_free_form_token_behind_a_cue_wins_over_the_wrapper(self):
        # Live 2026-09-27: "DONE TOKEN: TERRATAURI-RESUME-DONE" matched TOKEN_RE neither way, so
        # the payload's token was dropped and the watch armed on the wrapper token -- the daemon
        # watched one token while the session was told to emit another.
        body = ("SENTINEL CONTRACT\n"
                "DONE CRITERION: the flag is resolved and verified against the live build.\n"
                "DONE TOKEN: TERRATAURI-RESUME-DONE\n"
                "then KEEP WORKING - do not stop at the token.\n")
        self.assertEqual(fleet_ack.contract_token(body), 'TERRATAURI-RESUME-DONE')

    def test_prose_next_to_a_cue_is_not_mistaken_for_a_token(self):
        body = ("SENTINEL CONTRACT\n"
                "DONE CRITERION: land the increment, then prove it.\n"
                "then KEEP WORKING - do not stop at the token.\n")
        self.assertIsNone(fleet_ack.contract_token(body))


class UnadoptedTokenWarning(unittest.TestCase):
    """A cue with its token below the probe window arms a watch the payload never names.

    Live 2026-09-30: a contract whose cue line was "DONE CRITERION:" followed by three numbered
    asks and only then the token. The rule returned None, the watch armed on the wrapper token,
    and the pasted payload named two different tokens. The fix is a loud report, not a guess.
    """

    CUE_TOKEN_BELOW_WINDOW = ("SENTINEL CONTRACT (MARKER-X)\n"
                              "DONE CRITERION:\n"
                              "1. Close the sentence you abandoned, in one line: the suite state.\n"
                              "2. Attack the residual by measurement and put the numbers in\n"
                              "DESIGN.md, with a pinned regression test.\n"
                              "Emit DONE-FORMA-RESIDUAL when 1-2 are actually done, then KEEP "
                              "WORKING - do not stop at the token.\n")

    def test_warns_when_the_cue_token_sits_below_the_window(self):
        self.assertIsNone(fleet_ack.contract_token(self.CUE_TOKEN_BELOW_WINDOW))
        warn = fleet_ack.unadopted_token_warning(self.CUE_TOKEN_BELOW_WINDOW, None)
        self.assertIn('DONE-FORMA-RESIDUAL', warn)
        self.assertIn('two tokens', warn)

    def test_a_passing_reference_without_a_cue_does_not_warn(self):
        body = ("Keep working; the last run emitted token DONE-fw-ow-1790023971 and moved on.\n"
                "No contract in this payload.")
        self.assertIsNone(fleet_ack.unadopted_token_warning(body, None))

    def test_an_adopted_token_does_not_warn(self):
        tok = fleet_ack.contract_token(CRITERION_CUE)
        self.assertIsNone(fleet_ack.unadopted_token_warning(CRITERION_CUE, tok))

    def test_a_cue_with_no_token_at_all_does_not_warn(self):
        body = ("SENTINEL CONTRACT\n"
                "DONE CRITERION: land the increment, then prove it.\n"
                "then KEEP WORKING - do not stop at the token.\n")
        self.assertIsNone(fleet_ack.unadopted_token_warning(body, None))


class AltToken(unittest.TestCase):
    """A contract that can legitimately END BLOCKED names a NEEDS-INPUT alternative.

    Live 2026-09-30: a session ended blocked on a human input, emitted exactly the
    NEEDS-INPUT-<slug> token its contract offered for that route, and the completion watch --
    armed on the done token alone -- expired and fired SENTINEL-MISSED anyway, buying the
    operator a check-in for a question that had already been asked and answered. The
    alternative is adopted only from a cue line, same strictness as the done token.
    """

    CONTRACT_WITH_ALT = (
        "SENTINEL CONTRACT\n"
        "DONE CRITERION: the draft no longer asserts an unverified purchase address.\n"
        "EXACT DONE TOKEN: DONE-CATBUS-emailcheck-20260930-9\n"
        "If the address genuinely cannot be established from the desk, emit EXACTLY this "
        "instead: NEEDS-INPUT-CATBUS-emailcheck-20260930-9 then KEEP WORKING - do not stop at "
        "the token.\n")

    def test_alternative_token_is_adopted(self):
        ctok = fleet_ack.contract_token(self.CONTRACT_WITH_ALT)
        self.assertEqual(ctok, 'DONE-CATBUS-emailcheck-20260930-9')
        self.assertEqual(fleet_ack.alt_token(self.CONTRACT_WITH_ALT, ctok),
                         'NEEDS-INPUT-CATBUS-emailcheck-20260930-9')

    def test_no_alternative_means_none(self):
        ctok = fleet_ack.contract_token(REAL_CONTRACT)
        self.assertIsNone(fleet_ack.alt_token(REAL_CONTRACT, ctok))

    def test_passing_reference_is_not_an_alternative(self):
        body = ("Keep working. The last run emitted NEEDS-INPUT-fw-ow-1790023971 and stopped.\n"
                "DONE CRITERION: land the increment.\nDONE-ow-230922c\nthen KEEP WORKING.\n")
        ctok = fleet_ack.contract_token(body)
        self.assertEqual(ctok, 'DONE-ow-230922c')
        self.assertIsNone(fleet_ack.alt_token(body, ctok))


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
