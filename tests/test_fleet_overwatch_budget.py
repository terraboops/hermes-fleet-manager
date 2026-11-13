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

import argparse
import importlib
import json
import os
import sys
import tempfile
import unittest
from contextlib import ExitStack
from unittest import mock

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

    def test_translated_marker_is_silence(self):
        # The scheduler suppresses a translated marker (zh-Hans is in its marker set), so the
        # ledger must count it as silence too, or it spends the budget on a run nobody received.
        self.assertTrue(fleet_overwatch._is_silence('[静默]'))

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
        ('[静默]', True),
        ('静默。', True),
        ('Silent retry succeeded.', False),
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


class TestProviderAwarePrompt(unittest.TestCase):
    def test_session_provider_comes_from_registry(self):
        state = importlib.import_module("fleet_state")
        with mock.patch.object(state, "transcript_entry", return_value={"provider": "codex"}):
            self.assertEqual(fleet_overwatch._session_provider("cc-w-taste-codex-2"), "codex")

    def test_sandbox_arm_stores_provider_specific_codex_prompt(self):
        state = importlib.import_module("fleet_state")
        session = "cc-w-taste-codex-2"
        scratch = os.path.expanduser("~/.hermes/cache/scratch")
        with tempfile.TemporaryDirectory(prefix="codex-overwatch-arm-", dir=scratch) as tmp:
            root = os.path.join(tmp, "cc-watch")
            overwatch = os.path.join(root, "overwatch")
            os.makedirs(overwatch)
            registry = os.path.join(tmp, "registry.json")
            entry = {"name": session, "short": "taste-codex", "provider": "codex",
                     "uuid": "01a17a5a-100f-78f3-92f4-65802cee3c72"}
            with open(registry, "w", encoding="utf-8") as fh:
                json.dump({"sessions": [entry]}, fh)
            with open(os.path.join(root, "fleet_state.py"), "w", encoding="utf-8") as fh:
                fh.write("print('IDLE|01a17a5a-100|assistant')" + chr(10))

            capture = os.path.join(tmp, "fake-cron-argv.json")
            fake_hermes = os.path.join(tmp, "hermes")
            fake_source = chr(10).join((
                "#!" + sys.executable,
                "import json, os, sys",
                "with open(os.environ['FLEET_TEST_CAPTURE'], 'w') as out:",
                "    json.dump(sys.argv[1:], out)",
                "print('sandbox cron stored')",
                ""))
            with open(fake_hermes, "w", encoding="utf-8") as fh:
                fh.write(fake_source)
            os.chmod(fake_hermes, 0o755)

            with ExitStack() as stack:
                stack.enter_context(mock.patch.object(state, "REGISTRY", registry))
                stack.enter_context(mock.patch.multiple(
                    fleet_overwatch, STATE=root, OVERWATCH_DIR=overwatch,
                    ARMED=os.path.join(overwatch, "armed.json"), SCRIPTS_ROOT=root,
                    HERMES=fake_hermes))
                stack.enter_context(mock.patch.object(
                    fleet_overwatch, "_cron_nudges_enabled", return_value=False))
                stack.enter_context(mock.patch.dict(os.environ, {"FLEET_TEST_CAPTURE": capture}))
                result = fleet_overwatch.arm(argparse.Namespace(
                    session=session, session_short="taste-codex", focus_file=None, focus="",
                    heartbeat="0", name="taste-codex-overwatch", interval="30m", deliver="origin"))

            self.assertEqual(result, 0)
            with open(capture, encoding="utf-8") as fh:
                argv = json.load(fh)
            prompt = argv[3]
            self.assertIn("You are the OVERWATCH for ONE Codex session", prompt)
            self.assertIn("CODEX PROVIDER OVERRIDE", prompt)
            self.assertIn("UNKNOWN -> the fingerprint is inconclusive", prompt)
            self.assertNotIn("{{", prompt)
            self.assertIn("--monitor-script", argv)
            with open(os.path.join(overwatch, "armed.json"), encoding="utf-8") as fh:
                saved = json.load(fh)
            self.assertEqual(saved[session]["name"], "taste-codex-overwatch")
            self.assertEqual(saved[session]["deliver"], "origin")

    def test_unregistered_session_has_no_provider(self):
        state = importlib.import_module("fleet_state")
        with mock.patch.object(state, "transcript_entry", return_value=None):
            self.assertIsNone(fleet_overwatch._session_provider("unknown-session"))

    def test_codex_render_contains_safe_provider_override(self):
        prompt = fleet_overwatch._render("cc-w-taste-codex-2", "", "taste-codex-2", "codex")
        self.assertIn("You are the OVERWATCH for ONE Codex session", prompt)
        self.assertIn("Recognized official Codex updates are owner-authorized", prompt)
        self.assertIn("For hook-review, sign-in, or project-trust screens", prompt)
        self.assertIn("do not use fleet_answer.py", prompt)
        self.assertIn("do not select, type, or otherwise interact", prompt)
        self.assertIn("overrides every safe-option, pick, or answer rule below", prompt)
        self.assertIn("UNKNOWN -> the fingerprint is inconclusive", prompt)
        self.assertNotIn("{{CODEX_STARTUP_RULE}}", prompt)

    def test_claude_render_does_not_add_codex_startup_rule(self):
        prompt = fleet_overwatch._render("cc-w-taste", "", "taste", "claude")
        self.assertNotIn("Codex updater, hook-review, sign-in, and project-trust prompts", prompt)


if __name__ == '__main__':
    unittest.main()
