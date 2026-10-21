# Laya backends: choosing one, tuning it, fixing it

Laya scores lane-3 events for the gate. It can run on several backends. This page tells you which one runs on your machine, how to change that, what it costs in memory and time, and what to do when it fails.

Numbers come from two reports in `reports/`:

- `latency-memory-2026-10-21.md` (memory and latency)
- `parity-2026-10-21.md` (score drift per backend)

All were measured on one machine: Apple M4 Pro, 48 GiB, macOS 26.2. Only the `mlx`, `cpu` and loopback `remote` rungs ran on real hardware. The GPU rungs (`cuda`, `rocm`, `legacy-gpu`, `vulkan`) are unverified: no GPU was available to measure them. For the full verification record see [laya-backends.md](laya-backends.md).

## 1. The fallback ladder

At startup the manager tries the rungs in this order and keeps the first one that loads. A rung that is missing, out of memory or broken is skipped and the next one is tried. It never goes back up.

| # | Rung | Auto picks it when | Notes |
|---|---|---|---|
| 1 | `mlx` | Apple Silicon and the `laya_mlx` package imports | Needs the `laya-mlx` venv interpreter. Under a bare `python3` this rung is skipped. |
| 2 | `cuda` / `rocm` | NVIDIA compute capability 7.5+ or a supported AMD GPU is found | Unverified on hardware. Needs a `llama-server` binary that you provide. |
| 3 | `legacy-gpu` | NVIDIA compute capability 5.0+ is found | Unverified. CUDA 12.8 build. |
| 4 | `vulkan` | A hardware Vulkan device on Linux or Windows | Unverified. Not used on macOS. |
| 5 | `cpu` | Always eligible; used when nothing above loaded | `llama-server` on CPU. Needs the model files in the local cache. |
| 6 | `remote` | Never by auto alone. You must configure and consent to it (section 4.3) | Sends redacted event text to a `laya serve` host. |

If nothing loads, Laya runs model-less: lanes 1 and 2 still work by rule, and lane 3 shows you the event instead of scoring it. An unreachable remote never drops an event.

## 2. Memory and latency per rung

Peak memory is the highest process RSS sampled after each call. Latency is per scoring call, one event each, 307 measured calls after 10 warmup calls, on the recorded parity cases.

| Rung | Quantization | Peak memory | p50 ms | p95 ms |
|---|---|---|---|---|
| mlx | n/a (fixed checkpoint) | 943 MiB | 29.565 | 36.256 |
| cpu | auto (loads Q8_0) | 850 MiB | 195.206 | 315.411 |
| cpu | none (F16) | 857 MiB | 597.419 | 828.804 |
| cpu | 8bit (Q8_0) | 850 MiB | 218.452 | 371.571 |
| cpu | 4bit (Q4_K_M) | 527 MiB | 342.067 | 528.16 |
| remote | server's choice | 892 MiB (server process tree) | 211.95 | 340.745 |
| rule gate (no model) | n/a | 31 MiB | 0.006 | 0.009 |
| cuda, rocm, legacy-gpu, vulkan | all | not measured | not measured | not measured |

Read these with care:

- `mlx` memory excludes Metal buffers, so the real figure is higher.
- `remote` was a loopback `laya serve --backend cpu`. It shows server and client overhead only, with no network time. A real link adds its own round trip.
- `cpu auto` and `cpu 8bit` load the same Q8_0 file. The gap between them is run-to-run noise on a shared host.
- F16 and Q8_0 use almost the same memory. Quantization changes speed far more than memory. Only Q4_K_M cuts memory.
- The host was busy during the run, so p95 includes scheduler noise. Re-run on an idle host before quoting these as release numbers: `~/.hermes/venvs/laya-mlx/bin/python scripts/bench_rungs.py`.

## 3. Weak machines

Pick by free RAM and by whether you accept more score drift.

