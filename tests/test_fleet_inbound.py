#!/usr/bin/env python3
"""Inbound replies on the fleet channel: reach the right session as plain text, and can
never answer a choice UI or authorize a gated action.

Run:  ~/.hermes/hermes-agent/venv/bin/python -m unittest tests.test_fleet_inbound
"""
import asyncio
import importlib.util
import os
import re
import sys
import tempfile
import types
import unittest

PLUGIN = os.path.expanduser('~/.hermes/plugins/fleet')
HERMES = os.path.expanduser('~/.hermes/hermes-agent')

spec = importlib.util.spec_from_file_location('fleet_inbound_mod', os.path.join(PLUGIN, 'inbound.py'))
inb = importlib.util.module_from_spec(spec)
sys.modules['fleet_inbound_mod'] = inb
spec.loader.exec_module(inb)

ENV = {'FLEET_ALLOWED_USERS': 'terra'}
SESSIONS = [{'name': 'wolfgang-sess', 'short': 'wolfgang'}, {'name': 'pin', 'short': 'pin'},
            {'name': 'dupe-a', 'short': 'dupe'}, {'name': 'dupe-b', 'short': 'dupe'}]
QUIET = '> \n  ? for shortcuts'
CHOICE = 'Do you want to proceed?\n \u276f 1. Yes\n   2. No\nEsc to cancel'
GATED = 'Action blocked: [Production Deploy] this needs the user to approve.'


class Rec:
    def __init__(self, code=0):
        self.calls, self.code = [], code

    def __call__(self, session, path):
        self.calls.append((session, open(path).read()))
        return self.code, 'ACK armed'


def run(text='please check the migration', *, user='terra', chat='fleet', thread='wolfgang',
        env=ENV, pane=QUIET, disp=None):
    disp = disp or Rec()
    log = os.path.join(tempfile.mkdtemp(), 'in.jsonl')
    res = inb.handle_reply(user, chat, thread, text, env=env, sessions=SESSIONS,
                           pane_reader=lambda s: pane, dispatcher=disp, log_path=log)
    return res, disp


class Delivery(unittest.TestCase):
    def test_reply_reaches_the_threads_session_as_plain_message(self):
        res, d = run()
        self.assertTrue(res.ok)
        self.assertEqual([c[0] for c in d.calls], ['wolfgang-sess'])
        body = d.calls[0][1]
        self.assertIn('please check the migration', body)
        self.assertIn('not an answer to any prompt', body)
        self.assertGreater(body.count('\n'), 3)   # multi-line envelope, never a keystroke

    def test_other_session_untouched(self):
        _, d = run(thread='pin')
        self.assertEqual([c[0] for c in d.calls], ['pin'])


class Authorization(unittest.TestCase):
    def test_unset_env_refuses(self):
        res, d = run(env={})
        self.assertEqual((res.ok, res.reason, d.calls), (False, 'not-authorized', []))

    def test_empty_env_refuses(self):
        self.assertEqual(run(env={'FLEET_ALLOWED_USERS': ' , '})[0].reason, 'not-authorized')

    def test_wrong_user_refuses(self):
        res, d = run(user='mallory')
        self.assertEqual((res.reason, d.calls), ('not-authorized', []))

    def test_wildcard_and_allow_all_flag_do_not_open_it(self):
        self.assertEqual(run(env={'FLEET_ALLOWED_USERS': '*'}, user='x')[0].reason, 'not-authorized')
        env = {'FLEET_ALLOW_ALL_USERS': 'true'}
        self.assertEqual(run(env=env, user='x')[0].reason, 'not-authorized')

    def test_no_user_refuses(self):
        self.assertEqual(run(user='')[0].reason, 'not-authorized')


class Addressing(unittest.TestCase):
    def test_no_thread_unknown_thread_ambiguous_thread_wrong_chat(self):
        for kw in ({'thread': None}, {'thread': 'nobody'}, {'thread': 'dupe'}, {'chat': 'other'}):
            res, d = run(**kw)
            self.assertEqual((res.reason, d.calls), ('bad-address', []), kw)


