# Laya backends: ladder and verification report

> **NOT OWNER-APPROVED.** The runtime choice (pinned `llama-server`, GGUF artifacts, Q8_0 default) comes from `laya-runtime-decision.md`, which is a PROPOSAL. No approval is recorded. Everything here follows that proposal as written. If the owner changes an answer in section 9 of the memo, the artifacts and defaults change with it.
>
> **Only two rungs ran on real hardware: `mlx` and `cpu`, both on one machine (Apple M4 Pro, 48 GB, macOS 26.2).** Every GPU rung and the remote rung is marked UNVERIFIED below.
>
> **Neither CPU precision meets the drift bound.** The owner approved the bound as written (`laya-parity/DRIFT_BOUND.md`, commit 1d7335c): a backend outside it ships flagged. Both CPU precisions are therefore flagged (details under "Parity"). Only the runtime decision remains unapproved.

Code: `laya_backends.py`, `laya_gpu.py`, `laya_backend_manager.py`. Re-run the checks with `scripts/laya_e2e_chain.py` (see "Acceptance runs").

## Rungs

The manager tries rungs in this order and uses the first one that loads. An OOM, a failed load or an unavailable runtime moves it to the next rung. `LAYA_BACKEND=<rung>` starts at that rung and still falls down from it. `remote` is opt-in and never used without config.

| # | Rung | Backend | Artifact | Precision / quant | Minimum hardware generation | Verified on hardware |
|---|---|---|---|---|---|---|
| 1 | `mlx` | `laya_mlx` (existing service) | upstream safetensors, no GGUF | fp32 reference | Apple Silicon | **yes**, Apple M4 Pro (this Mac, "bc") |
| 2a | `cuda` | pinned `llama-server`, CUDA 13.4 build, `-ngl 99` | `laya-f16.gguf` (844 MB) | F16 default; Q8_0 via `8bit`; Q4_K_M via `4bit` | NVIDIA compute capability 7.5+ (Turing and later) | **NO. UNVERIFIED** (unit tests with faked tools only) |
| 2b | `rocm` | pinned `llama-server`, ROCm build | `laya-f16.gguf` | same as `cuda` | AMD gfx908/90a/942/950 and gfx1010+ (RDNA 1 and newer); GCN/Vega unsupported | **NO. UNVERIFIED** |
| 3 | `legacy-gpu` | pinned `llama-server`, CUDA 12.8 build (PTX sm_50+) | `laya-q8_0.gguf` (449 MB) | Q8_0 default; F16 via `none` | NVIDIA compute capability 5.0+ (Maxwell, Pascal, Volta); fast int8 needs 6.1 | **NO. UNVERIFIED** |
| 4 | `vulkan` | pinned `llama-server`, Vulkan build | `laya-q8_0.gguf` | Q8_0 default; F16 via `none` | any hardware Vulkan device on Linux or Windows; software renderers and macOS excluded. Mesa (RADV, ANV, NVK) correctness unknown | **NO. UNVERIFIED** |
| 5 | `cpu` | pinned `llama-server`, `-ngl 0` (llama.cpp commit 6868a52) | `laya-q8_0.gguf` | Q8_0 default; F16 via `none`; Q4_K_M via `4bit` | no GPU needed; free RAM >= 1.25 x file + 64 MiB | **yes**, Apple M4 Pro (Metal off, 10 threads). No x86 or low-core box tried |
| 6 | `remote` | HTTPS client to a `laya serve` host | none local | server's choice | none | **NO. UNVERIFIED** against a live server. Client tested only against a local mock HTTP server; `laya serve` tested over loopback |

GPU runtime binaries are never taken from `PATH`. They come from a per-rung env var or `<cache>/runtimes/<variant>/llama-server`. Fetching them is not built. So on a GPU box the rung stays unavailable until someone ships the binary.

## Weak-machine options and defaults

