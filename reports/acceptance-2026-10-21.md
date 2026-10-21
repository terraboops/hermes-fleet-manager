# Laya multi-backend runtime: acceptance checks, 2026-10-21

Host: Apple M4 Pro, 48 GB, macOS 26.2 ("bc"). Repo at `73acde4` on `main` (parity report `c02d224` on branch `parity-report-2026-10-21`, not merged). Interpreter for the gate: `~/.hermes/venvs/laya-mlx/bin/python3`. Fixtures: `tests/fixtures/laya_parity/v1-mlxref` (307 rows, sha 2cde16706ea1).

## Result

| # | Check | Result |
|---|---|---|
| 1 | Auto mode selects MLX; recorded fixtures unchanged | PASS |
| 2 | CPU backend within the drift bound | **FAIL** (both precisions) |
| 3 | Kill model process mid-run: fallback and /health | PASS |
| 4 | Remote backend against a second local instance | PASS |
| 5 | All model backends disabled: rule escalation and events | PASS |
| 6a | Invalid override value | PASS (observations below) |
| 6b | Unreachable remote | PASS |
| 6c | OOM on model load | PASS (simulated by an oversized context, see note) |

No fixes were made. The one FAIL (2) has no obvious code cause: it is the measured drift of the quantized models against a bound that is itself unapproved. Nothing was committed except this report.

## Prerequisite state

The parity task added no per-backend threshold and no config change (`t_61bf6e00`): `docs/laya-parity/DRIFT_BOUND.md` is PROPOSED with approval NOT GRANTED, it caps recalibration at 0, and the gate has one global threshold (0.59). So "the stated drift bound" below is the PROPOSED bound. Every run used the global threshold, unchanged.

## Method

Each gate check starts a real `laya_gate.py` on its own port, with `HERMES_HOME` pointing at a scratch directory holding a one-off `config.yaml`, and the held, labels, decisions and registry files redirected to scratch. The live gate (port 11436), the real `~/.hermes/config.yaml` and the live decision log were not touched. Events go in through `POST /decide`; state is read from `GET /health`. Lane 3 test event: `STALL-CC-ACC-1: no new transcript output for 360s (alive, not producing)` with `expecting: true`. Raw logs were kept in scratch, not committed.

## 1. Auto mode selects MLX; fixtures unchanged: PASS

Gate, empty `laya:` config, `LAYA_BACKEND` unset:

```
/health: backend=mlx active_label=mlx load_ms=745 model_loaded=true model_less=false
         override=auto (source default)  skipped=[]
         ladder=[mlx, cuda/rocm, legacy-gpu, vulkan, cpu, remote]
         fallback_chain=[mlx loaded]
```

Fixture replay through the MLX scorer:

```
env -u LAYA_BACKEND ~/.hermes/venvs/laya-mlx/bin/python3 scripts/laya.py parity \
  --backend laya_parity_mlx:score_rows --fixtures tests/fixtures/laya_parity/v1-mlxref
drift:     mean|d|=0.0000  max|d|=0.0000  pearson=1.0000  spearman=1.0000
agreement: 1.0000 at 0.59 overall; 1.0000 on near_threshold (99 rows)
flips:     0 (pass->hold 0, hold->pass 0)
```

Zero drift and zero flips against the recorded MLX scores on all 307 rows. Lane check on the same gate: a traceback event gave lane 1 escalate (`crash in the session output`), a delivery receipt gave lane 2 silent, the STALL event gave lane 3 confidence 0.5896, no escalation. `tests.test_laya_parity`: 23 tests OK.

Caveat (already documented in `docs/laya-backends.md`): selection depends on the interpreter. Under a bare `python3` without `laya_mlx`, auto picks `cpu`. `laya_gate.py` re-execs into the venv; other entry points do not.

## 2. CPU backend within the drift bound: FAIL

Bound: PROPOSED (agreement >= 0.990, near-threshold >= 0.96, max delta <= 0.005, mean delta <= 0.002). No recalibration.

```
LAYA_BACKEND=cpu LAYA_QUANT_LEVEL=auto python3 scripts/laya_cpu_parity.py --proposed-bound   # Q8_0, default
drift:     mean|d|=0.0102  max|d|=0.0309  spearman=0.9873
agreement: 0.9544 overall; 0.8586 near_threshold    flips: 14 (pass->hold 1, hold->pass 13)
bounds:    FAIL: agreement 0.954397 < 0.99; near 0.858586 < 0.96; max 0.0309036 > 0.005; mean 0.0101742 > 0.002
exit 1

LAYA_BACKEND=cpu LAYA_QUANT_LEVEL=none python3 scripts/laya_cpu_parity.py --proposed-bound   # F16
drift:     mean|d|=0.0011  max|d|=0.0049  spearman=0.9993
agreement: 0.9870 overall; 0.9596 near_threshold    flips: 4 (pass->hold 2, hold->pass 2)
bounds:    FAIL: agreement 0.986971 < 0.99; near_threshold 0.959596 < 0.96
exit 1
```

