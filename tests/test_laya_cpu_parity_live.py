"""Runs the parity fixtures under LAYA_BACKEND=cpu against the real cached model. Skipped when the
llama-server binary or the artifacts are absent. Bounds here are the ones measured and recorded for the
CPU rung (docs/laya-cpu-backend.md), not the stricter proposed drift bound."""
import json
import os
import subprocess
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
sys.path.insert(0, ROOT)
import laya_backends as lb  # noqa: E402


@unittest.skipUnless(lb.CpuBackend(lb.BackendOptions(quant_level="none")).is_available(),
                     "CPU backend artifacts or llama-server not available")
class LiveParityTest(unittest.TestCase):
    def run_script(self, **env):
        e = dict(os.environ, LAYA_BACKEND="cpu", **env)
        out = os.path.join(os.environ.get("TMPDIR", "/tmp"), "laya-cpu-live-%s.json" % os.getpid())
        r = subprocess.run([sys.executable, os.path.join(ROOT, "scripts", "laya_cpu_parity.py"),
                            "--limit", "40", "--out", out], env=e, capture_output=True, text=True, timeout=600)
        self.assertEqual(r.returncode, 0, r.stderr + r.stdout)
        rep = json.load(open(out))
        os.remove(out)
        return rep

    def test_f16_matches_mlx_within_the_proposed_bound(self):
        rep = self.run_script(LAYA_QUANT_LEVEL="none")
        self.assertLessEqual(rep["metrics"]["score_drift"]["max_abs_delta"], 0.005)
        self.assertEqual(rep["cpu_run"]["artifact"], "laya-f16")

    def test_threads_are_respected(self):
        rep = self.run_script(LAYA_NUM_THREADS="2")
        self.assertEqual(rep["cpu_run"]["threads_effective"], 2)
        self.assertEqual(rep["cpu_run"]["artifact"], "laya-q8_0")


if __name__ == "__main__":
    unittest.main()
