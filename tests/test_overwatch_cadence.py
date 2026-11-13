import json
from pathlib import Path
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import fleet_overwatch as ow


class CadenceTests(unittest.TestCase):
    def test_cadence_edits_existing_id_and_preserves_prompt_and_scope(self):
        self.assertTrue(hasattr(ow, 'cadence'), 'schedule-only fleet command missing')
        with tempfile.TemporaryDirectory() as directory:
            jobs = Path(directory) / 'jobs.json'
            job = dict(id='same-id', name='one-overwatch', enabled=True, prompt='keep', schedule={'kind':'interval','minutes':15}, deliver='local')
            jobs.write_text(json.dumps({'jobs':[job]}))
            armed = {'one': {'name':'one-overwatch', 'interval':'15m'}}
            def edit(args, **kwargs):
                self.assertEqual(args[-3:], ['same-id', '--schedule', '5m'])
                job['schedule']['minutes'] = 5
                jobs.write_text(json.dumps({'jobs':[job]}))
                return SimpleNamespace(returncode=0, stderr='')
            with patch.object(ow, 'CRON_JOBS', str(jobs)), patch.object(ow, '_load_armed', return_value=armed), patch.object(ow, '_save_armed') as save, patch.object(ow.subprocess, 'run', side_effect=edit):
                self.assertEqual(ow.cadence(SimpleNamespace(session='one', minutes=5)), 0)
                self.assertEqual(save.call_args.args[0]['one']['interval'], '5m')
                self.assertEqual(job['prompt'], 'keep')
                self.assertEqual(job['id'], 'same-id')


if __name__ == '__main__':
    unittest.main()
