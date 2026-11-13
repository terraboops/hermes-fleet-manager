# Shipping Laya across the hardware fallback chain: options and recommendation

Task: t_56de2cda. Inputs: `01-architecture.md`, `02-runtime-eval.md`, `04-remote-design.md` (this folder), `03-licences.md` (in `~/Developer/fleet-hierarchy/docs/laya-research/`, not in this repo), `~/Developer/fleet-hierarchy/docs/BB-PLAN.v2.md` (the thos plan), `laya_gate.py`, `templates/launchd-laya-gate.plist`.
Access date for every claim: 2026-10-14. Status: PROPOSAL. Nothing here is built. No GPU rung has been run by anyone (see `02`, section 5).

Marks: [doc] read in a file named here. [plan] stated in BB-PLAN.v2.md, which describes software that is not built. [unk] not established; stated so rather than guessed.

## 0. Recommendation in one paragraph

Ship a hybrid (option 4): lazy, manifest-pinned download (option 2) with the CPU runtime fetched first so a working model exists within minutes on any box, and GPU runtimes fetched only after that, only for the one rung hardware detection selects, and only if a probe run on that box confirms the score. The user installs one thing: the thos bb plugin (`bb plugin install git:...`). The plugin carries no model and no binary, only a pinned manifest, a few golden probe states and licence texts. Optional extras (option 1) do not fit the delivery mechanism and fail the "no second install step" rule. A bundled CPU fallback (option 3) does not fit a git-installed plugin. Details, sign-off list and the failure ladder follow.

## 1. How thos/bb is packaged today

Findings, so the proposal is anchored in what exists:

- thos does not exist as code yet. `BB-PLAN.v2.md` line 3 says "plan, not built". There is no `package.json`, `pyproject.toml`, entry point or installer for thos [plan]. Anything below about its packaging rests on the plan.
- The install unit is ONE bb plugin package, id `thos`, package name `bb-plugin-thos` [plan, section 2.1]. It is TypeScript compiled by `bb plugin build` to `dist/server.js` plus frontend and worker bundles. The plan's install command is `bb plugin install git:https://<repo-host>/bb-plugin-thos@^0`. bb asks for confirmation because plugins are full-trust code [plan 10.2].
- The plugin runs inside bb's server process. It may start child processes: the plan already uses async `child_process.spawn` for git and gh with timeouts [plan, table around line 212].
- Plugin state: SQLite at `~/.bb/plugins/thos/data.db`; secret settings as 0600 files under the plugin's `secrets/`; settings are flat key/value [plan, sections 2.4 and 5].
- The plan fixes the model seam: "The Laya model service stays an HTTP service. The plugin calls it through a `ModelPort` adapter with the endpoint in settings." [plan, section 3, Laya paragraph]. One consequence: this proposal must supply that HTTP endpoint, not a library.
- Installing from git builds from source and downloads a pinned toolchain the first time [plan 10.2]. Whether bb accepts platform-specific prebuilt assets or runs install-time scripts is [unk] (plan spike K3 covers path, git and prebuilt installs). This proposal assumes NEITHER. It uses no install hook.
- No Python exists in this path. The only Laya packaging that exists today is in this repo: `laya_gate.py` re-execs into `~/.hermes/venvs/laya-mlx/bin/python3` (`LAYA_GATE_PYTHON`) and imports `laya_mlx`; a launchd template (`templates/launchd-laya-gate.plist`) runs it on port 11436. The venv is hand-made, 292 MB on this box (mlx 0.32.3, mlx-metal, laya-mlx 0.2.0, numpy, tokenizers, huggingface_hub), weights come from the Hugging Face cache `aac6fef/laya-mlx` (842 MB F16). There is no pyproject, no `pip install -e`, no installer script, no repo-level dependency pin [doc: README line 171, laya_gate.py lines 1197-1215]. This is the "Mac-only venv" in the brief.

Consequence for the options: a bb user has Node and bb, and not necessarily Python, uv, a compiler, CUDA or ROCm. Whatever ships must be a self-contained native server binary plus a data file. `02` already concluded that is `llama-server` plus a Q8_0 GGUF.

## 2. Fixed facts used below

From `02-runtime-eval.md` and `01-architecture.md`:

