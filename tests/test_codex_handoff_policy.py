import sys
from pathlib import Path
import unittest
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import codex_handoff

class HandoffPolicyTest(unittest.TestCase):
    def test_explicit_policy_survives_resume_entry(self):
        policy = getattr(codex_handoff, 'launch_policy', None)
        self.assertTrue(callable(policy), 'handoff needs an explicit persisted launch policy')
        args = policy('gpt-6.1-sol', 'read-only', ['/Users/yourname/Developer/operate'])
        self.assertEqual(args, ['--no-daemon', '--model', 'gpt-6.1-sol', '--sandbox', 'read-only', '--ask-for-approval', 'on-request', '-c', 'model_reasoning_effort="high"'])
        self.assertEqual(policy('gpt-6.1-sol', 'workspace-write', ['/Users/yourname/Developer/operate'])[-2:], ['--add-dir', '/Users/yourname/Developer/operate'])
        with self.assertRaises(ValueError):
            policy('', 'read-only', [])
        with self.assertRaises(ValueError):
            policy('gpt-6.1-sol', 'danger-full-access', [])
