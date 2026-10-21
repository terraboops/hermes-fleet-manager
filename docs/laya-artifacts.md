# Laya artifacts per backend rung

> **NOT YET OWNER-APPROVED.** `laya-runtime-decision.md` is a PROPOSAL; no approval is recorded. Its recommendation (GGUF, pinned `llama-server`, Q8_0, convert from upstream) was followed as written. If the owner changes any answer in section 9 of the memo, rebuild.
>
> **NOT FOR REDISTRIBUTION YET.** Apache-2.0 allows it, but hosting is blocked on the open teacher-model and training-data licence question (memo Q8). No remote copy exists on purpose.
>
> **GPU rungs are UNVERIFIED.** Nothing here ran on CUDA, ROCm or Vulkan hardware. Parity was measured on CPU, Apple M4 Pro, with 8 synthetic states. Q8_0 near the 0.59 threshold is not proven (memo Q6, Q7).

Machine-readable manifest: `laya_artifacts.json` (repo root). Check a cache with `python3 scripts/laya_artifacts.py verify`.

## Artifacts

Source: `convaiinnovations/laya` rev `7b928d82...` (weights identical at the previously pinned `c5d78730...`; `model.safetensors` sha256 `891102d3...8d86c`). llama.cpp commit `6868a52`.

| Artifact | Rungs | Size (bytes) | sha256 | Max delta vs MLX fp32 | Decisions kept at 0.59 | CPU ms/req | Parity |
|---|---|---|---|---|---|---|---|
| laya-f16.gguf | GPU modern, GPU legacy, Vulkan (CPU works) | 844,026,784 | `e5e2a604c4a5513e7d60b79da33aaa803a9d271b3cdd3872e54de6b57e32d749` | 0.0020 | 8/8 | 519 | pass |
| laya-q8_0.gguf | all llama-server rungs, default | 449,397,664 | `320c00b3026bfe820a37486853a445d551c79cd4be6b2edca0a823d5efa43622` | 0.0277 | 8/8 | 223 | pass (0.003 of margin) |
| laya-q6_k.gguf | CPU, weak machines | 365,672,864 | `8182f12e5c7096f7ab1d9f095d185727c16a825a9875c06d8be34e6950602145` | 0.0983 | 7/8 | 252 | FAIL |
| laya-q4_k_m.gguf | CPU, smallest weak machines | 287,238,560 | `bc726982ecdcc74509201095340801233cad496a0c987a0e0310a2e04e575128` | 0.1086 | 7/8 | 280 | FAIL |

- Rung 1 (MLX) needs no artifact: it uses the existing laya-mlx service.
- Q6_K and Q4_K_M are real choices for weak machines but each flipped one decision. Do not select them automatically. A box that cannot run Q8_0 should take the floor (escalate) unless the owner accepts the drift.
- These hashes differ from the 16-hex hashes in the memo (F16 `8d387d39`, Q8_0 `edbf2bb7`) because those came from the MLX port with a tensor rename; this build uses upstream directly. Q8_0 scores match the memo (max delta 0.0277). Rebuilding gives the same hashes (a second F16 conversion and Q8_0 quantization gave identical hashes).

## Hosting and cache

- Remote location: none. Nothing is uploaded (licence caveat above). `remote` is `null` in the manifest. When hosting is approved, the host must serve each blob with `NOTICE` and the same sha256.
- Local cache, content addressed:
  - macOS `~/Library/Caches/thos/laya/`
  - Linux `${XDG_CACHE_HOME:-~/.cache}/thos/laya/`
  - Windows `%LOCALAPPDATA%\thos\laya\`
  - Layout: `blobs/<sha256>/<name>.gguf` plus `NOTICE`; `models/<name>.gguf` symlinks; `build/` scratch (safe to delete). `LAYA_CACHE` overrides the root.
- Every blob has `NOTICE` beside it (`docs/laya-artifacts-NOTICE.txt`): source, Apache-2.0, change marking.

## Reproduce

One command from the repo root: `scripts/laya_artifacts_build.sh`. It needs `uv`, git, a C++ compiler, Python 3.12 and about 6 GB of disk. Toolchain used: torch 2.14.1, safetensors 0.8.0, transformers 5.20.2, cmake 4.4.4, ninja 1.13.2. Steps it runs:

```
uv venv -p 3.12 .venv
uv pip install -p .venv/bin/python safetensors torch numpy transformers tokenizers sentencepiece cmake ninja huggingface_hub
git clone https://github.com/ggml-org/llama.cpp && git -C llama.cpp checkout 6868a52
.venv/bin/python -c "from huggingface_hub import snapshot_download; snapshot_download('convaiinnovations/laya', revision='7b928d828b7b0e022f929d9bd2e44165aa270148', local_dir='upstream_laya')"
PYTHONPATH=llama.cpp/gguf-py .venv/bin/python llama.cpp/convert_hf_to_gguf.py upstream_laya --outtype f16 --outfile laya-f16.gguf
PATH=$PWD/.venv/bin:$PATH cmake -S llama.cpp -B llama.cpp/build -G Ninja -DGGML_METAL=OFF -DGGML_BLAS=OFF -DLLAMA_CURL=OFF -DLLAMA_OPENSSL=OFF -DCMAKE_BUILD_TYPE=Release
PATH=$PWD/.venv/bin:$PATH cmake --build llama.cpp/build --target llama-server llama-quantize -j 8
llama.cpp/build/bin/llama-quantize laya-f16.gguf laya-q8_0.gguf Q8_0
llama.cpp/build/bin/llama-quantize laya-f16.gguf laya-q6_k.gguf Q6_K
llama.cpp/build/bin/llama-quantize laya-f16.gguf laya-q4_k_m.gguf Q4_K_M
.venv/bin/python trial_gguf.py laya-<level>.gguf     # parity, cwd has ref_mlx.json
```

Upstream needs no tensor rename (`remap_to_upstream.py` is only for the MLX port). Converter warnings (duplicate `add_*_token` keys, no `cls` handler) are expected. Conversion keeps 201 of 206 tensors.

## Not done

- No GPU, Vulkan or ROCm run (memo Q7). Those rungs stay `verified: false` until `trial_gguf.py` passes on real hardware with the matching `llama-server` binary.
- No `llama-server` binaries for rungs 2 to 4 were fetched or hashed; only the CPU build on this Mac was made.
- Regression set of real events in 0.50 to 0.70 (memo Q6) is not run; the 8-state result cannot prove threshold fidelity.