- Artifact: one Q8_0 GGUF, 449 MB (F16 844 MB; source F16 MLX file 842 MB). Q4_K_M rejected.
- Runtime: `llama-server` from a pinned llama.cpp release tag b11612 (commit 6868a52), called over HTTP `POST /v1/systemone`. `llama-cpp-python` cannot run Laya. ONNX Runtime and PyTorch are fallbacks only.
- Binary sizes (b11612): macOS arm64 12 MB; Ubuntu x64 CPU 18 MB, Vulkan 30 MB, CUDA 12.8 172 MB, CUDA 13.4 152 MB, ROCm 10.0 245 MB; Windows x64 CPU 20 MB, Vulkan 32 MB, CUDA 12.4 265 MB, ROCm 258 MB. Separate `cudart` bundles 440-594 MB.
- Not covered by any asset in that list: Linux arm64, macOS x86_64, Windows arm64. [unk] whether other assets exist for them; `02` did not list them. Those boxes get remote or nothing.
- Fidelity: Q8_0 max score delta 0.028 against MLX fp32 on 8 synthetic states; F16 0.002. Decision threshold 0.59. A real score in about 0.562 to 0.590 would flip under Q8_0. Q8_0 near the threshold is not proven safe.
- CPU speed: Q8_0 198 ms per request on an M4 Pro, after warmup. No other CPU measured [unk].
- Verified only on CPU. CUDA, ROCm and Vulkan results are from build files, never from a run. Laya on Vulkan RADV, ANV, NVK is [unk].
- Licences (`03`): weights Apache-2.0 end to end (ship LICENSE and NOTICE, mark changes); llama.cpp MIT. CUDA redistributables are the hard wall: "do not bundle, treat as an external dependency". cuDNN terms not found. ROCm is per component, GPL pieces must not be bundled, components not assessed. Vulkan: use the system loader. Open: teacher/training-data licence, card-metadata-only grant on the upstream HF repo.
- Remote (`04`): local-only by default; remote needs explicit per-box opt-in naming host, redaction level and allowed questions; failing local must not send to a remote that was not opted in; unavailable classifier takes the existing safe path (escalate, standing policy, not affirmed), never silence.

## 3. The six rungs, made precise

The owner's chain, with what each rung means for a shipped binary. Every rung is one `llama-server` process on `127.0.0.1` reading the same GGUF, except rung 1 and rung 6.

| Rung | Meaning | Runtime artifact | Status of evidence |
|---|---|---|---|
| 1 MLX | Apple Silicon, MLX, the calibrated reference (threshold 0.59 was fitted on its fp16 GPU output) | Existing laya-mlx HTTP service if present; otherwise see 6.3 | Runs today on this box |
| 2 Modern CUDA/ROCm | NVIDIA CC 7.5 and newer, or AMD GPU in the prebuilt ROCm target list | llama-server CUDA 12.8 or 13.4, or ROCm 10.0 | Not run |
| 3 Legacy GPU | NVIDIA CC 5.0 to 7.0 via the CUDA 12.8 build (PTX JIT); no AMD (GCN/Vega go to rung 4) | llama-server CUDA 12.8 | Not run; Maxwell/Pascal speed unmeasured |
| 4 Vulkan/open drivers | Any real Vulkan device (RADV, ANV, NVK, proprietary) | llama-server Vulkan | Not run; Laya graph on Mesa drivers [unk] |
| 5 Quantized CPU | Anything else | llama-server CPU | RUN on M4 Pro: 198 ms |
| 6 Optional remote | Opted-in server speaking the `04` protocol | none locally | Design only |

Below rung 6 there is an implicit floor: the gate's existing "classifier unavailable" path (escalate to the operator). It is never skipped.

A note the chain hides: the gate scores a few events per minute against a 3 s daemon budget. CPU Q8_0 at 198 ms on one machine suggests GPU rungs add download weight (30 to 245 MB, plus licence exposure) for latency nobody needs on most boxes. This proposal therefore keeps the owner's order for SELECTION but makes the GPU DOWNLOAD conditional (section 5.3, setting `laya.gpu`).

## 4. Options compared

### Option 1: optional extras (`bb[cuda]`, `bb[rocm]`, `bb[vulkan]`)

- What it is: the user picks a variant at install time.
- Fit: poor. thos is a bb plugin installed from git, not a pip distribution; there are no extras in that mechanism [plan]. Even if the Laya side were a Python package, `llama-server` is not on PyPI, and the one PyPI package that wraps llama.cpp (`llama-cpp-python` 0.3.36, sdist only) cannot run Laya (`02`).
- Rung detection: none at install; the user must know their GPU. Wrong choice means a second install, which breaks the brief.
- Download size: whatever extra the user picks, up front: 30 to 245 MB binary plus model, all at install.
- Offline: works after install, if the model came in the install. It cannot, because a 449 MB model in a git repo is the problem in option 3.
- Integrity: we would have to host wheels or archives ourselves and pin them; pip `--require-hashes` helps only on a Python path we do not have.
- Cache: pip site-packages; not applicable to bb.
- Upgrade: `bb plugin update` would not move extras; users re-pick.
- Licence: a `cuda` extra invites bundling CUDA libraries; `03` says do not.
- Verdict: REJECT. Fails "no second install step" and does not match the delivery mechanism.

### Option 2: lazy first-run download of backend and weights