- **Plenty of RAM, Apple Silicon:** leave everything on `auto`. `mlx` is the fastest rung by a wide margin.
- **No MLX, 1 GiB or more free:** use `cpu` with the default (`auto`, Q8_0). It is 850 MiB and about 195 ms p50.
- **Tight RAM (the CPU rung needs the file size x 1.25 free, plus context buffers):** set `LAYA_QUANT_LEVEL=4bit`. It takes 527 MiB, which is 323 MiB less than Q8_0, but it is slower (342 ms p50) and the code logs `laya cpu: laya-q4_k_m failed parity on the sample set; selected explicitly`. Q4_K_M parity was measured on one event only, so its drift is not characterised.
- **Do not pick F16 to save anything.** It uses about the same memory as Q8_0 and is three times slower at p50 (597 ms against 195 ms). Its only advantage is lower drift against the MLX reference (section 4.4).
- **Fewer cores:** set `LAYA_NUM_THREADS` to the number of physical cores. The default `0` lets llama.cpp choose. The benchmark ran with 10 threads.
- **Use the remote backend when** the machine cannot hold even the 4bit model, or when 4bit drift is not acceptable and you have a stronger host you trust. Remote sends redacted event text off the machine, so it is opt-in and needs your consent stamp. Do not use it to speed up a machine that already runs `cpu` Q8_0 locally unless the remote host is faster and on a quick link: the loopback figure above (212 ms p50) is a floor, not an expectation.
- **If nothing fits and remote is off,** Laya goes model-less. That is safe: lane 3 shows you the event instead of guessing.

## 4. Overrides

Order of precedence: environment variable, then `~/.hermes/config.yaml`, then default. The config file lives at `$HERMES_HOME/config.yaml` (default `~/.hermes/config.yaml`).

### 4.1 Which backend

| Env | Config key | Default | Valid values |
|---|---|---|---|
| `LAYA_BACKEND` | `laya.backend` | `auto` | `auto`, `mlx`, `cuda`, `rocm`, `legacy`, `vulkan`, `cpu`, `remote`, `stub`, `none` |

- A named value starts the ladder at that rung and still falls down from it. It does not skip the hardware check for rungs below it.
- `legacy` means the `legacy-gpu` rung.
- `stub` uses a fake scorer with no model. `none` runs model-less.
- An invalid value is logged, reported by `doctor`, and replaced by `auto`. It does not stop startup.

### 4.2 Weak-machine options

| Env | Config key (`laya.options.*`) | Default | Valid values |
|---|---|---|---|
| `LAYA_QUANT_LEVEL` | `quant_level` | `auto` | `auto`, `none` (F16), `8bit` (Q8_0), `4bit` (Q4_K_M). `auto` is F16 on cuda/rocm and Q8_0 on legacy-gpu, vulkan and cpu. |
| `LAYA_BATCH_SIZE` | `batch_size` | `8` | integer, 1 or more. Events per round; memory does not grow with it. |
| `LAYA_NUM_THREADS` | `num_threads` | `0` | integer, 0 or more. `0` lets the backend choose. |
| `LAYA_MAX_SEQ_LEN` | `max_seq_len` | `512` | integer, 1 or more. |

An invalid value is ignored with a warning and the default stays. Example config:

```yaml
laya:
  backend: auto
  options:
    quant_level: 8bit
    num_threads: 4
```

### 4.3 Remote backend

Remote is off unless every requirement holds. Any bad value leaves it off and records why. Only `https://` URLs work, except loopback, which may use `http://`. Inline tokens in config are refused. The full wire and privacy contract is in [laya-remote.md](laya-remote.md).

| Env | Config key (`laya.remote.*`) | Default | Meaning |
|---|---|---|---|
| `LAYA_GATE_BACKEND` | `enabled: true` | local | `remote` turns it on. |
| `LAYA_GATE_REMOTE_URL` | `url` | unset | Required. |
| `LAYA_GATE_REMOTE_TOKEN_FILE` | `token_file` | `~/.hermes/secrets/laya-remote.token` | Path to a file with mode 0600. Token form `laya_<id>_<secret>`. |
| `LAYA_GATE_REMOTE_PIN` | `pin` | unset | TLS key pin(s), comma separated. |
| `LAYA_GATE_REMOTE_REDACT` | `redact` | `R2` | `R1`, `R2` or `R3`. Higher removes more event text. |
| `LAYA_GATE_REMOTE_QUESTIONS` | `questions` | `decide` | Subset of `decide`, `respond`, `affirm`. |
| `LAYA_GATE_REMOTE_ACK` | `ack` | unset | Required consent stamp. Changing the host, pin, redaction level or questions voids it. |
| `LAYA_GATE_REMOTE_FALLBACK` | `fallback` | `local-then-standing` | What to do when the remote fails. |
| `LAYA_GATE_REMOTE_CONNECT_TIMEOUT` | `connect_timeout` | `0.5` s | Positive number. |
| `LAYA_GATE_REMOTE_TOTAL_TIMEOUT` | `total_timeout` | 1.5 s decide, 2.0 s respond and affirm | Positive number. Applies to all questions. |

