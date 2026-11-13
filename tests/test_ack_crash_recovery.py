"""Regression: a crash before the returned event is persisted must not lose it."""
import json
import pathlib
import sys
import tempfile
import time
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'scripts')]
import fleet_watch as watch
import escalation

class AckCrashRecovery(unittest.TestCase):
    def test_crash_after_ack_processing_before_event_persist_replays(self):
        with tempfile.TemporaryDirectory() as td:
            ack = pathlib.Path(td) / 'acks.json'
            marker = 'DISPATCH-fixture-1234567890'
            ack.write_text(json.dumps([{'session':'cc-fixture','marker':marker,'deadline':time.time()-1}]))
            with mock.patch.object(watch, 'ACK_FILE', str(ack)), mock.patch.object(watch, 'find_jsonl', return_value=None), mock.patch.object(watch, 'load_registry', return_value={'cc-fixture': {'provider':'codex'}}):
                self.assertEqual(len(watch.process_acks({})), 1)
                # Simulate the daemon dying before its caller persists the event.
                replay = watch.process_acks({})
                self.assertEqual(len(replay), 1, 'restart lost unpersisted uncertainty')

    def test_repeat_ledger_does_not_suppress_a_page_lost_before_persist(self):
        with tempfile.TemporaryDirectory() as td, \
             mock.patch.object(escalation, 'LOG_DIR', td), \
             mock.patch.object(escalation, 'mode', return_value='enforce'):
            text = "ACK-MISSED: dispatch 'DISPATCH-fixture-1234567890' receipt uncertain"
            first = []
            escalation.page(first, escalation.Escalation(
                'cc-fixture', escalation.Reason.NO_SAFE_NUDGE, text), now=1000)
            self.assertEqual(len(first), 1)
            # Crash discards the in-memory list before the caller persists it.
            recovered = []
            escalation.page(recovered, escalation.Escalation(
                'cc-fixture', escalation.Reason.NO_SAFE_NUDGE, text), now=1001)
            self.assertEqual(len(recovered), 1, 'repeat ledger suppressed a page that was never durable')
