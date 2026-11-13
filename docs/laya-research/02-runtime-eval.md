# Laya runtime evaluation for rungs 2-5 (GGUF, ONNX Runtime, PyTorch)

Question: can ONE model artifact cover rungs 2-5 of the fallback chain (modern CUDA/ROCm, legacy NVIDIA/AMD, open drivers, quantized CPU)?
Input: `01-architecture.md` (ModernBERT-large encoder, 2-layer head, marker pooling, `choice:2` temperature 1.9064, right-truncation at 512 tokens, gate threshold 0.59).
Access date for every source below: 2026-10-14. Trial host: Apple M4 Pro, CPU only (no NVIDIA or AMD hardware available).

## 1. Answer

Yes, for the artifact. One GGUF file (Q8_0, 449 MB, or F16, 844 MB) converts from the MLX weights, loads in stock `llama-server`, and returns the same calibrated score the gate reads. Rungs 2, 3 and 4 then differ only in which prebuilt `llama-server` binary runs it.

Two limits apply:

- `llama-cpp-python` CANNOT run Laya. Its newest release (0.3.36) vendors a llama.cpp with the ModernBERT encoder but without the decision head and without `/v1/systemone`. The runtime is the `llama-server` binary, called over HTTP. Evidence in section 4.
- Only the CPU path was run. CUDA, ROCm and Vulkan results come from release assets and source, not from a GPU run. Rung 2-4 behaviour is unverified until someone runs it on that hardware.

Q4_K_M is rejected: it flipped the 0.59 decision on 1 of 8 samples (section 3).

## 2. Comparison table

Rungs: R2 modern CUDA/ROCm, R3 legacy NVIDIA/AMD, R4 open drivers (Vulkan/Mesa), R5 CPU.
"unk" = unknown, reason given in the note column or section 5.

| | A. GGUF + `llama-server` (llama.cpp b11612) | B. ONNX Runtime 1.31 | C. PyTorch 2.14 port |
|---|---|---|---|
| R2 | Yes. Prebuilt: CUDA 12.8, CUDA 13.4, ROCm 10.0 (linux x86_64). Not run on GPU. | CUDA yes (`onnxruntime-gpu`). ROCm EP removed in ORT 1.23; MIGraphX replaces it (wheel 1.27.1, linux x86_64). Not run on GPU. | CUDA and ROCm wheels exist. Not run on GPU. |
| R3 | CUDA 12.8 build: PTX for sm_50+ (Maxwell). ROCm prebuilt: gfx908/90a/942/950 and gfx1010+; GCN/Vega not in the list, go to Vulkan. Not run. | unk: ORT does not publish its CUDA arch list. CUDA 13 wheel needs sm_75+ (toolkit limit). ROCm/MIGraphX list unk. | unk: per-wheel arch lists not checked. |
| R4 | Vulkan prebuilt (30 MB). Laya graph on Vulkan not run: unk whether every op is supported. | No Vulkan EP. Not covered. DirectML is Windows only, sustained engineering. | No desktop Vulkan backend. Not covered. |
| R5 | Yes. RUN: F16, Q8_0, Q4_K_M. | Yes. RUN: fp32 and int8. | Yes. RUN: fp32. |
| Conversion | RUN. `convert_hf_to_gguf.py` handles Laya natively after a tensor rename. 3 s. | RUN. Needs our own PyTorch port, then `torch.onnx.export`. No stock exporter covers the head. 6 s export. | RUN. Our port loads the MLX safetensors directly. |
| Score output | Native `/v1/systemone` returns `probabilities["interrupt her now"]`, temperature included. Tokenization and template done in the server. | Port returns logits and calibrated P. Tokenizer and sequence assembly stay in Python (reimplement `build_sequence`). | Same as ORT. |
| Min CC | 5.0 (CUDA 12.8 build, PTX JIT). 6.1 for fast int8 `dp4a`. 7.5 for the CUDA 13.4 build. | unk (not documented). 7.5 implied for the CUDA 13 wheel. | unk. |
| Min ROCm | Prebuilt ROCm 10.0. Older ROCm: build from source with `GPU_TARGETS`. unk for ROCm 5/6. | MIGraphX wheel only. Version matrix: see section 5. ROCm EP last in `onnxruntime-rocm` 1.22.2.post3 (ROCm 7.0). | unk. |
| Quant | Q8_0 max score delta 0.028. Q4_K_M max 0.109 and a flipped decision. F16 max 0.002. | int8 dynamic: max delta 0.074. int4: not run. fp32 exact. | torch int8: not run. |
| Wheels | None for Laya. PyPI has only the 0.3.36 sdist (77 MB), and that code lacks Laya. Use release tarballs. | PyPI wheels: cp311-cp314, macOS arm64, linux x86_64/aarch64, win. GPU wheel linux x86_64/aarch64, win. | PyPI wheels, plus separate CUDA and ROCm index URLs. |
| Size | Binary 18 MB CPU, 30 MB Vulkan, 152-172 MB CUDA, 245 MB ROCm. Model 449 MB (Q8_0). | Wheel 15-24 MB CPU, 168-256 MB GPU. Model 1.68 GB fp32, 422 MB int8. | Wheel 124-555 MB, installed 553 MB CPU on macOS. CUDA libs extra. Model 842 MB. |

