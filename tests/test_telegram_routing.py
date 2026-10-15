#!/usr/bin/env python3
"""Telegram is the pager: lane 1 (and lane 3 when the gate escalates) only.

Run:  python3 -m unittest tests.test_telegram_routing
"""
import os, sys, tempfile, time, unittest
from unittest import mock

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..')
sys.path.insert(0, ROOT)
import fleet_watch as fw  # noqa: E402


def ev(match, urgent=True, **kw):
    return {"session": "s", "match": match, "at": int(time.time()), "urgent": urgent, **kw}


class Routing(unittest.TestCase):
    def setUp(self):
        d = tempfile.mkdtemp()
        self.events = os.path.join(d, "events.log")
        self.spool = os.path.join(d, "spool")
        os.mkdir(self.spool)
        self.p = [mock.patch.object(fw, "EVENTS", self.events),
                  mock.patch.object(fw, "FLEET_SPOOL", self.spool),
                  mock.patch.object(fw, "TELEGRAM_VIA", "direct"),
                  mock.patch.object(fw, "TG_TOKEN", "t"), mock.patch.object(fw, "TG_CHAT", "c")]
        for x in self.p: x.start()
        self.addCleanup(lambda: [x.stop() for x in self.p])

    def test_lane1_goes_to_channel_and_telegram(self):
        with mock.patch.object(fw, "send_telegram", return_value=True) as tg:
            self.assertEqual(fw.deliver_batch([ev("NEEDS-INPUT-x")]), [])
        self.assertEqual(len(os.listdir(self.spool)), 1)
        self.assertIn("NEEDS-INPUT-x", tg.call_args[0][0])

    def test_lane2_never_telegram(self):
        """Lane 2 posts to the channel and never enters _pending, so deliver_batch is not reached."""
        with mock.patch.object(fw, "send_telegram") as tg:
            fw.post_fleet_channel("s", "ACK-OK-1")
        tg.assert_not_called()
        self.assertEqual(len(os.listdir(self.spool)), 1)

    def test_lane3_escalate_pages_non_escalate_does_not(self):
        with mock.patch.object(fw, "send_telegram", return_value=True) as tg:
            fw.deliver_batch([ev("worth-knowing", urgent=False)])   # gate said escalate
            self.assertEqual(tg.call_count, 1)
        with mock.patch.object(fw, "send_telegram") as tg:
            fw.post_fleet_channel("s", "held")                      # gate said hold
            tg.assert_not_called()

    def test_gateway_down_telegram_ok(self):
        with mock.patch.object(fw, "post_batch", side_effect=AssertionError("webhook used")), \
                mock.patch.object(fw, "send_telegram", return_value=True):
            self.assertEqual(fw.deliver_batch([ev("SESSION-DEAD-x")]), [])

    def test_telegram_down_falls_back_and_requeues(self):
        now = 1000.0
        with mock.patch.object(fw, "send_telegram", return_value=False):
            back = fw.deliver_batch([ev("SESSION-DEAD-x")], now=now)
        self.assertIn("SESSION-DEAD-x", open(self.events).read())
        self.assertEqual((back[0]["retries"], back[0]["not_before"]), (1, now + 15))
        # retry does not repost to the channel
        n = len(os.listdir(self.spool))
        with mock.patch.object(fw, "send_telegram", return_value=True):
            self.assertEqual(fw.deliver_batch(back, now=now + 20), [])
        self.assertEqual(len(os.listdir(self.spool)), n)

    def test_dropped_after_max_retries(self):
        with mock.patch.object(fw, "send_telegram", return_value=False):
            back = fw.deliver_batch([ev("x", retries=fw.TG_MAX_RETRIES)])
        self.assertEqual(back, [])

    def test_no_token_means_fallback(self):
        with mock.patch.object(fw, "TG_TOKEN", None):
            self.assertFalse(fw.send_telegram("hi"))

    def test_webhook_flag_restores_old_path(self):
        with mock.patch.object(fw, "TELEGRAM_VIA", "webhook"), mock.patch.object(fw, "WH_URL", "u"), \
                mock.patch.object(fw, "post_batch", return_value=True) as pb, \
                mock.patch.object(fw, "send_telegram") as tg:
            self.assertEqual(fw.deliver_batch([ev("x")]), [])
        pb.assert_called_once(); tg.assert_not_called()

    def test_forty_events_one_short_message(self):
        txt = fw.format_page([ev("NEEDS-INPUT-" + "y" * 200) for _ in range(40)])
        self.assertLess(len(txt), 3500)
        self.assertRegex(txt, r"\+\d+ more")

    def test_urgent_first(self):
        txt = fw.format_page([ev("routine", urgent=False), ev("URGENT")])
        self.assertTrue(txt.splitlines()[0].endswith("URGENT"))


if __name__ == "__main__":
    unittest.main()
