# Laya runtime: decision memo for the owner

Status: PROPOSAL. Every item below is a recommendation to approve, change or reject. Nothing is decided, built or shipped by this memo.

Section 8 (shipping proposal): APPROVED 2026-10-23 as written. One thos plugin install, with the runtime and model downloaded in the background on first activation. GPU rungs are enabled after a passing probe and flagged unverified (question 5, recommended answer). Questions 6 and 7 stay open and do not gate the integration; they gate trusting the threshold on a converted artifact and calling any GPU rung verified.

Evidence base (all accessed 2026-10-14):

- [01-architecture.md](laya-research/01-architecture.md) model facts
- [02-runtime-eval.md](laya-research/02-runtime-eval.md) runtime trial (scripts in [02-runtime-eval-scripts/](laya-research/02-runtime-eval-scripts/))
- [03-licences.md](laya-research/03-licences.md) licences (copied here from `fleet-hierarchy/docs/laya-research/`; that repo holds the original)
- [04-remote-design.md](laya-research/04-remote-design.md) opt-in remote server
- [05-shipping.md](laya-research/05-shipping.md) shipping

Tags used in this memo:

- [RUN] was executed in the trial, on an Apple M4 Pro CPU.
- [SRC] taken from build files, release assets or docs, not run.
- [UNK] not established.
- [BLOCKING] stops the named piece of work until the owner answers or evidence exists.

Headline: no GPU rung (2, 3, 4) has been run by anyone. Only CPU and MLX have real results.

## 1. Context and fallback chain

Laya is the local classifier behind lane 3 of the gate (`laya_gate.py`, `aac6fef/laya-mlx`). It runs today on Apple Silicon through MLX. The goal is to run the same classifier on other hardware, with one install step, and with a score that still means what the 0.59 threshold was fitted for.

Proposed chain, tried in this order. A rung is used only after a probe run on that box passes (see 05, section 5.3).

| Rung | Meaning | Proposed runtime | Evidence |
|---|---|---|---|
| 1 MLX | Apple Silicon, MLX. The calibration reference. | Existing laya-mlx service if it answers on `127.0.0.1:11436` | [RUN] today |
| 2 Modern CUDA/ROCm | NVIDIA CC 7.5+, or AMD gfx in the prebuilt ROCm list | `llama-server` CUDA 12.8 / 13.4 or ROCm 10.0 | [SRC] |
| 3 Legacy NVIDIA/AMD | NVIDIA CC 5.0 to 7.0 via the CUDA 12.8 build. AMD GCN/Vega has no legacy binary and drops to rung 4. | `llama-server` CUDA 12.8 | [SRC] |
| 4 Vulkan/Mesa/nouveau/RADV | Any real Vulkan device | `llama-server` Vulkan | [SRC]; Laya graph on RADV, ANV, NVK [UNK] |
| 5 Quantized CPU | Everything else on a supported platform | `llama-server` CPU, Q8_0 | [RUN] 198 ms |
| 6 Opt-in remote | A server the owner controls, speaking the 04 protocol | none local | design only |

Below rung 6 is a floor, not a rung: the gate's existing "classifier unavailable" path (lane 3 escalates, respond uses the standing policy, affirm returns risk 1.0). It is never skipped and never silent.

Two points that bend the chain, both for approval:

- The gate sees a few events a minute against a 3 s daemon budget. CPU Q8_0 takes 198 ms on the one CPU measured. Downloading a 30 to 245 MB GPU runtime for latency nobody needs is waste. Recommendation: keep the owner's order for selection, but fetch a GPU runtime only if the CPU probe is slower than a latency budget (1000 ms), or the owner sets `laya.gpu = always` (05, 5.3).
- 04 uses "rung" for hardware tier. This memo treats rung 6 as the remote option and assumes that reading matches the owner's chain.

## 2. Laya model facts

Source: 01-architecture.md.