## 3. Trial conversion

Reference scores come from the MLX runtime (`laya_mlx` 0.2.0) on 8 hand-written states, built with the gate's exact DOCTRINE text and QUESTION (`ref_mlx.py`). They are synthetic. They are NOT the gate's decision log.

### 3.1 Reference finding: MLX fp16 on CPU is not the same as fp16 on GPU

| run | max delta vs MLX fp32 |
|---|---|
| MLX fp16, GPU | 0.003 |
| MLX fp16, CPU | 0.073 (second logit off by up to 0.5) |

The gate's default device is GPU, so I use MLX fp32 as ground truth. fp32 on CPU matches my PyTorch port to 1e-5. Deltas against MLX fp16 GPU are within 0.001 of the numbers below.

### 3.2 Results (score = P("interrupt her now"), 4 decimals)

| sample | MLX fp32 | torch fp32 | ORT fp32 | ORT int8 | GGUF F16 | GGUF Q8_0 | GGUF Q4_K_M |
|---|---|---|---|---|---|---|---|
| 1 | 0.4879 | 0.4879 | 0.4879 | 0.4555 | 0.4859 | 0.4931 | 0.5965 |
| 2 | 0.4003 | 0.4003 | 0.4003 | 0.3927 | 0.4022 | 0.4007 | 0.3774 |
| 3 | 0.4921 | 0.4921 | 0.4921 | 0.4429 | 0.4923 | 0.4958 | 0.4837 |
| 4 | 0.5191 | 0.5191 | 0.5191 | 0.4456 | 0.5196 | 0.5275 | 0.5629 |
| 5 | 0.3538 | 0.3538 | 0.3538 | 0.3621 | 0.3535 | 0.3628 | 0.4409 |
| 6 | 0.7239 | 0.7239 | 0.7239 | 0.6782 | 0.7252 | 0.7503 | 0.6651 |
| 7 | 0.5589 | 0.5589 | 0.5589 | 0.5471 | 0.5600 | 0.5866 | 0.5760 |
| 8 | 0.5096 | 0.5096 | 0.5096 | 0.4928 | 0.5094 | 0.5178 | 0.5874 |
| max abs delta | - | 0.0000 | 0.0000 | 0.0735 | 0.0020 | 0.0277 | 0.1086 |
| decision at 0.59 preserved | - | 8/8 | 8/8 | 8/8 | 8/8 | 8/8 | 7/8 |
| model size | 842 MB | same | 1.68 GB | 422 MB | 844 MB | 449 MB | 287 MB |
| CPU ms/request (M4 Pro, after first) | - | - | 86 | 66 | 483 | 198 | 468 |

Read these with care:

