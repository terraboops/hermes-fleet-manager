import pathlib
import sys
import unittest
from unittest import mock
from types import SimpleNamespace
sys.path[:0] = [str(pathlib.Path(__file__).resolve().parents[1] / 'scripts'), str(pathlib.Path(__file__).resolve().parents[1])]
import fleet_boot as boot
import fleet_state

SCREEN = '''Update available · 0.162.1 → 0.164.0
Release notes: https://github.com/openai/codex/releases/latest
› 1. Update now (runs `npm install -g @openai/codex`)
  2. Skip
  3. Skip until next version
enter continue · esc skip'''
ENTRY = {'name': 'test-update', 'provider': 'codex', 'profile': 'work', 'command': 'codex', 'uuid': '01a17e03-154e-7ea0-a079-e66266e897b3', 'cwd': '/example'}

class UpdateTests(unittest.TestCase):
    def test_official_updater_is_owner_authorized(self):
        prompt = fleet_state.codex_startup_prompt(SCREEN)
        self.assertTrue(boot.codex_update_auto_allowed(ENTRY, prompt, SCREEN))

    def test_update_verifies_install_then_resumes_exact_uuid(self):
        prompt = fleet_state.codex_startup_prompt(SCREEN)
        with mock.patch.object(boot, 'pane_text', return_value=SCREEN), \
             mock.patch.object(boot, 'transcript_for', return_value='/native/transcript'), \
             mock.patch.object(boot, 'tmux_alive', return_value=False), \
             mock.patch.object(boot, 'sh', side_effect=[SimpleNamespace(returncode=0), SimpleNamespace(returncode=0, stdout='codex-cli 0.164.0\n')]) as shell, \
             mock.patch.object(boot, 'launch') as launch, \
             mock.patch.object(boot, 'record_codex_prompt'):
            self.assertTrue(boot.auto_resolve_codex_update(ENTRY['name'], ENTRY, prompt, timeout=1))
        launch.assert_called_once_with(ENTRY)
        self.assertEqual(shell.call_args_list[0].args[-1], 'Enter')
        self.assertEqual(ENTRY['uuid'], '01a17e03-154e-7ea0-a079-e66266e897b3')

    def test_wait_batch_updates_then_waits_for_real_ready(self):
        prompt = fleet_state.codex_startup_prompt(SCREEN)
        with mock.patch.object(boot, 'load_registry', return_value={'sessions': [ENTRY]}), \
             mock.patch.object(boot, 'tmux_alive', return_value=True), \
             mock.patch.object(boot, 'codex_startup_prompt', side_effect=[prompt, None, None, None]), \
             mock.patch.object(boot, 'pane_text', return_value=SCREEN), \
             mock.patch.object(boot, 'auto_resolve_codex_update', return_value=True) as update, \
             mock.patch.object(boot, 'claude_running', return_value=True), \
             mock.patch.object(boot, 'record_codex_prompt'), \
             mock.patch.object(boot.time, 'sleep'):
            self.assertEqual(boot.wait_batch([ENTRY['name']], timeout=15), {ENTRY['name']: 'READY'})
        update.assert_called_once()

    def test_negative_menus_never_qualify(self):
        for screen in [SCREEN + '\nHooks need review: trust all hooks', SCREEN.replace('@openai/codex', 'evil'), SCREEN.replace('3. Skip until next version', '3. Trust hooks'), SCREEN.replace('› ', ''), 'Hooks need review\n› 1. Trust all and continue', 'Sign in with device code']:
            with self.subTest(screen=screen):
                self.assertFalse(boot.codex_update_auto_allowed(ENTRY, fleet_state.codex_startup_prompt(screen), screen))
        self.assertFalse(boot.codex_update_auto_allowed(dict(ENTRY, profile='personal'), fleet_state.codex_startup_prompt(SCREEN), SCREEN))

    def test_codex_resume_does_not_inherit_claude_profile_flags(self):
        import fleet_harness
        cfg={'profiles': {'work': {'provider':'claude', 'command':'claude', 'args':['--remote-control'], 'env':{'CLAUDE_CONFIG_DIR':'/claude'}}}}
        resolved=fleet_harness.resolve(ENTRY,cfg)
        self.assertNotIn('--remote-control', resolved['args'])
        self.assertEqual(resolved['resume_flag'], ['resume'])

    def test_delayed_update_screen_cannot_be_ready(self):
        entry=dict(ENTRY, profile='personal')
        prompt=fleet_state.codex_startup_prompt(SCREEN)
        with mock.patch.object(boot, 'load_registry', return_value={'sessions':[entry]}), \
             mock.patch.object(boot, 'tmux_alive', return_value=True), \
             mock.patch.object(boot, 'codex_startup_prompt', side_effect=[None,prompt]), \
             mock.patch.object(boot, 'pane_text', return_value=SCREEN), \
             mock.patch.object(boot, 'claude_running', return_value=True), \
             mock.patch.object(boot, 'record_codex_prompt'), \
             mock.patch.object(boot.time,'sleep'):
            self.assertTrue(boot.wait_batch([entry['name']],timeout=15)[entry['name']].startswith('NEEDS-INPUT'))

    def test_failed_version_timeout_or_missing_identity_never_resume(self):
        prompt=fleet_state.codex_startup_prompt(SCREEN)
        for case in ('version','timeout','identity'):
            with self.subTest(case=case), \
                 mock.patch.object(boot,'pane_text',return_value=SCREEN), \
                 mock.patch.object(boot,'transcript_for',return_value=None if case=='identity' else '/native'), \
                 mock.patch.object(boot,'tmux_alive',return_value=case=='timeout'), \
                 mock.patch.object(boot,'sh',return_value=SimpleNamespace(returncode=0,stdout='codex-cli 0.162.1')) as shell, \
                 mock.patch.object(boot,'launch') as launch, \
                 mock.patch.object(boot,'record_codex_prompt'):
                self.assertFalse(boot.auto_resolve_codex_update(ENTRY['name'],ENTRY,prompt,timeout=0))
                launch.assert_not_called()
                if case=='identity':shell.assert_not_called()

    def test_update_lifecycle_events_use_real_privacy_minimized_logger(self):
        import tempfile,json
        with tempfile.TemporaryDirectory() as root:
            path=pathlib.Path(root)/'events.jsonl'
            prompt=fleet_state.codex_startup_prompt(SCREEN)
            for event in ('update-started','update-timeout','update-version-failed','update-verified'):
                self.assertTrue(fleet_state.record_codex_prompt_event(ENTRY,prompt,event,path=str(path)))
            records=[json.loads(line) for line in path.read_text().splitlines()]
            self.assertEqual(len(records),4)
            self.assertNotIn(ENTRY['uuid'],path.read_text())

from test_fleet_boot import Harness

class ReconcileUpdateTests(Harness):
    def test_live_updater_is_applied_then_readiness_verified(self):
        e=dict(ENTRY, desired='up'); self.put(e); self.alive.add(e['name'])
        with mock.patch.object(boot,'pane_text',return_value=SCREEN), \
             mock.patch.object(boot,'auto_resolve_codex_update',return_value=True) as update, \
             mock.patch.object(boot,'record_codex_prompt'):
            result=self.run_reconcile(nudge=False,report=False)
        update.assert_called_once()
        self.assertNotIn(e['name'],result['needs_input'])

if __name__ == '__main__': unittest.main()