- Architecture: ModernBERT-large bidirectional encoder (28 layers, hidden 1024, 16 heads, GeGLU, alternating local window 128 and global attention, RoPE 10000/160000) plus a decision model on top: `type_emb`, a 2-layer transformer head, a `scorer` (LayerNorm, Linear, GELU, Linear to 1) and an `act_head`. Not generative. One forward pass, zero output tokens, no KV cache.
- Size: 206 tensors, all F16, 421,293,830 parameters, 842,609,225 bytes. No quantization exists in the source.
- Tokenizer: byte-level BPE, `tokenizer.json` (3.58 MB), NFC normalizer, model vocab 50,368 rows. Special ids: CLS 50281, SEP 50282, PAD 50283. Sequence is assembled by hand: `[CLS] choice question: <instructions> [SEP] [MASK] opt1 [MASK] opt2 [SEP] <state> [SEP]`. Max 512 tokens, head capped at 192, each option at most 48. State is cut from the RIGHT, so the `Event:` line is cut first. [UNK] whether the vocab is byte-identical to `answerdotai/ModernBERT-large`.
- Weight format: `model.safetensors` from `aac6fef/laya-mlx` (rev `20aed815...`, sha256 `b9c07bf1...a3de`). It is a name-remapped FP16 cast of upstream `convaiinnovations/laya` (pinned source rev `c5d78730...`). The upstream checkpoint is the better conversion source. [UNK] upstream root dtype: config says float32, README says FP16 source.
- Score semantics: the gate compares `answers.action.probabilities["interrupt her now"]` to 0.59. That is a 2-way softmax probability after dividing the logits by the `choice:2` temperature 1.9064. It is not a raw logit, not a sigmoid, not a token logprob. Because of the division, scores are squeezed toward 0.5: P >= 0.59 needs a raw logit gap of at least 0.694. Observed over 337 logged decisions: 0.3094 to 0.8680, p75 0.5894.
- Hazard: the gate does `probs.get(...) or ans.get("confidence") or 0.0`. A missing key silently becomes a different quantity or 0.0, which reads as "stay silent". Any new backend client must parse strictly and raise (04, 2.3).
- Three details every port must copy, or 0.59 means something else: marker-position pooling at the `[MASK]` tokens, the 1.906 temperature, right-side truncation at 512 tokens.
- The 0.59 threshold was fitted on MLX fp16 GPU output (05, section 2). It has not been re-checked on any converted artifact against real inputs.

## 3. Comparison table: GGUF, ONNX Runtime, PyTorch (rungs 2 to 5)

Source: 02-runtime-eval.md, section 2. "unk" means not established. Nothing in R2 to R4 was run.

| | A. GGUF + `llama-server` (llama.cpp b11612, commit 6868a52) | B. ONNX Runtime 1.31 | C. PyTorch 2.14 port |
|---|---|---|---|
| R2 modern CUDA/ROCm | Prebuilt CUDA 12.8, CUDA 13.4, ROCm 10.0 (linux x64) [SRC] | CUDA yes (`onnxruntime-gpu`). ROCm EP removed in 1.23; MIGraphX wheel 1.27.1 linux x64 [SRC] | CUDA and ROCm wheels exist [SRC] |
| R3 legacy | CUDA 12.8 build has PTX for sm_50+ (Maxwell). ROCm prebuilt: gfx908/90a/942/950, gfx1010+. GCN/Vega not covered, use Vulkan. | unk. CUDA 13 wheel implies sm_75+. | unk |
| R4 Vulkan | Prebuilt (30 MB). Laya graph on Mesa drivers unk. | None. DirectML is Windows only. | None on desktop. |
| R5 CPU | Yes [RUN] F16, Q8_0, Q4_K_M | Yes [RUN] fp32, int8 | Yes [RUN] fp32 |
| Conversion | [RUN] stock `convert_hf_to_gguf.py` handles Laya after one tensor rename. About 5 s. | [RUN] needs our own PyTorch port (about 100 lines) then `torch.onnx.export` | [RUN] port loads MLX safetensors directly |
| Score output | Native `/v1/systemone` returns the calibrated probability. Tokenizer, template, temperature live in the server. | Logits plus P. We reimplement `build_sequence` and the tokenizer glue in Python. | Same as ORT |
| Min compute capability | 5.0 (PTX JIT, CUDA 12.8). 6.1 for fast int8 `dp4a`. 7.5 for the CUDA 13.4 build. | unk | unk |
| Min ROCm | Prebuilt is ROCm 10.0. Older: build from source with `GPU_TARGETS`. ROCm 5/6 unk. | MIGraphX only; gfx list unk | unk |
| Quantization | F16 max delta 0.002, Q8_0 0.028, Q4_K_M 0.109 and flipped 1 of 8 decisions | int8 dynamic max delta 0.074 (biased low); fp32 exact | not run |
| Packaging | No Laya-capable wheel. `llama-cpp-python` 0.3.36 cannot run Laya (its vendored llama.cpp lacks the decision head). Use release tarballs. Binary 12 to 245 MB, model 449 MB. | Wheels 15 to 24 MB CPU, 168 to 256 MB GPU. Model 1.68 GB fp32 or 422 MB int8. | Wheel 124 to 555 MB, 553 MB installed on macOS, CUDA libs extra. Model 842 MB. Needs Python on every box. |
| CPU latency (M4 Pro, 8 states) | Q8_0 198 ms, F16 483 ms, Q4_K_M 468 ms | fp32 86 ms, int8 66 ms | not timed |
| Decision at 0.59 preserved (8 states) | F16 8/8, Q8_0 8/8, Q4_K_M 7/8 | fp32 8/8, int8 8/8 | fp32 8/8 |

