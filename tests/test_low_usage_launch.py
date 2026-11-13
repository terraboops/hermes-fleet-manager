import unittest
import importlib.util
from pathlib import Path

class LowUsageLaunchTest(unittest.TestCase):
    def test_scoped_launch_preserves_identity_and_permissions(self):
        path = Path(__file__).resolve().parents[1] / 'scripts/fleet_low_usage.py'
        self.assertTrue(path.exists(), 'missing supported scoped low-usage launcher')
        spec = importlib.util.spec_from_file_location('low_usage', path)
        mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)
        old = {'name': 'x', 'uuid': 'same', 'args': ['--model','opus','--safe-mode','--restricted','--tools','Read,Bash','--allowedTools','Read']}
        new = mod.target_entry(old)
        self.assertEqual(old['args'][1], 'opus')
        self.assertEqual(new['uuid'], 'same')
        self.assertEqual(new['args'][1], 'sonnet')
        self.assertIn('--restricted', new['args'])
        self.assertIn('Read,Bash,Agent', new['args'])
        self.assertIn('Agent(wolfgang-evidence)', new['args'])

if __name__ == '__main__': unittest.main()
