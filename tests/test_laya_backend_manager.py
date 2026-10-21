"""Backend selection for the Laya gate: override, ladder walk, fallback chain, demotion, model-less mode."""
import importlib.util
import logging
import os
import platform
import tempfile
import unittest
from unittest import mock

import laya_backends as lb
import laya_backend_manager as bm
from laya_hardware import GpuProbe, HardwareInfo

EV = {"state": "s", "question": "q"}
OK_OUT = {"answers": {"action": {"probabilities": {"interrupt her now": 0.5}, "confidence": 0.5}}}


def make(name, load_exc=None, score_excs=None, tag=None):
    """A fake rung. `score_excs` is a list of exceptions raised by successive score() calls."""
    queue = list(score_excs or [])

    class Fake(lb.LayaBackend):
        def load(self):
            if load_exc is not None:
                raise load_exc

        def score(self, events):
            if queue:
                raise queue.pop(0)
            return [{"answers": {"action": {"probabilities": {"interrupt her now": 0.5}}}, "from": name}
                    for _ in events]
    Fake.name = name
    Fake.__name__ = f"Fake_{name}"
    return Fake


def apple():
    return HardwareInfo(apple_silicon=True, system="Darwin", machine="arm64")


def bare():
    return HardwareInfo(system="Linux", machine="x86_64")


def nvidia():
    return HardwareInfo(system="Linux", machine="x86_64", nvidia=GpuProbe(available=True, gpu_name="RTX"))


def rungs(**over):
    """The six real rung names as fakes; `over` replaces one by key (mlx, cudarocm, legacy, ...)."""
    names = {"mlx": "mlx", "cudarocm": "cuda/rocm", "legacy": "legacy-gpu", "vulkan": "vulkan",
             "cpu": "cpu", "remote": "remote"}
    return [over.get(k) or make(n) for k, n in names.items()]


def mgr(hw=None, env=None, config=None, rung_classes=None, by_name=None, **kw):
    rung_classes = rung_classes or rungs()
    if by_name is None:                    # override names resolve to the same fakes as the rungs
        by_rung = {c.name: c for c in rung_classes}
        by_name = {"stub": lb.StubBackend}
        for key, rung in (("mlx", "mlx"), ("cuda", "cuda/rocm"), ("rocm", "cuda/rocm"),
                          ("legacy", "legacy-gpu"), ("vulkan", "vulkan"), ("cpu", "cpu"),
                          ("remote", "remote")):
            by_name[key] = by_rung[rung]
    return bm.BackendManager(hw=hw or apple(), env=env if env is not None else {},
                             config_value=config, rungs=rung_classes,
                             backends_by_name=by_name, **kw)


class OverrideTest(unittest.TestCase):
    def test_default_is_auto_from_default_source(self):
        v, src, err = bm.resolve_override(env={}, config_value=None)
        self.assertEqual((v, src, err), ("auto", "default", None))

    def test_env_beats_config(self):
        v, src, err = bm.resolve_override(env={"LAYA_BACKEND": "cpu"}, config_value="vulkan")
        self.assertEqual((v, src, err), ("cpu", "env", None))

    def test_config_used_when_env_unset_or_blank(self):
        for env in ({}, {"LAYA_BACKEND": "  "}):
            v, src, _ = bm.resolve_override(env=env, config_value="Vulkan")
            self.assertEqual((v, src), ("vulkan", "config"))

    def test_invalid_value_names_every_valid_value_and_falls_to_auto(self):
        v, src, err = bm.resolve_override(env={"LAYA_BACKEND": "quantum"}, config_value="cpu")
        self.assertEqual((v, src), ("auto", "env"))
        for name in ("auto", "mlx", "cuda", "rocm", "legacy", "vulkan", "cpu", "remote", "stub", "none"):
            self.assertIn(name, err)
        self.assertIn("quantum", err)

    def test_valid_set_is_exactly_the_documented_one(self):
        self.assertEqual(set(bm.VALID_OVERRIDES),
                         {"auto", "mlx", "cuda", "rocm", "legacy", "vulkan", "cpu", "remote", "stub", "none"})

    def test_config_file_reader(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "config.yaml")
            with open(p, "w") as f:
                f.write("laya:\n  backend: vulkan\n")
            self.assertEqual(bm.read_config_backend(p), "vulkan")
            self.assertIsNone(bm.read_config_backend(os.path.join(d, "missing.yaml")))
            with open(p, "w") as f:
                f.write(": : not yaml [")
            self.assertIsNone(bm.read_config_backend(p))
            with open(p, "w") as f:
                f.write("laya: 3\n")
            self.assertIsNone(bm.read_config_backend(p))

    def test_manager_reports_override_and_source(self):
        m = mgr(env={"LAYA_BACKEND": "cpu"}, config="mlx")
        m.start()
        s = m.status()
        self.assertEqual((s.override, s.override_source), ("cpu", "env"))
        self.assertEqual(s.backend, "cpu")