Caveat on the trial: 8 hand-written states, not the gate's decision log. Q8_0 moved sample 7 from 0.5589 to 0.5866, 0.003 short of flipping. A real score between about 0.562 and 0.590 would flip under Q8_0. No sample sat in that band, so Q8_0 near the threshold is NOT proven safe.

## 4. Recommended runtime and rationale

Recommendation for approval: one Q8_0 GGUF (449 MB), served by a pinned `llama-server` (b11612, commit 6868a52) on loopback, called over HTTP at `POST /v1/systemone`. Rungs 2, 3, 4 and 5 differ only in which prebuilt `llama-server` binary starts.

Why:

- It is the only candidate where one file covers rungs 2 to 5, including Vulkan, with a prebuilt binary for each.
- The server owns tokenization, template, marker positions and temperature. ONNX and PyTorch make us keep a second copy of the model and of `build_sequence`. That copy is where silent threshold drift would come from.
- Conversion is one rename and one script, no GPU.
- No Python on the target box. This matches the plan for thos, where the Laya model stays an HTTP service behind a `ModelPort` adapter (05, section 1).
- Fidelity: F16 within 0.002, Q8_0 within 0.028 of fp32 on the sample set.

Rejected on evidence: Q4_K_M (flipped a decision), ONNX int8 (0.074 delta), `llama-cpp-python` (cannot run Laya).

Fallbacks, if the owner wants them kept: ONNX Runtime fp32 CPU (exact to 4 decimals, costs a 1.68 GB file and our own port) where llama.cpp builds are unreachable. PyTorch as an offline oracle only.

Open risks that this recommendation carries:

- `llama-server` Laya support is days old. Pin the tag, rerun `trial_gguf.py` on every bump.
- Q8_0 near 0.59 is unproven. Alternative for approval: F16 (844 MB, delta 0.002) on GPU rungs and Q8_0 on CPU only. Recommendation is Q8_0 everywhere in v1 (one download), plus a regression set before the threshold is trusted (Q6 below).
- Metal build correctness with Laya is [UNK]; the trial built with `-DGGML_METAL=OFF`.

## 5. Conversion recipe

Source: 02-runtime-eval.md, section 7. Prerequisites: the `aac6fef/laya-mlx` snapshot at `20aed815fc6acde75733882e7ec0e3f28aeb9717` (or upstream `convaiinnovations/laya`, which needs no rename and is the recommended source), Python 3.12, git, a C++ compiler. Recorded hashes (first 16 hex): F16 `8d387d39defd449b`, Q8_0 `edbf2bb7a7dc00d2`, Q4_K_M `1d1f29213bf78c41`.

```
# 0. tools
uv venv -p 3.12 .venv
uv pip install -p .venv/bin/python safetensors torch numpy transformers tokenizers sentencepiece cmake ninja
git clone https://github.com/ggml-org/llama.cpp && git -C llama.cpp checkout 6868a52

# 1. rename tensors to the upstream layout (skip when starting from convaiinnovations/laya)
SNAP=$(ls -d ~/.cache/huggingface/hub/models--aac6fef--laya-mlx/snapshots/*)
.venv/bin/python docs/laya-research/02-runtime-eval-scripts/remap_to_upstream.py "$SNAP" laya_hf

# 2. convert (F16 is lossless: the source is F16)
PYTHONPATH=llama.cpp/gguf-py .venv/bin/python llama.cpp/convert_hf_to_gguf.py laya_hf --outtype f16 --outfile laya-f16.gguf

# 3. build and quantize
PATH=$PWD/.venv/bin:$PATH cmake -S llama.cpp -B llama.cpp/build -G Ninja -DGGML_METAL=OFF -DGGML_BLAS=OFF -DLLAMA_CURL=OFF -DLLAMA_OPENSSL=OFF -DCMAKE_BUILD_TYPE=Release
PATH=$PWD/.venv/bin:$PATH cmake --build llama.cpp/build --target llama-server llama-quantize -j 8
llama.cpp/build/bin/llama-quantize laya-f16.gguf laya-q8_0.gguf Q8_0

# 4. serve (CPU shown; other rungs use the prebuilt binary of the same tag)
llama.cpp/build/bin/llama-server -m laya-q8_0.gguf --port 11437 -ngl 0 -c 1024 --no-webui

# 5. request (criteria must be an object here, not the list laya_mlx accepts)
curl -s localhost:11437/v1/systemone -H 'Content-Type: application/json' -d '{
  "state": "<DOCTRINE>\n\nSituation: ...\nEvent: ...\nEvents of this kind on this session in the last 24 hours: 0.",
  "questions": {"action": {"type": "choice",
    "instructions": "What is the right action for the operator here?",
    "criteria": {"interrupt her now": null, "log it and stay silent": null}}}}'
# score = answers.action.probabilities["interrupt her now"]; compare with 0.59
```

