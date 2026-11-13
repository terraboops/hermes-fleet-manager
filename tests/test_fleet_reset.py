"""Fail-closed reset gates. No live fleet access."""
import importlib.util
from pathlib import Path
import unittest

SCRIPT = Path(__file__).resolve().parents[1] / 'scripts' / 'fleet_reset.py'


class ResetTests(unittest.TestCase):
    def test_parked_is_excluded_before_discovery(self):
        self.assertTrue(SCRIPT.exists(), 'safe reset capability missing')
        spec = importlib.util.spec_from_file_location('fleet_reset', SCRIPT)
        assert spec and spec.loader
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        self.assertEqual(mod.scope_reason({'desired': 'parked'}), 'parked')
        self.assertEqual(mod.scope_reason({'desired': 'up', 'profile': 'personal'}), 'not work Claude')
        self.assertIsNone(mod.scope_reason({'desired': 'up', 'profile': 'work'}))


    def test_preflight_refuses_unsafe_observations(self):
        spec = importlib.util.spec_from_file_location('fleet_reset', SCRIPT)
        assert spec and spec.loader
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        self.assertTrue(hasattr(mod, 'gate'), 'preflight gate missing')
        good = dict(transcript=True, auth=True, writers=1, binding=True,
                    children=[], status='idle', composer=True, menu=False,
                    draft=False, auto=True, background=False, launch_matches=True)
        self.assertIsNone(mod.gate(good))
        for key, value in [('transcript', False), ('auth', False), ('writers', 2),
                           ('binding', False), ('children', [123]), ('status', 'shell'),
                           ('composer', False), ('menu', True), ('draft', True),
                           ('auto', False), ('background', True), ('launch_matches', False)]:
            with self.subTest(key=key):
                self.assertIsNotNone(mod.gate(dict(good, **{key: value})))


    def test_transaction_preserves_partial_recovery_and_continues(self):
        spec = importlib.util.spec_from_file_location('fleet_reset', SCRIPT)
        assert spec and spec.loader
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        self.assertTrue(hasattr(mod, 'reset_one'), 'transaction missing')
        from unittest.mock import Mock
        import tempfile
        with tempfile.TemporaryDirectory() as directory:
            ops = Mock()
            ops.preflight.return_value = {'reason': None, 'pid': 42}
            ops.stop.side_effect = RuntimeError('still alive')
            row = mod.reset_one({'name': 'one'}, Path(directory), ops)
            self.assertEqual(row['status'], 'held')
            ops.launch.assert_not_called()
            ops.stop.side_effect = None
            ops.launch.side_effect = RuntimeError('launch failed')
            row = mod.reset_one({'name': 'two'}, Path(directory), ops)
            self.assertEqual(row['status'], 'recovery-required')
            self.assertTrue((Path(directory) / 'two.json').exists())
            ops.launch.side_effect = None
            ops.verify.return_value = {'native_response': True}
            row = mod.reset_one({'name': 'three'}, Path(directory), ops)
            self.assertEqual(row['status'], 'verified')
            self.assertEqual(set(row['elapsed_s']), {'preflight', 'stop', 'launch', 'verify', 'total'})
            ops.preflight.return_value = {'reason': 'active tools'}
            ops.stop.reset_mock()
            row = mod.reset_one({'name': 'four'}, Path(directory), ops)
            self.assertEqual(row['status'], 'held')
            ops.stop.assert_not_called()


    def test_native_receipt_requires_fresh_linked_pwd_and_final_reply(self):
        spec = importlib.util.spec_from_file_location('fleet_reset', SCRIPT)
        assert spec and spec.loader
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        self.assertTrue(hasattr(mod, 'receipt'), 'native verification missing')
        records = [
            {'type': 'user', 'sessionId': 'u', 'message': {'content': 'RESET-abc'}},
            {'type': 'assistant', 'sessionId': 'u', 'message': {'content': [
                {'type': 'tool_use', 'id': 't', 'name': 'Bash', 'input': {'command': 'pwd'}}]}},
            {'type': 'user', 'sessionId': 'u', 'message': {'content': [
                {'type': 'tool_result', 'tool_use_id': 't', 'content': '/repo', 'is_error': False}]}},
            {'type': 'assistant', 'sessionId': 'u', 'message': {'content': [
                {'type': 'text', 'text': 'RESET-abc I retain the prior review constraints.'}]}}]
        self.assertTrue(mod.receipt(records, 'u', 'RESET-abc', '/repo'))
        self.assertFalse(mod.receipt(records[1:], 'u', 'RESET-abc', '/repo'))
        self.assertFalse(mod.receipt(records[:-1], 'u', 'RESET-abc', '/repo'))
        self.assertFalse(mod.receipt(records, 'other', 'RESET-abc', '/repo'))
        records[2]['message']['content'][0]['is_error'] = True
        self.assertFalse(mod.receipt(records, 'u', 'RESET-abc', '/repo'))

    def test_runtime_preflight_missing_transcript_never_stops(self):
        spec = importlib.util.spec_from_file_location('fleet_reset', SCRIPT)
        assert spec and spec.loader
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        self.assertTrue(hasattr(mod, 'Runtime'), 'live adapter missing')
        import tempfile
        with tempfile.TemporaryDirectory() as directory:
            runtime = mod.Runtime(Path(directory), Path(directory), auth=True)
            row = runtime.preflight(dict(name='test', desired='up', profile='work', uuid='not-a-uuid'))
            self.assertIn('UUID', row['reason'])


    def test_cli_help_has_explicit_apply_and_cache_scope_is_narrow(self):
        import subprocess
        p = subprocess.run(['python3', str(SCRIPT), '--help'], capture_output=True, text=True)
        self.assertIn('--apply', p.stdout)
        spec = importlib.util.spec_from_file_location('fleet_reset', SCRIPT)
        assert spec and spec.loader
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        state = {'one': 120, '_liveness': {'one': False, 'two': True},
                 '_stall': {'one': 1}, '_fired': {'one': ['watch']}, '_usage_limit': {'one': 5}}
        mod.clear_binding(state, 'one')
        self.assertNotIn('one', state['_liveness'])
        self.assertEqual(state['_liveness']['two'], True)
        self.assertEqual(state['_fired']['one'], ['watch'])
        self.assertEqual(state['one'], 120)


    def test_native_inventory_uses_one_utc_process_snapshot(self):
        spec = importlib.util.spec_from_file_location('fleet_reset', SCRIPT)
        assert spec and spec.loader
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        import tempfile, json
        from unittest.mock import patch
        from types import SimpleNamespace
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            (home / 'sessions').mkdir()
            for pid in (1, 2):
                (home / 'sessions' / f'{pid}.json').write_text(json.dumps({'pid': pid, 'procStart': 'Sat Oct 31 06:07:33 2026'}))
            with patch.object(mod, 'run', return_value=SimpleNamespace(stdout='1 Sat Oct 31 06:07:33 2026\n2 Sat Oct 31 06:07:33 2026\n')) as run:
                self.assertEqual(len(mod.native_sessions(home)), 2)
                self.assertEqual(run.call_count, 1)
                self.assertEqual(run.call_args.kwargs['env']['TZ'], 'UTC')


    def test_batch_has_bounded_parallel_workers_and_reports_completion(self):
        spec = importlib.util.spec_from_file_location('fleet_reset', SCRIPT)
        assert spec and spec.loader
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        self.assertTrue(hasattr(mod, 'batch'), 'bounded worker batch missing')
        import threading
        barrier = threading.Barrier(3)
        def work(entry):
            barrier.wait(timeout=2)
            return entry
        metrics = {}
        rows = list(mod.batch([1, 2, 3], work, workers=3, metrics=metrics))
        self.assertEqual(sorted(rows), [1, 2, 3])
        self.assertEqual(metrics['peak_active'], 3)


    def test_shutdown_suffix_accepts_only_exit_and_bookkeeping(self):
        spec = importlib.util.spec_from_file_location('fleet_reset', SCRIPT)
        assert spec and spec.loader
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        self.assertTrue(hasattr(mod, 'shutdown_only'), 'exit bookkeeping parser missing')
        records = [
            {'type': 'permission-mode', 'permissionMode': 'auto'},
            {'type': 'file-history-snapshot', 'snapshot': {'trackedFileBackups': {}}},
            {'type': 'user', 'isMeta': True, 'message': {'content': '<local-command-caveat>x</local-command-caveat>'}},
            {'type': 'user', 'message': {'content': '<command-name>/exit</command-name>\n <command-message>exit</command-message>\n <command-args></command-args>'}},
            {'type': 'user', 'message': {'content': '<local-command-stdout>Bye!</local-command-stdout>'}}]
        self.assertTrue(mod.shutdown_only(records))
        records[3]['uuid'] = 'exit-command'
        records[4]['parentUuid'] = 'exit-command'
        records[4]['message']['content'] = '<local-command-stdout>See ya!</local-command-stdout>'
        records[1]['messageId'] = 'exit-command'
        records[1]['snapshot']['trackedFileBackups'] = {'old.py': {'version': 2}}
        self.assertTrue(mod.shutdown_only(records))
        bad = {'type': 'user', 'parentUuid': 'other-command', 'message': {'content': '<local-command-stdout>See ya!</local-command-stdout>'}}
        self.assertFalse(mod.shutdown_only(records + [bad]))
        for extra in [{'type': 'assistant'}, {'type': 'user', 'message': {'content': 'new work'}}, {'type': 'permission-mode', 'permissionMode': 'bypassPermissions'}]:
            self.assertFalse(mod.shutdown_only(records + [extra]))
        unchanged = [{'type': 'file-history-snapshot', 'snapshot': {'trackedFileBackups': {'old.py': {'version': 2}}}}]
        self.assertTrue(mod.shutdown_only(unchanged, {'old.py': {'version': 2}}))
        self.assertFalse(mod.shutdown_only(unchanged, {'old.py': {'version': 1}}))


    def test_recovery_never_repeats_an_ambiguous_launch(self):
        spec = importlib.util.spec_from_file_location('fleet_reset', SCRIPT)
        assert spec and spec.loader
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        self.assertTrue(hasattr(mod, 'recover_one'), 'checkpoint recovery missing')
        from unittest.mock import Mock
        import tempfile
        with tempfile.TemporaryDirectory() as directory:
            ops = Mock()
            ops.verify.return_value = {'native_response': True}
            row = {'entry': {'name': 'one'}, 'before': {'launch_attempted': True}, 'status': 'recovery-required'}
            with self.assertRaises(RuntimeError):
                mod.recover_one(row, Path(directory), ops)
            ops.launch.assert_not_called()
            row['before'] = {}
            out = mod.recover_one(row, Path(directory), ops)
            self.assertEqual(out['status'], 'verified')
            ops.stop.assert_not_called()
            ops.launch.assert_called_once()


    def test_process_scan_finds_unregistered_duplicate_not_wrappers(self):
        spec = importlib.util.spec_from_file_location('fleet_reset', SCRIPT)
        assert spec and spec.loader
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        self.assertTrue(hasattr(mod, 'claude_writer_pids'), 'duplicate process scan missing')
        rows = '10 /Users/me/.local/share/claude/versions/2.1.316 --resume native-id\n11 tmux new-session claude --resume native-id\n12 claude --resume native-id\n13 python script.py native-id\n'
        self.assertEqual(mod.claude_writer_pids(rows, 'native-id'), {10, 12})


if __name__ == '__main__':
    unittest.main()