Identical to the numbers in `reports/parity-2026-10-21.md`. F16 misses by one flip (4 against 3) and by 0.0004 on the near band; it also has 2 pass-to-hold flips against a limit of 1. Q8_0 fails every metric. The CPU backend itself works (loads, scores all 307 rows).

Why it is not fixed here: the cause is quantization, not a defect. Closing the gap needs an owner decision (approve F16 on CPU as a near-miss, loosen the bound, or build and approve per-backend thresholds, which the proposed bound forbids). Left FAIL.

## 3. Kill the model process mid-run: PASS

Config `laya: {ladder: [cpu, mlx]}`. The CPU rung runs `llama-server` as a child of the gate.

```
before: backend=cpu load_ms=2497 fallback_chain=[cpu loaded] skipped=[]
        /decide -> lane 3, confidence 0.6103 (cpu, Q8_0)
kill -9 <llama-server child pid>
/decide (new session) -> lane 3, confidence 0.5489, 819 ms   (first call after the kill; answered)
after:  backend=mlx active_label=mlx load_ms=762
        skipped=[{rung: cpu, status: demoted,
                  reason: "score failed: BackendRuntimeError: cpu backend scored without a running server (call load())"}]
        fallback_chain=[cpu loaded, cpu demoted (same reason), mlx loaded]
next /decide -> lane 3, confidence 0.5574, 43 ms (served by mlx)
gate log: laya backend cpu demoted: score failed: BackendRuntimeError: ...
```

The request that hit the dead server was answered by the next rung with no error to the caller. `/health` names the active rung and carries the demotion event. Observation: the event has no timestamp, so /health says that a fallback happened, not when.

## 4. Remote backend against a second local instance: PASS

Second instance: `scripts/laya.py serve --backend mlx --port 12504` (token via `LAYA_SERVE_TOKEN`). Gate config: `ladder: [remote, mlx]`, `remote: {enabled: true, url: http://127.0.0.1:12504, token_file: <0600 file>, ack: dev}`.

```
server /health (bearer): {"status":"ok","backend":"mlx","version":"0.1.0","protocol":1,"model_loaded":true,"fallback_chain":["mlx:loaded"]}
gate /health: backend=remote active_label="remote http://127.0.0.1:12504" model_less=false
              fallback_chain=[remote loaded]  ladder=[remote http://127.0.0.1:12504, mlx]
gate /decide -> lane 3, confidence 0.5896   (same value as local MLX in check 1)
server log:    POST /v1/score status=200 ms=52
server stopped, then /decide (new session) -> answered (0.5489) by mlx
gate /health: backend=mlx, skipped=[{rung: remote, status: demoted,
              reason: "remote http://127.0.0.1:12504: connection refused", retry_in_s: 58}]
```

Scoring went over the wire (server log line), the score equals the local one, and the loss of the server dropped the gate to the next rung with a clear reason. Automated counterpart: `tests.test_laya_remote_e2e` (15 tests) passed in this run.

Observation: one `/healthz` poll during server start returned `ok: false, model_loaded: true`. `ok` waits on the load-complete event, `model_loaded` on the backend name; the two flip at slightly different moments. Harmless, but the pair can contradict for a moment.

## 5. All model backends disabled: PASS

Two ways to get a model-less gate: `LAYA_BACKEND=none`, and `ladder: [remote]` with remote not enabled. Both gave the same behavior.

```
/health: ok=true model_loaded=false model_less=true backend=null
         model_error="none skipped: override 'none' from env | interpreter=... "
         (second case: "remote skipped: remote is not enabled (local-only) | ...")
POST /decide traceback event      -> lane 1 escalate=true  rule "crash in the session output"
POST /decide SESSION-DEAD event   -> lane 1 escalate=true  rule "session is dead"
POST /decide ACK-OK receipt       -> lane 2 escalate=false rule "delivery receipt"
POST /decide STALL (lane 3)       -> lane 3 escalate=true  rule "classifier unavailable (BackendLoadError)"   (fails open: shown, not swallowed)
POST /respond IDLE                -> "nudge it with one concrete next increment", standing policy
POST /affirm "rotate the production API key" -> affirm=false, risk 1.0, lane 1 rule "credentials"
decisions.jsonl: 2 lines written (the lane 3 and respond decisions)
```

Lanes 1 and 2 decide by rule with no model. Lane 3 shows the event rather than dropping it. The reason is visible in both the verdict and `/health`. Note that `ok` stays true while model-less; consumers must read `model_less` or `model_error`.

## 6. Negative cases

### 6a. Invalid override value: PASS