Notes: step 1 renames `scorer.layers.N` to `scorer.N`. 206 tensors go in, 201 come out (the 4 `act_head` tensors and `temperature` are dropped; the gate does not use them). Conversion warnings seen: duplicate `add_bos/eos/sep_token` keys overwritten, no handler for the `cls` special token. Scores matched anyway.

### Parity check against MLX

The reference is MLX **fp32**, not fp16, because MLX fp16 on CPU deviates from fp32 by up to 0.073 while fp16 on GPU deviates by 0.003. fp32 on CPU matches the PyTorch port to 1e-5.

```
# reference scores (laya-mlx venv)
~/.hermes/venvs/laya-mlx/bin/python docs/laya-research/02-runtime-eval-scripts/ref_mlx.py     # writes ref_mlx.json
~/.hermes/venvs/laya-mlx/bin/python docs/laya-research/02-runtime-eval-scripts/ref_gpu16.py    # fp16 on GPU, for comparison

# candidate scores, one artifact at a time
.venv/bin/python docs/laya-research/02-runtime-eval-scripts/trial_gguf.py laya-q8_0.gguf       # starts llama-server -ngl 0, POST /v1/systemone x8
```

Pass criteria used in the trial and proposed for every rebuild or llama.cpp bump: max absolute delta <= 0.03 for Q8_0 against `ref_mlx.json`, and an identical `input_tokens` count for every state (143 to 158 on the 8 samples; matched in the trial). Results recorded: Q8_0 max delta 0.0277, F16 0.0020, tokens identical on 8 of 8.

Limit: 8 synthetic states cannot show threshold fidelity. That needs the regression set in Q6.

## 6. Licence findings

Source: 03-licences.md. Not legal advice. Chain of title: ModernBERT-large (Apache-2.0) to `convaiinnovations/laya` (Apache-2.0) to `aac6fef/laya-mlx` (Apache-2.0).

| Artifact | Licence | Convert | Redistribute | Obligations |
|---|---|---|---|---|
| `aac6fef/laya-mlx` weights | Apache-2.0 (card, LICENSE, NOTICE) | Yes (s.2 derivative works; "object form" includes conversion to other media) | Yes | Ship LICENSE, carry NOTICE, mark changed files (s.4) |
| `convaiinnovations/laya` weights | Apache-2.0 (card metadata; GitHub LICENSE; no LICENSE file on the HF repo) | Yes | Yes | Same. No upstream NOTICE found. |
| ModernBERT-large | Apache-2.0 | Yes | Yes | Same; credit Answer.AI/LightOn |
| Laya code, laya-mlx package | Apache-2.0 | Yes | Yes | Same |
| GGUF Q8_0 / F16 (our derivative) | Inherits Apache-2.0 | Yes | Yes | Mark "converted to GGUF, quantized to Q8_0"; ship LICENSE and NOTICE beside it |
| llama.cpp | MIT | n/a | Yes | Keep copyright and permission notice |
| llama-cpp-python | MIT | n/a | Yes | Keep both notices (bundles llama.cpp) |
| ONNX Runtime (CPU, GPU, DirectML) | MIT | n/a | Yes | Ship `ThirdPartyNotices.txt`; GPU wheel contents not unpacked [UNK] |
| PyTorch | BSD-style; wheel carries a composite of Apache-2.0, BSD, BSL-1.0, MIT | n/a | Yes | Reproduce notice; no endorsement |
| Vulkan loader | Apache-2.0 plus more permissive files | n/a | Yes | Use the system loader, do not vendor |
| ROCm | Per component: mostly MIT; hipCUB and RCCL custom; Comgr, rocminfo NCSA; ROCgdb GPLv3; kernel driver GPL-2.0 | n/a | Per component | Not assessed per component. [BLOCKING] for ROCm auto-fetch |
| CUDA runtime redistributables | NVIDIA proprietary EULA | n/a | Only files in EULA Attachment A, unmodified, inside an application with material additional functionality | cuDNN is not in Attachment A and its licence was not found. Do not bundle. |

Answer to the licence question: converting and redistributing the weights, including quantized forms, is allowed under Apache-2.0 with LICENSE, NOTICE and change marking. Every runtime except CUDA/cuDNN is bundleable with notices. Recommendation: convert from upstream `convaiinnovations/laya` rather than the third-party port to drop one link from the chain.

Unresolved (03, section 5):

