"""A lost Telegram response must not be mistaken for a confirmed failed send."""
import json
import pathlib
import sys
import tempfile
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'scripts')]
import fleet_watch as watch


class AmbiguousTelegramDelivery(unittest.TestCase):
    def test_lost_response_is_not_reported_as_confirmed_failure(self):
        accepted = []

        def server_accepts_then_connection_drops(request, timeout):
            accepted.append(json.loads(request.data)['text'])
            raise TimeoutError('response lost after server accepted')

        with mock.patch.object(watch.urllib.request, 'urlopen', side_effect=server_accepts_then_connection_drops):
            result = watch.send_telegram('test', token='fixture', chat='fixture')
        self.assertEqual(accepted, ['test'])
        self.assertIsNot(result, False, 'accepted delivery was classified as a definite failure')

    def test_uncertain_is_queued_and_survives_later_failures(self):
        with tempfile.TemporaryDirectory() as td, \
             mock.patch.object(watch, 'EVENTS', str(pathlib.Path(td) / 'events')), \
             mock.patch.object(watch, 'TELEGRAM_VIA', 'direct'), \
             mock.patch.object(watch, 'post_fleet_channel'), \
             mock.patch.object(watch, 'send_telegram', side_effect=['uncertain', False]):
            event = {'session': 'fixture', 'match': 'notice', 'urgent': False, 'at': 1}
            retry = watch.deliver_batch([event], now=100)
            self.assertTrue(retry[0]['delivery_uncertain'])
            retry[0]['retries'] = 100
            again = watch.deliver_batch(retry, now=200)
            self.assertEqual(len(again), 1)
            self.assertTrue(again[0]['delivery_uncertain'])
