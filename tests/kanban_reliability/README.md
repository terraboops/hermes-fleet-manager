# Kanban dispatcher reliability harness

Negative-case tests for the Hermes kanban dispatcher (claim, lease, heartbeat,
retry, crash recovery). It drives the real dispatcher code against a scratch
board with fake workers and SIGKILLs processes at chosen moments.

Invariants asserted:

- I1 no card lost: every card ends in a known status with a run history.
- I2 no concurrent duplicate: two live workers never hold one card at once.
  Each fake worker logs the pids of earlier workers of the same card that are
  still alive when it starts.
- I3 lease reclaim: a card whose holder is gone is reclaimed after the lease
  expires, and not before.

## Run

    ~/.hermes/hermes-agent/venv/bin/python tests/kanban_reliability/harness.py
    ~/.hermes/hermes-agent/venv/bin/python tests/kanban_reliability/harness.py --only S2b,S4a

Takes about 2 minutes. Output: console report, `results/results.md`,
`results/results.json`, and per-case `*.exec.log` (worker START/END lines),
`*.dispatcher*.log`, `worker-*.log`. Exit code is 0 when every case passes or
is a documented gap (S2c, S4a).

Safety: all paths are pinned to a temp directory, every inherited `HERMES_*`
variable is dropped, and the run refuses to start if the DB resolves under the
live board. Set `HERMES_AGENT_DIR` to test another checkout.

## What this cannot prove

SIGKILL is not a power cut. The OS page cache survives it, so a lost fsync is
invisible here. What it does cover: no clean shutdown, no WAL checkpoint, a
transaction cut mid-write. Durability on real power loss rests on
`PRAGMA synchronous=FULL` (set in `kanban_db_connect.connect`) and needs a VM
with a cut disk to confirm. The "gateway" is modelled as a dispatcher process
running the same `dispatch_once` loop; the real gateway was not killed, so
launchd behaviour (ExitTimeOut, process-group teardown) is unverified.

## How the dispatcher works (read from source, hermes-agent b236008)

- Claim: `kanban_db._claim_and_open_run`. One `BEGIN IMMEDIATE` transaction does
  `UPDATE tasks SET status='running', claim_lock=?, claim_expires=? WHERE
  status='ready' AND claim_lock IS NULL`, opens a `task_runs` row and writes a
  `claimed` event. The lock is `host:pid` of the claimer. Compare-and-set, so
  one winner.
- Single writer per tick: `dispatch_once` takes a non-blocking per-DB lock
  (`_dispatch_tick_lock`); a second dispatcher skips the tick.
- Spawn: `_default_spawn` starts `hermes chat -q` with `start_new_session=True`,
  then `_set_worker_pid` records pid plus a process fingerprint (guards against
  pid reuse after reboot). A worker also self-registers with
  `adopt_worker_pid` (tools/kanban_tools.py) to cover a dispatcher that dies
  between Popen and `_set_worker_pid`.
- Lease: `claim_expires = now + 900s` (`HERMES_KANBAN_CLAIM_TTL_SECONDS`).
  `heartbeat_claim` renews it; `heartbeat_worker` only stamps
  `last_heartbeat_at`. The kanban_heartbeat tool calls both.
- Reclaim, every tick, in this order: `reap_terminal_workers`,
  `release_stale_claims` (lease expired), `reconcile_orphaned_running`
  (running with NULL lock/expiry), `detect_stale_running` (off by default),
  `detect_crashed_workers` (host-local pid dead, no waiting for the lease),
  `enforce_max_runtime` (per-card cap, SIGTERM then SIGKILL).
- Expired lease with a live host-local pid: the lease is extended
  (`claim_extended`), not reclaimed, unless `last_heartbeat_at` exists and is
  older than 3600s. This avoids killing a slow model call.
- Retry: a crash, timeout, reclaim or spawn failure counts toward
  `consecutive_failures`. At `failure_limit` (default 2, or per-card
  `max_retries`) the card goes to `blocked` with a `gave_up` event. A
  respawn guard spaces retries. Rate-limit exit (75) does not count; config
  exit (78) blocks on the first failure.

## Results (run of 2026-10-12, hermes-agent b236008)