- 0.59 preserved means "same side of the line". Sample 7 (0.5589) sits 0.031 below the line. Q8_0 moved it to 0.5866, which is 0.003 away from flipping. A real score between about 0.562 and 0.590 would flip under Q8_0. No sample sits inside that band, so Q8_0 near the threshold is NOT proven safe.
- The ONNX int8 run moves scores by up to 0.074 and is biased low (6 of 8 below reference). Dynamic int8 on a 28-layer encoder is weaker than Q8_0.
- Timings are one CPU, 8 samples, default thread counts. They only show order of magnitude. F16 is slow on CPU because it computes in fp16 where the CPU has no fast path.
- Token counts from `llama-server` equal `len(ids)` from `laya_mlx` on all 8 samples (143 to 158). Tokenizer, template and marker placement match.

### 3.3 Commands that were run

Scripts are in `docs/laya-research/02-runtime-eval-scripts/`. Working directory was a scratch dir; artifacts were not kept (only scripts and `ref_mlx.json`).

```
# reference
~/.hermes/venvs/laya-mlx/bin/python ref_mlx.py        # writes ref_mlx.json (fp16 CPU + fp32 CPU)
~/.hermes/venvs/laya-mlx/bin/python ref_gpu16.py      # fp16 on GPU

# env (python 3.12)
uv venv -p 3.12 .venv
uv pip install -p .venv/bin/python torch --index-url https://download.pytorch.org/whl/cpu
uv pip install -p .venv/bin/python transformers onnx onnxruntime onnxscript numpy tokenizers safetensors gguf huggingface-hub cmake ninja
# resolved: torch 2.14.1, onnxruntime 1.31.0, onnx 1.23.2, transformers 5.19.0

# C. PyTorch port vs MLX fp32
python trial_torch.py

# B. ONNX
python export_onnx.py                  # opset 18, legacy exporter, dynamic sequence axis
python trial_ort.py q                  # fp32 run, then quantize_dynamic QInt8 and run

# A. GGUF
git clone --depth 1 https://github.com/ggml-org/llama.cpp     # commit 6868a52
python remap_to_upstream.py <laya-mlx snapshot> laya_hf        # scorer.layers.N -> scorer.N
PYTHONPATH=llama.cpp/gguf-py python llama.cpp/convert_hf_to_gguf.py laya_hf --outtype f16 --outfile laya-f16.gguf
llama-quantize laya-f16.gguf laya-q8_0.gguf Q8_0
llama-quantize laya-f16.gguf laya-q4_k_m.gguf Q4_K_M
cmake -B build -G Ninja -DGGML_METAL=OFF -DGGML_BLAS=OFF -DLLAMA_CURL=OFF -DLLAMA_OPENSSL=OFF -DCMAKE_BUILD_TYPE=Release
cmake --build build --target llama-server llama-quantize -j 8
python trial_gguf.py laya-f16.gguf     # starts llama-server -ngl 0, POST /v1/systemone x8
```

File hashes of the produced artifacts (first 16 hex of sha256): F16 `8d387d39defd449b`, Q8_0 `edbf2bb7a7dc00d2`, Q4_K_M `1d1f29213bf78c41`. Source `model.safetensors` (renamed copy) `c864c76bc70bd2bebe0c`. Original MLX file sha256 is in `01-architecture.md`.

Time cost: the GGUF conversion took about 5 s plus 1-2 s per quantization. The llama.cpp build took under 10 minutes. Everything fit in the "under 1 hour, no GPU" budget.

## 4. Per-candidate findings

### A. GGUF via llama.cpp

Conversion
- llama.cpp master (commit 6868a52, 2026-10-14) registers `ModernBertDecisionModel` in `conversion/bert.py` for `convaiinnovations/laya`. It detects a checkpoint by `encoder/config.json` plus `rl_agent_config.json`, appends the 2 head layers after the 28 encoder layers, and stores the temperatures as GGUF metadata (`decision.temperature.choice.2`).
- The MLX file needs one fix first: the converter expects upstream names, MLX stores `scorer.layers.0/1/3`. `remap_to_upstream.py` renames them. The head `in_proj.weight` name already works. 206 tensors in, 201 out (the 4 `act_head` tensors and `temperature` are dropped on purpose; the gate does not use them).
- Conversion warnings seen: duplicate `add_bos/eos/sep_token` keys overwritten, no handler for the `cls` special token. Scores matched anyway.

