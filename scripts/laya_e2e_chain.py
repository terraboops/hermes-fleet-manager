#!/usr/bin/env python3
"""End-to-end check of the Laya backend ladder on this machine.

Prints the chain the manager walked for three cases:
  1. auto selection with LAYA_BACKEND unset
  2. LAYA_BACKEND=cpu, scoring the parity fixtures (first N rows)
  3. an injected out-of-memory on MLX and on a GPU rung, falling through to CPU

Usage: python3 scripts/laya_e2e_chain.py [--rows N]
"""
import argparse
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import laya_backends as lb  # noqa: E402
from laya_backend_manager import BackendManager  # noqa: E402


def show(title, mgr):
    st = mgr.status()
    print(f"--- {title}")
    print(f"active backend: {st.backend}  load_ms: {st.load_ms}  override: {st.override} ({st.override_source})")
    for step in st.fallback_chain:
        print(f"  {step['rung']:<12} {step['status']:<9} {step['reason'][:150]}")


def fixture_events(n):
    d = ROOT / "tests" / "fixtures" / "laya_parity" / "v1-mlxref"
    files = sorted(d.glob("*.json*"))
    return d, files


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--rows", type=int, default=20)
    args = ap.parse_args()
    env = {k: v for k, v in os.environ.items() if k != "LAYA_BACKEND"}

    # 1. auto
    m = BackendManager(env=dict(env), config_value="")
    t0 = time.monotonic()
    m.start()
    show("1. auto (LAYA_BACKEND unset)", m)
    print(f"wall: {time.monotonic() - t0:.2f}s")

    # 2. cpu on parity fixtures
    sys.argv = ["x"]
    import subprocess
    cmd = [sys.executable, str(ROOT / "scripts" / "laya_cpu_parity.py"), "--limit", str(args.rows)]
    e2 = dict(env, LAYA_BACKEND="cpu")
    print(f"--- 2. LAYA_BACKEND=cpu {' '.join(cmd[1:])}")
    r = subprocess.run(cmd, env=e2, capture_output=True, text=True, timeout=900)
    print((r.stdout + r.stderr)[-2500:])
    print(f"exit {r.returncode}")
    m2 = BackendManager(env=dict(env, LAYA_BACKEND="cpu"), config_value="")
    m2.start()
    show("2b. manager with LAYA_BACKEND=cpu", m2)

    # 3. injected OOM: MLX, then a GPU rung (forced past probe), then real CPU
    class OomMlx(lb.MlxBackend):
        def load(self):
            raise lb.BackendOOMError("injected: MLX out of memory")

    class OomGpu(lb.LegacyGpuBackend):
        name = "legacy-gpu"

        def probe(self):
            return lb.Probe(True)

        def load(self):
            raise lb.BackendOOMError("injected: CUDA out of memory")

    rungs = [OomMlx, OomGpu, lb.CpuBackend]
    byname = dict(lb.BACKENDS_BY_NAME)
    byname.update({"mlx": OomMlx})
    m3 = BackendManager(env=dict(env), config_value="", rungs=rungs, backends_by_name=byname, detect=lambda: None)
    m3.start()
    show("3. injected OOM on mlx and legacy-gpu, then real cpu", m3)
    assert m3.status().backend == "cpu", "expected CPU to be the final rung"

    # 3b. injected OOM on MLX alone through the real ladder
    m4 = BackendManager(env=dict(env), config_value="",
                        rungs=[OomMlx] + [c for c in lb.RUNGS if c is not lb.MlxBackend],
                        backends_by_name=byname)
    m4.start()
    show("3b. injected OOM on mlx only, real ladder below it", m4)
    print("OK" if m4.status().backend else "FAIL: model-less")


if __name__ == "__main__":
    main()