### 4.4 Thresholds

There is one score threshold for every backend. There are no per-backend thresholds.

| Env | Default | Meaning |
|---|---|---|
| `LAYA_GATE_CHOICE_THRESHOLD` | `0.59` | A lane-3 escalation score at or above this escalates. |

This was a decision, not an omission. The parity run on 2026-10-21 measured how far each backend's scores drift from the MLX reference over 307 recorded cases:

| Backend | Mean abs drift | Max abs drift | Agreement with MLX | Flips (pass to hold) |
|---|---|---|---|---|
| mlx (reference) | 0 | 0 | 1.0000 | 0 (0) |
| remote (loopback, hosting MLX) | 0 | 0 | 1.0000 | 0 (0) |
| cpu F16 (`none`) | 0.0011 | 0.0049 | 0.9870 (4 flips) | 4 (2) |
| cpu Q8_0 (`auto`) | 0.0102 | 0.0309 | 0.9544 (14 flips) | 14 (1) |
| cuda, rocm, legacy-gpu, vulkan | not run | | | |

The proposed drift bound (`docs/laya-parity/DRIFT_BOUND.md`) is not approved. Both CPU precisions miss it. The same document forbids per-backend recalibration (a backend that needs another threshold fails), and the gate has no per-backend threshold mechanism. The best-fit values (F16 0.5882, Q8_0 0.5991) were reported as diagnostics only. So the CPU rung can flip a few borderline decisions relative to MLX. Expect that, and do not tune the threshold to hide it.

## 5. `laya doctor` and dry-run

`doctor` shows the hardware it found, the backend it chose, and why each rung was taken or skipped. Run it from the repository root. Use the venv interpreter to get `mlx`; the bare system `python3` cannot import `laya_mlx`, so it picks `cpu`.

```
~/.hermes/venvs/laya-mlx/bin/python scripts/laya.py doctor
```

Output (the model download progress bar and a checkpoint warning are printed to stderr first):

```
hardware:
  Apple Silicon: Apple M4 Pro, 14 cores, 48 GiB (Darwin/arm64)
  nvidia: absent (nvidia-smi not found on PATH)
  rocm: absent (rocm-smi not found on PATH)
  vulkan: absent (vulkaninfo not found on PATH)

backend: mlx   (loaded in 744 ms)

why:
  override: auto (source: default)
  rung mlx: loaded
```

With the bare system interpreter the same command falls to the CPU rung:

```
python3 scripts/laya.py doctor
```

```
backend: cpu   (loaded in 462 ms)

why:
  override: auto (source: default)
  rung mlx: skipped - unavailable: laya_mlx is not installed
  rung cuda/rocm: skipped - no NVIDIA or ROCm device (nvidia: nvidia-smi not found on PATH; rocm: rocm-smi not found on PATH)
  rung legacy-gpu: skipped - no GPU found (nvidia, rocm, vulkan all absent)
  rung vulkan: skipped - no Vulkan device (vulkaninfo not found on PATH)
  rung cpu: loaded
```

(The hardware block is the same as above and is left out here.)

Force a rung to see it load:

```
LAYA_BACKEND=cpu python3 scripts/laya.py doctor
```

```
backend: cpu   (loaded in 442 ms)

why:
  override: cpu (source: env)
  rung cpu: loaded
```

### Dry-run

`--dry-run` forces the stub backend. No model is loaded and nothing is downloaded, so it is instant and safe on any machine. Use it to check that the tool, your environment and the gate wiring work. It does not test the real ladder, and it ignores `LAYA_BACKEND`.

```
python3 scripts/laya.py doctor --dry-run
```