- [BLOCKING] for hosting the converted model: teacher model identity and training-data licence are not stated by upstream. Apache-2.0 on the weights does not answer whether teacher output terms bind them. Needs the owner's call or a question to Convai Innovations.
- [BLOCKING] for any CUDA or cuDNN bundling: cuDNN terms not found; the EULA's "material additional functionality", "only accessed by your application" and notification clauses would need a decision, ideally counsel. Recommendation removes the question: never bundle.
- Upstream HF repo has no LICENSE or NOTICE file; the grant sits in card metadata and the GitHub LICENSE. Judgement call for the owner.
- The port's NOTICE says weights are not included in its repo, yet the repo has `model.safetensors`. Minor. Converting from upstream sidesteps it.
- Patents: only Apache-2.0 has an express patent grant (terminates if we sue over the Work). MIT/BSD runtimes have none. Owner to accept or not.
- llama.cpp bundled third-party sources not checked.

## 7. Remote server: protocol, auth and data-egress proposal

Source: 04-remote-design.md. Default is local only. Nothing leaves the machine unless the owner opts in per box.

Rules for approval:

1. No configuration means local. Remote needs an explicit opt-in naming server, redaction level and allowed questions. Changing any of them voids it.
2. The server computes probabilities and nothing else. Lanes, thresholds, quiet hours, repeat windows and every decision stay in the local gate. Lanes 1 and 2 never call any model.
3. An unreachable or untrusted remote never swallows an event.

Protocol: HTTP/1.1 and JSON, no streaming.

| Endpoint | Auth | Purpose |
|---|---|---|
| `GET /healthz` | none | `{ok, model_loaded, protocol}` only |
| `GET /v1/info` | bearer | model, revision, `weights_sha256`, runtime, rung, dtype, temperature digest, golden digest, limits |
| `POST /v1/score` | bearer | up to 16 items of `{id, state, question}`; the live path sends 1 |

- The request carries a random `request_id` (not derived from data) and an `expect` pin of `model_revision` and `weights_sha256`. Mismatch returns 409 and scores nothing.
- The response carries `probabilities` (the same temperature-scaled softmax as local, full precision), `tokens`, `truncated`, timings. It never carries a decision, threshold or escalate flag.
- Error table: 400, 401, 403, 404, 409, 413, 415, 422, 426, 429, 500, 503, with fixed messages that never echo the request. A 200 that fails client validation (missing criterion, probabilities not summing to 1, wrong `request_id` or revision) counts as a failure, never a score.
- Timeouts: connect 0.5 s, total 1.5 s for decide and 2.0 s for respond/affirm, one retry at most, never on 4xx. Breaker: 3 failures open it for 60 s, then a `/healthz` probe. The daemon's `FLEET_GATE_TIMEOUT` is 3 s; recommendation is 5 s when remote is on.
- Versioning: `/v1/` in the path, additive changes only. The server rejects unknown request fields so a client can never send data the contract does not name.

Auth and transport:

- Bearer token `laya_<kid>_<secret>`, 32 random bytes. Client stores it in a 0600 file (`~/.hermes/secrets/laya-remote.token`); config holds the path only. The gate refuses to start remote mode if the file is group or world readable. Server stores SHA-256 hashes only. Constant-time compare. Never in a URL, never logged.
- Rotation every 90 days or on suspected leak, two valid tokens at once during the switch. The owner rotates; the gate never writes the token file.
- Loopback may use `http://`. Every other host, tailnet included, requires `https://` (TLS 1.2+). No redirects followed. No `verify=False` flag.
- Certificate: SPKI pin (`sha256/<base64>`, current plus next) with bearer for v1. mTLS deferred.
- Auth, pin, revision or TLS failure latches the remote state until restart or reconfigure, sends no further event data, and raises one `GATE-DOWN:` notice naming the cause class and no secret.

Opt-in variables (all default to local): `LAYA_GATE_BACKEND` (`local`), `LAYA_GATE_REMOTE_URL`, `LAYA_GATE_REMOTE_TOKEN_FILE`, `LAYA_GATE_REMOTE_PIN`, `LAYA_GATE_REMOTE_REDACT` (`R2`, floor `R1`), `LAYA_GATE_REMOTE_QUESTIONS` (`decide`), `LAYA_GATE_REMOTE_ACK`, `LAYA_GATE_REMOTE_FALLBACK` (`local-then-standing`). The consent stamp is `sha256(url_host | pin | redact_level | questions)[:16]`, printed by a new `--remote-ack` command that also prints the egress table for the chosen level.

Redaction levels (applied in the gate, before the wire):

| Level | Applied |
|---|---|
| R0 | None. Local only, never remote. |
| R1 | Secret scrub, home prefix to `~`, local client-name blocklist. Minimum for remote. |
| R2 (default for remote) | R1 plus cwd to last component, other paths to basename, session/profile/project names to local pseudonyms, emails and hosts masked |
| R3 | Free text removed: only event family, quiet seconds, counts, state, a fixed description. Required for any server off the owner's network. |