class SelectionTest(unittest.TestCase):
    def test_auto_picks_first_rung_when_it_loads(self):
        m = mgr()
        m.start()
        s = m.status()
        self.assertEqual(s.backend, "mlx")
        self.assertFalse(s.model_less)
        self.assertIsInstance(s.load_ms, int)
        self.assertEqual(s.fallback_chain[-1]["status"], "loaded")
        self.assertEqual(s.fallback_chain[-1]["rung"], "mlx")

    def test_auto_skips_absent_hardware_with_reason(self):
        m = mgr(hw=bare())
        m.start()
        s = m.status()
        self.assertEqual(s.backend, "cpu")
        by_rung = {e["rung"]: e for e in s.fallback_chain}
        for r in ("mlx", "cuda/rocm", "legacy-gpu", "vulkan"):
            self.assertEqual(by_rung[r]["status"], "skipped")
            self.assertTrue(by_rung[r]["reason"])
        self.assertIn("Apple Silicon", by_rung["mlx"]["reason"])

    def test_nvidia_makes_cuda_rung_eligible(self):
        m = mgr(hw=nvidia())
        m.start()
        self.assertEqual(m.status().backend, "cuda/rocm")

    def test_oom_on_load_falls_down_and_reason_is_in_chain(self):
        classes = rungs(mlx=make("mlx", load_exc=lb.BackendOOMError("Metal out of memory")))
        with self.assertLogs("laya.backend", level="WARNING") as cm:
            m = mgr(rung_classes=classes)
            m.start()
        s = m.status()
        self.assertEqual(s.fallback_chain[0]["rung"], "mlx")
        self.assertEqual(s.fallback_chain[0]["status"], "failed")
        self.assertIn("out of memory", s.fallback_chain[0]["reason"])
        self.assertIn("oom", s.fallback_chain[0]["reason"].lower())
        self.assertNotEqual(s.backend, "mlx")
        self.assertTrue(any("mlx" in line and "out of memory" in line for line in cm.output))

    def test_load_error_is_not_silent(self):
        classes = rungs(mlx=make("mlx", load_exc=lb.BackendLoadError("no package")))
        with self.assertLogs("laya.backend", level="WARNING") as cm:
            mgr(rung_classes=classes).start()
        self.assertTrue(any("no package" in line for line in cm.output))

    def test_unexpected_exception_on_load_propagates(self):
        # Only typed load errors mean "try the next rung"; a stray exception is a bug, not a fallback.
        classes = rungs(mlx=make("mlx", load_exc=RuntimeError("weird")))
        with self.assertRaises(RuntimeError):
            mgr(rung_classes=classes).start()

    def test_all_rungs_failing_is_model_less(self):
        classes = [make(n, load_exc=lb.BackendLoadError("down"))
                   for n in ("mlx", "cuda/rocm", "legacy-gpu", "vulkan", "cpu", "remote")]
        m = mgr(rung_classes=classes)
        m.start()
        s = m.status()
        self.assertTrue(s.model_less)
        self.assertIsNone(s.backend)
        self.assertIsNone(s.load_ms)
        self.assertEqual(len(s.fallback_chain), 6)
        with self.assertRaises(lb.BackendRuntimeError):
            m.score([EV])

    def test_none_override_is_model_less_and_loads_nothing(self):
        m = mgr(env={"LAYA_BACKEND": "none"})
        m.start()
        s = m.status()
        self.assertTrue(s.model_less)
        self.assertIsNone(s.backend)
        self.assertEqual(s.override, "none")

    def test_stub_override_uses_stub_backend(self):
        m = bm.BackendManager(hw=bare(), env={"LAYA_BACKEND": "stub"})
        m.start()
        self.assertEqual(m.status().backend, "stub")
        self.assertEqual(len(m.score([EV])), 1)

    def test_remote_is_selectable_regardless_of_hardware(self):
        m = mgr(hw=apple(), env={"LAYA_BACKEND": "remote"})
        m.start()
        self.assertEqual(m.status().backend, "remote")

    def test_explicit_backend_that_fails_still_falls_down(self):
        classes = rungs(vulkan=make("vulkan", load_exc=lb.BackendLoadError("no icd")))
        m = mgr(env={"LAYA_BACKEND": "vulkan"}, rung_classes=classes, hw=bare())
        with self.assertLogs("laya.backend", level="WARNING"):
            m.start()
        s = m.status()
        self.assertEqual(s.fallback_chain[0]["rung"], "vulkan")
        self.assertEqual(s.fallback_chain[0]["status"], "failed")
        self.assertEqual(s.backend, "cpu")      # falls down from vulkan, never up to mlx

    def test_explicit_backend_ignores_hardware_detection(self):
        m = mgr(hw=bare(), env={"LAYA_BACKEND": "mlx"})
        m.start()
        self.assertEqual(m.status().backend, "mlx")

    def test_invalid_override_does_not_crash_and_is_recorded(self):
        with self.assertLogs("laya.backend", level="ERROR") as cm:
            m = mgr(env={"LAYA_BACKEND": "warp-drive"})
            m.start()
        text = "\n".join(cm.output)
        for name in bm.VALID_OVERRIDES:
            self.assertIn(name, text)
        s = m.status()
        self.assertEqual(s.backend, "mlx")           # fell back to auto
        self.assertEqual(s.override, "auto")
        self.assertIn("warp-drive", s.override_error)
        self.assertIn("stub", s.override_error)

    def test_hardware_probe_crash_does_not_stop_selection(self):
        def boom():
            raise RuntimeError("probe died")
        m = bm.BackendManager(detect=boom, env={}, rungs=rungs())
        m.start()
        self.assertEqual(m.status().backend, "mlx")
        self.assertIn("probe died", m.status().hardware_error)

    def test_status_to_dict_has_the_health_fields(self):
        m = mgr()
        m.start()
        d = m.status().to_dict()
        for k in ("backend", "fallback_chain", "load_ms", "model_less", "override", "override_source"):
            self.assertIn(k, d)