Score output
- `llama-server` serves `POST /v1/systemone` with `{"state": str, "questions": {...}}`. It runs one forward pass, zero output tokens, and returns `answers.<id>.probabilities`. `criteria` for a choice question must be an object (`{"interrupt her now": null, ...}`), not the list that `laya_mlx` accepts.
- `llama-cpp-python` has the llama.cpp embedding and pooling API, but 0.3.36 (2026-10-01) vendors a llama.cpp that has ModernBERT (1 hit in `llama-arch.cpp`) and no decision code (0 hits for "systemone" and "decision" in the relevant files). It cannot run the head. Even an embedding-only route would need the head, marker pooling and temperature re-implemented outside, which defeats the point.
- Net: the Python integration is an HTTP call to a local `llama-server`, like the current gate already is a local HTTP service.

Legacy GPU coverage (sources: `ggml/src/ggml-cuda/CMakeLists.txt`, `.github/workflows/release.yml` at 6868a52)
- CUDA default architectures when the toolkit is below 13: `50-virtual 61-virtual 70-virtual 75-virtual 80-virtual 86-real 89-real 90-virtual` (+ Blackwell on 12.8). The comment says "50 == Maxwell, lowest CUDA 12 standard". With CUDA 13 the 50/61/70 entries are skipped, so the minimum is 7.5. Kepler (3.x): not covered by any current build; unk whether an old CUDA 11 source build still works.
- `GGML_CUDA_CC_DP4A` is 610: Pascal and newer get the fast int8 path. Maxwell runs quantized weights by the slow route (unmeasured).
- ROCm release job: `ROCM_VERSION 10.0.0`, targets `gfx908;gfx90a;gfx942;gfx950;gfx1010;gfx1011;gfx1012;gfx1030-1036;gfx1100-1103;gfx1150-1152;gfx1200;gfx1201` on Linux. Windows ROCm omits the Instinct targets. GCN and Vega (gfx803, gfx900, gfx906) are NOT in the prebuilt list: use Vulkan or build with `-DGGML_HIP=ON -DGPU_TARGETS=gfx900`. `docs/build.md` also documents `HSA_OVERRIDE_GFX_VERSION` for unlisted cards (not supported on Windows).
- Vulkan: prebuilt linux x86_64 and win x64. I did not find a vendor allow-list that blocks Mesa RADV, ANV or NVK. Whether the Laya graph (sliding window mask, 2 RoPE bases, a non-causal head) runs correctly on those drivers: unk, needs a run.

Quantization
- Q8_0 is the usable floor on this evidence (max delta 0.028 on 8 samples). Q4_K_M changed the decision once and moved scores by up to 0.109. Q4_K_M is also no faster on this CPU (468 ms vs 198 ms for Q8_0, unexplained; likely a kernel path, not investigated).
- Recommendation: Q8_0 for rung 5 and GPU rungs; F16 where VRAM is free and exactness matters. Re-fit or re-check the threshold against the decision log before trusting Q8_0 near 0.59.

Packaging
- Release b11612 (2026-10-14): `llama-b11612-bin-ubuntu-x64` 18 MB, `-ubuntu-vulkan-x64` 30 MB, `-ubuntu-cuda-12.8-x64` 172 MB, `-ubuntu-cuda-13.4-x64` 152 MB, `-ubuntu-rocm-10.0-x64` 245 MB, `-macos-arm64` 12 MB, `-win-cpu-x64` 20 MB, `-win-cuda-12.4-x64` 265 MB, `-win-vulkan-x64` 32 MB, `-win-rocm-10.0-x64` 258 MB. Separate `cudart` bundles (440-594 MB) exist for hosts without a CUDA runtime.
- `llama-cpp-python` 0.3.36: sdist only on PyPI. Prebuilt wheels live on `abetlen.github.io/llama-cpp-python/whl/{cpu,cu121..cu125,vulkan}` (the `rocm` path returns 404). Irrelevant for Laya, because of the vendored-source gap.
- The "latest" release object (tag v0.6.0) has no binary assets. The binaries are on the `bNNNN` tags. Pin one explicitly.

