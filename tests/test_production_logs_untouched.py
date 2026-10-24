"""The self-test and the suite must never write to the production gate logs.

Runs both in a subprocess with the production log paths exported, so the check holds even if
the isolation in tests/__init__.py were removed: the subprocess for the self-test gets the real
paths and a forced quiet window, which is the exact condition that leaked fixtures before.
"""
import hashlib
import os
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOGS = os.path.expanduser("~/.hermes/logs")
PROD_HELD = os.path.join(LOGS, "laya-gate-held.jsonl")
WATCHED = [PROD_HELD, PROD_HELD + ".sent",
           os.path.join(LOGS, "laya-gate-labels.jsonl")]
FIXTURES = (b'"cc-x"', b'"cc-p-forma-fc3"')


def _sig(path):
    try:
        with open(path, "rb") as f:
            data = f.read()
    except OSError:
        return None
    return hashlib.sha256(data).hexdigest(), data


def _fixture_rows(path):
    s = _sig(path)
    return 0 if s is None else sum(s[1].count(x) for x in FIXTURES)


class ProductionLogsUntouched(unittest.TestCase):
    def _run(self, args, env_extra=None):
        env = dict(os.environ)
        for k in ("LAYA_GATE_HELD", "LAYA_GATE_DECISIONS", "LAYA_GATE_LABELS",
                  "LAYA_GATE_REVIEW_DIR", "FLEET_GATE_LOG"):
            env.pop(k, None)                      # production paths, as the real gate sees them
        env["LAYA_GATE_QUIET_HOURS"] = "00:00-23:59"
        env.update(env_extra or {})
        return subprocess.run([sys.executable] + args, cwd=ROOT, env=env,
                              capture_output=True, text=True, timeout=600)

    def test_selftest_leaves_production_logs_byte_identical(self):
        before = {p: _sig(p) for p in WATCHED}
        decisions_fixtures = _fixture_rows(os.path.join(LOGS, "laya-gate-decisions.jsonl"))
        r = self._run(["laya_gate.py", "--selftest"])
        self.assertEqual(r.returncode, 0, r.stderr[-800:])
        self.assertEqual({p: _sig(p) for p in WATCHED}, before)
        self.assertEqual(_fixture_rows(os.path.join(LOGS, "laya-gate-decisions.jsonl")),
                         decisions_fixtures)

    def test_affected_tests_leave_production_logs_byte_identical(self):
        before = {p: _sig(p) for p in WATCHED}
        decisions_fixtures = _fixture_rows(os.path.join(LOGS, "laya-gate-decisions.jsonl"))
        r = self._run(["-m", "unittest", "tests.test_escalation_door",
                       "tests.test_laya_gate_decisions", "tests.test_fleet_watch_emission"])
        self.assertEqual(r.returncode, 0, r.stderr[-1500:])
        self.assertEqual({p: _sig(p) for p in WATCHED}, before)
        self.assertEqual(_fixture_rows(os.path.join(LOGS, "laya-gate-decisions.jsonl")),
                         decisions_fixtures)

    def test_guard_refuses_fixture_session_for_production_held_log(self):
        import laya_gate
        before = _sig(PROD_HELD)
        with mock.patch.object(laya_gate, "HELD_FILE", PROD_HELD), \
                mock.patch("sys.stderr") as err:
            laya_gate.hold("cc-x", "STALL", "fixture", "r", "w")
        self.assertEqual(_sig(PROD_HELD), before)
        self.assertTrue(err.write.called, "refusal must leave a visible line")

    def test_guard_allows_fixture_session_in_a_temp_log(self):
        import laya_gate
        with tempfile.TemporaryDirectory() as d:
            held = os.path.join(d, "held.jsonl")
            with mock.patch.object(laya_gate, "HELD_FILE", held):
                laya_gate.hold("cc-x", "STALL", "fixture", "r", "w")
            self.assertEqual(_fixture_rows(held), 1)

    def test_classify_log_false_writes_nothing(self):
        import laya_gate
        with tempfile.TemporaryDirectory() as d:
            held = os.path.join(d, "held.jsonl")
            with mock.patch.object(laya_gate, "HELD_FILE", held), \
                    mock.patch.object(laya_gate, "in_quiet_hours", return_value=True):
                laya_gate.classify("cc-x", "MATCH", "NEEDS-INPUT-PICK-A-REPO", log=False)
            self.assertFalse(os.path.exists(held))


if __name__ == "__main__":
    unittest.main()
