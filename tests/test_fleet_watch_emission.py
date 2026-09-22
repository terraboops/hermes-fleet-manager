#!/usr/bin/env python3
"""Tests for the sentinel EMISSION rule: a token counts only as a line of its own.

The failure these exist to stop (live, 2026-09-22): a sentinel contract handed a session a
done token; the session DECLINED the contract and said so in prose -- "I am not emitting
`DONE-ow-230922v`". The token-pattern search matched that sentence, fired a MATCH, and the
armed completion watch was satisfied and retired, so the fleet overwatch went quiet on a
contract that was never met. A false emission is the dangerous direction: it reports a
completion that never happened and silently retires the watch on unfinished work. A missed
emission only fires SENTINEL-MISSED, which costs one check-in.

Run:  python3 -m unittest discover -s tests -t .
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))

import fleet_watch  # noqa: E402

TOKEN = 'DONE-ow-230922v'
DONE_PAT = [p for p in fleet_watch._token_pats('ow') + fleet_watch._generic_pats()
            if p.search(TOKEN)][0]
TRACEBACK_PAT = [p for p in fleet_watch._token_pats('ow')
                 if 'traceback' in p.pattern][0]

# Verbatim from the session that declined the contract.
REFUSAL = ("## On the token\n\nI am **not** emitting `DONE-ow-230922v`. Its criterion — "
           "stopgap LIVE, both `kustomization.yaml` edits pushed — is unreachable by me.")
# A plan to emit later, not an emission.
PLAN = f"Once the gate clears I will emit {TOKEN} and keep working."
# The shape a real emission takes: the token alone.
EMITTED = f"{TOKEN}\nKEEP WORKING on the next increment."


class EmissionOnly(unittest.TestCase):
    def test_standalone_token_fires(self):
        self.assertEqual(fleet_watch.standalone_token(EMITTED, DONE_PAT), TOKEN)

    def test_refusal_mention_does_not_fire(self):
        self.assertIsNone(fleet_watch.standalone_token(REFUSAL, DONE_PAT))

    def test_plan_mention_does_not_fire(self):
        self.assertIsNone(fleet_watch.standalone_token(PLAN, DONE_PAT))

    def test_markdown_decorated_standalone_fires(self):
        for line in (f"`{TOKEN}`", f"**{TOKEN}**", f"  {TOKEN}  ", f"*{TOKEN}*"):
            with self.subTest(line=line):
                self.assertEqual(fleet_watch.standalone_token(line, DONE_PAT), TOKEN)

    def test_traceback_stays_substring(self):
        """A traceback is a block of text, never a standalone line -- it must still fire."""
        self.assertFalse(fleet_watch.is_sentinel_pattern(TRACEBACK_PAT.pattern))
        self.assertTrue(fleet_watch.is_sentinel_pattern(DONE_PAT.pattern))
        self.assertIsNotNone(TRACEBACK_PAT.search("Traceback (most recent call last):"))

    def test_watch_not_satisfied_by_a_refusal(self):
        """The whole point: an armed watch survives a mention and clears on an emission."""
        orig_load, orig_save = fleet_watch._load_watches, fleet_watch._save_watches
        state = {"ws": [{"id": "t", "session": "cc-x", "token": TOKEN, "deadline": 2**31}]}
        fleet_watch._load_watches = lambda: list(state["ws"])
        fleet_watch._save_watches = lambda ws: state.__setitem__("ws", list(ws))
        try:
            fleet_watch.process_watches({}, {"cc-x": [REFUSAL]})
            self.assertEqual([w["token"] for w in state["ws"]], [TOKEN],
                             "watch must survive a refusal")
            fleet_watch.process_watches({}, {"cc-x": [EMITTED]})
            self.assertEqual(state["ws"], [], "watch must clear on a real emission")
        finally:
            fleet_watch._load_watches, fleet_watch._save_watches = orig_load, orig_save


if __name__ == '__main__':
    unittest.main()