class SafetyGuards(unittest.TestCase):
    def test_answer_like_replies_never_dispatch(self):
        for t in ['1', '2', ' 3 ', 'y', 'N', 'yes', 'no', 'approve', 'Approved.', 'allow', 'deny',
                  'proceed', 'continue', 'confirm', 'go ahead', 'do it', 'enter', 'esc', 'ctrl-c',
                  'option 2', 'ok']:
            res, d = run(t)
            self.assertEqual((res.ok, res.reason, d.calls), (False, 'answer-like', []), t)

    def test_commands_control_chars_empty_and_huge_refused(self):
        for t, why in [('/approve', 'slash-command'), ('hi\x1b[B', 'control-characters'),
                       ('   ', 'empty'), ('x' * 5000, 'too-long')]:
            res, d = run(t)
            self.assertEqual((res.reason, d.calls), (why, []), t)

    def test_choice_ui_on_screen_blocks_even_a_plain_sentence(self):
        res, d = run('option 1 is fine, go with it', pane=CHOICE)
        self.assertEqual((res.ok, res.reason, d.calls), (False, 'choice-ui-on-screen', []))

    def test_gated_action_on_screen_blocks_approval_wording(self):
        for t in ['I approve the production deploy, ship it', 'you have my permission to deploy']:
            res, d = run(t, pane=GATED)
            self.assertEqual((res.ok, res.reason, d.calls), (False, 'gated-action-on-screen', []), t)

    def test_unreadable_pane_refuses(self):
        res, d = run(pane=None)
        self.assertEqual((res.reason, d.calls), ('pane-unreadable', []))

    def test_only_route_is_fleet_ack_and_nothing_sends_keys(self):
        src = open(os.path.join(PLUGIN, 'inbound.py')).read()
        code = re.sub(r'""".*?"""', '', src, flags=re.S)
        for banned in ('fleet_answer.py', 'import fleet_answer', 'send-keys', 'send_keys', 'fleet_input',
                       'fleet_dispatch'):
            self.assertNotIn(banned, code)
        self.assertIn('fleet_ack.py', code)
        adapter_src = open(os.path.join(PLUGIN, 'adapter.py')).read()
        for banned in ('fleet_answer', 'send-keys', 'fleet_input'):
            self.assertNotIn(banned, adapter_src)

    def test_failed_dispatch_reports_not_delivered(self):
        res, _ = run(disp=Rec(code=1))
        self.assertEqual((res.ok, res.reason), (False, 'not-delivered'))


@unittest.skipUnless(os.path.isdir(HERMES), 'hermes tree not present')
class AdapterEntry(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        sys.path.insert(0, HERMES)
        try:
            from gateway.config import Platform, PlatformConfig
        except Exception as e:
            raise unittest.SkipTest('hermes gateway not importable: %r' % e)
        pkg = types.ModuleType('fleetplug2')
        pkg.__path__ = [PLUGIN]
        sys.modules['fleetplug2'] = pkg
        for name in ('inbound', 'adapter'):
            sp = importlib.util.spec_from_file_location('fleetplug2.' + name, os.path.join(PLUGIN, name + '.py'))
            m = importlib.util.module_from_spec(sp)
            sys.modules['fleetplug2.' + name] = m
            sp.loader.exec_module(m)
        cls.ad = sys.modules['fleetplug2.adapter']
        Platform._add_pseudo_member('fleet')
        cls.cfg = PlatformConfig(enabled=True)

    def _event(self, text, user='terra', thread='pin'):
        from gateway.platforms.event import MessageEvent
        a = self.ad.FleetAdapter(self.cfg, spool_dir=tempfile.mkdtemp())
        src = a.build_source(chat_id='fleet', chat_name='fleet', chat_type='dm',
                             user_id=user, user_name='t', thread_id=thread)
        return a, MessageEvent(text=text, source=src, user_id=user)

    def test_handle_message_never_starts_an_agent_turn_and_reports(self):
        a, ev = self._event('check logs')
        called, posts = [], []
        a._message_handler = lambda e: called.append(e)     # the agent-turn path
        a._inbound = lambda u, c, t, x: inb.Result(True, 'sent')
        a._poster = posts.append
        asyncio.run(a.handle_message(ev))
        self.assertEqual(called, [])
        self.assertIn('sent to the session', posts[0]['content'])

    def test_refusal_is_reported_and_choice_answers_do_not_pass(self):
        a, ev = self._event('2')
        posts = []
        a._poster = posts.append
        seen = []
        a._inbound = lambda u, c, t, x: (seen.append((u, c, t, x)), inb.Result(False, 'answer-like'))[1]
        asyncio.run(a.handle_message(ev))
        self.assertEqual(seen[0], ('terra', 'fleet', 'pin', '2'))
        self.assertIn('NOT sent (answer-like)', posts[0]['content'])


if __name__ == '__main__':
    unittest.main()