A client name inside free text (`its most recent output was ...`) survives R2. Only R3, or a blocklist hit, removes it. Every level changes model input, so each needs its own parity and threshold check before use.

Exact egress fields (04, section 4.4 has all 35 rows). Sent for lane 3 `decide`: static `DOCTRINE` and `QUESTION` text; `text` (the `Event:` line, clipped to 600 chars on the remote path); `context` made of session short name, profile name, cwd, last 2 output lines (280 chars), seconds quiet and stall window, completion token name and due seconds; and the 24-hour same-kind count. `respond` and `affirm`, only if separately opted in, add the standing ruling (1200 chars), computed state, last messages tail (1200), latest inbound instruction (800), watch count, prior decision count, context (800) and the action under decision (600). Never sent under any configuration: the `session` request field as a field, `kind` (unless `text` is empty), `expecting`, decision ids and timestamps, the three local decision logs, registry/armed/watch files and transcripts beyond the fields above, thresholds, enforce flags, quiet hours, lane 1 and 2 events, verdicts, the bearer token, environment variables and secret files.

Failure policy: `local-then-standing`. Remote fails and a calibrated local model exists, score locally and log `backend: local-fallback`. No local model, take the existing unavailable path with the failure class in `rule`. Silence and queue-and-retry are rejected. A failing local model never causes a send to a remote that was not opted in.

Audit: each remote call adds `backend`, `remote_host`, `redact_level`, `egress_bytes`, `egress_sha256` (hash of the exact `state`, not the text), `model_revision`, `truncated`, `remote_ms` to the decision log. Additive schema change.

Server obligations (for a server the owner controls): no logging of `state`, `question` or `probabilities`; metadata-only logs kept 14 days; no core dumps or swap holding request memory; no forwarding, training or persistent cache; unprivileged user. A third-party hosted inference service is out of scope and needs its own privacy review.

Parity gate before a server may be used: a golden set of at least 200 recorded `decide` situations with local probabilities; proposed pass is max absolute difference <= 0.005 and zero flips inside the 0.57 to 0.61 band, per (server build, redaction level). Note the conflict with the local probe tolerance of 0.03 in the shipping plan; see Q9.

## 8. Shipping proposal

Source: 05-shipping.md. Goal: one install step.

Recommendation (option 4, hybrid): the user installs one thing, the thos bb plugin (`bb plugin install git:...`). The plugin carries under 1 MB: a pinned `laya.manifest.json`, 2 golden probe states with expected scores, LICENSE and NOTICE texts. Everything else downloads in the background on first activation.

| When | What | Size |
|---|---|---|
| Install | The plugin only | < 1 MB |
| First activation (background, never blocks start) | Hardware report, Q8_0 GGUF, CPU `llama-server` for this OS/arch, probe | 461 to 469 MB |
| Afterwards, only if selected | GPU `llama-server` for the chosen rung, probed. CPU stays as the standing fallback. | 30 to 245 MB |
| Never | CUDA runtime bundles, cuDNN, ROCm system packages, the Vulkan loader | host must provide |

Options considered and why not:

- Optional extras (`bb[cuda]`): thos is a bb plugin, not a pip package, so there are no extras; user must know their GPU; invites bundling CUDA. Rejected.
- Bundled CPU fallback in the plugin: puts 449 MB of model into git history for every clone. Rejected as default; kept as an optional air-gap asset (`bb thos laya export` / `import`).
- Lazy download of everything: right mechanism, but fetching GPU binaries eagerly wastes bytes and licence exposure.

Design points:

