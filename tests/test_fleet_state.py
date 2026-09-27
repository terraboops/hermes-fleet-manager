#!/usr/bin/env python3
"""Tests for the QUEUED state rule: a real indicator is short chrome, not a sentence.

The failure these exist to stop (live, 2026-09-27): a session finished its turn with the
sentence "…post a "❌ ignore the brief" note, because queued messages can't be deleted."
still in the live pane region. The old rule was a bare substring scan for "queued messages"
over that region, so a stopped, IDLE session fingerprinted as QUEUED. QUEUED means "leave
it alone", so the overwatch went silent on the one session that was parked waiting on the
operator's decision -- the false positive swallowed the escalation.

A missed queue costs nothing (the next change wakes the overwatch anyway); a false queue
costs the escalation. So the rule is a SHAPE test: short line, indicator phrasing.

Run:  python3 -m unittest discover -s tests -t .
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))

from scripts.fleet_state import queued_state  # noqa: E402

# Verbatim from the live transcript that produced the false QUEUED.
PROSE = [
    '    - Compare the live calendar against the queued briefs.',
    '    - For a cancelled or declined meeting, post a "❌ ignore the brief" note, because',
    "      queued messages can't be deleted.",
    '  So I won\'t route around the block with another tool.',
]


class TestQueuedState(unittest.TestCase):
    def test_prose_about_queued_messages_is_not_queued(self):
        self.assertFalse(queued_state(PROSE))

    def test_real_indicator_is_queued(self):
        self.assertTrue(queued_state(['❯ ', 'Press up to edit queued messages']))
        self.assertTrue(queued_state(['  1 queued message']))
        self.assertTrue(queued_state(['queued messages']))

    def test_blank_and_chrome_only_is_not_queued(self):
        self.assertFalse(queued_state([]))
        self.assertFalse(queued_state(['', '   ']))

    def test_wrapped_prose_continuation_is_not_queued(self):
        # The false positive itself: a wrapped sentence's tail row, short enough to slip any
        # length guard. Only a line that IS the indicator counts.
        self.assertFalse(queued_state(["      queued messages can't be deleted."]))
        self.assertFalse(queued_state(['      queued message handling is unchanged']))
        self.assertFalse(queued_state(['  ⏵⏵ auto mode on · 1 feedback draft']))

    def test_overlong_line_is_not_queued(self):
        self.assertFalse(queued_state(['x' * 61 + ' queued message']))
        self.assertEqual(len('queued messages'), 15)


if __name__ == '__main__':
    unittest.main()