| Option | Env | Default | Effect |
|---|---|---|---|
| `quant_level` | `LAYA_QUANT_LEVEL` | `auto` | `auto`: F16 on cuda/rocm, Q8_0 on legacy-gpu, vulkan and cpu. `none`: F16. `8bit`: Q8_0. `4bit`: Q4_K_M, with a logged warning (it failed parity) |
| `num_threads` | `LAYA_NUM_THREADS` | `0` (llama.cpp decides) | `-t` and `-tb`. Measured on the CPU rung, Q8_0, 20 rows: 1 thread 19.3 s, 10 threads 6.1 s |
| `max_seq_len` | `LAYA_MAX_SEQ_LEN` | `512` | `-c`, `-b`, `-ub` |
| `batch_size` | `LAYA_BATCH_SIZE` | `8` | events sent per round. One server slot, so memory does not grow with it |

Q6_K exists as an artifact but no rung selects it. It failed parity like Q4_K_M. A machine that cannot run Q8_0 should take the floor (escalate) unless the owner accepts the drift.

## Load times measured

All on Apple M4 Pro, 48 GB, warm file cache, this session. One run each unless stated; not a benchmark.

| Rung / artifact | Load | Peak RSS | Source |
|---|---|---|---|
| `mlx` (via manager, `laya-mlx` venv) | 706 to 847 ms (two runs) | not measured | acceptance run 1 |
| `cpu` Q8_0 | 461 to 662 ms across several runs; 1.13 s once in an earlier run | 850 to 861 MiB | acceptance runs, CPU task |
| `cpu` F16 | 0.56 s | 860 MiB | CPU task |
| `cpu` Q4_K_M | 0.41 s | 516 MiB | CPU task |
| `cuda`, `rocm`, `legacy-gpu`, `vulkan` | NOT MEASURED | | no GPU on the build host |

CPU scoring, Q8_0, 307 rows: 77.4 s (about 4 texts/s). F16: 191 s (about 1.6 texts/s).

## Parity (CPU rung vs MLX reference, 307 fixtures `v1-mlxref`)

| Artifact | Max abs delta | Mean abs delta | Flips of 307 | Near-threshold agreement |
|---|---|---|---|---|
| Q8_0 (default) | 0.0309 | 0.0102 | 14 (1 pass to hold, 13 hold to pass) | 0.8586 |
| F16 | 0.0049 | 0.0011 | 4 | 0.9596 |
| Q4_K_M | one event only | | | |

The proposed bound is 3 flips and 0.96 near-threshold agreement. F16 misses by one flip and 0.0004. Q8_0 misses on every metric and is 0.0009 over the memo's own 0.03 delta limit. The bound is approved as written, so both are flagged; F16 on CPU is the closer option if the flag must clear.

## Acceptance runs

Command (the `laya-mlx` venv python is needed for criterion 1; the system `python3` has no `laya_mlx`):

```
~/.hermes/venvs/laya-mlx/bin/python3 scripts/laya_e2e_chain.py --rows 20
```

**1. No `LAYA_BACKEND`: auto-selection picks MLX.** Output:

```
--- 1. auto (LAYA_BACKEND unset)
active backend: mlx  load_ms: 847  override: auto (default)
  mlx          loaded
```

Caveat found while testing: under the bare system `python3`, `laya_mlx` is not importable, so the same command picks `cpu` (mlx "skipped: unavailable: laya_mlx is not installed"; cuda, legacy-gpu and vulkan skipped for no device). MLX selection depends on the interpreter. `laya_gate.py` re-execs into the venv for its `__main__`; any other entry point (for example `laya serve` run with a bare `python3`) does not.

**2. `LAYA_BACKEND=cpu` loads and scores the parity fixtures.**

```
LAYA_BACKEND=cpu python3 scripts/laya_cpu_parity.py
laya parity: backend=cpu fixtures=v1-mlxref (307 rows) sha=2cde16706ea1
drift:     mean|d|=0.0102  max|d|=0.0309  p95|d|=0.0217  pearson=0.9908  spearman=0.9873
agreement: 0.9544 at 0.59 overall; 0.8586 on near_threshold (99 rows)
flips:     14 (pass->hold 1, hold->pass 13)
timing:    77.422s, 4 texts/s
cpu run: {"artifact": "laya-q8_0", "sha256": "320c00b3...", "load_seconds": 0.559, "threads_effective": 10, "peak_rss_mib": 854}
```

The manager also reports `active backend: cpu  load_ms: 608  override: cpu (env)` with `LAYA_BACKEND=cpu`. It loads and scores all 307 rows. It does not pass the drift bound (above).

