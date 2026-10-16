#!/usr/bin/env python3
"""Usage meter: parsing, dedupe, windows, estimator, controller, and the nudge hook.

Run:  python3 -m unittest tests.test_usage_meter
"""
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))

import usage_meter as um  # noqa: E402

T0 = 1_800_000_000 // 3600 * 3600       # an hour boundary


def iso(ts):
    from datetime import datetime, timezone
    return datetime.fromtimestamp(ts, timezone.utc).isoformat().replace("+00:00", "Z")


def usage_line(ts, mid, req, comps=(1, 100, 0, 1000)):
    return json.dumps({"type": "assistant", "timestamp": iso(ts), "requestId": req, "message": {
        "id": mid, "model": "claude-x", "usage": {
            "input_tokens": comps[0], "output_tokens": comps[1],
            "cache_creation_input_tokens": comps[2], "cache_read_input_tokens": comps[3]}}})


def limit_line(ts, text="You've hit your session limit \u00b7 resets 6pm (America/Vancouver)", api_error=True):
    return json.dumps({"type": "assistant", "timestamp": iso(ts), "isApiErrorMessage": api_error,
                       "message": {"model": "<synthetic>", "content": [{"type": "text", "text": text}],
                                   "usage": {"input_tokens": 0, "output_tokens": 0}}})


class Parsing(unittest.TestCase):
    def test_usage_record(self):
        r = um.parse_line(usage_line(T0, "m1", "r1", (2, 10, 5, 7)))
        self.assertEqual(r[0], "usage")
        self.assertEqual(r[3], (2, 10, 5, 7))

    def test_genuine_limit_message(self):
        self.assertEqual(um.parse_line(limit_line(T0))[:1], ("limit",))
        self.assertEqual(um.parse_line(limit_line(T0, "You've hit your weekly limit \u00b7 resets 2am"))[2], "weekly")

    def test_quoted_limit_text_is_not_an_event(self):
        """A user line or tool result quoting the message must not count."""
        quoted = json.dumps({"type": "user", "timestamp": iso(T0),
                             "message": {"content": "he said You've hit your session limit"}})
        self.assertIsNone(um.parse_line(quoted))
        self.assertIsNone(um.parse_line(limit_line(T0, api_error=False)))

    def test_spend_limit_is_not_a_window(self):
        text = ("You've hit your org's monthly spend limit \u00b7 run /usage-credits to raise it "
                "\u00b7 your session limit resets 6:30pm")
        self.assertIsNone(um.parse_line(limit_line(T0, text)))

    def test_garbage_and_empty_usage(self):
        self.assertIsNone(um.parse_line("not json \"usage\""))
        self.assertIsNone(um.parse_line(usage_line(T0, "m", "r", (0, 0, 0, 0))))