### B. ONNX Runtime

Conversion
- No stock exporter exists for the Laya head, type embedding or marker pooling. I wrote a plain PyTorch port (`laya_port.py`, about 100 lines, matches MLX fp32 to 1e-5) and exported it with `torch.onnx.export` (opset 18, legacy TorchScript exporter, which prints a deprecation warning; the dynamo exporter was not tried). The graph takes `input_ids [1,L]` and `marker_pos [1,2]` and returns `logits` and calibrated `prob`. Dynamic `L` works: tested on lengths 143 to 158 only.
- Batch size is fixed at 1 (the port has no padding mask). Fine for the gate, which calls one event at a time.
- fp32 result is exact to 4 decimals. The cost is that we own a second implementation of the model, and the tokenizer plus `build_sequence` (192-token head cap, 48-token option cap, right truncation at 512) must be copied into Python. A wrong copy silently shifts the score.

Score output
- Raw logits come out directly, plus the temperature-scaled probability if exported that way. No special EP support is needed.

Legacy GPU coverage
- CUDA table (ORT docs): 1.27.x to 1.30.x built with CUDA 13.0 by default on PyPI; separate CUDA 12.8 packages require CUDA 12.8 or newer; 1.21 to 1.26 CUDA 12.8; CUDA 11.8 builds only up to 1.18 on PyPI. The docs do not list compute capabilities. Min CC: unk. CUDA 13 toolkit support implies 7.5 or higher. Pascal and Maxwell on ORT CUDA 12.8: unk.
- AMD: the ROCm EP was removed in ORT 1.23. `onnxruntime-rocm` on PyPI stops at 1.22.2.post3 (ROCm 7.0 was the last officially supported release). The replacement is the MIGraphX EP: PyPI `onnxruntime-migraphx` 1.27.1, linux x86_64 only, Python 3.10-3.14. A later note says MIGraphX moves to a plug-in EP sourced from the AMDGPU repo. gfx target list: unk (not on the pages I read).
- Vulkan: none. DirectML: `onnxruntime-directml` 1.24.4, Windows only, in sustained engineering (docs say use WinML for new projects). Rung 4 is not covered.

Quantization
- `quantize_dynamic(QInt8)`: file 422 MB, max score delta 0.074, logit error up to 0.47. Static int8 with calibration, int4 (MatMulNBits) and fp16 were not run. Each is one more conversion path to validate.

Packaging
- 1.31.0 (2026-10-09), Python >= 3.11. CPU wheel 15-24 MB on macOS arm64, linux x86_64/aarch64, win amd64/arm64 (cp311-cp314). `onnxruntime-gpu` 168-256 MB (linux x86_64/aarch64, win amd64). Installed CPU size 76 MB.
- The CUDA 12.8 variant for >= 1.27 is described but its package index was not checked: unk whether it needs a custom index.

### C. PyTorch / transformers

Conversion
- Nothing to convert. The same port loads `model.safetensors` straight from the MLX snapshot (`laya_port.load`), after renaming keys. Stock `transformers.ModernBertModel` was not used because it covers only the encoder; I wrote the encoder out too so the mask and RoPE match MLX exactly.
- This is the oracle: it proves the architecture facts in `01-architecture.md` (marker pooling at `[MASK]`, `type_emb[0]` for choice, ReLU in the head layers, local window `|i-j| <= 64`).

Score output
- Any head works: the port returns the 2 logits and the calibrated probability.

Legacy GPU coverage
- Wheels for CUDA and ROCm exist on the PyTorch index URLs; I did not read their arch lists, so min CC and min ROCm are unk. I know of no desktop Vulkan backend (the PyTorch Vulkan backend targets mobile; from memory, not re-checked today). Treated as not covered for rung 4.

Quantization
- Not run. Torch int8 dynamic quantization of `nn.Linear` is available, but it is CPU only and was not trialled, so no accuracy number exists.

