import datetime
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import fleet_watch
import usage_meter as um

# A Friday, 19:00 local.
NOW = datetime.datetime(2026, 10, 16, 19, 0).timestamp()


def at(ts):
    return datetime.datetime.fromtimestamp(ts).strftime("%a %H:%M")


class ParseResetTest(unittest.TestCase):
    def test_clock_time_is_the_next_one(self):
        self.assertEqual(at(um.parse_reset("11:00 PM", NOW)), "Fri 23:00")
        self.assertEqual(at(um.parse_reset("7:00 AM", NOW)), "Sat 07:00")

    def test_weekday_is_the_next_one_even_if_today(self):
        self.assertEqual(at(um.parse_reset("Fri 2:00 AM", NOW)), "Fri 02:00")
        self.assertGreater(um.parse_reset("Fri 2:00 AM", NOW), NOW + 6 * 86400)
        self.assertEqual(at(um.parse_reset("Mon 12:00 AM", NOW)), "Mon 00:00")

    def test_countdown(self):
        self.assertEqual(um.parse_reset("in 2 hr 53 min", NOW), NOW + 2 * 3600 + 53 * 60)
        self.assertEqual(um.parse_reset("2h53m", NOW), NOW + 2 * 3600 + 53 * 60)

    def test_junk_is_refused(self):
        for bad in ("", "soon", "25:99"):
            with self.assertRaises(ValueError):
                um.parse_reset(bad, NOW)


class FitTest(unittest.TestCase):
    def test_a_reading_replaces_limit_events_that_disagree_by_an_order_of_magnitude(self):
        comps = [1_000_000, 1_000_000, 1_000_000, 10_000_000]
        events = [{"ts": 1, "scope": "session", "comps": comps},
                  {"ts": 2, "scope": "session", "comps": comps},
                  {"ts": 3, "scope": "session", "comps": comps, "frac": 0.02, "source": "reading"}]
        est = um.fit(events, "session")
        self.assertAlmostEqual(est.fraction(comps), 0.02, delta=0.005)

    def test_without_readings_limit_events_still_fit_to_one(self):
        comps = [0, 1_000_000, 0, 0]
        est = um.fit([{"ts": 1, "scope": "session", "comps": comps}], "session")
        self.assertAlmostEqual(est.fraction(comps), 1.0, delta=0.1)

    def test_three_agreeing_readings_become_trusted(self):
        evs = [{"ts": i, "scope": "weekly", "comps": [0, k * 1_000_000, 0, 0], "frac": k * 0.05,
                "source": "reading"} for i, k in enumerate((1, 2, 3))]
        self.assertTrue(um.fit(evs, "weekly").trusted())


class MeterReadingTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.prof = os.path.join(self.tmp, ".claude-test")
        os.makedirs(os.path.join(self.prof, "projects"))
        self.m = um.Meter(directory=os.path.join(self.tmp, "state"), profiles=[self.prof])
        self.p = self.m.profiles[".claude-test"]
        # 10M cache-read tokens an hour for the last 20 hours.
        for h in range(20):
            self.p.buckets[int((NOW - h * 3600) // 300 * 300)] = [0, 0, 0, 10_000_000]

    def test_reading_trains_the_session_estimate(self):
        before, after = self.m.add_reading(".claude-test", 10, "session", NOW + 3600, NOW)
        self.assertNotAlmostEqual(before, 0.10, delta=0.01)
        self.assertAlmostEqual(after, 0.10, delta=0.02)

    def test_weekly_reading_fixes_the_reset_and_projects_to_it(self):
        reset = NOW + 3 * 86400
        self.m.add_reading(".claude-test", 30, "weekly", reset, NOW)
        st = self.m.status(NOW)[".claude-test"]
        self.assertEqual(st["weekly_end"], reset)
        self.assertAlmostEqual(st["weekly_used"], 0.30, delta=0.03)
        self.assertGreater(st["weekly_projected"], st["weekly_used"])

    def test_survives_a_restart_and_a_backfill(self):
        self.m.add_reading(".claude-test", 30, "weekly", NOW + 86400, NOW)
        m2 = um.Meter(directory=os.path.join(self.tmp, "state"), profiles=[self.prof])
        self.assertEqual(len(m2.est[(".claude-test", "weekly")].events), 1)
        m2.backfill(NOW)
        self.assertTrue(any(e.get("source") == "reading" for e in m2.calibration[".claude-test"]))

    def test_a_long_lived_meter_saving_does_not_erase_a_reading_typed_elsewhere(self):
        daemon = self.m                                   # holds its state in memory for days
        cli = um.Meter(directory=os.path.join(self.tmp, "state"), profiles=[self.prof])
        cli.p = cli.profiles[".claude-test"]
        cli.p.buckets.update(self.p.buckets)
        cli.add_reading(".claude-test", 30, "weekly", NOW + 86400, NOW)
        daemon.save()                                     # the daemon's once-a-minute save
        daemon.refresh_readings()                         # and its next tick picks the reading up
        self.assertTrue(any(e.get("source") == "reading" for e in daemon.calibration[".claude-test"]))
        fresh = um.Meter(directory=os.path.join(self.tmp, "state"), profiles=[self.prof])
        self.assertEqual(len(fresh.est[(".claude-test", "weekly")].events), 1)

    def test_refuses_nonsense(self):
        with self.assertRaises(ValueError):
            self.m.add_reading(".claude-test", 0, "session", NOW + 3600, NOW)
        with self.assertRaises(ValueError):
            self.m.add_reading(".claude-test", 5, "monthly", NOW + 3600, NOW)
        with self.assertRaises(KeyError):
            self.m.add_reading(".claude-nope", 5, "session", NOW + 3600, NOW)
        with self.assertRaises(ValueError):          # a window with no tokens counted
            self.m.add_reading(".claude-test", 5, "session", NOW + 3600 + 30 * 86400, NOW + 30 * 86400)

    def test_advice_when_the_week_will_run_out(self):
        self.m.add_reading(".claude-test", 80, "weekly", NOW + 3 * 86400, NOW)
        adv = self.m.advice(self.m.status(NOW))
        self.assertEqual([a["kind"] for a in adv], ["will_run_out"])
        self.assertFalse(adv[0]["trusted"])

    def test_no_advice_when_the_week_has_room(self):
        self.m.add_reading(".claude-test", 2, "weekly", NOW + 86400, NOW)
        self.assertEqual(self.m.advice(self.m.status(NOW)), [])


class AdviceDeliveryTest(unittest.TestCase):
    ITEM = {"profile": ".claude-work", "kind": "will_run_out", "used": 0.22, "projected": 1.9,
            "eta": NOW + 86400, "reset": NOW + 6 * 86400, "trusted": False, "events": 1}

    def test_untrusted_advice_asks_for_a_reading_and_says_it_is_a_guess(self):
        t = fleet_watch.usage_advice_text(self.ITEM)
        self.assertIn("work is on course", t)
        self.assertIn("22%", t)
        self.assertIn("not calibrated", t)
        self.assertIn("weekly 22% resets", t)

    def test_trusted_advice_says_pacing_acts(self):
        t = fleet_watch.usage_advice_text({**self.ITEM, "trusted": True, "events": 4})
        self.assertIn("Pacing will slow nudges", t)
        self.assertNotIn("not calibrated", t)

    def test_said_once_a_day(self):
        d = tempfile.mkdtemp()
        with mock.patch.dict(os.environ, {"USAGE_METER_DIR": d}):
            um._write_json(os.path.join(d, "posture.json"), {"at": NOW, "advice": [self.ITEM]})
            with mock.patch.object(fleet_watch, "post_fleet_channel", return_value=True) as post:
                self.assertEqual(fleet_watch._usage_advice(NOW), 1)
                self.assertEqual(fleet_watch._usage_advice(NOW + 3600), 0)
                self.assertEqual(fleet_watch._usage_advice(NOW + 86500), 0)    # posture file is stale by then
                um._write_json(os.path.join(d, "posture.json"), {"at": NOW + 86500, "advice": [self.ITEM]})
                self.assertEqual(fleet_watch._usage_advice(NOW + 86500), 1)
                self.assertEqual(post.call_count, 2)

    def test_stale_posture_says_nothing(self):
        d = tempfile.mkdtemp()
        with mock.patch.dict(os.environ, {"USAGE_METER_DIR": d}):
            um._write_json(os.path.join(d, "posture.json"), {"at": NOW - 5000, "advice": [self.ITEM]})
            with mock.patch.object(fleet_watch, "post_fleet_channel", return_value=True) as post:
                self.assertEqual(fleet_watch._usage_advice(NOW), 0)
                post.assert_not_called()


if __name__ == "__main__":
    unittest.main()
