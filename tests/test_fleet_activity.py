import importlib.util
from pathlib import Path
import unittest

PATH = Path(__file__).resolve().parents[1] / 'scripts/fleet_activity.py'


def event(kind, content=None, timestamp=100, **extra):
    return {'type': kind, 'timestamp': timestamp, 'message': {'content': content or []}, **extra}


def tool(uid):
    return {'type': 'tool_use', 'id': uid, 'name': 'Bash', 'input': {}}


def result(uid):
    return {'type': 'tool_result', 'tool_use_id': uid, 'content': 'ok'}


class ActivityTests(unittest.TestCase):
    def load(self):
        self.assertTrue(PATH.exists(), 'native activity reducer missing')
        spec = importlib.util.spec_from_file_location('fleet_activity', PATH)
        assert spec and spec.loader
        m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(m)
        return m

    def test_long_tool_and_parallel_unmatched_are_never_idle(self):
        m = self.load()
        records = [event('assistant', [tool('a'), tool('b')]), event('user', [result('a')], 101)]
        r = m.classify(records, now=2000, alive=True, supervised=True)
        self.assertEqual(r['state'], 'WAITING_TOOL')
        self.assertEqual(r['pending_tools'], ['b'])
        self.assertFalse(r['idle_eligible'])
        self.assertTrue(r['stale_observation'])

    def test_completed_turn_clock_ignores_housekeeping_and_scope(self):
        m = self.load()
        records = [event('assistant', [{'type': 'text', 'text': 'done'}])]
        records[0]['message']['stop_reason'] = 'end_turn'
        records.append(event('system', timestamp=399, subtype='progress'))
        self.assertFalse(m.classify(records, now=399, alive=True, supervised=True)['idle_eligible'])
        r = m.classify(records, now=400, alive=True, supervised=True)
        self.assertTrue(r['idle_eligible'])
        self.assertEqual(r['last_meaningful_at'], 100)
        self.assertFalse(m.classify(records, now=400, alive=True, supervised=False)['idle_eligible'])

    def test_background_completion_and_owner_hold(self):
        m = self.load()
        records = [event('assistant', [tool('a')]), event('user', [result('a')], toolUseResult={'backgroundTaskId': 'bg'})]
        r = m.classify(records, now=500, alive=True, supervised=True)
        self.assertEqual(r['state'], 'WAITING_TOOL')
        records += [event('user', '<task-notification><task-id>bg</task-id><status>completed</status></task-notification>', 200), event('assistant', timestamp=201)]
        records[-1]['message']['stop_reason'] = 'end_turn'
        self.assertTrue(m.classify(records, now=501, alive=True, supervised=True)['idle_eligible'])
        self.assertEqual(m.classify(records, now=501, alive=True, approval=True)['state'], 'NEEDS_INPUT')
        records[2] = {'type': 'queue-operation', 'operation': 'enqueue', 'timestamp': 200, 'content': '<task-notification><task-id>bg</task-id><status>completed</status></task-notification>'}
        self.assertTrue(m.classify(records, now=501, alive=True, supervised=True)['idle_eligible'])
        records.append({'type': 'queue-operation', 'operation': 'enqueue', 'timestamp': 500, 'content': 'new real instruction'})
        self.assertFalse(m.classify(records, now=501, alive=True, supervised=True)['idle_eligible'])

    def test_dead_truncated_and_live_child_fail_closed(self):
        m = self.load()
        records = [event('assistant')]
        records[0]['message']['stop_reason'] = 'end_turn'
        self.assertEqual(m.classify(records, now=500, alive=False)['state'], 'DEAD')
        self.assertEqual(m.classify(records, now=500, alive=True, truncated=True)['state'], 'UNKNOWN')
        self.assertEqual(m.classify(records, now=500, alive=True, child_pids=[42])['state'], 'WAITING_TOOL')
        old = [event('assistant', [tool('a')]), event('user', [result('a')], toolUseResult={'backgroundTaskId': 'old'})]
        self.assertEqual(m.classify(old, now=500, alive=True, process_started_at=200)['state'], 'UNKNOWN')


    def test_native_status_is_used_by_monitor_and_has_no_heartbeat(self):
        import sys
        sys.path.insert(0, str(PATH.parent))
        import fleet_state
        from unittest.mock import patch
        from types import SimpleNamespace
        m = self.load()
        observed = {'state': 'WAITING_TOOL', 'reason': 'unmatched tool', 'last_meaningful_at': 100, 'idle_eligible': False}
        with patch.object(fleet_state, 'transcript_entry', return_value={'profile': 'work', 'provider': 'claude', 'uuid': 'u'}), patch.object(fleet_state, 'transcript_of', return_value='/fixture'), patch.object(fleet_state, 'sh', return_value=''), patch.object(fleet_state.subprocess, 'run', return_value=SimpleNamespace(returncode=0)), patch('fleet_activity.inspect_entry', return_value=observed):
            value = fleet_state.fingerprint('test')
            self.assertTrue(value.startswith('WORKING|native|'), value)
            self.assertIn('unmatched tool', value)
            self.assertNotIn('hb=', value)


if __name__ == '__main__':
    unittest.main()