- What it is: the plugin installs tiny; on first activation it detects hardware and downloads the runtime and model into a cache.
- Rung detection: at runtime, section 5.2.
- Download size: model 449 MB plus one binary. Examples: macOS 461 MB; Linux or Windows CPU about 467 to 469 MB; Vulkan about 479 to 481 MB; CUDA 12.8 about 621 MB; CUDA 13.4 about 601 MB; ROCm about 694 MB. With a missing CUDA runtime add 440 to 594 MB (not recommended, section 8).
- Offline: first run offline cannot work. After the cache is warm, fully offline. Needs an import path for air-gapped boxes.
- Integrity: manifest with pinned sha256 per asset; verify before the file is made executable.
- Cache: outside the plugin tree, content-addressed (section 5.5).
- Upgrade: new manifest in a new plugin release; fetch in background; switch after the probe passes.
- Licence: we do not ship NVIDIA code; the user's box fetches from upstream release URLs. Model hosting is a redistribution by us and needs LICENSE and NOTICE alongside.
- Weakness taken as given: until the first download completes there is no model; the gate uses its safe unavailable path (noisy, not unsafe).
- Verdict: right mechanism, but downloading the GPU binary eagerly wastes bytes and licence exposure.

### Option 3: bundled CPU fallback, GPU backends lazily added

- What it is: the plugin ships a CPU binary and the model, so the first run works offline.
- Size: 449 MB model plus 12 to 20 MB per platform, inside the plugin. The model alone is 449 MB, which on a slow link eats the plan's 10-minute install target by itself.
- Problems: a git-installed plugin clones the model; every model revision adds about 450 MB to history for every user's clone; a git install has no per-platform asset selection (and prebuilt installs are [unk]); the CPU binary differs per OS and arch, so either all of them are in the repo or none. `bb plugin update` re-fetches history. The plan's install target is 10 minutes on a warm network [plan 10.1] and the plugin is "a compiled source tree", not a data carrier.
- Offline: the only option with a working first run offline.
- Integrity: the git tag; strong and simple.
- Verdict: REJECT as the default. Keep as an optional release asset for air-gapped sites: `thos-laya-offline-<ver>.tar` containing the exact cache tree, imported with `bb thos laya import` (section 5.5).

### Option 4: hybrid (recommended)

- What it is: option 2's lazy, manifest-pinned fetch, ordered CPU-first, with GPU runtimes added only when selected and verified, plus option 3's offline bundle as a side asset.
- The plugin carries (under 1 MB): `laya.manifest.json`, 2 golden probe states with expected scores, LICENSE and NOTICE texts.
- Verdict: RECOMMENDED. Reasons: single install; first usable model in minutes with the smallest download that works; GPU bytes and CUDA/ROCm exposure only where earned; every rung is guarded by the same probe; failures degrade by one mechanism.

### Comparison

| | 1 Extras | 2 Lazy all | 3 Bundled CPU | 4 Hybrid |
|---|---|---|---|---|
| Matches bb plugin delivery | no | yes | partly (bloats git) | yes |
| Second install step | yes | no | no | no |
| Rung detection | user | runtime | runtime | runtime + probe |
| Install download | 30 to 245 MB + model | under 1 MB | 470 MB+ | under 1 MB |
| First-run download | none | 461 to 694 MB (+ cudart) | GPU binary only | 461 to 481 MB, GPU extra only if selected |
| Works offline first run | only if bundled | no | yes | no (air-gap bundle available) |
| Pinned hashes | pip only | manifest | git | manifest |
| Upgrade | manual re-pick | manifest | git history grows | manifest + probe gate |
| CUDA licence exposure | high | medium | high | lowest (never fetch CUDA runtime) |

## 5. The recommended design

### 5.1 What the user installs, what comes later, and when

