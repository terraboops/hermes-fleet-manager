import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import fleet_watch

class NudgeHoldTest(unittest.TestCase):
    def test_hold_suppresses_only_nudges_for_named_session(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / 'holds.json'
            p.write_text(json.dumps({'cc-w-target': {'reason': 'PR approval unchanged'}}))
            with patch.object(fleet_watch, 'NUDGE_HOLD_FILE', str(p), create=True), patch.object(fleet_watch, '_episode_for', return_value={'nudge_count': 999, 'episode_id': 'x', 'first_nudge_at': None, 'last_nudge_at': None}) as episode:
                self.assertIsNone(fleet_watch._maybe_nudge('cc-w-target', 1000, state='IDLE'))
                episode.assert_not_called()
            self.assertFalse(fleet_watch.is_paused('cc-w-target'))

    def test_unheld_and_bad_files_do_not_silence(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / 'holds.json'
            with patch.object(fleet_watch, 'NUDGE_HOLD_FILE', str(p)):
                for text in ('{}', '{"other":{"reason":"gate"}}', '[]', 'broken'):
                    p.write_text(text)
                    self.assertFalse(fleet_watch.nudge_held('target'))

if __name__ == '__main__': unittest.main()
