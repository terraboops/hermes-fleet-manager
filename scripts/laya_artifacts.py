#!/usr/bin/env python3
"""Laya artifact manifest tool.

  laya_artifacts.py manifest   rebuild laya_artifacts.json from the cache and the parity logs
  laya_artifacts.py verify     check every artifact in the cache against laya_artifacts.json

Exit 0 only when every artifact exists with the recorded size and sha256.
"""
import hashlib, json, os, re, sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
MANIFEST = REPO / "laya_artifacts.json"
if sys.platform == "darwin":
    DEFAULT_CACHE = Path.home() / "Library/Caches/thos/laya"
else:
    DEFAULT_CACHE = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "thos/laya"
CACHE = Path(os.environ.get("LAYA_CACHE", DEFAULT_CACHE))
BOUND = 0.03  # max abs delta vs MLX fp32 (docs/laya-runtime-decision.md section 5)

ARTIFACTS = [
    # name, precision, quantize arg, rungs, role
    ("laya-f16", "F16", None, ["gpu_modern", "gpu_legacy", "vulkan", "cpu"],
     "GPU rungs when the owner chooses F16 (decision memo Q3); lossless against the F16 source"),
    ("laya-q8_0", "Q8_0", "Q8_0", ["gpu_modern", "gpu_legacy", "vulkan", "cpu"],
     "recommended default on every llama-server rung; the only quantized level that passes parity"),
    ("laya-q6_k", "Q6_K", "Q6_K", ["cpu"],
     "weak-machine option, FAILS parity on the 8-state sample: do not select automatically"),
    ("laya-q4_k_m", "Q4_K_M", "Q4_K_M", ["cpu"],
     "smallest weak-machine option, FAILS parity on the 8-state sample: do not select automatically"),
]


def sha256(p):
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def parity(name):
    p = CACHE / "build" / f"parity-{name.removeprefix('laya-')}.txt"
    rows = [l for l in p.read_text().splitlines() if " d " in l]
    ds = [float(re.search(r" d ([0-9.]+) ", l).group(1)) for l in rows]
    ok_dec = sum(l.rstrip().endswith("dec True") for l in rows)
    ms = int(re.search(r"ms/req \(excl first\) (\d+)", p.read_text()).group(1))
    return {"states": len(rows), "max_abs_delta": max(ds), "decisions_preserved": f"{ok_dec}/{len(rows)}",
            "cpu_ms_per_request_m4pro": ms, "bound": BOUND,
            "pass": max(ds) <= BOUND and ok_dec == len(rows)}


def manifest():
    out = []
    for name, prec, q, rungs, role in ARTIFACTS:
        f = next(CACHE.glob(f"blobs/*/{name}.gguf"), None) or (CACHE / "build" / f"{name}.gguf")
        sha = sha256(f)
        out.append({
            "name": name, "file": f"{name}.gguf", "precision": prec, "rungs": rungs, "role": role,
            "size_bytes": f.stat().st_size, "sha256": sha,
            "cache_path": f"blobs/{sha}/{name}.gguf",
            "remote": None,
            "produced_by": ["scripts/laya_artifacts_build.sh"] + (["llama-quantize laya-f16.gguf %s.gguf %s" % (name, q)] if q else []),
            "parity_vs_mlx_fp32": parity(name),
        })
    doc = {
        "schema": 1,
        "approval": {"owner_approved": False,
                     "flag": "NOT YET OWNER-APPROVED: docs/laya-runtime-decision.md is a PROPOSAL; recommendation followed as written."},
        "redistribution": {"allowed_now": False,
                           "reason": "Apache-2.0 permits it, but hosting is BLOCKING on the unresolved teacher-model and training-data licence (decision memo Q8). No remote copy exists."},
        "source": {"repo": "convaiinnovations/laya", "revision": "7b928d828b7b0e022f929d9bd2e44165aa270148",
                   "weights_identical_at": "c5d78730f3493e4fe16d61507ef4b78eef7318cf",
                   "model_safetensors_sha256": "891102d372688fc2a094dac56a384bc537b87c63f21f9f3dac0be2b7cbc8d86c",
                   "licence": "Apache-2.0"},
        "toolchain": {"llama_cpp_commit": "6868a52cb337450658006ad2f9281c2eb63f1666", "python": "3.12", "torch": "2.14.1",
                      "safetensors": "0.8.0", "transformers": "5.20.2", "cmake": "4.4.4", "ninja": "1.13.2"},
        "cache": {"root": {"darwin": "~/Library/Caches/thos/laya/", "linux": "${XDG_CACHE_HOME:-~/.cache}/thos/laya/",
                           "windows": "%LOCALAPPDATA%\\thos\\laya\\"},
                  "layout": "blobs/<sha256>/<file>.gguf (+ NOTICE), models/<name>.gguf symlinks, build/ scratch"},
        "rungs": {"mlx": "no artifact: existing laya-mlx service/safetensors (aac6fef/laya-mlx)",
                  "gpu_modern": "llama-server CUDA 12.8/13.4 or ROCm, UNVERIFIED on hardware",
                  "gpu_legacy": "llama-server CUDA 12.8 (sm_50+), UNVERIFIED on hardware",
                  "vulkan": "llama-server Vulkan, UNVERIFIED on hardware",
                  "cpu": "llama-server CPU, parity measured on Apple M4 Pro only"},
        "artifacts": out,
    }
    MANIFEST.write_text(json.dumps(doc, indent=2) + "\n")
    print("wrote", MANIFEST)


def verify():
    doc = json.loads(MANIFEST.read_text())
    bad = 0
    for a in doc["artifacts"]:
        p = CACHE / a["cache_path"]
        if not p.is_file():
            print("MISSING", a["name"], p); bad += 1; continue
        size, sha = p.stat().st_size, sha256(p)
        ok = size == a["size_bytes"] and sha == a["sha256"]
        print("OK     " if ok else "MISMATCH", a["name"], size, sha[:16])
        bad += not ok
    sys.exit(1 if bad else 0)


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    {"manifest": manifest, "verify": verify}.get(cmd, lambda: sys.exit(__doc__))()
