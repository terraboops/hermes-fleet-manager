# Laya MLX baseline (fixtures v1)

The production MLX scoring path, run through `laya parity` on the frozen v1 fixtures
(307 rows, sha256 `30641fefdc9f...`).

Files in this directory:

- `baseline-mlx-v1.json`: full report, per-row scores under `rows`, environment under `meta.environment`.
- `baseline-mlx-v1.summary.txt`: text summary of the same run.
- `nondeterminism-mlx-v1.json`: per-row scores of all four runs, plus the two context-sensitivity variants.

Reproduce:

    ~/.hermes/venvs/laya-mlx/bin/python scripts/laya.py parity \
        --backend laya_parity_mlx:score_rows --out baseline-mlx-v1.json

## Backend

`laya_parity_mlx.py` is a thin adapter. It builds the same state string as `laya_gate.classify`
(DOCTRINE, Situation, Event, 24h count), calls `laya_mlx` `.predict(state, QUESTION)`, and returns
P("interrupt her now"). It reuses `laya_gate.DOCTRINE`, `QUESTION` and `MODEL`, so it cannot drift
from production wording. `score_rows` also takes the logged 24h count from each fixture row
(harness change: a backend with `wants_rows = True` receives the matching rows).

## Environment

- Hardware: Apple M4 Pro, 48 GB. macOS (Darwin 25.2.0, arm64).
- Interpreter: `~/.hermes/venvs/laya-mlx` Python 3.12.15 (the interpreter the gate service runs under).
- mlx 0.32.3, laya-mlx 0.2.0.
- Model: `aac6fef/laya-mlx`, checkpoint revision `20aed815fc6acde75733882e7ec0e3f28aeb9717`,
  float16.
- Weights: `model.safetensors` sha256 `b9c07bf14be2fa5c78a9193a3e6d840ac80e89e62fc40f425834c3d8a6eaa3de`
  (computed over the file, equal to the blob name). The checkpoint was cached on 2026-10-03,
  before the first fixture row (2026-10-11), so the weights did not change under the log.
- Load emits a laya-mlx warning: the checkpoint ships temperatures outside [0.5, 5] and the
  `choice:11+` bucket is clamped. Confidence from that bucket is uncalibrated. Same in production.

## Runtime

307 rows take 29.5 to 36.2 s per run (about 9 to 10 texts/s), plus about 2 s model load.
Well under the few-minute budget. A `--tags near_threshold` run (110 rows) would take about 11 s at that rate (estimated, not run), so a
fast mode is not needed yet. Batch size does not change the cost: the adapter calls `.predict`
once per row, because laya_mlx has no batch API and production also scores one event at a time.

## Headline metrics (run 1, batch 32)

- Decision agreement with the log at 0.59: 0.7199 overall, 0.8273 on near_threshold (110 rows).
- Score drift against the logged score: mean 0.0554, max 0.2768, p95 0.1603.
- Pearson -0.27, Spearman -0.23.
- Flips: 86 (pass to hold 7, hold to pass 79).
- Best-fit threshold 0.7923 gives 0.9772 agreement, 7 flips remaining.

This is not the "roughly 100%" the task expected. The cause is below. It is a limit of the v1
fixtures, not a model or adapter fault.

## Nondeterminism

Four fresh-process runs, batch sizes 32, 32, 1 and 7.

- Run-to-run score delta: max 0.0000, mean 0.0000, over all 6 run pairs and 307 rows.
- Decision flips between runs: 0.
- laya_mlx rounds probabilities to 4 decimals, so the resolution of this test is 1e-4. Any
  float noise below that is not visible. Fixture scores are also at 4 decimals.
- Batch size had no effect, as expected from the per-row call pattern. A real batched backend
  (CUDA, GGUF) must repeat this check; this one does not show batch or padding effects.
- Row order was not varied.

## Why agreement is not 100%

Production scored this state: DOCTRINE, a `Situation:` line, the Event, the 24h count.
The log keeps only the Event string and the state length. The `Situation` line is built by
`fleet_watch.event_context` from the session registry entry, the last two lines of the session's
output (up to 280 chars) and the stall age. It is not logged and cannot be rebuilt.
Fixture `state_chars` confirms it: the Situation text varies from 223 to 315 chars across rows.

Evidence that missing context explains it, from the same checkpoint (scores in
`nondeterminism-mlx-v1.json`):

| Input | Agreement | Mean abs delta | Max delta | Mean signed delta |
|---|---|---|---|---|
| Event + 24h count, default Situation (baseline) | 0.7199 | 0.0554 | 0.2768 | +0.023 |
| Event with no 24h count, default Situation | 0.4951 | 0.1002 | 0.3132 | n/a |
| Event + 24h count + synthetic Situation skeleton | 0.9772 | 0.0548 | 0.1473 | -0.048 |

- The 24h count is a strong input: dropping it moves agreement from 0.72 to 0.50.
- A synthetic Situation (session name, no output, quiet for N seconds) restores 0.977 agreement
  with only 7 flips. Those 7 are the 7 rows the log passed, scored 0.5912 to 0.6346, all within
  0.05 of the threshold. The skeleton does not carry the session's last output, so these stay
  below the line.
- The other causes were checked and ruled out:
  - Weights changed: no, the weights predate the log.
  - Preprocessing: the adapter reuses the gate's own strings.
  - Batching or padding: zero deltas across batch sizes.
  - Float precision: zero run-to-run delta at 4 decimals.
  - Rows inconsistent in the source: the fixture builder found 0.
- Rows with the same text and different logged scores (the same stall line seen at different
  times) cannot be told apart without the Situation. This is the same gap.

## How to use this baseline

- Compare a new backend to the MLX scores on the same harness inputs
  (`rows[].score` in `baseline-mlx-v1.json`), not to the logged scores. That isolates backend
  drift from the missing context. The harness's own drift metrics compare to the log, so read them
  as relative to this baseline's 0.72 / 0.0554 / 0.2768, not as absolute.
- To get true parity against the log, v2 fixtures need the full scorer input logged. The log
  change is `laya_gate.classify` writing the Situation text (or its hash and text) next to
  `state_chars`. Then the expected agreement is about 100% by construction, since this run shows
  the model is deterministic.
