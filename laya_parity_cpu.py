"""CPU scoring backend for `laya parity`: the quantized CPU rung as a score(texts) callable.

Same input as the MLX adapter (laya_parity_mlx.build_state), scored by `CpuBackend`. Options come
from the LAYA_* variables, so the same run covers every weak-machine setting:

    LAYA_BACKEND=cpu LAYA_QUANT_LEVEL=none python3 scripts/laya.py parity \\
        --backend laya_parity_cpu:score_rows --fixtures tests/fixtures/laya_parity/v1-mlxref

`run_report` also records load time, peak RSS and the options in effect.
"""
import atexit
import os

import laya_backends as lb
import laya_gate
import laya_parity_mlx as _mlx

LABEL = "interrupt her now"
_backend = None
_load = None


def backend():
    """The shared CpuBackend, loaded on first use. Refuses to run unless LAYA_BACKEND selects cpu."""
    global _backend, _load
    if _backend is None:
        if os.environ.get("LAYA_BACKEND", "").strip().lower() != "cpu":
            raise RuntimeError("laya_parity_cpu needs LAYA_BACKEND=cpu")
        b = lb.CpuBackend(lb.BackendOptions.resolve())
        probe = b.probe()
        if not probe.available:
            raise RuntimeError("cpu backend unavailable: " + probe.reason)
        _load = b.load()
        _backend = b
        atexit.register(b.close)
    return _backend


def score(texts, priors=None):
    b = backend()
    events = [{"state": _mlx.build_state(t, priors[i] if priors else 0), "question": laya_gate.QUESTION}
              for i, t in enumerate(texts)]
    out = []
    for raw in b.score(events):
        ans = raw["answers"]["action"]
        p = (ans.get("probabilities") or {}).get(LABEL)
        if p is None:
            raise lb.BackendRuntimeError("answer has no probability for %r" % LABEL)
        out.append(float(p))
    return out


def score_rows(texts, rows=None):
    return score(texts, priors=_mlx._priors(rows) if rows else None)


score_rows.wants_rows = True


def run_report():
    """Facts about the last run, for notes: options, artifact, load time, peak RSS."""
    b = backend()
    return {"options": vars(b.options), "artifact": b.artifact["name"], "sha256": b.artifact["sha256"],
            "load_seconds": round(_load.load_seconds, 3), "threads_effective": b.threads_effective,
            "peak_rss_mib": b.peak_rss >> 20}