Packaging
- 2.14.1: macOS arm64 wheel 127 MB, linux x86_64 wheel 555 MB (before the separate NVIDIA libraries), win 124 MB. Installed CPU build on macOS is 553 MB. By far the heaviest option; also needs a Python install on every target box.

## 5. Unknowns that block a hard claim

- GPU runs: none of R2, R3, R4 were executed. Everything stated about CUDA, ROCm and Vulkan comes from build files, release assets and docs.
- Laya graph on Vulkan with Mesa RADV, ANV and NVK: unk.
- ORT and PyTorch minimum compute capability and ROCm version: not documented in the pages read.
- ONNX MIGraphX supported ROCm version and gfx list: the page lists a version matrix I did not extract.
- Real-input accuracy: only 8 synthetic states. Closest to the threshold was 0.5589 (MLX fp32, sample 7). No check against the gate's 337+ logged decisions. UNK whether the decision log stores the full input (carried over from 01).
- The 0.59 threshold was calibrated on fp16 MLX outputs. After conversion it should be re-checked, not assumed.
- Maxwell and Pascal performance with Q8_0: unmeasured.
- `llama-server` Laya support is days old. The decision head was added by an upstream commit I did not date. Pin the build and rerun `trial_gguf.py` on every bump.

## 6. Recommendation

Use candidate A: the single Q8_0 GGUF, served by a pinned `llama-server` binary.

Why:
- It is the only candidate where one file covers all four rungs, including Vulkan (rung 4) and CPU, with a prebuilt binary for each.
- The server owns tokenization, the prompt template, marker positions and temperature. Candidates B and C make us reimplement `build_sequence` and keep a second copy of the model. That copy is where silent threshold drift would come from.
- Conversion is a rename plus one script, 5 seconds, no GPU.
- Score fidelity: F16 within 0.002, Q8_0 within 0.028 of fp32 on the sample set. Binary size is 18-245 MB, the model 449 MB.
- No Python runtime dependency on the target box. The gate keeps its current shape (HTTP to a local service).

Fallbacks:
- Rung 5 exactness or no network access to llama.cpp builds: ONNX Runtime fp32 CPU. Scores equal MLX to 4 decimals. It costs a 1.68 GB file and the port. Skip int8.
- Rung 2 where `llama-server` fails on a GPU: PyTorch port as a reference only, not as a deployed path (size).
- Do not use Q4_K_M, ONNX int8 or `llama-cpp-python`.

Before any rung is declared done (not done by this task):
1. Run `trial_gguf.py` on one box per rung: CUDA 12.8 on an old NVIDIA (Pascal or Maxwell), ROCm on gfx1030 and a Vega card, Vulkan on RADV, ANV and NVK.
2. Build a regression set from real events near the threshold (scores 0.50 to 0.70) and compare Q8_0 against MLX. If the log lacks inputs, start retaining them.
3. Decide whether the threshold moves for Q8_0 or the artifact is F16 on GPU and Q8_0 on CPU only.

## 7. Reproducible conversion recipe

Prerequisites: the `aac6fef/laya-mlx` snapshot at revision `20aed815fc6acde75733882e7ec0e3f28aeb9717` (or upstream `convaiinnovations/laya`, which needs no rename), Python 3.12, git, a C++ compiler.

