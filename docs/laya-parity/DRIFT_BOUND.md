# Laya backend drift bound

**Status: PROPOSED - pending owner approval.**
Approval: NOT GRANTED. Approver: owner. Date approved: none. Nothing here is in force until this line is changed to APPROVED with a date.

## Summary for sign-off

A new Laya scoring backend counts as "not materially different from MLX" if, on the 307 frozen fixture rows, scored against the MLX scores, with the threshold fixed at 0.59:

- at most 3 decision flips (agreement >= 0.990), at most 1 of them pass to hold (an escalation the backend would swallow);
- no score differs from MLX by more than 0.005, and the mean difference is at most 0.002;
- rank correlation with MLX is at least 0.995.

No recalibration of the threshold is allowed as part of acceptance. A backend that needs a different threshold fails.

What it means in practice: 3 flips out of 307 is 1.0%. On this data 79 rows pass and 228 hold. Nothing in this proposal needs a new model run: MLX is bit-identical across runs, so the bound is set by decision impact, not by MLX noise.

Decision needed: approve, or name different numbers. The effect of each number is in the table below.

## Why the bound is measured against MLX scores, not the log

The `v1` fixtures hold the logged scores. The log lacks the Situation text the gate scored, so MLX itself agrees with the log only 72% (max delta 0.2768). See `BASELINE.md`. A bound set against the log would have to admit that gap, and a bound that loose accepts nearly any backend.

The bound is therefore measured on `tests/fixtures/laya_parity/v1-mlxref/`: the same 307 rows and texts, with `mlx_score` replaced by the MLX baseline score on the harness input (`baseline-mlx-v1.json`, `rows[].score`). Decisions and the near-threshold tag are recomputed from that score (near = within 0.05 of 0.59, 99 rows). Rebuild or check with `scripts/build_laya_mlx_reference_fixtures.py [--check]`. The MLX path itself scores 100% / delta 0 on this set by construction.

Limit: the set is only as good as v1. Every row is a STALL event from 5 sessions, and the input lacks the Situation line. It measures backend drift on that input. It does not measure agreement with real production traffic. Cut v2 fixtures once the gate logs the full scorer input.

## The bound

Threshold: fixed 0.59, rule `score >= 0.59`. Recalibration cap: 0 (none). The report's `best fit` threshold is a diagnostic only. If it sits further than 0.005 from 0.59, flag the backend for review even when the bound passes.

| Metric | Bound | Enforced by |
|---|---|---|
| Decision agreement, overall | >= 0.990 (<= 3 flips of 307) | `--min-agreement 0.990` |
| Decision agreement, near-threshold band | >= 0.96 (<= 3 flips of 99) | `--min-near-agreement 0.96` |
| Max absolute score delta | <= 0.005 | `--max-delta 0.005` |
| Mean absolute score delta | <= 0.002 | `--max-mean-delta 0.002` |
| Spearman vs MLX | >= 0.995 | JSON check (no flag) |
| Pass-to-hold flips (swallowed escalations) | <= 1 | JSON check (no flag) |
| Mean signed delta | within +/-0.001 | JSON check, advisory |

Agreement arithmetic on 307 rows: 3 flips is 304/307 = 0.9902 (passes), 4 flips is 0.9870 (fails). On the 99-row band: 3 flips is 0.9697 (passes 0.96), 4 flips is 0.9596 (fails).

## Rationale from the data

MLX noise. Four fresh-process runs, batch sizes 32/32/1/7: max run-to-run delta 0.0000 at 4 dp, zero flips (`BASELINE.md`). Scores are rounded to 1e-4, so the measurable floor is 1e-4. "Comfortably above noise" therefore means well above that resolution: 0.005 is 50x it, and the mean bound 0.002 is 20x it. A bound at or near 1e-4 would fail a correct backend over rounding.

Threshold density. MLX scores on this input, rows within a distance of 0.59:

| Within | Rows | Below (hold) | Above (pass) |
|---|---|---|---|
| 0.002 | 6 | 3 | 3 |
| 0.003 | 8 | 4 | 4 |
| 0.005 | 13 | 7 | 6 |
| 0.010 | 16 | 8 | 8 |
| 0.028 | 60 | 27 | 33 |

A backend whose scores all shift by d flips roughly the rows within d on one side. Shift 0.005 flips 6 or 7 rows (about 2%). Shift 0.028 flips 27 to 33 rows (about 10%).

Random noise. Simulation: Gaussian noise on the MLX scores, 2,000 trials each:

| Noise sigma | Mean flips | 95th pct | 99th pct | Mean max delta | Mean mean delta |
|---|---|---|---|---|---|
| 0.001 | 1.1 | 3 | 3 | 0.0031 | 0.0008 |
| 0.003 | 3.0 | 6 | 7 | 0.0093 | 0.0024 |
| 0.005 | 4.4 | 7 | 9 | 0.0155 | 0.0040 |
| 0.010 | 8.0 | 12 | 14 | 0.0309 | 0.0080 |

