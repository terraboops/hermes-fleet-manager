"""The backend contract: typed load errors, cheap probe, load-time reporting, options, fallback."""
import logging
import unittest
from unittest import mock

import laya_backends as lb
import laya_backend_manager as bm
from laya_hardware import HardwareInfo

APPLE = HardwareInfo(apple_silicon=True, system="Darwin", machine="arm64")


def manager(rungs, hw=APPLE, env=None, **kw):
    return bm.BackendManager(hw=hw, env=env if env is not None else {}, config_value=None,
                             rungs=rungs, backends_by_name={"stub": lb.StubBackend}, **kw)


class ErrorTypesTest(unittest.TestCase):
    def test_hierarchy(self):
        self.assertTrue(issubclass(lb.BackendOOMError, lb.BackendLoadError))
        self.assertTrue(issubclass(lb.BackendUnavailable, lb.BackendLoadError))
        self.assertFalse(issubclass(lb.BackendRuntimeError, lb.BackendLoadError))


class FallbackTest(unittest.TestCase):
    def test_simulated_oom_falls_to_next_rung(self):
        first = lb.make_fake_backend("first", oom=True)
        second = lb.make_fake_backend("second")
        m = manager([first, second])
        with self.assertLogs("laya.backend", level="WARNING"):
            m.start()
        st = m.status()
        self.assertEqual(st.backend, "second")
        self.assertEqual(first.loads, 1)
        self.assertEqual(st.fallback_chain[0]["status"], "failed")
        self.assertIn("out of memory", st.fallback_chain[0]["reason"])

    def test_unavailable_backend_is_skipped_without_loading(self):
        gone = lb.make_fake_backend("gone", available=False, reason="no device")
        there = lb.make_fake_backend("there")
        m = manager([gone, there])
        with self.assertLogs("laya.backend", level="WARNING"):
            m.start()
        self.assertEqual(gone.loads, 0)
        self.assertEqual(m.status().backend, "there")
        step = m.status().fallback_chain[0]
        self.assertEqual((step["rung"], step["status"]), ("gone", "skipped"))
        self.assertIn("no device", step["reason"])

    def test_late_unavailable_error_falls_through(self):
        late = lb.make_fake_backend("late", load_exc=lb.BackendUnavailable("driver missing"))
        m = manager([late, lb.make_fake_backend("next")])
        with self.assertLogs("laya.backend", level="WARNING"):
            m.start()
        self.assertEqual(m.status().backend, "next")
        self.assertIn("unavailable", m.status().fallback_chain[0]["reason"])

    def test_unrelated_error_is_not_swallowed(self):
        buggy = lb.make_fake_backend("buggy", load_exc=TypeError("bug in the backend"))
        nxt = lb.make_fake_backend("next")
        m = manager([buggy, nxt])
        with self.assertRaises(TypeError):
            m.start()
        self.assertEqual(nxt.loads, 0)

    def test_unavailable_placeholders_never_load(self):
        for cls in (lb.CudaBackend, lb.CpuBackend, lb.RemoteBackend):
            self.assertFalse(cls().is_available())


class MlxFirstTest(unittest.TestCase):
    def test_mlx_is_first_rung_and_auto_chosen_on_apple_silicon(self):
        self.assertIs(lb.RUNGS[0], lb.MlxBackend)
        fake_mod = mock.Mock()
        with mock.patch.dict("sys.modules", {"laya_mlx": fake_mod}):
            m = bm.BackendManager(hw=APPLE, env={}, config_value=None)
            m.start()
        self.assertEqual(m.status().backend, "mlx")

    def test_mlx_conforms_to_contract(self):
        b = lb.MlxBackend(model="m")
        self.assertIsInstance(b, lb.LayaBackend)
        self.assertIsInstance(b.probe(), lb.Probe)
        fake_mod = mock.Mock()
        with mock.patch.dict("sys.modules", {"laya_mlx": fake_mod}):
            result = b.load()
        self.assertIsInstance(result, lb.LoadResult)
        self.assertGreaterEqual(result.load_seconds, 0)

    def test_mlx_probe_does_not_load_weights(self):
        fake_mod = mock.Mock()
        with mock.patch.dict("sys.modules", {"laya_mlx": fake_mod}):
            self.assertTrue(lb.MlxBackend().is_available())
        fake_mod.load.assert_not_called()

    def test_mlx_missing_package_probe_is_false(self):
        with mock.patch.dict("sys.modules", {}), \
                mock.patch("importlib.util.find_spec", return_value=None):
            import sys
            sys.modules.pop("laya_mlx", None)
            p = lb.MlxBackend().probe()
        self.assertFalse(p.available)
        self.assertIn("laya_mlx", p.reason)

    def test_env_override_forces_a_backend(self):
        fake = lb.make_fake_backend("fake")
        m = bm.BackendManager(hw=APPLE, env={"LAYA_BACKEND": "cpu"}, config_value=None,
                              rungs=[lb.MlxBackend, fake],
                              backends_by_name={"cpu": fake})
        m.start()
        self.assertEqual(m.status().backend, "fake")
        self.assertEqual(m.status().override_source, "env")


class LoadTimeTest(unittest.TestCase):
    def test_load_seconds_is_reported_and_logged(self):
        slow = lb.make_fake_backend("slow", load_seconds=1.5)
        m = manager([slow])
        with self.assertLogs("laya.backend", level="INFO") as cm:
            m.start()
        self.assertEqual(m.status().load_ms, 1500)
        self.assertTrue(any("slow loaded in 1.500s" in line for line in cm.output))


class OptionsTest(unittest.TestCase):
    def test_defaults(self):
        o = lb.BackendOptions.resolve(env={}, config=None)
        self.assertEqual((o.quant_level, o.batch_size, o.num_threads, o.max_seq_len),
                         ("auto", 8, 0, 512))

    def test_env_beats_config(self):
        o = lb.BackendOptions.resolve(env={"LAYA_BATCH_SIZE": "2"},
                                      config={"batch_size": 4, "num_threads": 3})
        self.assertEqual((o.batch_size, o.num_threads), (2, 3))

    def test_bad_values_are_ignored_with_a_warning(self):
        with self.assertLogs("laya.backend", level="WARNING") as cm:
            o = lb.BackendOptions.resolve(env={"LAYA_QUANT_LEVEL": "3bit", "LAYA_BATCH_SIZE": "0",
                                               "LAYA_MAX_SEQ_LEN": "abc"}, config=None)
        self.assertEqual(o, lb.BackendOptions())
        self.assertEqual(len(cm.output), 3)

    def test_options_reach_the_backend(self):
        got = []
        base = lb.make_fake_backend("opt")

        class Capture(base):
            def __init__(self, options=None):
                super().__init__(options)
                got.append(self.options)
        Capture.name = "opt"
        manager([Capture], env={"LAYA_QUANT_LEVEL": "4bit", "LAYA_NUM_THREADS": "2"}).start()
        self.assertEqual((got[0].quant_level, got[0].num_threads), ("4bit", 2))


if __name__ == "__main__":
    logging.basicConfig()
    unittest.main()