| Input | Behavior |
|---|---|
| `LAYA_BACKEND=cpuu` | Falls to auto (mlx). `/health` `backend_status.override_error`: `invalid laya backend 'cpuu' from env; valid values: auto, mlx, cuda, rocm, legacy, vulkan, cpu, remote, stub, none. Using auto.` Gate log at ERROR level. |
| `laya.backend: gpu9000` in config | Same, source `config`. |
| `LAYA_BACKEND='REMOTE;rm'` with valid `cpu` in config | Env wins, rejected, auto. Nothing executed. |
| `LAYA_QUANT_LEVEL=3bit` | Ignored, default used. Log: `laya option quant_level from env ignored: '3bit' is not one of auto, none, 8bit, 4bit`. |
| `LAYA_MAX_SEQ_LEN=abc` | Ignored, default 512. Log: `laya option max_seq_len from env ignored: invalid literal for int()...` |
| `ladder: [teleport, cpu]` | Bad entry dropped, ladder is `[cpu]`. Log: `laya.ladder: unknown ladder entry 'teleport'`. |

The gate stayed up and answered lane 1 and lane 3 events in every case. Observations, not failures: (a) the override error is nested under `backend_status`, not a top-level field; (b) the two rejected option values appear in the gate log only, not in `/health`; (c) falling back to auto on a typo means a user who meant `cpu` silently gets MLX unless they read the error. A stricter design would refuse to start. Not changed: the documented contract is "Using auto".

### 6b. Unreachable remote (nothing listening on 127.0.0.1:12599): PASS

| Setup | Behavior |
|---|---|
| `ladder: [remote, mlx]` | `fallback_chain=[remote failed "remote http://127.0.0.1:12599: connection refused", mlx loaded]`, `retry_in_s: 58`. Serves from mlx (0.5896). |
| `ladder: [remote]` | Model-less. `model_error: remote failed: remote http://127.0.0.1:12599: connection refused | ...`. Lane 1 escalates by rule; lane 3 escalates with `classifier unavailable (BackendLoadError)`. |
| `LAYA_BACKEND=remote`, default ladder | The ladder below remote is empty (remote is last), so model-less with the same reason; same safe behavior. |
| remote enabled without `ack` | Not used. Notice in `/health`: `remote is configured but not enabled (consent stamp (ack) is not set); staying local-only`. Serves from mlx. No socket opened. |

No crash, no hang beyond the timeout, no silent misroute: every case names the cause and the host and port only, never the token. Note: forcing `LAYA_BACKEND=remote` gives no local safety net by design.

### 6c. OOM on model load: PASS (simulated)

A memory limit is not reliable on macOS (`ulimit -v` is not enforced), so the OOM was forced with a context that cannot be allocated: `LAYA_MAX_SEQ_LEN=200000000 LAYA_QUANT_LEVEL=none`, CPU rung. The model files are not oversized; the failure is `llama-server` failing to create its context.

```
ladder [cpu, mlx]: skipped=[{rung: cpu, status: failed,
    reason: "out of memory: BackendOOMError: llama-server died loading the model (exit 1): ... E srv load_model: failed to create_context with model '.../laya-f16.gguf' ..."}]
                   backend=mlx; /decide -> lane 3, 0.5896
ladder [cpu] only: model_less=true, model_error carries the same OOM text;
                   /decide lane 3 -> escalate=true "classifier unavailable (BackendLoadError)"; lane 1 rule decisions unaffected;
                   gate process alive throughout
```

The error is labelled out of memory, the next rung takes over, and an empty ladder fails open. Limits of this evidence: the failure is a context allocation, not a weight load; real GPU OOM mapping is covered by unit tests with faked tools only (see `docs/laya-backends.md`).

## Regression tests run

```
~/.hermes/venvs/laya-mlx/bin/python3 -m unittest tests.test_laya_backend_manager tests.test_laya_gate_backend \
  tests.test_laya_remote_ladder tests.test_laya_remote_e2e tests.test_laya_serve tests.test_laya_health_doctor
Ran 99 tests ... OK
```

## Open items

- Orphaned model servers: stopping a gate with SIGTERM leaves its `llama-server` child running (parent pid 1). Three were left behind by this run's scratch gates and were killed by hand. `laya_gate.py` installs no signal handler or exit hook that calls the backend `close()`, so a gate restart on the CPU rung leaks one server per restart. Cause is clear (no shutdown path in the gate's `__main__`); not fixed here because `laya_gate.py` carries uncommitted edits from other work and a fix with its test belongs in its own change. Needs a SIGTERM handler that closes the manager, plus a test that the child is gone after the gate stops.
- Check 2 stays FAIL until the owner approves a bound or a CPU precision (see `reports/parity-2026-10-21.md`).
- `/health` has no timestamp on fallback events.
- Rejected `LAYA_QUANT_LEVEL` / `LAYA_MAX_SEQ_LEN` values are logged but not shown in `/health`.
- Transient `ok` / `model_loaded` mismatch on the server's `/healthz` during start.
- Not verified here: any GPU rung, any non-Apple machine, a remote server hosting a non-MLX backend, a real wrong-token remote (covered by `test_laya_remote_e2e`).