The bound admits noise up to about sigma 0.001, which is 10x the rounding floor, and rejects sigma 0.003 and above (the 95th percentile is 6 flips, and max delta passes 0.005). That is the intent: pass float-level differences, fail quantization-level ones.

Prior evidence from `docs/laya-research/02-runtime-eval.md` (8 synthetic states, MLX fp32 as reference, so indicative only):

- F16: max delta 0.002. Passes.
- Q8_0: max delta 0.028. Fails the max-delta bound. By the table above it would flip about 10% of rows under a uniform shift.
- int8 ONNX dynamic: 0.074. Fails.
- Q4_K_M: 0.109 and a flipped decision. Fails.

Also consistent with the 0.005 proposed in `04-remote-design.md` (D6) for a remote backend.

Why 3 flips, not 0. Zero flips would fail any backend with an honest 1e-3 difference, because 6 rows sit within 0.002 of the line. 1% is the largest figure I can defend as "not material" for a lane that gates interrupts, and it is the example figure from the task. If the owner wants a stricter line, 2 flips (>= 0.9935) is still reachable by float-level backends. If looser, 0.98 (<= 6 flips) already admits sigma 0.003 noise.

## Risks

- Direction. A pass-to-hold flip swallows an escalation she would have seen. A hold-to-pass flip is one extra interruption. Swallowed events cost about ten times an unnecessary page, so a false block counts more than a false pass. The proposal gives pass-to-hold a cap of 1, hold-to-pass the remaining 2. The baseline log-vs-MLX gap is lopsided too: 79 hold-to-pass versus 7 pass-to-hold, which is a one-way bias from missing context, not a backend effect.
- Direction cannot be enforced by a harness flag today. Use the JSON check below until a `--max-pass-to-hold` flag exists.
- Concentration. Every possible flip is in the 13 rows within 0.005 of the threshold, and these are STALL events from 5 sessions. A backend could pass on this set and still differ on other event families. The set cannot show this.
- Batch and padding effects. MLX scores one row per call, so the zero-noise result does not cover batched backends (CUDA, GGUF, ONNX). A candidate must repeat the batch-size check at several sizes and row orders before its numbers count.
- Resolution. Scores are 4 dp. A backend within 0.0001 of the line is noise, and `>=` versus `>` is untestable on this data (no row equals 0.59).
- Max-delta alone is weak: 0.005 still allows up to 13 flips. The agreement bound is what holds the line, so keep both.
- Tail risk: Spearman has no CLI flag, and with ties at 4 dp it is a coarse check. It guards ordering, not the threshold.

## Enforcement

Build the reference set once (already committed):

    python3 scripts/build_laya_mlx_reference_fixtures.py --check

Run the bound for backend X (flagged bounds exit 2 on violation; the second command checks the three metrics without a flag):

    python3 scripts/laya.py parity --backend X \
        --fixtures tests/fixtures/laya_parity/v1-mlxref \
        --min-agreement 0.990 --min-near-agreement 0.96 \
        --max-delta 0.005 --max-mean-delta 0.002 \
        --out report-X.json

    python3 - report-X.json <<'PY'
    import json, sys
    m = json.load(open(sys.argv[1]))["metrics"]
    rho = m["score_drift"]["spearman"]
    p2h = m["flips"]["by_direction"]["pass_to_hold"]
    ok = rho >= 0.995 and p2h <= 1
    print("spearman %.4f pass_to_hold %d -> %s" % (rho, p2h, "PASS" if ok else "FAIL"))
    sys.exit(0 if ok else 1)
    PY

Both commands must exit 0. Run `X` under the interpreter the backend needs (the MLX path uses `~/.hermes/venvs/laya-mlx/bin/python`).

Checked on 2026-10-21: the `replay` backend (identity with MLX) passes every flagged bound with 0 flips; a Gaussian-noise stand-in with sigma 0.002 fails `--max-delta 0.005` (max delta 0.0058) while passing agreement, as the table predicts. No real second backend has been measured yet.

## Follow-ups (not part of this approval)

- Add `--max-pass-to-hold` and `--min-spearman` flags and a `--reference` option to `laya parity`, so no separate JSON check or derived fixture directory is needed.
- Log the full scorer input in `laya_gate.classify`, then cut v2 fixtures and re-derive these bounds against real traffic and more event families.

## Approval record

| Field | Value |
|---|---|
| Status | PROPOSED - pending owner approval |
| Approved by | (none) |
| Approved on | (none) |
| Changes requested | (none) |