| When | What | Size | Trigger |
|---|---|---|---|
| Install | `bb plugin install git:...bb-plugin-thos@^0`. Nothing else. Manifest, probe states, licence texts ride inside. | under 1 MB | the user |
| First activation of the model lane, background, never blocks plugin start | 1. Hardware report. 2. Model GGUF Q8_0. 3. CPU `llama-server` for this OS and arch (macOS arm64: the macOS binary). 4. Probe. After step 4 passes, lane 3 runs on rung 5 (or macOS runtime). | 461 to 469 MB | automatic (default `laya.autoFetch` = true; owner sign-off S1) |
| Shortly after, only if selected | The GPU `llama-server` for the selected rung (30 to 245 MB), then probe it. On pass, it becomes the active rung; CPU stays as the standing fallback. | 30 to 245 MB | automatic when the rung is eligible AND (`laya.gpu = always` OR the CPU probe's p50 exceeds `laya.latencyBudgetMs`) |
| Later | Upgrade fetches on a plugin update that changes the manifest | per changed asset | the next activation after `bb plugin update` |
| Never by default | CUDA runtime bundles, cuDNN, ROCm system packages, the Vulkan loader | n/a | host must provide; else the rung is unavailable |

Until step 4 first passes, the gate takes its existing unavailable path. That is the safe direction (`04`, section 5): escalate, standing policy, not affirmed. `bb thos doctor` prints `laya: fetching model 212/449 MB` so the noise has a visible cause.

### 5.2 Rung detection (runtime)

Detection yields candidates in the owner's order; the probe decides. Detection alone never promotes a rung.

1. Platform key: `process.platform` and `process.arch`. Only `darwin-arm64`, `linux-x64`, `win32-x64` have assets in `02`'s list. Others: skip to rung 6 (if opted in) or the floor.
2. Rung 1: `darwin-arm64` AND (a Laya HTTP service answers the health call at `laya.mlx.endpoint`, default `http://127.0.0.1:11436`). Not found: see 6.3.
3. NVIDIA: run `nvidia-smi --query-gpu=name,compute_cap,driver_version,memory.total --format=csv,noheader` (the `compute_cap` field exists on newer drivers; on older ones it errors [unk which version], fall back to a name table). Absent tool: check for `libcuda.so.1` (Linux) or `nvcuda.dll` (Windows).
   - compute_cap 7.5 or higher and driver new enough for the CUDA 13.4 build or the 12.8 build: rung 2 (CUDA). Driver floors: from memory about 570 for CUDA 12.8 on Linux, not re-checked [unk]; the probe is the real test, so the table errs permissive.
   - compute_cap 5.0 to 7.0 (below 7.5): rung 3 with the CUDA 12.8 build (Windows: the 12.4 asset; arch coverage of that asset [unk]). Maxwell/Pascal speed unmeasured.
4. AMD: `rocm_agent_enumerator` or `rocminfo` for the gfx id (Linux); `/sys/class/kfd` and `/sys/class/drm/*/device/vendor` = 0x1002 as a cross-check. gfx in the prebuilt list (gfx908/90a/942/950, gfx1010-1012, gfx1030-1036, gfx1100-1103, gfx1150-1152, gfx1200/1201) AND a working ROCm install: rung 2. Not in the list (GCN, Vega gfx803/900/906): not rung 3 (no ROCm legacy binary), go to rung 4. Whether the prebuilt ROCm tarball needs a host ROCm [unk]; the probe settles it.
5. Vulkan: `vulkaninfo --summary` or a loader check (`libvulkan.so.1`; ICD json under `/usr/share/vulkan/icd.d`). A device whose type is CPU (llvmpipe, lavapipe) does NOT count as a GPU rung; it is software and slower than rung 5. Accept DISCRETE, INTEGRATED, VIRTUAL.
6. Rung 5 always eligible on a supported platform.
7. Rung 6 eligible only if the per-box opt-in from `04` section 4.1 is present.
8. Hardware fingerprint = platform, GPU names, driver versions, compute caps / gfx ids, Vulkan driver id and version. Stored with each probe verdict. A changed fingerprint voids stored verdicts.

### 5.3 Probe (the arbiter for every rung)

Detection says what might work. A probe says what works.

1. Start the rung's `llama-server` on `127.0.0.1`, random port, random per-start `--api-key`, `-c 1024`, GPU layers per rung (`-ngl 99` for GPU rungs, `0` for CPU).
2. Wait for health, bounded by `laya.startTimeoutMs` (default 60 s; CUDA PTX JIT on Maxwell/Pascal may be slower on a cold start [unk]).
3. Score the shipped golden states (2 of the 8 in `02-runtime-eval-scripts/ref_mlx.json`: one far below the threshold, one near it). Each call must return in time and
   - `input_tokens` equals the recorded token count (catches tokenizer/template drift, as `02` section 7 requires), and
   - `|score - expected|` is at most `laya.parityTolerance` (default 0.03, from `02` section 7 for Q8_0).
4. Time the second call after warmup: `p50_ms`.
5. Persist the verdict: rung, runtime sha, model sha, hardware fingerprint, p50, result.

Limit, stated: 2 synthetic states do not prove the band 0.562 to 0.590 is safe. The probe catches broken or wildly wrong backends (a kernel miscomputing on a Mesa driver would show). It does not prove threshold fidelity; that is the regression set in section 9.

GPU download policy (`laya.gpu`):
- `auto` (default): fetch a GPU runtime only if the CPU probe's `p50_ms` is above `laya.latencyBudgetMs` (default 1000), or the rung was already cached.
- `always`: fetch the selected GPU runtime after CPU is ready.
- `never`: CPU or remote only.

Rungs 2 to 4 carry `verified: false` in the manifest until someone runs the parity probe on that hardware (`02` section 6, "before any rung is declared done"). Setting default for unverified rungs: usable, but only through `auto` or `always` and only after a passing probe; first activation prints that the rung is unverified.

### 5.4 Rung selection pseudocode

```
select_rung(cfg, hw, state):
    # cfg: settings (5.7). hw: detection report (5.2). state: persisted verdicts, breakers.
    candidates = []
    if hw.platform == "darwin-arm64":
        candidates += [R1_MLX_SERVICE]                 # only if endpoint answers, 6.3
    if cfg.laya.gpu != "never":
        candidates += eligible_gpu(hw)                 # R2, then R3, then R4, by detection order
    candidates += [R5_CPU]                             # always the standing local floor
    if cfg.remote.role == "fallback" and remote_opted_in(cfg):  # 04 s0, s4.1
        candidates += [R6_REMOTE]

    for r in candidates:
        if state.breaker_open(r, hw.fingerprint):     continue   # backed off, see 5.6
        if r.is_gpu and cfg.laya.gpu == "auto" and not state.cpu_too_slow() and not state.cached(r):
            continue                                              # no GPU bytes until earned
        if not ensure_assets(r, cfg):                 continue   # missing, offline, or hash mismatch
        server = start(r)                                         # may fail: ENOEXEC, missing lib, no device
        if not server:                                state.fail(r, "start"); continue
        v = probe(server, cfg)                                    # 5.3
        if not v.ok:                                  stop(server); state.fail(r, v.reason); continue
        state.activate(r, v)
        return r

    return FLOOR_UNAVAILABLE      # gate takes classifier-unavailable path; never silent


score(event):                      # called per gate request, within the daemon's budget
    r = state.active
    try:
        s = r.score(event, timeout=cfg.timeoutMs)
        record(decision_log, backend=r.id, rung=r.rung, model_sha=r.model_sha, runtime_sha=r.runtime_sha)
        return s
    except (Timeout, ConnectionError, BadReply, ProcessExit) as e:
        state.fail(r, classify(e))                 # backoff doubles: 10 min .. 6 h cap
        nxt = select_rung(cfg, hw, state)          # next eligible rung, probed
        if nxt != FLOOR_UNAVAILABLE and budget_left(event) >= nxt.p50:
            return nxt.score(event)                # rescore this event once on the next rung
        return UNAVAILABLE                         # existing safe path; not swallowed

background tick (every 10 min, and at plugin start):
    for r in higher_than(state.active) with breaker expired:
        re-probe r; if ok: state.activate(r)       # promote back up
```

Notes:
- Selection is idempotent and cheap; hardware reports are cached by fingerprint.
- The order inside the GPU block is rung 2 (modern), rung 3 (legacy), rung 4 (Vulkan), because those are the owner's ranks. On an NVIDIA box with a working Vulkan driver, a failed CUDA rung falls to Vulkan (rung 4) before CPU.
- Remote is considered last unless the owner opts in as the PRIMARY per `04` (`laya.backend = remote`); then `04` section 5.2 applies: remote first, local as fallback. Both orders are explicit opt-ins; neither is a default.

### 5.5 Download, integrity, cache

Manifest `laya.manifest.json` (in the plugin, so its trust root is the plugin git tag):

```json
{
  "schema": 1,
  "model": {
    "id": "laya-q8_0",
    "url": "https://<host>/laya-q8_0.gguf",
    "sha256": "...", "bytes": 449000000,
    "source": "convaiinnovations/laya", "source_revision": "...",
    "convert": { "llama_cpp_commit": "6868a52", "recipe": "02-runtime-eval.md s7", "remap_script_sha256": "..." },
    "license": { "spdx": "Apache-2.0", "files": ["LICENSE", "NOTICE"], "changed_notice": "Converted to GGUF and quantized to Q8_0" },
    "calibration": { "threshold": 0.59, "fitted_on": "mlx-fp16-gpu", "q8_check": "pending" }
  },
  "runtime": {
    "llama_cpp_tag": "b11612",
    "assets": {
      "darwin-arm64:cpu":    { "url": "...", "sha256": "...", "bytes": 12000000, "members": ["llama-server", "..."], "verified": "cpu" },
      "linux-x64:cpu":       { ... "verified": "cpu" },
      "linux-x64:vulkan":    { ... "verified": false },
      "linux-x64:cuda12.8":  { ... "verified": false },
      "linux-x64:cuda13.4":  { ... "verified": false },
      "linux-x64:rocm10.0":  { ... "verified": false },
      "win32-x64:cpu|vulkan|cuda12.4|rocm10.0": { ... }
    }
  },
  "golden": [ { "state_sha256": "...", "expected": 0.4879, "input_tokens": 143 } ]
}
```

The hashes are values we compute ourselves by downloading each upstream asset once at release time. Nothing is taken from the release page at run time.

Rules:
1. HTTPS only. Allow-listed hosts only, including the hosts that GitHub release downloads redirect to; the exact redirect hosts must be confirmed at implementation [unk]. Redirects to anything else fail.
2. Download to `<name>.part`, resume with HTTP Range, stream the sha256, compare to the manifest AND to `bytes`. A mismatch deletes the file and raises a `laya.integrity` error. Nothing is executable before the hash passes.
3. Archives: extract only members listed in `members`; reject absolute paths and `..`; set the execute bit only on the server binary; no scripts run.
4. Proxy: honour `HTTPS_PROXY`/`NO_PROXY`.
5. Atomic rename into the cache after verification; a second verify (hash) happens on start only if `laya.verifyOnStart` is true (default false: size and mtime check; full hash on `bb thos laya verify` and after an unclean shutdown).
6. Signed manifest: not in v1. The manifest is part of the signed-by-tag plugin release (git tag), and bb already asks the user to trust the plugin. A runtime-updatable manifest (to bump llama.cpp without a plugin release) would need a detached ed25519 signature checked against a public key compiled into the plugin. Key custody is owner decision S9; it is deferred, not skipped.

Cache location (outside `~/.bb`, so `bb plugin remove` plus reinstall does not delete 0.5 GB; per `BB-PLAN`, remove deletes settings and secrets):
- macOS `~/Library/Caches/thos/laya/`
- Linux `${XDG_CACHE_HOME:-~/.cache}/thos/laya/`
- Windows `%LOCALAPPDATA%\thos\laya\`
- Override: `laya.cacheDir` or env `THOS_LAYA_CACHE`.
- Layout: `blobs/sha256/<hex>` (content-addressed model and archives), `runtime/<llama_tag>-<platform>-<variant>/` (extracted), `verdicts.json` (probe verdicts keyed by hardware fingerprint), `current.json` (active set, previous set).
- The same cache can later serve `laya_gate.py` in this repo so the Mac venv is no longer the only supported copy. Out of scope here.

Offline behaviour:
- Warm cache: fully offline, no network call ever.
- Cold cache and offline: `ensure_assets` fails, rung unavailable, the gate takes the unavailable path (escalate), `bb thos doctor` prints `laya: needs 467 MB, offline`. The plugin still activates. Nothing retries faster than every 15 minutes with backoff.
- Air-gapped: `bb thos laya export <dir>` on a connected box and `bb thos laya import <dir>` on the target. Import re-verifies every file against the manifest. The offline release asset (option 3's useful half) is the same tree.

### 5.6 Failure ladder: how each rung degrades

One mechanism for all rungs: a per-(rung, hardware fingerprint) breaker. A failure opens it for 10 minutes, doubling to a 6-hour cap. A passing re-probe closes it. Verdicts persist, so a known-bad driver is not retried on every start, and are voided when the driver version or the runtime sha changes.

| Rung | Failure | What happens | Next |
|---|---|---|---|
| 1 MLX service | endpoint down, health fails, timeout, or score outside parity | breaker opens; the plugin does NOT start or install MLX | rung 2 to 5 (GGUF path on this Mac: macOS binary, see 6.3) |
| 2 CUDA/ROCm | runtime missing from host (no libcuda, no ROCm), binary exits, CUDA error at start, OOM, probe timeout or score off | stop server; log class (`start`, `oom`, `parity`, `timeout`); breaker opens | rung 3 if NVIDIA and CC under 7.5 or CUDA 12.8 build not yet tried; else rung 4 |
| 3 Legacy GPU | PTX JIT fails, unsupported arch, slow beyond budget (`p50_ms` over `laya.latencyBudgetMs`), parity fails | same | rung 4 |
| 4 Vulkan | no ICD, only a software device, graph unsupported on this driver [unk], parity fails | same | rung 5 |
| 5 CPU | binary will not run (illegal instruction, missing lib), `ENOMEM`, timeout over budget | breaker opens; this is the last local rung | rung 6 if opted in, else the floor |
| 6 Remote | `04` section 5: timeout, auth, pin, revision, 5xx | verdict `local-then-standing`; after 3 consecutive failures send one `GATE-DOWN:` notice | the floor |
| Floor | no rung available | the gate's existing unavailable path: lane 3 escalates, response uses the standing policy, affirm returns risk 1.0 (not affirmed) | none; never silent |

Runtime failure mid-session (a good rung that later breaks): the request that failed is rescored once on the next rung if the daemon budget allows (`FLEET_GATE_TIMEOUT`, 3 s, or 5 s with remote on per `04` D7); otherwise it takes the floor. The next tick re-promotes when the breaker expires.

Every decision record gets `rung`, `backend`, `model_sha`, `runtime_sha`. This extends `04` D12 and is what lets a later review separate "the model said so" from "a different backend said so".

### 5.7 Config surface

Plugin settings are flat [plan]. Proposed keys, all with defaults. Existing `LAYA_GATE_*` variables for the Hermes gate are untouched.

| Key | Default | Meaning |
|---|---|---|
| `laya.enabled` | true | Master switch. Off: floor only. |
| `laya.autoFetch` | true | Download automatically (S1). False: `bb thos laya fetch` does it. |
| `laya.maxDownloadMB` | 800 | Refuse a plan above this without confirmation. |
| `laya.gpu` | `auto` | `auto`, `always`, `never` (5.3). |
| `laya.rungs` | `mlx,gpu,cpu` | Allowed local rungs in order; remove one to ban it. |
| `laya.cacheDir` | platform default | 5.5. |
| `laya.parityTolerance` | 0.03 | Probe tolerance (5.3). |
| `laya.latencyBudgetMs` | 1000 | CPU p50 above this makes GPU eligible; GPU p50 above it is a failure. |
| `laya.startTimeoutMs` | 60000 | Server start bound. |
| `laya.timeoutMs` | 2500 | Per-request bound (inside the daemon's 3 s). |
| `laya.verifyOnStart` | false | Full hash on every start. |
| `laya.mlx.endpoint` | `http://127.0.0.1:11436` | Existing MLX service (6.3). |
| `laya.mlx.bootstrap` | false | Create a venv if absent (6.3). |
| `laya.manifestOverride` | unset | Path to a manifest for testing. |
| `laya.remote.role` | `off` | `off`, `fallback`, `primary`; any value other than `off` also requires the full `04` opt-in (URL, token file, redaction, questions, consent stamp). |

Secrets (remote token) stay in the plugin's `secrets/` files, 0600, as the plan already requires. Commands: `bb thos laya status | fetch | probe | verify | export | import | gc`.

### 5.8 Upgrade path

1. A plugin update changes the manifest. On the next activation, new blobs download in the background. The current set keeps serving.
2. The new set is probed on a second port. Promote only on a pass; write `current.json` with the old set as `previous`.
3. If the new set fails the probe, stay on the old set and print why (`bb thos doctor`). `bb plugin update` itself rolls back only for activation failure [plan], so this gate is separate and necessary.
4. Rollback: `bb thos laya use previous`. Blobs for `previous` stay; `gc` removes sets older than the previous one after 14 days.
5. A change of model sha or of the llama.cpp tag resets `calibration.q8_check`. Re-run the 8-state check and the regression set (section 9) before the threshold is trusted; `02` says the same ("rerun `trial_gguf.py` on every bump").
6. Disk: peak during an upgrade is two model copies, about 0.9 GB plus binaries.

### 5.9 Licence interaction

| Item | Treatment |
|---|---|
| Model weights (Apache-2.0) | We host the GGUF, so we redistribute. Carry LICENSE and the port's NOTICE text beside it, include Convai/Laya credit, mark "converted to GGUF, quantized to Q8_0" in the GGUF metadata and in the manifest. Convert from `convaiinnovations/laya`, not the third-party MLX port, to drop a link in the chain (`03` section 2). |
| llama.cpp (MIT) | Download upstream release assets; keep the copyright and permission notice. Extract the LICENSE file member if the archive carries it [unk], or ship the text in the plugin. |
| CUDA | Never fetch the `cudart` bundles or cuDNN, never host them. A CUDA rung needs the host's own CUDA runtime. If it is missing, the rung is unavailable and the chain goes to Vulkan. This follows `03` section 4 ("safest route: do not bundle CUDA"). |
| ROCm | The prebuilt ROCm tarball's contents are not assessed (`03` item 6). Do not enable auto-fetch for it until they are (S4). |
| Vulkan | Use the system loader; fetch only the llama-server binary. |
| Patents, teacher model | Open items 3, 4 and 10 in `03`. They are not blocked by this design and not closed by it. |

## 6. Special cases

### 6.1 Windows

Assets exist for CPU, Vulkan, CUDA 12.4, ROCm. The Windows CUDA asset is 12.4, not 12.8; its arch coverage is [unk]. Windows ROCm omits the Instinct targets (`02`). Treat the same selection logic; mark `win32-x64:cuda12.4` `verified: false`.

### 6.2 Unsupported platforms

Linux arm64, macOS x86_64, Windows arm64 have no listed asset. They go to rung 6 if opted in, otherwise the floor. No attempt to build from source at the user's machine.

### 6.3 Rung 1 on a Mac

Rung 1 is MLX, and MLX needs Python, a venv and 842 MB of F16 weights, which is a second install by any reading. Proposal:

1. If the existing laya-mlx service answers, adopt it as rung 1. It is the calibration reference and needs nothing new.
2. If not, the plugin does NOT bootstrap Python. It uses the macOS arm64 `llama-server` (12 MB, CPU or Metal) with the same GGUF. Whether the Metal build runs Laya correctly is [unk]: `02` ran `-DGGML_METAL=OFF`. So until a Metal parity run passes, macOS uses `-ngl 0`, which is the measured 198 ms path.
3. `laya.mlx.bootstrap` (default off) would create a hash-pinned venv with `uv` if present. It adds a Python dependency chain and 842 MB of weights; not recommended without a reason.

Cost of that choice: a fresh Mac gets GGUF scores, not MLX scores; the Q8_0 delta (0.028) applies, and the threshold 0.59 is not re-fitted (S6, S7).

## 7. Sign-off required from the owner

Recommended answer first. Items marked BLOCKING stop the implementation of the affected piece; the rest can be defaulted.

| # | Decision | Recommendation |
|---|---|---|
| S1 | Automatic download of about 0.47 to 0.7 GB on first activation, with no prompt? | Yes, default on, with `maxDownloadMB` and a visible doctor line. |
| S2 | BLOCKING (model hosting): who hosts the converted GGUF and where (GitHub release, Hugging Face), with LICENSE and NOTICE and a changed-notice. Close `03` items 3 and 4 (teacher/training-data licence; evidence of the upstream grant). | Host on a repo the owner controls; convert from upstream, not the MLX port; ask Convai about the teacher question. |
| S3 | BLOCKING for any CUDA bundling, not for this design: never fetch or host CUDA runtime or cuDNN. | Yes, never. Host-provided CUDA only; else Vulkan. |
| S4 | BLOCKING for ROCm auto-fetch: per-component licence review of the ROCm tarball. | Disable ROCm auto-fetch until done; Vulkan covers AMD meanwhile. |
| S5 | Ship rungs 2 to 4 enabled but unverified, or disabled until a run on real hardware (`02` section 6)? | Enabled only through `gpu=auto` after a passing probe, flagged unverified in the doctor line. Then run hardware parity and flip `verified`. |
| S6 | Artifact and tolerance: Q8_0 for every rung (one download), or F16 on GPU rungs (844 MB, delta 0.002)? Local probe tolerance 0.03, while `04` proposes 0.005 for a remote. Re-fit or re-check the 0.59 threshold on real inputs. | Q8_0 everywhere in v1; start retaining inputs for a regression set; revisit F16 if the band 0.562 to 0.590 holds real events. |
| S7 | Fresh Macs: GGUF on CPU (default) or MLX bootstrap? | GGUF on CPU; adopt MLX service when present. |
| S8 | Remote as rung 6: is a per-box `fallback` opt-in acceptable under `04` rule 4 and D1 to D12? Or primary only? | `fallback` allowed as an explicit setting; default off; `04` consent stamp applies. |
| S9 | Trust root: manifest pinned inside plugin releases (v1) vs a signed runtime-updatable manifest (v2, needs a key and its custody). | Pinned manifest now; signed manifest later. |
| S10 | Cache outside `~/.bb` (per-OS cache dirs). | Yes. |
| S11 | Unsupported platforms get remote or the floor; no source builds. | Yes. |
| S12 | The plugin starts local child processes (`llama-server`) on loopback with a random key, from files it downloaded. Under bb's full-trust model that is allowed by construction [plan]; is a download-then-execute behaviour acceptable, given pinned hashes? | Yes, with the hash gate in 5.5. |

## 8. Risks and unknowns (not closed by this document)

- No GPU rung has run. All GPU eligibility rules rest on release files, not evidence. The probe is the mitigation.
- Whether bb allows a plugin to spawn long-lived children and to write outside `~/.bb` is [unk] for the SDK: the plan shows `child_process.spawn` for short jobs and a data dir under `~/.bb`. Spike before building.
- Whether bb's server runtime allows a persistent child listening on loopback while a plugin reloads: [unk]. On reload the plugin must either adopt a running server (by pidfile and key) or stop it.
- Redirect hosts for GitHub release assets, the Metal build's correctness with Laya, `llama-server`'s licence file in the archives, CPU instruction-set requirements of the prebuilt CPU binaries (AVX2 or not): all [unk].
- Windows and Linux CPU latency: only an M4 Pro was timed. A weak x86 CPU may exceed the 1000 ms budget, in which case `gpu=auto` fetches a GPU runtime that is itself unverified.
- Q8_0 near 0.59: unproven (`02`). The threshold fit is on fp16 MLX.
- `llama-server` Laya support is days old (`02`). Pin the tag. Rerun `trial_gguf.py` on every bump.

## 9. What would close the main unknowns

1. Spike in the thos repo: a plugin that spawns a trivial child on loopback, survives `bb plugin reload`, and writes to a per-OS cache dir.
2. Run `02-runtime-eval-scripts/trial_gguf.py` once per rung: CUDA 12.8 on a Pascal/Maxwell card, ROCm on gfx1030 and a Vega card, Vulkan on RADV, ANV, NVK, Metal on this Mac. Record in the manifest `verified` field.
3. Build a regression set from real events with scores 0.50 to 0.70 and compare Q8_0 with MLX (needs retained inputs; `02` says it is unknown whether the decision log stores them).
4. Time Q8_0 on one low-end x86 CPU and one old ARM box to set `latencyBudgetMs` from data.
5. Resolve S2 to S4 before the first public release.
