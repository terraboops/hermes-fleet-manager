"""ACK detection must leave a durable event until the delivery queue owns it."""
import json
import pathlib
import tempfile
import time
from unittest import mock
import fleet_watch as watch


def test_readable_native_ack_event_survives_restart_until_queue_commit():
    with tempfile.TemporaryDirectory() as td:
        ack = pathlib.Path(td) / 'acks.json'
        native = pathlib.Path(td) / 'native.jsonl'
        native.write_text('')
        marker = 'DISPATCH-outbox-1234567890'
        ack.write_text(json.dumps([{'session': 'cc-fixture', 'marker': marker,
                                   'deadline': time.time() - 1}]))
        with mock.patch.object(watch, 'ACK_FILE', str(ack)), \
             mock.patch.object(watch, 'find_jsonl', return_value=str(native)), \
             mock.patch.object(watch, 'load_registry', return_value={'cc-fixture': {'provider': 'codex'}}):
            events = watch.process_acks({})
            assert len(events) == 1
            assert watch.process_acks({}) == []
            # No process-local state: recovery reads the durable outbox.
            assert watch.pending_ack_events() == events
            watch.commit_acks([])
            assert watch.pending_ack_events() == events
            watch.commit_acks([{'session': events[0][0], 'match': events[0][1]}])
            assert watch.pending_ack_events() == []
            assert watch.process_acks({}) == []
            native.write_text(json.dumps({'type': 'response_item', 'payload': {
                'type': 'message', 'role': 'user', 'content': [{'type': 'input_text', 'text': marker}]}}) + '\n')
            assert watch.process_acks({}) == []
            assert json.loads(ack.read_text()) == []
