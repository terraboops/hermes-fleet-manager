"""/health top-level backend fields and the `laya doctor` command."""
import contextlib
import io
import json
import os
import sys
import threading
import unittest
import urllib.request
from http.server import ThreadingHTTPServer
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

import laya_backends as lb  # noqa: E402
import laya_doctor  # noqa: E402
import laya_gate  # noqa: E402
from laya_hardware import HardwareInfo  # noqa: E402

NAMES = ("mlx", "cuda/rocm", "legacy-gpu", "vulkan", "cpu", "remote")


def fake(name, load_exc=None):
    class F(lb.LayaBackend):
        def load(self):
            if load_exc:
                raise load_exc

        def score(self, events):
            return [{} for _ in events]
    F.name = name
    return F


def get_health():
    server = ThreadingHTTPServer(("127.0.0.1", 0), laya_gate.Handler)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    try:
        url = f"http://127.0.0.1:{server.server_address[1]}/health"
        with urllib.request.urlopen(url, timeout=5) as r:
            return json.load(r)
    finally:
        server.shutdown()
        server.server_close()


class _Base(unittest.TestCase):
    def setUp(self):
        self._saved = (laya_gate._model, laya_gate._model_error, laya_gate._manager)
        laya_gate._model = laya_gate._manager = laya_gate._model_error = None
        self._env = mock.patch.dict(os.environ, {}, clear=False)
        self._env.start()
        os.environ.pop("LAYA_BACKEND", None)
        self._cfg = mock.patch("laya_backend_manager.read_config_backend", return_value=None)
        self._cfg.start()
        self._hw = mock.patch("laya_hardware.detect_hardware",
                              return_value=HardwareInfo(apple_silicon=True, system="Darwin",
                                                        machine="arm64"))
        self._hw.start()

    def tearDown(self):
        self._hw.stop()
        self._cfg.stop()
        self._env.stop()
        laya_gate._model, laya_gate._model_error, laya_gate._manager = self._saved

    def rungs(self, **over):
        return mock.patch.object(lb, "RUNGS", [over.get(n) or fake(n) for n in NAMES])


class HealthTest(_Base):
    def test_mlx_success(self):
        with self.rungs():
            laya_gate.load_model()
            h = get_health()
        self.assertEqual(h["backend"], "mlx")
        self.assertFalse(h["model_less"])
        self.assertIsInstance(h["load_ms"], int)
        self.assertEqual(h["fallback_chain"], [{"rung": "mlx", "status": "loaded", "reason": ""}])
        self.assertTrue(h["ok"])                      # existing fields survive
        for k in ("model_loaded", "model_error", "interpreter", "enforce", "port"):
            self.assertIn(k, h)

    def test_oom_fallback(self):
        oom = fake("mlx", load_exc=lb.BackendOOMError("metal alloc failed"))
        with self.rungs(mlx=oom):
            laya_gate.load_model()
            h = get_health()
        self.assertNotEqual(h["backend"], "mlx")
        self.assertFalse(h["model_less"])
        first = h["fallback_chain"][0]
        self.assertEqual((first["rung"], first["status"]), ("mlx", "failed"))
        self.assertIn("out of memory", first["reason"])
        self.assertEqual(h["fallback_chain"][-1]["status"], "loaded")
        self.assertEqual(h["fallback_chain"][-1]["rung"], h["backend"])

    def test_fully_model_less(self):
        down = {n: fake(n, load_exc=lb.BackendLoadError("down")) for n in NAMES}
        with self.rungs(**down):
            with self.assertRaises(lb.BackendLoadError):
                laya_gate.load_model()
            h = get_health()
        self.assertIsNone(h["backend"])
        self.assertTrue(h["model_less"])
        self.assertIsNone(h["load_ms"])
        self.assertEqual(len(h["fallback_chain"]), len(NAMES))
        self.assertTrue(all(e["status"] in ("failed", "skipped") and e["reason"] for e in h["fallback_chain"]))

    def test_before_any_load(self):
        h = get_health()
        self.assertIsNone(h["backend"])
        self.assertEqual(h["fallback_chain"], [])
        self.assertTrue(h["model_less"])


class DoctorTest(_Base):
    def run_doctor(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = laya_doctor.main(list(argv))
        return code, out.getvalue()

    def test_dry_run_uses_stub_and_loads_no_model(self):
        with mock.patch("laya_backends.MlxBackend.load", side_effect=AssertionError("real load")):
            code, out = self.run_doctor("--dry-run")
        self.assertEqual(code, 0)
        self.assertIn("hardware", out.lower())
        self.assertIn("Apple Silicon", out)
        self.assertRegex(out, r"backend:\s+stub")
        self.assertIn("dry-run", out)
        self.assertIn("why", out.lower())

    def test_reports_override_and_each_rung(self):
        os.environ["LAYA_BACKEND"] = "mlx"
        oom = fake("mlx", load_exc=lb.BackendOOMError("metal alloc failed"))
        with self.rungs(mlx=oom), mock.patch.dict(lb.BACKENDS_BY_NAME, {"mlx": oom}):
            code, out = self.run_doctor()
        self.assertEqual(code, 0)
        self.assertRegex(out, r"override:\s+mlx \(source: env\)")
        self.assertIn("mlx", out)
        self.assertIn("out of memory", out)

    def test_model_less_exits_zero(self):
        down = {n: fake(n, load_exc=lb.BackendLoadError("down")) for n in NAMES}
        with self.rungs(**down):
            code, out = self.run_doctor()
        self.assertEqual(code, 0)
        self.assertIn("model-less", out)

    def test_json_output(self):
        code, out = self.run_doctor("--dry-run", "--json")
        d = json.loads(out)
        self.assertEqual(d["status"]["backend"], "stub")
        self.assertIn("hardware", d)

    def test_laya_entry_point_routes_doctor(self):
        sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts"))
        import laya as laya_cli
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = laya_cli.main(["doctor", "--dry-run"])
        self.assertEqual(code, 0)
        self.assertRegex(out.getvalue(), r"backend:\s+stub")


if __name__ == "__main__":
    unittest.main()