| case | scenario | verdict | evidence |
|---|---|---|---|
| S1 | (a) worker SIGKILLed mid-run | PASS 6/6 | crash seen on next tick, no lease wait, card re-run once, done, 2 runs |
| S1b | (a) worker killed on both attempts | PASS 4/4 | card parked `blocked` after 2 failures, not lost, nothing pages anyone |
| S2 | (b) dispatcher SIGKILLed, worker alive | PASS 5/5 | worker survives (own session); new dispatcher extends the lease, no respawn, 1 execution |
| S2b | (b) dispatcher dies after claim, before spawn | PASS 5/5 | card stays `running` until the lease ends, not reclaimed early, reclaimed after, re-run once |
| S2c | (b) dispatcher dies after Popen, pid not recorded, worker does not self-register | FAIL (gap G2) | second worker started while first alive; first got `complete=False` |
| S2d | same, worker self-registers (shipped hermes worker) | PASS 3/3 | `worker_registered`, lease extended, 1 execution |
| S3 | (c) dispatcher and worker SIGKILLed together | PASS 6/6 | integrity ok, card re-run on restart without waiting for a 900s lease |
| S3b | (c) writer SIGKILLed mid-transaction x6 | PASS 2/2 | integrity ok, every card has its `created` event |
| S4a | (d) hung worker, pid alive, no heartbeat, lease expired | FAIL (gap G3) | `claim_extended` forever; never reclaimed |
| S4b | (d) worker heartbeats once then wedges | PASS 4/4 | reclaimed after heartbeat age > limit, old worker killed, successor done (limit patched 3600 -> 4s) |
| S4c | (d) `max_runtime_seconds` on a hung worker | PASS 3/3 | `timed_out`, re-run, done |
| S5 | three dispatchers racing, 12 cards | PASS 3/3 | each card executed exactly once |

Logs: `results/<case>.exec.log` and neighbours.

Answer to the three invariants: no card was lost in any case, no concurrent
duplicate occurred with the shipped worker, and a card whose holder is dead is
reclaimed (S1, S2b, S3). A card whose holder is alive but stalled is not
reclaimed by the lease (S4a).

## Gaps and proposed fixes

G1. The fleet bridge board verbs are not live. `hermes kanban claim-next`,
    `release`, `fail`, and `heartbeat --worker` do not exist in the running
    hermes (`hermes kanban claim-next` answers "not a command"). Commits
    b71108d03e and 446a46870a sit only in
    `refs/hermes-update-backups/diverged-main-20261009-224625-446a46870abe`:
    an update reset `main` and displaced them. Fix: re-apply the two commits
    on `main`, add a check that fails when `claim-next` is missing, and keep
    the bridge out of `hermes update`'s reset path (carry it as a plugin or
    upstream it). This is the highest-impact finding.

G2. Pid-record window. A worker that does not call `adopt_worker_pid`
    (any tmux or fleet worker) is duplicated when the dispatcher dies between
    Popen and `_set_worker_pid` (S2c). Fix: write the pid in the same
    transaction as the claim by spawning inside the claim, or have the
    dispatcher record an intent row (`spawning`) before Popen and reconcile it
    on restart. Until then every non-hermes worker must call the adopt step.

G3. Alive-but-stalled worker is never reclaimed unless it heartbeated once or
    the card has `max_runtime_seconds` (S4a). `last_heartbeat_at IS NULL`
    disables the staleness backstop entirely. Fix: treat a NULL heartbeat as
    `started_at` for the staleness test, and give every card a default
    `max_runtime_seconds`. The 3600s staleness window is also long; make it a
    config key and set it near 3x the expected heartbeat interval.

G4. Lease extension ignores the claim's own TTL. `_extend_live_stale_claim`
    re-reads the env or the 900s default, so a short lease (for fast
    failover) becomes 900s after its first expiry. Found while writing S4b.
    Fix: store the TTL on the run (`lease_ttl_seconds` column) and reuse it.

G5. Two heartbeat functions, one worker path. The shipped worker tools call
    both `heartbeat_claim` (moves `claim_expires`) and `heartbeat_worker`
    (stamps `last_heartbeat_at`), so a hermes worker is fine. A worker built
    on `heartbeat_worker` alone (my fake worker, any external one) stamps
    liveness but never renews the lease. Fix: make `heartbeat_worker` renew
    the lease, or merge the two. The bridge's `heartbeat --worker` (G1) did.

G6. A card that fails twice is `blocked` with no alert (S1b). The board
    keeps it, but nothing tells anyone. Fix: emit a lane-1 event on
    `gave_up` into the fleet event stream.

G7. Retries are not idempotent. A re-run starts from scratch and repeats
    side effects (comments, PRs, deploys) from the killed attempt. Fix: the
    worker context already lists prior attempts; add a rule that side-effecting
    steps record an idempotency key in task metadata before acting.

G8. Unverified: power-loss durability and real-gateway restart (see "What
    this cannot prove"). Fix: run S3 inside a VM with a hard power-off, and
    kill the launchd-managed gateway on a throwaway instance (never the live
    one).