```
backend: stub   (loaded in 0 ms)   [dry-run: no real model loaded]

why:
  override: stub (source: --dry-run)
  rung stub: loaded
```

### JSON

`--json` prints the same report as JSON (`hardware`, `status`, `override_source`, `dry_run`). It combines with `--dry-run`:

```
python3 scripts/laya.py doctor --dry-run --json
```

### Exit codes

`doctor` exits 0 when it determined a backend or model-less mode, even if the result is model-less. It exits 1 only when the check itself broke.

## 6. Troubleshooting

### Out of memory

Symptom: the log says `laya backend <rung> out of memory ... falling down the ladder`, and `doctor` shows `failed - out of memory` for the rung. The ladder already moved to the next rung, so scoring continues. Real OOM was never observed on the GPU rungs; the walk was tested by injecting a failure at each rung's load. This is the output of the injected case, where `mlx` failed and the real ladder ran below it (`scripts/laya_e2e_chain.py --rows 20`):

```
active backend: cpu  load_ms: 519  override: auto (default)
  mlx          failed    out of memory: BackendOOMError: injected: MLX out of memory
  cuda/rocm    skipped   no NVIDIA or ROCm device (nvidia: nvidia-smi not found on PATH; rocm: rocm-smi not found on PATH)
  legacy-gpu   skipped   no GPU found (nvidia, rocm, vulkan all absent)
  vulkan       skipped   no Vulkan device (vulkaninfo not found on PATH)
  cpu          loaded
```

Fix: if it keeps happening on the same rung, free memory, or choose a smaller model with `LAYA_QUANT_LEVEL=4bit` (527 MiB), or set `LAYA_BACKEND` to a lower rung. If the CPU rung also fails, use the remote backend or accept model-less mode.

### Remote is unreachable

Symptom: `doctor` shows `rung remote: failed` and the backend is `none`. Reproduce it safely with a closed loopback port, a valid-format token file with mode 0600 and a placeholder consent stamp:

```
LAYA_BACKEND=remote LAYA_GATE_BACKEND=remote LAYA_GATE_REMOTE_URL=http://127.0.0.1:9 \
  LAYA_GATE_REMOTE_ACK=x LAYA_GATE_REMOTE_TOKEN_FILE=/path/to/token python3 scripts/laya.py doctor
```

```
backend: none (model-less mode: lanes 1 and 2 run by rule, lane 3 shows the event)

why:
  override: remote (source: env)
  rung remote: failed - remote http://127.0.0.1:9: connection refused
```

The reason text tells the cause. What you may see:

- `connection refused`: nothing is listening at that URL. Check the host, the port and that `laya serve` is running.
- `auth failed: token file is missing or unreadable` or `token file does not hold a valid token`: fix the path, the mode (0600) or the token form `laya_<id>_<secret>` (id 4 to 16 letters or digits, secret at least 32 characters).
- A failed remote never drops an event. After 3 failures in a row the client waits 60 s before it tries again.

### Invalid override

Symptom: `doctor` prints `override error:` and falls back to `auto`.

```
LAYA_BACKEND=gpu python3 scripts/laya.py doctor
```

```
backend: cpu   (loaded in 509 ms)

why:
  override: auto (source: env)
  override error: invalid laya backend 'gpu' from env; valid values: auto, mlx, cuda, rocm, legacy, vulkan, cpu, remote, stub, none. Using auto.
  rung mlx: skipped - unavailable: laya_mlx is not installed
  ...
  rung cpu: loaded
```

Fix: use one of the listed values. Do not test an override with `--dry-run`, which ignores it.

An invalid option is reported by a warning in the log, and the default is kept:

```
LAYA_QUANT_LEVEL=2bit LAYA_BACKEND=cpu python3 scripts/laya.py doctor
```

```
laya option quant_level from env ignored: '2bit' is not one of auto, none, 8bit, 4bit
```

An invalid remote setting leaves remote off and lists every problem:

```
LAYA_GATE_BACKEND=remote LAYA_GATE_REMOTE_URL=http://example.com LAYA_GATE_REMOTE_REDACT=R9 python3 scripts/laya.py doctor
```

```
laya: remote is configured but not enabled (url must be https unless it is loopback; redact level must be R1, R2 or R3; consent stamp (ack) is not set); staying local-only
```
