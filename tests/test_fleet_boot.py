#!/usr/bin/env python3
"""Tests for fleet_boot's reconcile rules.

The failures these exist to stop:
  * a live session bounced by a recovery pass (blast radius = whole fleet);
  * a parked or never-declared session resurrected by a timer;
  * a missing uuid or transcript turning into a brand-new empty conversation;
  * a crash-looping session restarted forever;
  * the restart note or the summary going out twice for one boot.

Run:  python3 -m unittest discover -s tests -t .
"""
import json
import contextlib
import io
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'scripts'))

import fleet_boot as fb  # noqa: E402


def entry(name, desired='up', uuid='11111111-2222-3333-4444-555555555555', cwd='~'):
    e = {'name': name, 'short': name.split('-', 2)[-1], 'profile': 'work',
         'config_dir': '~/.claude-work', 'cwd': cwd}
    if desired is not None:
        e['desired'] = desired
    if uuid:
        e['uuid'] = uuid
    return e


class Harness(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        fb.PROMPT_CACHE.clear()
        fb.PROMPT_LOGGED.clear()
        fb.PROMPT_ATTEMPTS.clear()
        self.alive = set()
        self.launched = []
        self.sent = []
        self.nudged = []
        self.reg = []
        self.transcripts = True
        self.pat = [
            mock.patch.object(fb, 'REG', os.path.join(self.tmp, 'reg.json')),
            mock.patch.object(fb, 'STATE', os.path.join(self.tmp, 'state.json')),
            mock.patch.object(fb, 'LOG', os.path.join(self.tmp, 'log')),
            mock.patch.object(fb, 'boot_id', lambda: '100'),
            mock.patch.object(fb, 'boot_cfg', lambda: dict(fb.DEFAULTS, max_parallel=2, restart_cap=3)),
            mock.patch.object(fb, 'tmux_alive', lambda n: n in self.alive),
            mock.patch.object(fb, 'launch', self._launch),
            mock.patch.object(fb, 'wait_batch', lambda names, timeout=0: {n: n in self.alive for n in names}),
            mock.patch.object(fb, 'transcript_for', lambda e: '/t' if self.transcripts else None),
            mock.patch.object(fb, 'ensure_for', lambda *a, **k: []),
            mock.patch.object(fb, 'send_nudge', self._nudge),
            mock.patch.object(fb, 'send_telegram', lambda t: self.sent.append(t) or True),
        ]
        for p in self.pat:
            p.start()

    def tearDown(self):
        for p in self.pat:
            p.stop()

    def _launch(self, e):
        self.launched.append(e['name'])
        self.alive.add(e['name'])      # a healthy launch

    def _nudge(self, name):
        self.nudged.append(name)
        return True

    def put(self, *entries):
        self.reg = list(entries)
        with open(fb.REG, 'w') as fh:
            json.dump({'sessions': self.reg}, fh)

    def run_reconcile(self, **k):
        return fb.reconcile(**k)


class TestRules(Harness):
    def test_codex_boot_liveness_uses_codex_tui_markers(self):
        entry = {'provider': 'codex', 'command': 'codex', 'profile': 'codex'}
        with mock.patch.object(fb, 'pane_text', return_value='› Ask Codex to do anything\\nGPT-6-Luna default'):
            self.assertTrue(fb.claude_running('codex-one', entry))
        with mock.patch.object(fb, 'pane_text', return_value='command not found: codex'):
            self.assertFalse(fb.claude_running('codex-one', entry))

    def test_codex_startup_prompt_is_needs_input_not_timeout(self):
        entry = {'name': 'codex-one', 'provider': 'codex', 'command': 'codex',
                 'profile': 'codex', 'uuid': 'thread-id', 'cwd': '/tmp/project'}
        screen = ('Hooks need review\\n  2 hooks are new or changed.\\n'
                  '› 1. Review hooks\\n  2. Trust all and continue\\n'
                  '  3. Continue without trusting (hooks won\'t run)\\n'
                  '  enter confirm · esc skip')
        wait_patch = self.pat[7]
        wait_patch.stop()
        try:
            with mock.patch.object(fb, 'load_registry', return_value={'sessions': [entry]}):
                with mock.patch.object(fb, 'tmux_alive', return_value=True):
                    with mock.patch.object(fb, 'pane_text', return_value=screen):
                        result = fb.wait_batch(['codex-one'], timeout=0.1)
        finally:
            wait_patch.start()
        self.assertEqual(result['codex-one'],
                         'NEEDS-INPUT: Codex hook review requires an owner decision (2 changed hooks)')
        with mock.patch.object(fb, 'pane_text', return_value=screen):
            self.assertTrue(fb.claude_running('codex-one', entry))

    def test_codex_project_trust_auto_policy_is_work_only_and_fails_closed_on_project_settings(self):
        screen = ('Folder access\\nTrust this folder?\\n'
                  '› 1. Trust and continue\\n  2. Back to Agent Command Center')
        prompt = fb._fleet_state.codex_startup_prompt(screen)
        work = {'provider': 'codex', 'profile': 'work', 'cwd': self.tmp,
                'codex_auto_trust_project': True}
        self.assertTrue(fb.codex_project_trust_auto_allowed(work, prompt))
        personal = dict(work, profile='personal')
        self.assertFalse(fb.codex_project_trust_auto_allowed(personal, prompt))
        config_dir = os.path.join(self.tmp, '.codex')
        os.mkdir(config_dir)
        self.assertFalse(fb.codex_project_trust_auto_allowed(work, prompt))
        self.assertFalse(fb.codex_project_trust_auto_allowed(
            work, dict(prompt, category='hook-review')))

    def test_codex_work_project_trust_is_auto_resolved_once_and_logged(self):
        e = {'name': 'cc-w-codex', 'short': 'codex', 'provider': 'codex',
             'command': 'codex', 'profile': 'work', 'codex_auto_trust_project': True,
             'cwd': self.tmp, 'uuid': 'thread-id'}
        trust = ('Folder access\\nTrust this folder?\\n'
                 '› 1. Trust and continue\\n  2. Back to Agent Command Center')
        ready = '› Ask Codex to do anything\\nGPT-6-Luna default'
        self.alive.add(e['name'])
        wait_patch = self.pat[7]
        wait_patch.stop()
        calls, events = [], []
        try:
            with contextlib.ExitStack() as stack:
                stack.enter_context(mock.patch.object(fb, 'load_registry', return_value={'sessions': [e]}))
                stack.enter_context(mock.patch.object(fb, 'pane_text', side_effect=[trust, ready, ready, ready, ready, ready]))
                stack.enter_context(mock.patch.object(fb, 'sh', side_effect=lambda *a, **k: calls.append(a) or mock.Mock(returncode=0)))
                stack.enter_context(mock.patch.object(fb, 'record_codex_prompt', side_effect=lambda *a, **k: events.append(k.get('event', 'observed'))))
                stack.enter_context(mock.patch.object(fb.time, 'sleep'))
                result = fb.wait_batch([e['name']], timeout=0.1)
        finally:
            wait_patch.start()
        self.assertEqual(result[e['name']], 'READY')
        self.assertEqual([call[-1] for call in calls], ['Enter'])
        self.assertIn('auto-resolved', events)
        self.assertIn('cleared', events)

    def test_live_codex_project_trust_is_auto_resolved_without_needs_input(self):
        e = entry('cc-w-codex', cwd=self.tmp)
        e.update({'provider': 'codex', 'command': 'codex'})
        self.put(e)
        self.alive.add(e['name'])
        trust = ('Folder access\\nTrust this folder?\\n'
                 '› 1. Trust and continue\\n  2. Back to Agent Command Center')
        ready = '› Ask Codex to do anything\\nGPT-6-Luna default'
        calls, events = [], []
        with contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch.object(fb, 'pane_text', side_effect=[trust, ready]))
            stack.enter_context(mock.patch.object(fb, 'sh', side_effect=lambda *a, **k: calls.append(a) or mock.Mock(returncode=0)))
            stack.enter_context(mock.patch.object(fb, 'record_codex_prompt', side_effect=lambda *a, **k: events.append(k.get('event', 'observed'))))
            out = self.run_reconcile(nudge=False, report=False)
        self.assertEqual(out['needs_input'], {})
        self.assertEqual([call[-1] for call in calls], ['Enter'])
        self.assertEqual(events, ['observed', 'auto-resolved', 'cleared'])
        with open(fb.STATE) as fh:
            saved = json.load(fh)
        self.assertNotIn(e['name'], saved['needs_input'])
        self.assertNotIn(e['name'], saved['codex_auto_attempts'])

    def test_codex_auto_trust_moves_selection_only_after_verifying_the_prompt(self):
        e = {'name': 'cc-w-codex', 'short': 'codex', 'provider': 'codex',
             'command': 'codex', 'profile': 'work', 'codex_auto_trust_project': True,
             'cwd': self.tmp, 'uuid': 'thread-id'}
        back = ('Folder access\\nTrust this folder?\\n'
                '  1. Trust and continue\\n› 2. Back to Agent Command Center')
        trust = ('Folder access\\nTrust this folder?\\n'
                 '› 1. Trust and continue\\n  2. Back to Agent Command Center')
        ready = '› Ask Codex to do anything\\nGPT-6-Luna default'
        self.alive.add(e['name'])
        wait_patch = self.pat[7]
        wait_patch.stop()
        calls = []
        try:
            with contextlib.ExitStack() as stack:
                stack.enter_context(mock.patch.object(fb, 'load_registry', return_value={'sessions': [e]}))
                stack.enter_context(mock.patch.object(fb, 'pane_text', side_effect=[back, trust, ready, ready, ready, ready, ready]))
                stack.enter_context(mock.patch.object(fb, 'sh', side_effect=lambda *a, **k: calls.append(a) or mock.Mock(returncode=0)))
                stack.enter_context(mock.patch.object(fb, 'record_codex_prompt'))
                stack.enter_context(mock.patch.object(fb.time, 'sleep'))
                result = fb.wait_batch([e['name']], timeout=0.1)
        finally:
            wait_patch.start()
        self.assertEqual(result[e['name']], 'READY')
        self.assertEqual([call[-1] for call in calls], ['Up', 'Enter'])

    def test_codex_startup_prompt_is_recorded_and_clears_after_owner_resolves(self):
        e = entry('cc-w-codex')
        e.update({'provider': 'codex', 'command': 'codex', 'profile': 'codex'})
        self.put(e)
        reason = 'Codex update prompt requires an owner decision'
        with mock.patch.object(fb, 'wait_batch', return_value={'cc-w-codex': 'NEEDS-INPUT: ' + reason}):
            out = self.run_reconcile(nudge=False, report=False)
        self.assertEqual(out['needs_input']['cc-w-codex'], reason)
        self.assertNotIn('cc-w-codex', out['failed'])
        saved = json.load(open(fb.STATE))
        self.assertEqual(saved['needs_input']['cc-w-codex'], reason)
        self.alive.add('cc-w-codex')
        with mock.patch.object(fb, 'pane_text', return_value='› Ask Codex to do anything'):
            self.run_reconcile(nudge=False, report=False)
        saved = json.load(open(fb.STATE))
        self.assertNotIn('cc-w-codex', saved['needs_input'])

    def test_codex_prompt_log_records_new_prompt_once_and_resolution(self):
        e = entry('cc-w-codex')
        e.update({'provider': 'codex', 'command': 'codex', 'profile': 'codex'})
        self.put(e)
        self.alive.add('cc-w-codex')
        log_path = os.path.join(self.tmp, 'codex_prompt_events.jsonl')
        screen = ('Update available · 0.162.1 → 0.164.0\\n'
                  '› 1. Update now\\n  2. Skip\\n  3. Skip until next version\\n'
                  'enter continue · esc skip')
        with mock.patch.object(fb, 'pane_text', return_value=screen), \
             mock.patch.object(fb, 'CODEX_PROMPT_LOG', log_path, create=True):
            self.run_reconcile(nudge=False, report=False)
            self.run_reconcile(nudge=False, report=False)
        with open(log_path) as fh:
            rows = [json.loads(line) for line in fh]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['event'], 'observed')
        self.assertEqual(rows[0]['category'], 'update')
        self.assertEqual(rows[0]['choices'], ['Update now', 'Skip', 'Skip until next version'])
        with contextlib.redirect_stdout(io.StringIO()) as output:
            fb.cmd_status()
        self.assertIn('choices: Update now | Skip | Skip until next version', output.getvalue())
        with mock.patch.object(fb, 'pane_text', return_value='› Ask Codex to do anything'), \
             mock.patch.object(fb, 'CODEX_PROMPT_LOG', log_path, create=True):
            self.run_reconcile(nudge=False, report=False)
        with open(log_path) as fh:
            rows = [json.loads(line) for line in fh]
        self.assertEqual([row['event'] for row in rows], ['observed', 'cleared'])

    def test_live_codex_prompt_supersedes_stale_failed_launch_mark(self):
        e = entry('cc-w-codex')
        e.update({'provider': 'codex', 'command': 'codex', 'profile': 'codex'})
        self.put(e)
        self.alive.add('cc-w-codex')
        prior = {'boot_id': '100', 'first_seen': 1, 'restarts': {},
                 'failed': {'cc-w-codex': 'harness not showing in the pane after 120s'},
                 'restored': [], 'nudged': {}, 'reported': False}
        with open(fb.STATE, 'w') as fh:
            json.dump(prior, fh)
        screen = 'Hooks need review\\n2 hooks are new or changed\\nTrust all and continue'
        with mock.patch.object(fb, 'pane_text', return_value=screen):
            out = self.run_reconcile(nudge=False, report=False)
        saved = json.load(open(fb.STATE))
        self.assertEqual(out['needs_input']['cc-w-codex'],
                         'Codex hook review requires an owner decision (2 changed hooks)')
        self.assertNotIn('cc-w-codex', saved['failed'])

    def test_codex_boot_transcript_uses_codex_home_and_uuid(self):
        entry = {'provider': 'codex', 'command': 'codex', 'profile': 'codex',
                 'uuid': 'thread-id', 'cwd': '/tmp/project', 'codex_home': self.tmp}
        expected = os.path.join(self.tmp, 'rollout.jsonl')
        with open(expected, 'w') as fh:
            fh.write('{}\\n')
        self.pat[8].stop()
        try:
            with mock.patch.object(fb.fleet_transcript, 'resolve_codex', return_value=(expected, 'session_id')) as resolve:
                self.assertEqual(fb.transcript_for(entry), expected)
            self.assertEqual(resolve.call_args.args[0]['uuid'], 'thread-id')
        finally:
            self.pat[8].start()

    def test_live_session_is_never_touched(self):
        self.put(entry('cc-w-a'), entry('cc-w-b'))
        self.alive.add('cc-w-a')
        out = self.run_reconcile()
        self.assertEqual(self.launched, ['cc-w-b'])
        self.assertIn('cc-w-a', out['never_down'])

    def test_parked_and_undeclared_are_left_down(self):
        self.put(entry('cc-w-up'), entry('cc-w-parked', desired='parked'), entry('cc-w-unset', desired=None))
        out = self.run_reconcile()
        self.assertEqual(self.launched, ['cc-w-up'])
        self.assertEqual(out['parked'], 2)

    def test_no_uuid_never_starts_a_fresh_conversation(self):
        self.put(entry('cc-w-x', uuid=None))
        out = self.run_reconcile()
        self.assertEqual(self.launched, [])
        self.assertIn('no uuid', out['failed']['cc-w-x'])

    def test_missing_transcript_refuses(self):
        self.transcripts = False
        self.put(entry('cc-w-x'))
        out = self.run_reconcile()
        self.assertEqual(self.launched, [])
        self.assertIn('transcript', out['failed']['cc-w-x'])

    def test_priority_order_comes_from_config(self):
        self.put(entry('cc-w-a'), entry('cc-w-b'), entry('cc-w-c'))
        with mock.patch.object(fb, 'boot_cfg', lambda: dict(fb.DEFAULTS, max_parallel=1, priority=['cc-w-c', 'cc-w-a'])):
            self.run_reconcile()
        self.assertEqual(self.launched, ['cc-w-c', 'cc-w-a', 'cc-w-b'])

    def test_crash_loop_is_capped_and_reported(self):
        self.put(entry('cc-w-x'))
        for _ in range(3):                # dies each time after launch
            self.run_reconcile(nudge=False, report=False)
            self.alive.discard('cc-w-x')
        out = self.run_reconcile(nudge=False, report=False)
        self.assertEqual(self.launched.count('cc-w-x'), 3)
        self.assertIn('died 3 times', out['failed']['cc-w-x'])

    def test_failed_launch_is_marked_not_retried_forever(self):
        self.put(entry('cc-w-x'))
        with mock.patch.object(fb, 'launch', lambda e: self.launched.append(e['name'])):   # never comes up
            self.run_reconcile(nudge=False, report=False)
            out = self.run_reconcile(nudge=False, report=False)
        self.assertEqual(self.launched, ['cc-w-x'])
        self.assertIn('cc-w-x', out['failed'])

    def test_dry_run_changes_nothing(self):
        self.put(entry('cc-w-x'))
        out = self.run_reconcile(dry=True)
        self.assertEqual(self.launched, [])
        self.assertEqual(out['would_launch'], ['cc-w-x'])
        self.assertFalse(os.path.exists(fb.STATE))


