"""The gate on top of the backend manager: bad override, no backend, demotion; lanes 1 and 2 need no model."""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

import laya_backends as lb  # noqa: E402
import laya_gate  # noqa: E402

CRASH = "Traceback (most recent call last): ValueError in tools.py"
RECEIPT = "ACK-OK delivered"
UNREMARKABLE = "the build log was rotated"


def fake(name, load_exc=None, score_excs=None):
    queue = list(score_excs or [])

    class F(lb.LayaBackend):
        def load(self):
            if load_exc:
                raise load_exc

        def score(self, events):
            if queue:
                raise queue.pop(0)
            return [{"answers": {"action": {"probabilities": {"interrupt her now": 0.1}}}} for _ in events]
    F.name = name
    return F


NAMES = ("mlx", "cuda/rocm", "legacy-gpu", "vulkan", "cpu", "remote")


class _Base(unittest.TestCase):
    def setUp(self):
        self._saved = (laya_gate._model, laya_gate._model_error, laya_gate._manager)
        laya_gate._model = laya_gate._manager = laya_gate._model_error = None
        self._env = mock.patch.dict(os.environ, {}, clear=False)
        self._env.start()
        os.environ.pop("LAYA_BACKEND", None)
        self._cfg = mock.patch("laya_backend_manager.read_config_backend", return_value=None)
        self._cfg.start()
        laya_gate._recent.clear()
        # Quiet hours read the real wall clock; unpinned, lane 3 is held from 23:00 to 05:00.
        self._quiet = mock.patch.object(laya_gate, "in_quiet_hours", return_value=False)
        self._quiet.start()

    def tearDown(self):
        self._quiet.stop()
        self._cfg.stop()
        self._env.stop()
        laya_gate._model, laya_gate._model_error, laya_gate._manager = self._saved

    def rungs(self, **over):
        return mock.patch.object(lb, "RUNGS", [over.get(n) or fake(n) for n in NAMES])

    def lane3(self):
        return laya_gate.classify("cc-t", "", UNREMARKABLE)


class NoModelGateTest(_Base):
    def _all_failing(self):
        return self.rungs(**{n: fake(n, load_exc=lb.BackendLoadError("down")) for n in NAMES})

    def test_lanes_1_and_2_work_with_every_backend_failing(self):
        with self._all_failing():
            crash = laya_gate.classify("cc-t", "", CRASH)
            receipt = laya_gate.classify("cc-t", "", RECEIPT)
        self.assertEqual((crash["lane"], crash["escalate"]), (1, True))
        self.assertEqual(receipt["lane"], 2)
        self.assertFalse(receipt["escalate"])

    def test_lane_3_fails_toward_showing_the_event(self):
        with self._all_failing():
            v = self.lane3()
        self.assertTrue(v["escalate"], v)
        self.assertIn("classifier unavailable", v["rule"])

    def test_none_override_is_model_less_and_still_shows_events(self):
        os.environ["LAYA_BACKEND"] = "none"
        v = self.lane3()
        self.assertTrue(v["escalate"])
        self.assertIn("classifier unavailable", v["rule"])
        st = laya_gate.backend_status()
        self.assertTrue(st["model_less"] and st["backend"] is None)   # the manager is kept so /health shows why

    def test_failed_load_is_retried_with_a_fresh_manager(self):
        with self._all_failing():
            self.lane3()
        with self.rungs():
            v = self.lane3()
        self.assertFalse(v["escalate"], v)
        self.assertEqual(laya_gate.backend_status()["backend"], "mlx")


class OverrideGateTest(_Base):
    def test_invalid_override_logs_valid_values_and_gate_still_decides(self):
        os.environ["LAYA_BACKEND"] = "hyperdrive"
        with self.rungs(), self.assertLogs("laya.backend", level="ERROR") as cm:
            v = self.lane3()
        for name in ("auto", "mlx", "cuda", "rocm", "legacy", "vulkan", "cpu", "remote", "stub", "none"):
            self.assertIn(name, "\n".join(cm.output))
        self.assertNotIn("classifier unavailable", v["rule"])
        st = laya_gate.backend_status()
        self.assertEqual((st["override"], st["override_source"]), ("auto", "env"))
        self.assertIn("hyperdrive", st["override_error"])

    def test_invalid_override_with_nothing_loadable_does_not_crash(self):
        os.environ["LAYA_BACKEND"] = "hyperdrive"
        down = {n: fake(n, load_exc=lb.BackendLoadError("down")) for n in NAMES}
        with self.rungs(**down), self.assertLogs("laya.backend", level="ERROR"):
            v = self.lane3()
        self.assertTrue(v["escalate"])
        self.assertIn("hyperdrive", laya_gate._model_error)


class DemotionGateTest(_Base):
    def test_mid_run_score_error_demotes_and_the_decision_still_comes_back(self):
        with self.rungs(mlx=fake("mlx", score_excs=[RuntimeError("metal fault")])):
            v = self.lane3()
            chain = laya_gate.backend_status()["fallback_chain"]
        self.assertNotIn("classifier unavailable", v["rule"])
        self.assertTrue(any(e["status"] == "demoted" and "metal fault" in e["reason"] for e in chain))

    def test_health_reports_the_backend_block(self):
        with self.rungs():
            self.lane3()
        st = laya_gate.backend_status()
        for k in ("backend", "fallback_chain", "load_ms", "model_less", "override", "override_source"):
            self.assertIn(k, st)
        self.assertFalse(st["model_less"])


if __name__ == "__main__":
    unittest.main()
