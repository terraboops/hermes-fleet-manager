"""Backend abstraction for the Laya gate: interface contract, stub, mlx wrapper, placeholders, registry."""
import sys
import types
import unittest
from unittest import mock

import laya_backends as lb

Q = "should this interrupt her now?"


def ev(text="a session stopped", q=Q):
    return {"state": text, "question": q}


class ContractTest(unittest.TestCase):
    def test_base_is_abstract(self):
        with self.assertRaises(TypeError):
            lb.LayaBackend()

    def test_exception_hierarchy(self):
        self.assertTrue(issubclass(lb.BackendOOMError, lb.BackendLoadError))
        self.assertTrue(issubclass(lb.BackendLoadError, Exception))
        self.assertTrue(issubclass(lb.BackendRuntimeError, Exception))
        self.assertFalse(issubclass(lb.BackendRuntimeError, lb.BackendLoadError))

    def test_every_registered_class_has_a_name_and_contract(self):
        for name, cls in lb.BACKENDS_BY_NAME.items():
            self.assertTrue(issubclass(cls, lb.LayaBackend), name)
            self.assertTrue(isinstance(cls.name, str) and cls.name, name)
            self.assertTrue(callable(cls.load) and callable(cls.score), name)


class StubTest(unittest.TestCase):
    def test_load_always_succeeds(self):
        b = lb.StubBackend()
        b.load()
        b.load()

    def test_deterministic_across_instances(self):
        a, b = lb.StubBackend(), lb.StubBackend()
        a.load(), b.load()
        events = [ev("one"), ev("two")]
        self.assertEqual(a.score(events), b.score(events))
        self.assertEqual(a.score(events), a.score(events))

    def test_differs_by_content_and_is_a_probability(self):
        b = lb.StubBackend()
        b.load()
        s1, s2 = b.score([ev("one"), ev("two")])
        self.assertNotEqual(s1, s2)
        for s in (s1, s2):
            p = s["answers"]["action"]["probabilities"]["interrupt her now"]
            self.assertTrue(0.0 <= p <= 1.0)

    def test_output_shape_matches_model_shape(self):
        b = lb.StubBackend()
        b.load()
        out = b.score([ev()])[0]
        probs = out["answers"]["action"]["probabilities"]
        self.assertAlmostEqual(sum(probs.values()), 1.0, places=6)

    def test_empty_batch(self):
        self.assertEqual(lb.StubBackend().score([]), [])


class FakeAgent:
    def __init__(self, raises=None):
        self.calls = []
        self.raises = raises

    def predict(self, state, question):
        self.calls.append((state, question))
        if self.raises:
            raise self.raises
        return {"answers": {"action": {"probabilities": {"interrupt her now": 0.7, "hold": 0.3},
                                       "confidence": 0.7}}}


def fake_module(agent=None, load_exc=None):
    m = types.ModuleType("laya_mlx")
    m.load_calls = []

    def load(model):
        m.load_calls.append(model)
        if load_exc:
            raise load_exc
        return agent
    m.load = load
    return m


class MlxTest(unittest.TestCase):
    def test_scores_identical_to_direct_predict(self):
        agent = FakeAgent()
        mod = fake_module(agent)
        with mock.patch.dict(sys.modules, {"laya_mlx": mod}):
            b = lb.MlxBackend(model="m/x")
            b.load()
            got = b.score([ev("s1", "q1"), ev("s2", "q2")])
        direct = FakeAgent()
        want = [direct.predict("s1", "q1"), direct.predict("s2", "q2")]
        self.assertEqual(got, want)
        self.assertEqual(mod.load_calls, ["m/x"])
        self.assertEqual(agent.calls, [("s1", "q1"), ("s2", "q2")])

    def test_import_failure_is_load_error(self):
        with mock.patch.dict(sys.modules, {"laya_mlx": None}):
            with self.assertRaises(lb.BackendLoadError):
                lb.MlxBackend(model="m").load()

    def test_load_failure_is_load_error_not_oom(self):
        mod = fake_module(load_exc=OSError("checkpoint missing"))
        with mock.patch.dict(sys.modules, {"laya_mlx": mod}):
            with self.assertRaises(lb.BackendLoadError) as cm:
                lb.MlxBackend(model="m").load()
        self.assertNotIsInstance(cm.exception, lb.BackendOOMError)

    def test_memory_error_on_load_is_oom(self):
        for exc in (MemoryError(), RuntimeError("[metal::malloc] Attempting to allocate 9 bytes"),
                    RuntimeError("Out of memory")):
            mod = fake_module(load_exc=exc)
            with mock.patch.dict(sys.modules, {"laya_mlx": mod}):
                with self.assertRaises(lb.BackendOOMError):
                    lb.MlxBackend(model="m").load()

    def test_score_failure_is_runtime_error(self):
        mod = fake_module(FakeAgent(raises=ValueError("bad")))
        with mock.patch.dict(sys.modules, {"laya_mlx": mod}):
            b = lb.MlxBackend(model="m")
            b.load()
            with self.assertRaises(lb.BackendRuntimeError):
                b.score([ev()])

    def test_score_before_load_is_runtime_error(self):
        with self.assertRaises(lb.BackendRuntimeError):
            lb.MlxBackend(model="m").score([ev()])


class PlaceholderTest(unittest.TestCase):
    def test_placeholders_raise_load_error(self):
        for cls in (lb.RemoteBackend,):      # the GPU rungs are real now: tests/test_laya_gpu_backends.py
            with self.assertRaises(lb.BackendLoadError) as cm:
                cls().load()
            self.assertIn("not implemented", str(cm.exception))
            with self.assertRaises(lb.BackendRuntimeError):
                cls().score([ev()])


class RegistryTest(unittest.TestCase):
    def test_rung_order(self):
        self.assertEqual([r.name for r in lb.RUNGS],
                         ["mlx", "cuda/rocm", "legacy-gpu", "vulkan", "cpu", "remote"])

    def test_stub_not_a_rung(self):
        self.assertNotIn(lb.StubBackend, lb.RUNGS)

    def test_override_lookup(self):
        want = {"mlx": lb.MlxBackend, "cuda": lb.CudaBackend, "rocm": lb.RocmBackend,
                "legacy": lb.LegacyGpuBackend, "vulkan": lb.VulkanBackend, "cpu": lb.CpuBackend,
                "remote": lb.RemoteBackend, "stub": lb.StubBackend}
        self.assertEqual(lb.BACKENDS_BY_NAME, want)
        for k, v in want.items():
            self.assertIs(lb.backend_for(k), v)
            self.assertIs(lb.backend_for(k.upper()), v)

    def test_unknown_override(self):
        with self.assertRaises(KeyError):
            lb.backend_for("tpu")


if __name__ == "__main__":
    unittest.main()
