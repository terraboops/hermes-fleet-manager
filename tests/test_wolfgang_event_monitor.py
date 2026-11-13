import unittest
from pathlib import Path
import importlib.util

class EventMonitorTest(unittest.TestCase):
    def test_quiet_clock_edges_are_not_events(self):
        path = Path(__file__).resolve().parents[1] / 'scripts/wolfgang_event_monitor.py'
        self.assertTrue(path.exists(), 'missing event-only monitor')
        spec = importlib.util.spec_from_file_location('wem', path)
        assert spec and spec.loader
        mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)
        a = 'IDLE|native|123|idle_due=0|done|hb=1'
        b = 'IDLE|native|123|idle_due=1|done|hb=2'
        self.assertEqual(mod.stable_state(a), mod.stable_state(b))
        self.assertNotEqual(mod.stable_state(a), mod.stable_state('IDLE|native|124|done'))
        self.assertNotEqual(mod.stable_state(a), mod.stable_state('DEAD'))

if __name__ == '__main__': unittest.main()
