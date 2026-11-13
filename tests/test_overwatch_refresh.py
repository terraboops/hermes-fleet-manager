import json
import pathlib
import sys
import tempfile
import unittest
from unittest import mock
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / 'scripts'))
import fleet_overwatch as ow

class RefreshPaused(unittest.TestCase):
    def test_refresh_edits_existing_job_without_arm_or_resume(self):
        with tempfile.TemporaryDirectory() as td:
            jobs = pathlib.Path(td)/'jobs.json'
            job = dict(id='job', name='x-overwatch', enabled=False, state='paused', context_from=['self'], deliver='local')
            jobs.write_text(json.dumps({'jobs':[job]}))
            with mock.patch.object(ow, 'CRON_JOBS', str(jobs)), mock.patch.object(ow, '_load_armed', return_value={'cc-w-x':dict(name='x-overwatch', short='x')}), mock.patch.object(ow, '_session_provider', return_value='codex'), mock.patch.object(ow.subprocess, 'run') as run:
                run.return_value.returncode=0
                self.assertTrue(hasattr(ow, 'refresh_paused'), 'missing paused in-place refresh')
                ow.refresh_paused('cc-w-x', 'preserve brief')
                cmd=run.call_args.args[0]
                self.assertEqual(cmd[1:4], ['cron','edit','job'])
                self.assertIn('CODEX PROVIDER OVERRIDE', cmd[cmd.index('--prompt')+1])
                self.assertNotIn('--deliver',cmd)
                self.assertNotIn('--no-continuity',cmd)
