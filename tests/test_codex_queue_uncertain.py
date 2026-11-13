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

class QueueUncertainty(unittest.TestCase):
    def test_expired_codex_queue_is_preserved_and_rechecked(self):
        with tempfile.TemporaryDirectory() as td:
            ack = pathlib.Path(td) / 'acks.json'
            native = pathlib.Path(td) / 'native.jsonl'
            native.write_text('')
            marker = 'DISPATCH-fixture-1234567890'
            ack.write_text(json.dumps([{'session':'cc-fixture','marker':marker,'deadline':time.time()-1}]))
            with mock.patch.object(watch, 'ACK_FILE', str(ack)), mock.patch.object(watch, 'find_jsonl', return_value=str(native)), mock.patch.object(watch, 'load_registry', return_value={'cc-fixture': {'provider':'codex'}}):
                events = watch.process_acks({})
                self.assertEqual(len(events), 1)
                self.assertIn('uncertain', events[0][1].lower())
                self.assertNotIn('re-deliver', events[0][1])
                self.assertEqual(len(json.loads(ack.read_text())), 1)
                self.assertEqual(watch.process_acks({}), [])
                native.write_text(json.dumps({'type':'response_item','payload':{'type':'message','role':'user','content':[{'type':'input_text','text':marker}]}})+'\n')
                self.assertEqual(watch.process_acks({}), [])
                self.assertEqual(json.loads(ack.read_text()), [])

    def test_distinct_dispatch_misses_are_not_suppressed(self):
        with tempfile.TemporaryDirectory() as td, mock.patch.object(escalation, 'LOG_DIR', td):
            a = escalation.Escalation('cc-fixture', escalation.Reason.NO_SAFE_NUDGE, "ACK-MISSED: dispatch 'DISPATCH-fixture-1234567890' pending", rule='dispatch undelivered')
            b = escalation.Escalation('cc-fixture', escalation.Reason.NO_SAFE_NUDGE, "ACK-MISSED: dispatch 'DISPATCH-fixture-1234567891' pending", rule='dispatch undelivered')
            self.assertFalse(escalation.repeated(a, 1000))
            self.assertTrue(escalation.repeated(a, 1001))
            self.assertFalse(escalation.repeated(b, 1002))
