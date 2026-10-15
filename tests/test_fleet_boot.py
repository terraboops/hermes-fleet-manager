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
            mock.patch.object(fb, 'dep_embed', lambda: (True, 'up')),
            mock.patch.object(fb, 'dep_compose', lambda cfg: (True, 'ok')),
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
