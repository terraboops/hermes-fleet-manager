"""Hindsight must see the whole transcript, not a tail.

A decision can be a day older than the last megabyte of a transcript that is hundreds of megabytes
long. When the review read only the tail, every decision in that position saw nothing but recent
activity and was scored as followed by hours of silence: on 2026-10-03 all 46 rows of the day's
"held, and the session then went quiet" list were that artifact, including one decision at 09:46
printed as "quiet for 20.9h afterwards" whose session's next output was 15 minutes later.
"""
import datetime
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import laya_gate  # noqa: E402


def _iso(ts):
    return datetime.datetime.fromtimestamp(ts, datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _line(ts):
    return json.dumps({"timestamp": _iso(ts), "type": "assistant", "message": "x"})


class HindsightBase(unittest.TestCase):
    def setUp(self):
        # A test must not touch production state, and the timestamp cache is module-level.
        laya_gate._TIMESTAMP_CACHE.clear()
        self.tmp = tempfile.mkdtemp(prefix="hindsight-")
        self.path = os.path.join(self.tmp, "transcript.jsonl")
        self.now = int(laya_gate.time.time())   # whole seconds: the transcript stores second resolution

    def write(self, lines):
        with open(self.path, "w") as f:
            for ln in lines:
                f.write(ln + "\n")

    def decision(self, at):
        return {"id": "t", "session": "cc-test", "at": at, "text": "STALL-CC-TEST", "verdict": {}}

    def outcome(self, at):
        with mock.patch.object(laya_gate, "_transcript_for", return_value=self.path):
            return laya_gate.outcome_for(self.decision(at))


class TestWholeTranscriptHindsight(HindsightBase):
    def test_an_old_decision_sees_the_output_that_followed_it(self):
        """The real next output came 5 minutes later, and it is far behind a huge tail."""
        at = self.now - 86400
        self.write([_line(at - 7200), _line(at + 300)] + [_line(at + 86400)])
        with open(self.path, "a") as f:
            f.write(("no timestamps here at all. " * 40 + "\n") * 30000)   # ~2 MB of filler
        with open(self.path, "a") as f:
            f.write(_line(at + 90000) + "\n")

        o = self.outcome(at)
        self.assertEqual(o["verdict"], "resumed", o)
        self.assertEqual(o["delay_s"], 300, o)

    def test_the_tail_would_have_called_that_same_decision_silence(self):
        """NEGATIVE CONTROL: the old code path is still here, and it is still wrong."""
        at = self.now - 86400
        self.write([_line(at - 7200), _line(at + 300)])
        with open(self.path, "a") as f:
            f.write(("no timestamps here at all. " * 40 + "\n") * 30000)
        with open(self.path, "a") as f:
            f.write(_line(at + 90000) + "\n")

        tail = [e for e in (laya_gate._epoch(x) for x in laya_gate._tail_timestamps(self.path)) if e]
        self.assertTrue(tail, "the tail should still find the recent timestamp")
        self.assertNotIn(at + 300, tail, "the tail cannot reach the real next output")
        # and that is exactly what produced the false alarm
        after = [t for t in tail if t > at]
        self.assertGreaterEqual(min(after) - at, laya_gate.QUIET_AFTER)

        whole = laya_gate._all_timestamps(self.path)
        self.assertIn(at + 300, whole, "the whole-file read does reach it")

    def test_a_genuinely_quiet_session_is_still_quiet(self):
        at = self.now - 86400
        self.write([_line(at - 3600), _line(at + 86400)])
        o = self.outcome(at)
        self.assertEqual(o["verdict"], "stayed_quiet", o)

    def test_nothing_after_a_fresh_decision_is_too_recent_not_quiet(self):
        at = self.now - 60
        self.write([_line(at - 3600)])
        o = self.outcome(at)
        self.assertEqual(o["verdict"], "too_recent", o)

    def test_output_in_the_unclear_band_is_unclear(self):
        at = self.now - 86400
        self.write([_line(at + 1800)])
        o = self.outcome(at)
        self.assertEqual(o["verdict"], "unclear", o)

    def test_no_transcript_is_unknown_not_quiet(self):
        with mock.patch.object(laya_gate, "_transcript_for", return_value=None):
            o = laya_gate.outcome_for(self.decision(self.now - 86400))
        self.assertEqual(o["verdict"], "unknown", o)

    def test_the_cache_refreshes_when_the_transcript_grows(self):
        at = self.now - 86400
        self.write([_line(at - 60)])
        self.assertEqual(self.outcome(at)["verdict"], "stayed_quiet")
        with open(self.path, "a") as f:
            f.write(_line(at + 120) + "\n")
        self.assertEqual(self.outcome(at)["verdict"], "resumed")


class TestTailIsStillAvailable(unittest.TestCase):
    def test_tail_timestamps_unchanged_for_callers_that_want_cheap(self):
        tmp = tempfile.mkdtemp(prefix="hindsight-")
        p = os.path.join(tmp, "t.jsonl")
        with open(p, "w") as f:
            f.write(_line(1000) + "\n" + _line(2000) + "\n")
        self.assertEqual(laya_gate._tail_timestamps(p), [_iso(1000), _iso(2000)])


if __name__ == "__main__":
    unittest.main()