**3. Injected OOM falls through to the next rung and finally to CPU.** The hook is a rung subclass whose `load()` raises `BackendOOMError`, fed to `BackendManager(rungs=...)`. First, MLX and a GPU rung both OOM, hardware detection disabled so no rung is skipped, real CPU below:

```
--- 3. injected OOM on mlx and legacy-gpu, then real cpu
active backend: cpu  load_ms: 522  override: auto (default)
  mlx          failed    out of memory: BackendOOMError: injected: MLX out of memory
  legacy-gpu   failed    out of memory: BackendOOMError: injected: CUDA out of memory
  cpu          loaded
```

Then MLX alone OOMs, with the real ladder below it:

```
--- 3b. injected OOM on mlx only, real ladder below it
active backend: cpu  load_ms: 635  override: auto (default)
  mlx          failed    out of memory: BackendOOMError: injected: MLX out of memory
  cuda/rocm    skipped   no NVIDIA or ROCm device (nvidia: nvidia-smi not found on PATH; rocm: rocm-smi not found on PATH)
  legacy-gpu   skipped   no GPU found (nvidia, rocm, vulkan all absent)
  vulkan       skipped   no Vulkan device (vulkaninfo not found on PATH)
  cpu          loaded
```

The OOM here is injected at the manager's `load()` boundary. Real GPU OOM mapping (VRAM reading, `cudaMalloc failed` markers in the server log) is unit-tested with faked tools only. No real GPU OOM was observed.

## Test suite

Re-run after the failing tests were fixed at their causes (HEAD b66b284). The runner pins the interpreter to the hermes-agent venv (Python 3.11+), because `tests.test_intake` cannot import `hermes_cli` under the system python 3.9.

```
scripts/run_tests.sh
Ran 951 tests in 233.346s
OK (skipped=1)
```

Green. The one skip is pre-existing. The earlier 10 failures and 11 errors are fixed; the root cause of each is on card t_3b27166e. All backend suites are included (`test_laya_backends`, `test_laya_backend_contract`, `test_laya_backend_manager`, `test_laya_cpu_backend`, `test_laya_gpu_backends`, `test_laya_remote_backend`, `test_laya_serve`, `test_laya_hardware`).

Open flag from that card, not a backend matter: the 10-minute stopped-session bound is no longer enforced in code (`SILENCE_ALERT_AFTER` is unused in `laya_gate.py`). It needs an owner decision.

## Artifact hosting and cache

- Hosted copy: none. Nothing is uploaded. Hosting is blocked on the open teacher-model and training-data licence question (memo Q8), even though the weights are Apache-2.0. `remote` is `null` in `laya_artifacts.json`.
- Source: `convaiinnovations/laya` rev `7b928d82...`, converted locally by `scripts/laya_artifacts_build.sh` (reproducible hashes).
- Local cache, content addressed: macOS `~/Library/Caches/thos/laya/`, Linux `${XDG_CACHE_HOME:-~/.cache}/thos/laya/`, Windows `%LOCALAPPDATA%\thos\laya\`. `LAYA_CACHE` overrides. Layout: `blobs/<sha256>/<name>.gguf` plus `NOTICE`, `models/<name>.gguf` symlinks, `build/` scratch, `runtimes/<variant>/` for GPU server binaries (empty everywhere).
- Every load checks size and sha256 against the manifest. This Mac holds F16, Q8_0, Q6_K and Q4_K_M, 7.4 GB in total including the `build/` scratch.
- A new machine has no artifacts and nowhere to fetch them from. Until hosting is approved, the CPU rung works only where the build script was run or the cache was copied.

## Open items

- Owner approval of the runtime decision (pinned `llama-server`, GGUF, Q8_0) is still outstanding.
- Hardware runs needed to close each UNVERIFIED rung: Pascal or Maxwell card (legacy-gpu), Turing or newer (cuda), RDNA (rocm), RADV/ANV/NVK (vulkan), a live remote host, and a non-Apple CPU.
- Hosting/licence answer (memo Q8) before any machine other than this one can fetch artifacts.
- GPU runtime binary fetching is not built.
