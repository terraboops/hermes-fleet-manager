"""A queued Codex turn is not lost merely because the current turn is busy."""
import json
import os
import pathlib
import sys
import tempfile
import time
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'scripts')]
import fleet_watch as watch


class CodexAckDeadline(unittest.TestCase):
    def test_late_native_user_turn_satisfies_ack_without_false_miss(self):
        with tempfile.TemporaryDirectory() as td:
            ack = pathlib.Path(td) / 'acks.json'
            marker = 'DISPATCH-fixture-1234567890'
            ack.write_text(json.dumps([{'session': 'cc-fixture', 'marker': marker,
                                        'deadline': time.time()-1, 'created': time.time()-61}]))
            transcript = pathlib.Path(td) / 'native.jsonl'
            transcript.write_text(json.dumps({'type': 'response_item', 'payload': {
                'type': 'message', 'role': 'user', 'content': [
                    {'type': 'input_text', 'text': 'continue\n[' + marker + ']'}]}}) + '\n')
            with mock.patch.object(watch, 'ACK_FILE', str(ack)), \
                 mock.patch.object(watch, 'find_jsonl', return_value=str(transcript)):
                events = watch.process_acks({})
            self.assertEqual(events, [])
            self.assertEqual(json.loads(ack.read_text()), [])

    def test_assistant_echo_is_not_delivery(self):
        with tempfile.TemporaryDirectory() as td:
            ack = pathlib.Path(td) / 'acks.json'
            marker = 'DISPATCH-fixture-1234567890'
            ack.write_text(json.dumps([{'session': 'cc-fixture', 'marker': marker,
                                        'deadline': time.time()-1}]))
            transcript = pathlib.Path(td) / 'native.jsonl'
            transcript.write_text(json.dumps({'type': 'response_item', 'payload': {
                'type': 'message', 'role': 'assistant', 'content': [
                    {'type': 'output_text', 'text': marker}]}}) + '\n')
            with mock.patch.object(watch, 'ACK_FILE', str(ack)), \
                 mock.patch.object(watch, 'find_jsonl', return_value=str(transcript)):
                events = watch.process_acks({})
            self.assertEqual(len(events), 1)
            self.assertIn('ACK-MISSED', events[0][1])

    def test_future_deadline_keeps_unseen_ack(self):
        with tempfile.TemporaryDirectory() as td:
            ack = pathlib.Path(td) / 'acks.json'
            row = {'session': 'cc-fixture', 'marker': 'DISPATCH-fixture-1234567890',
                   'deadline': time.time()+60}
            ack.write_text(json.dumps([row]))
            with mock.patch.object(watch, 'ACK_FILE', str(ack)), \
                 mock.patch.object(watch, 'find_jsonl') as lookup:
                self.assertEqual(watch.process_acks({}), [])
            lookup.assert_not_called()
            self.assertEqual(json.loads(ack.read_text()), [row])