```
# 0. tools
uv venv -p 3.12 .venv
uv pip install -p .venv/bin/python safetensors torch numpy transformers tokenizers sentencepiece cmake ninja
git clone https://github.com/ggml-org/llama.cpp && git -C llama.cpp checkout 6868a52   # or a newer tag, then rerun step 5

# 1. rename tensors to the upstream layout (skip if starting from convaiinnovations/laya)
SNAP=$(ls -d ~/.cache/huggingface/hub/models--aac6fef--laya-mlx/snapshots/*)
.venv/bin/python docs/laya-research/02-runtime-eval-scripts/remap_to_upstream.py "$SNAP" laya_hf

# 2. convert (F16 is lossless: the source is F16)
PYTHONPATH=llama.cpp/gguf-py .venv/bin/python llama.cpp/convert_hf_to_gguf.py laya_hf --outtype f16 --outfile laya-f16.gguf

# 3. quantize
PATH=$PWD/.venv/bin:$PATH cmake -S llama.cpp -B llama.cpp/build -G Ninja -DGGML_METAL=OFF -DGGML_BLAS=OFF -DLLAMA_CURL=OFF -DLLAMA_OPENSSL=OFF -DCMAKE_BUILD_TYPE=Release
PATH=$PWD/.venv/bin:$PATH cmake --build llama.cpp/build --target llama-server llama-quantize -j 8
llama.cpp/build/bin/llama-quantize laya-f16.gguf laya-q8_0.gguf Q8_0

# 4. serve (CPU shown; use a prebuilt CUDA, ROCm or Vulkan binary of the same tag for other rungs)
llama.cpp/build/bin/llama-server -m laya-q8_0.gguf --port 11437 -ngl 0 -c 1024 --no-webui

# 5. request (same body shape the gate builds; criteria is an object)
curl -s localhost:11437/v1/systemone -H 'Content-Type: application/json' -d '{
  "state": "<DOCTRINE>\n\nSituation: ...\nEvent: ...\nEvents of this kind on this session in the last 24 hours: 0.",
  "questions": {"action": {"type": "choice",
    "instructions": "What is the right action for the operator here?",
    "criteria": {"interrupt her now": null, "log it and stay silent": null}}}}'
# score = answers.action.probabilities["interrupt her now"]; compare with 0.59
```

Verification after each rebuild or llama.cpp bump: run `trial_gguf.py` against `ref_mlx.json` (8 states) and require max delta <= 0.03 for Q8_0 and an identical `input_tokens` count for each state.

## 8. Sources (all accessed 2026-10-14)

- llama.cpp source at commit 6868a52: `conversion/bert.py` (ModernBertDecisionModel), `ggml/src/ggml-cuda/CMakeLists.txt` (CUDA arch list), `ggml/src/ggml-cuda/common.cuh` (DP4A cc 610), `.github/workflows/release.yml` (ROCm 10.0.0 gfx lists), `docs/build.md` (HIP, HSA_OVERRIDE), `tools/server/server-decision.cpp`, `tools/server/tests/unit/test_systemone.py`. https://github.com/ggml-org/llama.cpp
- llama.cpp release b11612 assets (names and sizes): https://api.github.com/repos/ggml-org/llama.cpp/releases/tags/b11612
- llama-cpp-python 0.3.36 sdist, vendored `vendor/llama.cpp` inspected: https://pypi.org/project/llama-cpp-python/ and https://github.com/abetlen/llama-cpp-python (latest commit "bump version to 0.3.36", 2026-10-01); wheel indexes https://abetlen.github.io/llama-cpp-python/whl/{cpu,cu121,cu122,cu123,cu124,cu125,vulkan}/llama-cpp-python/
- ONNX Runtime CUDA EP requirements: https://onnxruntime.ai/docs/execution-providers/CUDA-ExecutionProvider.html
- ONNX Runtime install: https://onnxruntime.ai/docs/install/
- ONNX Runtime ROCm EP (removed after 7.0): https://onnxruntime.ai/docs/execution-providers/ROCm-ExecutionProvider.html
- ONNX Runtime MIGraphX EP: https://onnxruntime.ai/docs/execution-providers/MIGraphX-ExecutionProvider.html and https://github.com/onnxruntime/onnxruntime-ep-amdgpu/blob/main/docs/MIGraphX-ExecutionProvider.md
- Wheel listings (PyPI JSON API): https://pypi.org/pypi/onnxruntime/json (1.31.0), `onnxruntime-gpu` (1.31.0), `onnxruntime-directml` (1.24.4), `onnxruntime-rocm` (1.22.2.post3), `onnxruntime-migraphx` (1.27.1), `llama-cpp-python` (0.3.36), `torch` (2.14.1)
- Local evidence: `01-architecture.md`, `laya_mlx` 0.2.0 source (`model.py`, `agent.py`, `common.py`), scripts in `02-runtime-eval-scripts/`.