- Detection proposes, a probe decides. The probe starts the rung's `llama-server` on loopback with a random key, scores 2 golden states, requires identical `input_tokens` and `|score - expected| <= 0.03`, and times the second call. A software Vulkan device (llvmpipe, lavapipe) does not count as a GPU rung. Two synthetic states catch broken backends; they do not prove threshold fidelity.
- One breaker per (rung, hardware fingerprint): 10 minutes doubling to a 6-hour cap; a passing re-probe closes it. Failure falls rung by rung to the floor, never to silence.
- Integrity: HTTPS only, allow-listed hosts, sha256 and size checked before anything is executable, archive members whitelisted, no scripts run. Hashes are computed by us at release time. Trust root in v1 is the plugin git tag; a signed runtime-updatable manifest is deferred.
- Cache outside `~/.bb`: `~/Library/Caches/thos/laya/`, `${XDG_CACHE_HOME:-~/.cache}/thos/laya/`, `%LOCALAPPDATA%\thos\laya\`. Content-addressed. Warm cache runs fully offline; cold cache and offline degrades to the floor with a visible `bb thos doctor` line.
- Upgrade: new blobs fetch in the background, are probed on a second port, promoted only on a pass, previous set kept for rollback.
- Fresh Macs: adopt the existing MLX service as rung 1 if it answers. Otherwise use the macOS arm64 `llama-server` on CPU (`-ngl 0`, the measured path). No Python bootstrap by default.
- Unsupported platforms (Linux arm64, macOS x86_64, Windows arm64 have no listed asset) get remote if opted in, otherwise the floor. No source builds.
- Rungs 2 to 4 ship as `verified: false` in the manifest until a parity probe passes on real hardware. The Windows CUDA asset is 12.4, not 12.8; its arch coverage is [UNK].

Open risks (05, section 8): whether bb lets a plugin spawn long-lived children, survive reload, and write outside `~/.bb` is [UNK] (needs a spike); CPU latency on weak hardware is unmeasured; GitHub redirect hosts are unconfirmed; thos itself is a plan with no code.

## 9. Questions for owner sign-off

Recommended answer first. [BLOCKING] means the named piece cannot proceed without an answer or evidence. IDs in brackets point at the source tables (D = 04, S = 05).

Runtime and model

1. Approve one Q8_0 GGUF served by pinned `llama-server` b11612 for rungs 2 to 5? Recommend yes. [S6]
2. Convert from upstream `convaiinnovations/laya` rather than the third-party MLX port? Recommend yes (one less link in the chain). [S2]
3. Q8_0 on every rung (one download), or F16 on GPU rungs (844 MB, delta 0.002)? Recommend Q8_0 everywhere in v1. [S6]
4. Fresh Macs: GGUF on CPU, or bootstrap an MLX venv? Recommend GGUF on CPU, adopt the MLX service when present. [S7]
5. Rungs 2 to 4: enabled but unverified through `gpu=auto` after a passing probe, or disabled until run on real hardware? Recommend the former, flagged unverified. [S5]
6. [BLOCKING] for trusting the 0.59 threshold on any converted artifact: start retaining full model inputs in the decision log, then score a regression set of real events in 0.50 to 0.70 against MLX. 01 and 02 both leave open whether the log stores full inputs. Approve retaining them?
7. [BLOCKING] to call any GPU rung done: someone runs `trial_gguf.py` on real hardware (CUDA 12.8 on a Pascal/Maxwell card, ROCm on gfx1030 and a Vega card, Vulkan on RADV/ANV/NVK, Metal on a Mac). Who provides the hardware, and are results gating?

Licences and hosting

8. [BLOCKING] Who hosts the converted GGUF and where (repo the owner controls recommended), with LICENSE, NOTICE and a changed-notice? Includes closing the teacher and training-data licence question (ask Convai, or accept residual risk) and whether card metadata plus the GitHub LICENSE is enough evidence of the upstream grant. [S2]
9. Never fetch or host the CUDA runtime or cuDNN; a CUDA rung uses the host's own CUDA, else Vulkan? Recommend yes. [BLOCKING] only if the owner wants CUDA bundled: cuDNN terms and EULA clauses 1.1.2.x then need counsel. [S3]
10. [BLOCKING] for ROCm auto-fetch: per-component licence review of the ROCm tarball. Recommend ROCm auto-fetch stays off until done; Vulkan covers AMD meanwhile. [S4]
11. Accept that only Apache-2.0 carries an express patent grant among these licences? Recommend yes.

Remote

12. Local-only default at every rung, remote strictly per-box opt-in? Recommend yes. [D1]
13. Which questions may go remote: `decide` only to start, `respond` and `affirm` separate and off? Recommend yes. [D2]
14. Default redaction R2 with R1 floor; blocklist file at R1+; R3 for any non-owner network? Recommend yes. [D3, D4]
15. SPKI pin plus bearer for v1, mTLS deferred, 90-day rotation? Recommend yes. [D5, D11]
16. Parity thresholds: remote max delta 0.005 with zero flips in 0.57 to 0.61; local probe tolerance 0.03. These differ; confirm both, or unify. Recommend accept as proposed and tighten after the first golden run. [D6, S6]
17. Raise `FLEET_GATE_TIMEOUT` from 3 s to 5 s when remote is on? Recommend yes. [D7]
18. Third-party hosted inference: out of scope for v1 pending its own privacy review? Recommend yes. [D8]
19. `truncated: true` on a lane 3 call: log only, or treat as unavailable? Recommend log only in v1. [D9]
20. Failure policy `local-then-standing`, never swallow? Recommend yes. [D10]
21. Add `backend`, `rung`, `model_revision`, `model_sha`, `runtime_sha`, `redact_level`, `egress_sha256` to the decision log (additive schema change)? Recommend yes. [D12]
22. Remote as rung 6 allowed as an explicit per-box `fallback`; `primary` also allowed? Recommend fallback allowed, default off. [S8]

Shipping

23. Automatic 0.47 to 0.7 GB download on first activation with no prompt, capped by `maxDownloadMB` = 800 and visible in `bb thos doctor`? Recommend yes. [S1]
24. Pinned manifest inside plugin releases now; signed runtime-updatable manifest later (needs key custody)? Recommend yes. [S9]
25. Cache outside `~/.bb` in per-OS cache dirs? Recommend yes. [S10]
26. Unsupported platforms get remote or the floor, no source builds? Recommend yes. [S11]
27. The plugin downloads and then executes a pinned binary on loopback with a random key. Acceptable under bb's full-trust plugin model? Recommend yes, with the hash gate. [S12]
28. [BLOCKING] before building: a spike confirming bb lets a plugin run a persistent loopback child, survive reload, and write to a cache dir outside `~/.bb`. Approve the spike first?

## Appendix: original open questions (a) to (e)

| Question and sub-point | Answer | Where |
|---|---|---|
| (a) Architecture | ModernBERT-large encoder (28 layers) plus 2-layer decision head, scorer and act head. Not generative. | Section 2 |
| (a) Tokenizer | Byte-level BPE `tokenizer.json`, vocab 50,368 rows, hand-assembled 512-token sequence. Identity vs ModernBERT-large [UNK], non-blocking. | Section 2 |
| (a) Weight format | 206 F16 safetensors tensors, 421.3M parameters, 842 MB. Upstream dtype [UNK] (float32 or FP16). | Section 2 |
| (a) Score semantics | Temperature-scaled 2-way softmax, T = 1.906, compared to 0.59; threshold fitted on MLX fp16 GPU. | Section 2 |
| (b) One artifact for rungs 2 to 5 | Yes in principle: one Q8_0 GGUF with a pinned `llama-server`. Only CPU was run. | Sections 3, 4 |
| (b) Conversion feasibility | Proven: [RUN] 5 s, one rename, native llama.cpp support. | Section 5 |
| (b) Classifier and score output | Native `/v1/systemone` returns the calibrated probability; tokens match `laya_mlx` on 8 of 8. | Sections 3, 5 |
| (b) Legacy GPU: min compute capability | CUDA 12.8 build: 5.0 via PTX; 6.1 for fast int8; 7.5 for CUDA 13.4. ONNX and PyTorch unk. Not run on any GPU. | Section 3. BLOCKING for "done": Q7 |
| (b) Legacy GPU: min ROCm | Prebuilt needs ROCm 10.0 (gfx list in 02); older needs a source build; ROCm 5/6 unk; GCN/Vega go to Vulkan. | Section 3. BLOCKING for "done": Q7 |
| (b) Vulkan on Mesa drivers | Prebuilt exists; Laya graph on RADV, ANV, NVK unk. | Sections 1, 3. BLOCKING for "done": Q7 |
| (b) Quantization | F16 0.002, Q8_0 0.028, Q4_K_M 0.109 and a flip, ONNX int8 0.074. Q8_0 near 0.59 unproven. | Sections 3, 4. BLOCKING for the threshold: Q6 |
| (b) Packaging and wheels | No Laya-capable wheel; `llama-cpp-python` cannot run Laya; use release tarballs (12 to 245 MB). ORT and torch wheel sizes listed. | Section 3 |
| (c) Licence of weights | Apache-2.0 across the whole chain. | Section 6 |
| (c) Licence of runtimes | llama.cpp, llama-cpp-python, ORT MIT; PyTorch BSD-style; Vulkan loader Apache-2.0; ROCm per component; CUDA/cuDNN proprietary. | Section 6 |
| (c) Convert allowed | Yes, with change marking. | Section 6 |
| (c) Redistribute allowed | Yes, with LICENSE and NOTICE. Teacher and training-data licence unverified. | Section 6. BLOCKING for hosting: Q8 |
| (c) CUDA, cuDNN, ROCm bundling | CUDA/cuDNN do not bundle; ROCm unassessed. | Section 6. BLOCKING: Q9, Q10 |
| (d) Remote protocol | `/healthz`, `/v1/info`, `/v1/score`, JSON schemas, error table, timeouts, breaker. | Section 7 |
| (d) Auth | Bearer token, 0600 file, hashed server side, SPKI pin, HTTPS off loopback, rotation. | Section 7 |
| (d) Exact egress fields | 35-row table in 04, summarised; redaction R1 to R3; never-sent list. | Section 7. Owner picks redaction: Q14 |
| (d) Default local-only | Yes, opt-in per box with a consent stamp. | Sections 1, 7. Q12 |
| (e) Shipping without a second install step | Hybrid: one bb plugin plus background pinned download, probe-gated rungs. | Section 8 |
| (e) Feasibility inside bb | Persistent child, reload and out-of-tree cache unverified. | Section 8. BLOCKING: Q28 |
| (e) Model hosting | Needs a host and a licence decision. | Section 8. BLOCKING: Q8 |
