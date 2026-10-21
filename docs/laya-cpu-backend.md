# Laya CPU backend

Rung `cpu`: the last local rung (the opt-in `remote` rung follows it). A pinned `llama-server` runs on
loopback with `-ngl 0` and serves one GGUF from the artifact cache. Select with `LAYA_BACKEND=cpu`.

## Options

| Option (env) | Effect on the CPU rung |
|---|---|
| `quant_level` (`LAYA_QUANT_LEVEL`) | `auto`/`8bit` -> `laya-q8_0`, `none` -> `laya-f16`, `4bit` -> `laya-q4_k_m`. The file's sha256 and size are checked against `laya_artifacts.json` before the server starts. `4bit` logs a warning: it failed parity on the sample set. |
| `num_threads` (`LAYA_NUM_THREADS`) | `-t` and `-tb`. 0 leaves the choice to llama.cpp. The thread count the server reports is in the load detail and `threads_effective`. |
| `max_seq_len` (`LAYA_MAX_SEQ_LEN`) | `-c`, `-b`, `-ub`. |
| `batch_size` (`LAYA_BATCH_SIZE`) | Events sent to the server per round; one slot, so memory does not grow with it. |

Binary lookup: `LAYA_LLAMA_SERVER`, then `<cache>/build/llama.cpp/build/bin/llama-server`, then `PATH`.

## Contract

- `probe()`: binary found, artifact cached, free RAM at least 1.25x the file plus 64 MiB. Starts nothing.
- `load()`: low RAM or a server that dies with an allocation failure -> `BackendOOMError`; checksum or
  size mismatch, missing file, any other server failure -> `BackendLoadError`; no binary -> `BackendUnavailable`.
  Returns `LoadResult(load_seconds, detail)`.
- `score()`: criteria lists are sent as objects; an answer without probabilities is a `BackendRuntimeError`.

## Parity run

    LAYA_BACKEND=cpu [LAYA_QUANT_LEVEL=none] [LAYA_NUM_THREADS=2] python3 scripts/laya_cpu_parity.py \
        [--proposed-bound] [--limit N] [--out report.json]

Fixtures: `tests/fixtures/laya_parity/v1-mlxref` (307 rows, MLX scores on the harness input). Live test:
`tests/test_laya_cpu_parity_live.py` (skipped when the artifacts are not cached).

## Measured on Apple M4 Pro (10 threads), 307 rows, against the proposed drift bound

| Artifact | Max abs delta | Mean abs delta | Flips of 307 | Near-threshold agreement | Load | Peak RSS |
|---|---|---|---|---|---|---|
| laya-f16 | 0.0049 | 0.0011 | 4 (bound: 3) | 0.9596 (bound: 0.96) | 0.56 s | 860 MiB |
| laya-q8_0 | 0.0309 | 0.0102 | 14 | 0.8586 | 1.13 s | 861 MiB |

Neither passes the proposed bound. F16 misses it by one flip (and by 0.0004 on the near band); Q8_0 fails
every metric. Both stay within the decision memo's 8-state criterion (Q8_0 <= 0.03 is borderline: 0.0309 here).
Q4_K_M: load 0.41 s, 516 MiB, not run through the full set.

Threads, 20 rows of Q8_0: 1 thread 19.3 s, 10 threads 6.1 s. F16 full set at 10 threads: 191 s (0.62 s per text).
