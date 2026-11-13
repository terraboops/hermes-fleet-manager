import json
import pathlib
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / 'scripts'))
import fleet_transcript

class CodexTranscriptIdentity(unittest.TestCase):
    def test_child_parent_session_id_is_not_its_identity(self):
        with tempfile.TemporaryDirectory() as home:
            directory = pathlib.Path(home) / 'sessions'
            directory.mkdir()
            parent = directory / 'parent.jsonl'
            child = directory / 'child.jsonl'
            for path, meta in [(parent, {'id': 'parent', 'session_id': 'parent'}),
                               (child, {'id': 'child', 'session_id': 'parent', 'parent_thread_id': 'parent'})]:
                path.write_text(json.dumps({'type': 'session_meta', 'payload': meta}) + '\n')
            self.assertEqual(fleet_transcript.resolve_codex({'codex_home': home, 'uuid': 'parent'}), (str(parent), 'session_id'))

    def test_duplicate_true_id_remains_ambiguous(self):
        with tempfile.TemporaryDirectory() as home:
            directory = pathlib.Path(home) / 'sessions'
            directory.mkdir()
            for name in ('a', 'b'):
                (directory / (name + '.jsonl')).write_text(json.dumps({'type': 'session_meta', 'payload': {'id': 'parent', 'session_id': 'parent'}}) + '\n')
            self.assertEqual(fleet_transcript.resolve_codex({'codex_home': home, 'uuid': 'parent'}), (None, 'ambiguous'))
