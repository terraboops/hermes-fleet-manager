#!/usr/bin/env python3
"""Tests for the overwatch report budget's ledger.

The budget answers one question -- "has this session's overwatch already reported
inside the hour?" -- and it answers it by reading its own past run records. That
read has to agree with DELIVERY, or the ledger spends the budget on runs nobody
received and holds a legitimate routine report back for an hour.

The failure these tests exist to stop (observed live 2026-09-29): the ledger only
looked at the first 60 characters of a run's response for the silence marker. Runs
that answer with a short note and put the marker on their LAST line -- which the
cron scheduler treats as silence and suppresses -- were counted as delivered
reports. Two such runs in a row consumed the hour's budget while the operator
received nothing.

Run:  python3 -m unittest discover -s tests -t .
"""

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'scripts'))

import fleet_overwatch  # noqa: E402


def _run_file(body, tmpdir, name='run.md'):
    path = os.path.join(tmpdir, name)
    with open(path, 'w') as fh:
        fh.write("# Cron Job: wolfgang-overwatch\n\n## Run Context\n\ntext\n\n## Response\n\n")
        fh.write(body)
    return path


class TestSilenceRecognition(unittest.TestCase):
    """The ledger's verdict on a response must match the scheduler's."""

    def test_marker_alone_is_silence(self):
        self.assertTrue(fleet_overwatch._is_silence('[SILENT]'))

    def test_note_then_marker_on_the_last_line_is_silence(self):
        # The live regression: this was counted as a delivered report.
        body = ('No intervention: state is WORKING with heartbeats advancing.\n\n'
                '[SILENT]')
        self.assertTrue(fleet_overwatch._is_silence(body))

    def test_marker_then_note_is_silence(self):
        self.assertTrue(fleet_overwatch._is_silence('[SILENT]\nno changes detected'))

    def test_bracketed_marker_as_same_line_prefix_is_silence(self):
        self.assertTrue(fleet_overwatch._is_silence('[SILENT] No changes detected'))

    def test_bracketless_marker_is_silence(self):
        self.assertTrue(fleet_overwatch._is_silence('NO_REPLY'))
        self.assertTrue(fleet_overwatch._is_silence('no reply'))

    def test_marker_buried_mid_sentence_is_a_report(self):
        body = 'I considered staying [SILENT] but the ring fired, so here is the summary.'
        self.assertFalse(fleet_overwatch._is_silence(body))

    def test_ordinary_report_is_a_report(self):
        self.assertFalse(fleet_overwatch._is_silence('Milestone: the change shipped.'))

    def test_translated_marker_is_a_report(self):
        # A translated marker does NOT suppress delivery, so the ledger must not
        # treat it as silence either.
        self.assertFalse(fleet_overwatch._is_silence('[静默]'))

    def test_empty_is_not_silence(self):
        self.assertFalse(fleet_overwatch._is_silence(''))
        self.assertFalse(fleet_overwatch._is_silence(None))


class TestLocalFallbackMirrorsScheduler(unittest.TestCase):
    """Where gateway is not importable the mirrored matcher gives the same verdicts."""

    CASES = [
        ('[SILENT]', True),
        ('No intervention.\n\n[SILENT]', True),
        ('[SILENT]\nno changes', True),
        ('[SILENT] No changes detected', True),
        ('NO_REPLY', True),
        ('no reply', True),
        ('I considered staying [SILENT] but here is the summary.', False),
        ('Milestone: the change shipped.', False),
        ('[静默]', False),
    ]

    def test_fallback_matches(self):
        for body, expected in self.CASES:
            with self.subTest(body=body):
                self.assertEqual(fleet_overwatch._local_silence_match(body), expected)


class TestDeliveredReadsRunRecords(unittest.TestCase):

    def test_silenced_run_is_not_a_delivered_report(self):
        with tempfile.TemporaryDirectory() as d:
            p = _run_file('Nudged it; nothing worth a report.\n\n[SILENT]', d)
            self.assertFalse(fleet_overwatch._delivered(p))

    def test_real_report_is_delivered(self):
        with tempfile.TemporaryDirectory() as d:
            p = _run_file('The change shipped and was validated on prod.', d)
            self.assertTrue(fleet_overwatch._delivered(p))

    def test_suppressed_tick_is_not_a_report(self):
        with tempfile.TemporaryDirectory() as d:
            p = _run_file('agent run suppressed\n\n[SILENT]', d)
            self.assertFalse(fleet_overwatch._delivered(p))

    def test_missing_response_block_is_not_a_report(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, 'run.md')
            with open(p, 'w') as fh:
                fh.write('# Cron Job: x\n\n## Run Context\n\nstill running\n')
            self.assertFalse(fleet_overwatch._delivered(p))

    def test_empty_response_is_not_a_report(self):
        with tempfile.TemporaryDirectory() as d:
            p = _run_file('   \n', d)
            self.assertFalse(fleet_overwatch._delivered(p))

    def test_unreadable_path_is_not_a_report(self):
        self.assertFalse(fleet_overwatch._delivered('/nonexistent/run.md'))


if __name__ == '__main__':
    unittest.main()