class TestOncePerBoot(Harness):
    def test_nudge_and_summary_once(self):
        self.put(entry('cc-w-a'), entry('cc-w-b'))
        self.run_reconcile()
        self.run_reconcile()
        self.run_reconcile()
        self.assertEqual(sorted(self.nudged), ['cc-w-a', 'cc-w-b'])
        self.assertEqual(len(self.sent), 1)
        self.assertIn('Restored (2)', self.sent[0])

    def test_new_boot_resets_failures_and_notes(self):
        self.put(entry('cc-w-a'))
        self.run_reconcile()
        self.alive.clear()
        with mock.patch.object(fb, 'boot_id', lambda: '200'):
            out = self.run_reconcile()
        self.assertTrue(out['new_boot'])
        self.assertEqual(self.launched, ['cc-w-a', 'cc-w-a'])
        self.assertEqual(len(self.sent), 2)

    def test_summary_names_open_items_and_stays_short(self):
        self.put(entry('cc-w-a'), entry('cc-w-b', uuid=None))
        self.run_reconcile()
        self.assertEqual(len(self.sent), 1)
        self.assertIn('Still open (1)', self.sent[0])
        self.assertLess(len(self.sent[0]), fb.TG_MAX + 1)


class TestLaunchLine(unittest.TestCase):
    """The failure: `exec VAR=val cmd` is not valid shell, so every launch died with status 127
    while every mocked test passed. This runs the REAL command line through a real shell."""

    def test_launch_line_runs_and_carries_env_and_resume_args(self):
        import subprocess
        e = {'name': 'cc-w-t', 'command': 'sh', 'args': ['-c', 'env | grep ^CLAUDE_CONFIG_DIR=; echo "$@"', '_'],
             'config_dir': '/tmp/cfg dir', 'uuid': 'abc-123'}
        r = subprocess.run(fb.launch_cmd(e), shell=True, capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn('CLAUDE_CONFIG_DIR=/tmp/cfg dir', r.stdout)
        self.assertIn('--resume abc-123', r.stdout)


class TestDesiredState(Harness):
    def test_park_and_up_by_short_or_name(self):
        self.put(entry('cc-w-alpha'), entry('cc-w-beta'))
        self.assertEqual(fb.set_desired(['alpha'], 'parked'), [])
        self.assertEqual(fb.set_desired(['cc-w-beta', 'ghost'], 'parked'), ['ghost'])
        reg = json.load(open(fb.REG))['sessions']
        self.assertEqual([e['desired'] for e in reg], ['parked', 'parked'])
        fb.set_desired(['alpha'], 'up')
        self.assertEqual(json.load(open(fb.REG))['sessions'][0]['desired'], 'up')


if __name__ == '__main__':
    unittest.main()