class DemotionTest(unittest.TestCase):
    def test_score_exception_demotes_records_and_retries(self):
        classes = rungs(mlx=make("mlx", score_excs=[lb.BackendRuntimeError("metal fault")]))
        m = mgr(rung_classes=classes)
        m.start()
        with self.assertLogs("laya.backend", level="WARNING") as cm:
            out = m.score([EV])
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["from"], m.status().backend)
        s = m.status()
        self.assertNotEqual(s.backend, "mlx")
        dem = [e for e in s.fallback_chain if e["status"] == "demoted"]
        self.assertEqual(len(dem), 1)
        self.assertEqual(dem[0]["rung"], "mlx")
        self.assertIn("metal fault", dem[0]["reason"])
        self.assertTrue(any("metal fault" in line for line in cm.output))

    def test_any_exception_type_demotes(self):
        classes = rungs(mlx=make("mlx", score_excs=[ValueError("bad tensor")]))
        m = mgr(rung_classes=classes)
        m.start()
        m.score([EV])
        self.assertNotEqual(m.status().backend, "mlx")

    def test_demotion_walks_past_a_rung_that_fails_to_load(self):
        classes = rungs(mlx=make("mlx", score_excs=[RuntimeError("x")]),
                        cudarocm=make("cuda/rocm", load_exc=lb.BackendLoadError("no driver")))
        hw = HardwareInfo(apple_silicon=True, nvidia=GpuProbe(available=True))
        m = mgr(hw=hw, rung_classes=classes)
        m.start()
        m.score([EV])
        s = m.status()
        self.assertEqual(s.backend, "legacy-gpu")
        statuses = [(e["rung"], e["status"]) for e in s.fallback_chain]
        self.assertIn(("cuda/rocm", "failed"), statuses)
        self.assertIn(("mlx", "demoted"), statuses)

    def test_demotion_skips_hardware_absent_rungs(self):
        # on a bare box the active rung is cpu; explicit mlx override demotes down, never up
        classes = rungs(mlx=make("mlx", score_excs=[RuntimeError("x")]))
        m = mgr(hw=bare(), env={"LAYA_BACKEND": "mlx"}, rung_classes=classes)
        m.start()
        m.score([EV])
        self.assertEqual(m.status().backend, "cpu")

    def test_nothing_left_enters_model_less(self):
        only = make("remote", score_excs=[RuntimeError("remote 500")])
        m = mgr(env={"LAYA_BACKEND": "remote"}, rung_classes=rungs(remote=only))
        m.start()
        with self.assertRaises(lb.BackendRuntimeError):
            m.score([EV])
        s = m.status()
        self.assertTrue(s.model_less)
        self.assertIsNone(s.backend)
        self.assertEqual(s.fallback_chain[-1]["status"], "demoted")
        self.assertIn("remote 500", s.fallback_chain[-1]["reason"])
        with self.assertRaises(lb.BackendRuntimeError):
            m.score([EV])

    def test_healthy_scoring_does_not_touch_the_chain(self):
        m = mgr()
        m.start()
        before = list(m.status().fallback_chain)
        m.score([EV])
        self.assertEqual(m.status().fallback_chain, before)

    def test_adapter_predict_goes_through_demotion(self):
        classes = rungs(mlx=make("mlx", score_excs=[RuntimeError("x")]))
        m = mgr(rung_classes=classes)
        m.start()
        out = m.adapter.predict("state", "question")
        self.assertIn("answers", out)
        self.assertNotEqual(m.status().backend, "mlx")


class MlxEquivalenceTest(unittest.TestCase):
    def test_mocked_apple_silicon_auto_selects_real_mlx_backend(self):
        fake_agent = mock.Mock()
        fake_agent.predict.return_value = OK_OUT
        fake_mod = mock.Mock()
        fake_mod.load.return_value = fake_agent
        with mock.patch.dict("sys.modules", {"laya_mlx": fake_mod}):
            m = bm.BackendManager(hw=apple(), env={}, model="some/model")
            m.start()
            self.assertEqual(m.status().backend, "mlx")
            fake_mod.load.assert_called_once_with("some/model")
            out = m.adapter.predict("S", "Q")
        self.assertIs(out, OK_OUT)
        fake_agent.predict.assert_called_once_with("S", "Q")

    @unittest.skipUnless(platform.machine() == "arm64" and platform.system() == "Darwin"
                         and importlib.util.find_spec("laya_mlx") is not None,
                         "needs Apple Silicon with laya_mlx installed")
    def test_real_apple_silicon_auto_selects_mlx(self):
        m = bm.BackendManager(env={})
        m.start()
        self.assertEqual(m.status().backend, "mlx")
        self.assertFalse(m.status().model_less)


if __name__ == "__main__":
    logging.basicConfig()
    unittest.main()