class Dedupe(unittest.TestCase):
    def test_same_message_counts_once(self):
        p = um.Profile("x")
        for _ in range(3):
            p.add(T0 + 10, "m:r", (1, 100, 0, 1000))
        self.assertEqual(sum(sum(v) for v in p.buckets.values()), 1101)

    def test_streamed_rewrite_keeps_larger_counts_without_double_counting(self):
        p = um.Profile("x")
        p.add(T0 + 10, "m:r", (1, 10, 0, 100))
        p.add(T0 + 20, "m:r", (1, 80, 0, 100))      # output grew while streaming
        p.add(T0 + 30, "m:r", (1, 50, 0, 100))      # a stale smaller copy changes nothing
        self.assertEqual(p.buckets[int(T0 // um.BUCKET * um.BUCKET)], [1, 80, 0, 100])


class Incremental(unittest.TestCase):
    def test_offsets_and_partial_line(self):
        with tempfile.TemporaryDirectory() as d:
            f = os.path.join(d, "s.jsonl")
            p = um.Profile("x")
            with open(f, "w") as fh:
                fh.write(usage_line(T0, "a", "1") + "\n" + usage_line(T0 + 5, "b", "2")[:40])
            self.assertEqual(p.scan_file(f), 1)        # the half line waits
            with open(f, "a") as fh:
                fh.write(usage_line(T0 + 5, "b", "2")[40:] + "\n" + usage_line(T0 + 9, "c", "3") + "\n")
            self.assertEqual(p.scan_file(f), 2)
            self.assertEqual(p.scan_file(f), 0)
            self.assertEqual(sum(v[1] for v in p.buckets.values()), 300)


class Windows(unittest.TestCase):
    def test_block_start_and_gap_rule(self):
        b = {T0 + 600: [1, 0, 0, 0], T0 + 4 * 3600: [1, 0, 0, 0], T0 + 5 * 3600 + 300: [1, 0, 0, 0]}
        bl = um.blocks(b)
        self.assertEqual(bl[0][:2], (T0, T0 + 5 * 3600))
        self.assertEqual(len(bl), 2)                   # activity past the five hour mark opens a new block
        self.assertEqual(bl[1][0], T0 + 5 * 3600)

    def test_idle_gap_opens_a_block(self):
        b = {T0: [1, 0, 0, 0], T0 + 4 * 3600: [1, 0, 0, 0]}
        self.assertEqual(len(um.blocks(b)), 1)
        b = {T0: [1, 0, 0, 0], T0 + 6 * 3600: [1, 0, 0, 0]}
        self.assertEqual(len(um.blocks(b)), 2)

    def test_usage_at_stops_at_the_event(self):
        b = {T0: [0, 10, 0, 0], T0 + 3600: [0, 10, 0, 0], T0 + 7200: [0, 10, 0, 0]}
        self.assertEqual(um.usage_at(b, T0 + 3700, "session")[1], 20)


class Calibration(unittest.TestCase):
    def profile_with_hit(self, n_repeat=4):
        p = um.Profile("x")
        p.add(T0 + 60, "a:1", (0, 1000000, 0, 0))
        for i in range(n_repeat):                     # every retry prints another message
            p.limits.append((T0 + 3600 + i, "session"))
        return p

    def test_repeats_collapse_to_one_event(self):
        ev = um.calibration_events(self.profile_with_hit())
        self.assertEqual(len(ev), 1)
        self.assertEqual(ev[0]["comps"][1], 1000000)

    def test_hit_with_no_usage_is_dropped(self):
        p = um.Profile("x")
        p.limits.append((T0 + 5, "session"))
        self.assertEqual(um.calibration_events(p), [])


class Estimator(unittest.TestCase):
    TRUE = (0.02, 0.10, 0.025, 0.002)               # fraction of limit per million tokens

    def make_event(self, comps):
        x = [c / 1e6 for c in comps]
        f = sum(t * v for t, v in zip(self.TRUE, x))
        return [c / f for c in comps]                # scale so the true usage is exactly 1.0

    def test_recovers_the_scale_from_events(self):
        mixes = [(10, 300, 40, 9000), (30, 200, 80, 12000), (5, 500, 20, 7000),
                 (20, 250, 60, 15000), (15, 400, 30, 8000)]
        evs = [[int(v) for v in self.make_event([m * 1000 for m in mix])] for mix in mixes]
        est = um.Estimator()
        est.seed_scale(evs)
        for e in evs:
            est.update(e)
        for e in evs:
            self.assertAlmostEqual(est.fraction(e), 1.0, delta=0.06)
        self.assertTrue(est.trusted())

    def test_few_events_are_not_trusted(self):
        est = um.Estimator()
        est.update([0, 1_000_000, 0, 0])
        self.assertFalse(est.trusted())

    def test_inconsistent_events_are_not_trusted(self):
        est = um.Estimator()
        for comps in ([0, 1_000_000, 0, 0], [0, 100_000_000, 0, 0], [0, 30_000_000, 0, 0]):
            est.update(comps)
        self.assertGreater(est.spread(), um.MAX_CV)
        self.assertFalse(est.trusted())

    def test_lower_bound_scales_down_never_up(self):
        est = um.Estimator()
        before = list(est.theta)
        est.lower_bound([0, 1, 0, 0])                 # far below the limit: no change
        self.assertEqual(est.theta, before)
        est.lower_bound([0, 100_000_000, 0, 0])       # estimate says over the limit, yet no hit
        self.assertLess(est.theta[1], before[1])
        self.assertAlmostEqual(est.fraction([0, 100_000_000, 0, 0]), 1.0, places=6)


class RobustFit(unittest.TestCase):
    def ev(self, tokens_m, scope="session"):
        return {"ts": 0, "scope": scope, "comps": [0, int(tokens_m * 1e6 / 5), 0, 0]}   # prior-weighted = tokens_m

    def test_outliers_are_reported_but_not_fitted(self):
        events = [self.ev(m) for m in (9, 10, 11, 12, 10, 0.2, 1.5, 30)]
        est = um.fit(events, "session")
        self.assertEqual(est.total_events, 8)
        self.assertEqual(len(est.events), 5)
        self.assertTrue(est.trusted())
        self.assertAlmostEqual(est.fraction(events[1]["comps"]), 1.0, delta=0.15)

    def test_no_cluster_stays_untrusted(self):
        events = [self.ev(m) for m in (0.2, 1.5, 4, 30, 90, 0.5, 9)]
        self.assertFalse(um.fit(events, "session").trusted())

    def test_scopes_do_not_mix(self):
        events = [self.ev(10)] * 3 + [self.ev(200, "weekly")] * 3
        self.assertAlmostEqual(um.fit(events, "weekly").fraction(self.ev(200)["comps"]), 1.0, delta=0.15)
        self.assertAlmostEqual(um.fit(events, "session").fraction(self.ev(10)["comps"]), 1.0, delta=0.15)

    def test_state_round_trip_keeps_the_totals(self):
        est = um.fit([self.ev(m) for m in (9, 10, 11, 0.2)], "session")
        again = um.Estimator.from_json(json.loads(json.dumps(est.to_json())))
        self.assertEqual((again.total_events, len(again.events)), (4, 3))


class ControllerTests(unittest.TestCase):
    def test_untrusted_only_observes(self):
        c = um.Controller()
        self.assertEqual(c.step(1, 0.2, False, False, 100)[0], "observe")

    def test_under_pace_pushes_and_over_pace_holds(self):
        c = um.Controller()
        self.assertEqual(c.step(1, 0.3, True, False, 100)[0], "push")
        c = um.Controller()
        self.assertEqual(c.step(1, 1.4, True, False, 100)[0], "hold")

    def test_on_target_is_steady(self):
        c = um.Controller()
        self.assertEqual(c.step(1, 0.9, True, False, 100)[0], "steady")

    def test_hit_holds_for_the_rest_of_the_window_then_releases(self):
        c = um.Controller()
        self.assertEqual(c.step(1, 0.3, True, True, 100)[0], "hold")
        self.assertEqual(c.step(1, 0.3, True, False, 200)[0], "hold")     # still the same window
        self.assertEqual(c.step(2, 0.3, True, False, 300)[0], "push")     # a new window

    def test_integral_is_clamped_and_halved_across_windows(self):
        c = um.Controller()
        for i in range(200):
            c.step(1, 0.0, True, False, 100 + i * 600)
        self.assertLessEqual(c.integral, 2.0)
        top = c.integral
        c.step(2, 0.0, True, False, 10**6)
        self.assertLess(c.integral, top)


class EndToEnd(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = self.tmp.name
        self.profile = os.path.join(self.root, ".claude-test")
        os.makedirs(os.path.join(self.profile, "projects", "p"))
        self.state = os.path.join(self.root, "state")

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, lines, name="s1.jsonl"):
        with open(os.path.join(self.profile, "projects", "p", name), "a") as f:
            f.write("\n".join(lines) + "\n")

    def meter(self):
        return um.Meter(directory=self.state, profiles=[self.profile])

    def test_hit_becomes_calibration_and_survives_a_restart(self):
        self.write([usage_line(T0 + 60 * i, "m%d" % i, "r%d" % i, (10, 400000, 1000, 5000000)) for i in range(10)])
        self.write([limit_line(T0 + 3000), limit_line(T0 + 3001)])
        m = self.meter()
        self.assertEqual(m.scan(T0 + 4000), 1)
        m.save()
        m2 = self.meter()
        self.assertEqual(len(m2.est[(".claude-test", "session")].events), 1)
        self.assertEqual(m2.scan(T0 + 4100), 0)          # nothing new, no duplicate event

    def test_status_projects_the_window(self):
        self.write([usage_line(T0 + 600 * i, "m%d" % i, "r%d" % i, (0, 100000, 0, 0)) for i in range(6)])
        m = self.meter()
        m.scan(T0 + 3600)
        row = m.status(T0 + 3600)[".claude-test"]
        self.assertGreater(row["used"], 0)
        self.assertGreater(row["projected"], row["used"])

    def test_posture_file_and_nudge_factor(self):
        self.write([usage_line(T0 + 600 * i, "m%d" % i, "r%d" % i, (0, 100000, 0, 0)) for i in range(6)])
        m = self.meter()
        m.tick(T0 + 3600)
        self.assertEqual(json.load(open(os.path.join(self.state, "posture.json")))["profiles"][".claude-test"]["posture"],
                         "observe")
        self.assertEqual(um.nudge_factor(".claude-test", self.state), 1.0)    # observe changes nothing
        self.assertEqual(um.nudge_factor(None, self.state), 1.0)            # no profile, no change

    def test_nudge_factor_follows_posture_and_ignores_a_stale_file(self):
        import time
        os.makedirs(self.state)
        for posture, want in (("push", 0.5), ("hold", 2.0), ("steady", 1.0), ("observe", 1.0)):
            json.dump({"at": time.time(), "profiles": {"a": {"posture": posture}}},
                      open(os.path.join(self.state, "posture.json"), "w"))
            self.assertEqual(um.nudge_factor("a", self.state), want, posture)
        json.dump({"at": time.time() - 5000, "profiles": {"a": {"posture": "hold"}}},
                  open(os.path.join(self.state, "posture.json"), "w"))
        self.assertEqual(um.nudge_factor("a", self.state), 1.0)
        self.assertEqual(um.nudge_factor("a", os.path.join(self.root, "missing")), 1.0)
        json.dump({"at": time.time(), "profiles": {"a": {"posture": "push"}}},
                  open(os.path.join(self.state, "posture.json"), "w"))
        self.assertEqual(um.nudge_factor("b", self.state), 1.0)             # another profile is untouched


class AllAgents(unittest.TestCase):
    """Usage is the sum of every agent in a profile, however deep it runs."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = self.tmp.name
        self.profile = os.path.join(self.root, ".claude-test")
        self.state = os.path.join(self.root, "state")
        self.db = os.path.join(self.root, "state.db")

    def tearDown(self):
        self.tmp.cleanup()

    def put(self, rel, line):
        p = os.path.join(self.profile, "projects", rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "a") as f:
            f.write(line + "\n")

    def make_db(self, rows):
        import sqlite3
        con = sqlite3.connect(self.db)
        con.execute("CREATE TABLE sessions (id TEXT, started_at REAL, last_activity_at REAL, billing_provider TEXT, "
                    "model TEXT, input_tokens INT, output_tokens INT, cache_write_tokens INT, cache_read_tokens INT)")
        con.executemany("INSERT INTO sessions VALUES (?,?,?,?,?,?,?,?,?)", rows)
        con.commit()
        con.close()

    def total(self, meter):
        return sum(sum(v) for v in meter.profiles[".claude-test"].buckets.values())

    def test_every_depth_of_transcript_is_counted(self):
        self.put("proj/main.jsonl", usage_line(T0, "a", "1", (0, 100, 0, 0)))
        self.put("proj/sess/subagents/agent-x.jsonl", usage_line(T0, "b", "2", (0, 100, 0, 0)))
        self.put("proj/sess/subagents/workflows/wf_1/agent-y.jsonl", usage_line(T0, "c", "3", (0, 100, 0, 0)))
        self.put("proj/sess/subagents/workflows/wf_1/deeper/agent-z.jsonl", usage_line(T0, "d", "4", (0, 100, 0, 0)))
        m = um.Meter(directory=self.state, profiles=[self.profile])
        m.scan(T0 + 10)
        self.assertEqual(self.total(m), 400)

    def test_a_subagent_message_echoed_in_two_files_counts_once(self):
        self.put("proj/main.jsonl", usage_line(T0, "same", "1", (0, 100, 0, 0)))
        self.put("proj/sess/subagents/agent-x.jsonl", usage_line(T0, "same", "1", (0, 100, 0, 0)))
        m = um.Meter(directory=self.state, profiles=[self.profile])
        m.scan(T0 + 10)
        self.assertEqual(self.total(m), 100)

    def test_hermes_agents_are_ignored_until_a_profile_is_named(self):
        self.make_db([("s1", T0, T0 + 60, "anthropic", "claude-sonnet-5-5", 10, 200, 0, 5000)])
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("USAGE_METER_HERMES_PROFILE", None)
            m = um.Meter(directory=self.state, profiles=[self.profile], hermes_db=self.db)
            m.scan(T0 + 100)
            self.assertEqual(self.total(m), 0)

    def test_hermes_agents_are_added_to_the_named_profile_once(self):
        self.make_db([("s1", T0, T0 + 60, "anthropic", "claude-sonnet-5-5", 10, 200, 0, 5000),
                      ("s2", T0, T0 + 60, "fireworks", "deepseek", 99999, 99999, 0, 99999),
                      ("s3", T0, T0 + 60, "anthropic", "gpt-x", 99999, 99999, 0, 99999)])
        with mock.patch.dict(os.environ, {"USAGE_METER_HERMES_PROFILE": ".claude-test"}):
            m = um.Meter(directory=self.state, profiles=[self.profile], hermes_db=self.db)
            m.scan(T0 + 100)
            self.assertEqual(self.total(m), 5210)                 # only the anthropic claude session
            m.scan(T0 + 200)
            self.assertEqual(self.total(m), 5210)                 # a second read adds nothing

    def test_a_growing_hermes_session_adds_only_the_change(self):
        import sqlite3
        self.make_db([("s1", T0, T0 + 60, "anthropic", "claude-x", 10, 200, 0, 5000)])
        with mock.patch.dict(os.environ, {"USAGE_METER_HERMES_PROFILE": ".claude-test"}):
            m = um.Meter(directory=self.state, profiles=[self.profile], hermes_db=self.db)
            m.scan(T0 + 100)
            m.save()
            con = sqlite3.connect(self.db)
            con.execute("UPDATE sessions SET output_tokens=900, cache_read_tokens=8000, last_activity_at=?", (T0 + 400,))
            con.commit()
            con.close()
            m2 = um.Meter(directory=self.state, profiles=[self.profile], hermes_db=self.db)    # a restart in between
            m2.scan(T0 + 500)
            self.assertEqual(self.total(m2), 10 + 900 + 8000)


class HermesSetting(unittest.TestCase):
    def state(self, value):
        env = {} if value is None else {"USAGE_METER_HERMES_PROFILE": value}
        with mock.patch.dict(os.environ, env):
            if value is None:
                os.environ.pop("USAGE_METER_HERMES_PROFILE", None)
                with mock.patch.dict(os.environ, {"HERMES_HOME": "/nonexistent"}):
                    return um.hermes_setting()
            return um.hermes_setting()

    def test_empty_or_missing_is_unset_and_uncounted_boops_is_deliberate(self):
        self.assertEqual(self.state(None), ("unset", None))
        self.assertEqual(self.state(""), ("unset", None))
        self.assertEqual(self.state("   "), ("unset", None))
        self.assertEqual(self.state("uncounted-boops"), ("uncounted", None))

    def test_a_name_maps_to_the_profile_directory(self):
        self.assertEqual(self.state("work"), ("counted", ".claude-work"))
        self.assertEqual(self.state(".claude-personal"), ("counted", ".claude-personal"))

    def test_the_setting_is_read_from_the_plugin_entry_in_config(self):
        import yaml
        with tempfile.TemporaryDirectory() as home:
            with open(os.path.join(home, "config.yaml"), "w") as f:
                yaml.safe_dump({"plugins": {"entries": {"fleet-platform": {"settings": {"hermes_usage_profile": "personal"}}}}}, f)
            env = {"HERMES_HOME": home}
            with mock.patch.dict(os.environ, env):
                os.environ.pop("USAGE_METER_HERMES_PROFILE", None)
                self.assertEqual(um.hermes_setting(), ("counted", ".claude-personal"))

    def test_uncounted_and_unset_add_nothing(self):
        for value in ("uncounted-boops", ""):
            with tempfile.TemporaryDirectory() as d:
                import sqlite3
                db = os.path.join(d, "s.db")
                con = sqlite3.connect(db)
                con.execute("CREATE TABLE sessions (id TEXT, started_at REAL, last_activity_at REAL, billing_provider TEXT, "
                            "model TEXT, input_tokens INT, output_tokens INT, cache_write_tokens INT, cache_read_tokens INT)")
                con.execute("INSERT INTO sessions VALUES ('s',1,?, 'anthropic','claude-x',5,5,5,5)", (T0,))
                con.commit()
                con.close()
                prof = os.path.join(d, ".claude-test")
                os.makedirs(prof)
                with mock.patch.dict(os.environ, {"USAGE_METER_HERMES_PROFILE": value}):
                    m = um.Meter(directory=os.path.join(d, "st"), profiles=[prof], hermes_db=db)
                    m.scan(T0 + 10)
                    self.assertEqual(m.profiles[".claude-test"].buckets, {}, value)

    def test_daemon_asks_once_a_day_only_while_unset(self):
        import fleet_watch
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        patcher = mock.patch.dict(os.environ, {"USAGE_METER_DIR": tmp.name})
        patcher.start()
        self.addCleanup(patcher.stop)
        calls = []
        h = (3, 1, 2, 3, 4)
        with mock.patch.object(fleet_watch, "post_fleet_channel", side_effect=lambda s, m: calls.append((s, m)) or True), \
                mock.patch.object(um, "hermes_recent", return_value=h):
            fleet_watch._usage_asked[0] = 0.0
            with mock.patch.object(um, "hermes_setting", return_value=("unset", None)):
                self.assertTrue(fleet_watch._ask_usage_setup(now=10**9))
                self.assertFalse(fleet_watch._ask_usage_setup(now=10**9 + 3600))      # same day
                self.assertTrue(fleet_watch._ask_usage_setup(now=10**9 + 90000))      # a day later
            self.assertEqual(len(calls), 2)
            self.assertEqual(calls[0][0], "usage-meter")
            fleet_watch._usage_asked[0] = 0.0
            for state in ("uncounted", "counted"):
                with mock.patch.object(um, "hermes_setting", return_value=(state, ".claude-work")):
                    self.assertFalse(fleet_watch._ask_usage_setup(now=10**9))
            self.assertEqual(len(calls), 2)


class RingTail(unittest.TestCase):
    def test_tail_lines_reads_only_the_end_and_never_a_partial_line(self):
        import fleet_watch
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "t.jsonl")
            lines = ["line-%04d-%s" % (i, "x" * 40) for i in range(1000)]
            with open(p, "w") as f:
                f.write("\n".join(lines) + "\n")
            small = os.path.getsize(p)
            self.assertEqual(fleet_watch._tail_lines(p, small * 2), lines)          # whole file when it fits
            tail = fleet_watch._tail_lines(p, 1000)
            self.assertLess(len(tail), 40)
            self.assertEqual(tail, lines[-len(tail):])                                # whole lines only, in order
            self.assertEqual(fleet_watch._tail_lines(p, 1), [])                       # tiny window: nothing, no crash


class TickSafety(unittest.TestCase):
    def test_tick_logs_a_failure_and_returns_none(self):
        with mock.patch.object(um, "Meter", side_effect=RuntimeError("boom")), \
                mock.patch.object(um, "_METER", None):
            with self.assertLogs("fleetwatch.usage", level="ERROR") as cm:
                self.assertIsNone(um.tick(now=10**9, force=True))
            self.assertIn("boom", "\n".join(cm.output))

    def test_tick_runs_and_is_rate_limited(self):
        with tempfile.TemporaryDirectory() as d:
            fake = mock.Mock()
            fake.tick.return_value = {"x": 1}
            with mock.patch.object(um, "Meter", return_value=fake), mock.patch.object(um, "_METER", None), \
                    mock.patch.object(um, "_LAST_TICK", 0.0):
                self.assertEqual(um.tick(now=10**9), {"x": 1})
                self.assertIsNone(um.tick(now=10**9 + 5))                 # inside the minute
                self.assertEqual(um.tick(now=10**9 + 61), {"x": 1})


class DaemonWiring(unittest.TestCase):
    def test_a_session_follows_its_own_profiles_posture(self):
        import time
        from unittest import mock
        import fleet_watch
        with tempfile.TemporaryDirectory() as d:
            json.dump({"at": time.time(), "profiles": {".claude-work": {"posture": "push"},
                                                       ".claude-personal": {"posture": "hold"}}},
                      open(os.path.join(d, "posture.json"), "w"))
            reg = {"cc-w-a": {"profile": "work"}, "cc-p-b": {"profile": "personal"}, "cc-x": {}}
            with mock.patch.dict(os.environ, {"USAGE_METER_DIR": d}), \
                    mock.patch.object(fleet_watch, "load_registry", return_value=reg):
                self.assertEqual(fleet_watch._usage_factor("cc-w-a"), 0.5)
                self.assertEqual(fleet_watch._usage_factor("cc-p-b"), 2.0)
                self.assertEqual(fleet_watch._usage_factor("cc-x"), 1.0)       # no profile
                self.assertEqual(fleet_watch._usage_factor("unknown"), 1.0)    # not registered

    def test_a_broken_registry_changes_nothing(self):
        from unittest import mock
        import fleet_watch
        with mock.patch.object(fleet_watch, "load_registry", side_effect=RuntimeError("boom")):
            self.assertEqual(fleet_watch._usage_factor("cc-w-a"), 1.0)


if __name__ == "__main__":
    unittest.main()
