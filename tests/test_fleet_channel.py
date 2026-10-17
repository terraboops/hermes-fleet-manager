#!/usr/bin/env python3
"""Lane 2 receipts go to the fleet channel spool, never to the webhook/Telegram path.

Covers the daemon side (post_fleet_channel) and, when the Hermes tree is importable, the
plugin side (spool -> channel post addressing, retry when the post fails).

Run:  python3 -m unittest tests.test_fleet_channel
"""
import ast
import importlib.util
import json
import os
import sys
import tempfile
import types
import unittest
from unittest import mock

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..')
sys.path.insert(0, ROOT)

import fleet_watch  # noqa: E402

PLUGIN = os.path.expanduser('~/.hermes/plugins/fleet')
HERMES = os.path.expanduser('~/.hermes/hermes-agent')


class DaemonSide(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def test_no_spool_dir_means_channel_not_live(self):
        self.assertFalse(fleet_watch.post_fleet_channel('s', 'ACK-OK', spool=os.path.join(self.tmp, 'absent')))

    def test_event_is_spooled_atomically(self):
        self.assertTrue(fleet_watch.post_fleet_channel('wolfgang', 'WATCH-SATISFIED-X', spool=self.tmp))
        files = os.listdir(self.tmp)
        self.assertEqual(len(files), 1)
        self.assertTrue(files[0].endswith('.json'))
        d = json.load(open(os.path.join(self.tmp, files[0])))
        self.assertEqual((d['session'], d['match']), ('wolfgang', 'WATCH-SATISFIED-X'))

    def test_spool_failure_falls_back_to_events_file(self):
        ev = os.path.join(self.tmp, 'events.log')
        spool = os.path.join(self.tmp, 'spool')
        os.mkdir(spool)
        with mock.patch.object(fleet_watch, 'EVENTS', ev), \
                mock.patch.object(fleet_watch.os, 'replace', side_effect=OSError('disk full')):
            self.assertTrue(fleet_watch.post_fleet_channel('pin', 'ACK-OK-1', spool=spool))
        self.assertIn('pin: ACK-OK-1', open(ev).read())

    def test_lane2_branches_never_touch_pending_or_telegram(self):
        """Both lane 2 branches post to the channel and `continue`: no _pending append, so no
        webhook batch and no events-file/Telegram digest."""
        src = open(os.path.join(ROOT, 'fleet_watch.py')).read()
        tree = ast.parse(src)
        main = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == 'main')
        hits = 0
        for n in ast.walk(main):
            if not isinstance(n, ast.If):
                continue
            body_nodes = [c for b in n.body for c in ast.walk(b)]
            if any(isinstance(c, ast.Call) and getattr(c.func, 'id', '') == 'post_fleet_channel' for c in body_nodes):
                # tightest branch only: skip enclosing ifs whose body merely contains another branch
                inner = [c for c in body_nodes if isinstance(c, ast.If)]
                if any(any(isinstance(d, ast.Call) and getattr(d.func, 'id', '') == 'post_fleet_channel'
                           for b in i.body for d in ast.walk(b)) for i in inner):
                    continue
                hits += 1
                self.assertFalse(any(isinstance(c, ast.Call) and getattr(c.func, 'attr', '') == 'append'
                                     for c in body_nodes))
        self.assertEqual(hits, 2)
        self.assertEqual(src.count('post_fleet_channel('), 6)   # def + bookkeeping + gate lane 2 + deliver_batch + usage setup ask + usage advice


@unittest.skipUnless(os.path.isdir(PLUGIN) and os.path.isdir(HERMES), 'hermes tree not present')
class PluginSide(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        sys.path.insert(0, HERMES)
        try:
            from gateway.config import Platform, PlatformConfig
        except Exception as e:  # wrong interpreter for the hermes tree
            raise unittest.SkipTest('hermes gateway not importable: %r' % e)
        pkg = types.ModuleType('fleetplug')
        pkg.__path__ = [PLUGIN]
        sys.modules['fleetplug'] = pkg
        spec = importlib.util.spec_from_file_location('fleetplug.adapter', os.path.join(PLUGIN, 'adapter.py'))
        cls.ad = importlib.util.module_from_spec(spec)
        sys.modules['fleetplug.adapter'] = cls.ad
        spec.loader.exec_module(cls.ad)
        Platform._add_pseudo_member('fleet')
        cls.cfg = PlatformConfig(enabled=True)

    def test_spool_to_channel_addressing_and_cleanup(self):
        spool = tempfile.mkdtemp()
        a = self.ad.FleetAdapter(self.cfg, spool_dir=spool)
        posts = []
        a._poster = posts.append
        fleet_watch.post_fleet_channel('Wolfgang', 'ACK-OK-1', spool=spool)
        fleet_watch.post_fleet_channel('pin', 'WATCH-EXPIRED-2', spool=spool)
        self.assertEqual(a.drain_once(), 2)
        self.assertEqual([(p['chat_id'], p['thread_id'], p['content']) for p in posts],
                         [('fleet', 'wolfgang', '[wolfgang] ACK-OK-1'), ('fleet', 'pin', '[pin] WATCH-EXPIRED-2')])
        self.assertEqual(os.listdir(spool), [])

    def test_failed_post_keeps_event_for_retry(self):
        spool = tempfile.mkdtemp()
        a = self.ad.FleetAdapter(self.cfg, spool_dir=spool)   # no session store attached: gateway not ready
        fleet_watch.post_fleet_channel('pin', 'ACK-OK-1', spool=spool)
        self.assertEqual(a.drain_once(), 0)
        self.assertEqual(len(os.listdir(spool)), 1)

    def test_transcript_write_uses_session_store(self):
        spool = tempfile.mkdtemp()
        a = self.ad.FleetAdapter(self.cfg, spool_dir=spool)
        store = mock.Mock()
        store.get_or_create_session.return_value = mock.Mock(session_id='sid1')
        a.set_session_store(store)
        fleet_watch.post_fleet_channel('pin', 'ACK-OK-1', spool=spool)
        self.assertEqual(a.drain_once(), 1)
        src = store.get_or_create_session.call_args.args[0]
        self.assertEqual((src.chat_id, src.thread_id), ('fleet', 'pin'))
        sid, msg = store.append_to_transcript.call_args.args
        self.assertEqual((sid, msg['content']), ('sid1', '[pin] ACK-OK-1'))

    def test_send_takes_content_and_register_sets_allowlist_env(self):
        import asyncio
        a = self.ad.FleetAdapter(self.cfg, spool_dir=tempfile.mkdtemp())
        with mock.patch.object(self.ad, 'OUTBOX', __import__('pathlib').Path(tempfile.mkdtemp()) / 'o.jsonl'):
            r = asyncio.run(a.send(chat_id='fleet', content='hi', metadata={'thread_id': 'pin'}))
        self.assertTrue(r.success)
        ctx = mock.Mock()
        self.ad.register(ctx)
        kw = ctx.register_platform.call_args.kwargs
        self.assertEqual((kw['name'], kw['allowed_users_env']), ('fleet', 'FLEET_ALLOWED_USERS'))


if __name__ == '__main__':
    unittest.main()
