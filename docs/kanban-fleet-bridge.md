# Kanban ↔ fleet bridge (v1)

Contract between the Kanban board (`hermes kanban`, `~/.hermes/kanban.db`, code in
`hermes_cli/kanban_db*.py`) and the fleet plugin (`hermes-fleet-manager`, `fleet_*` CLIs).
Both sides implement this file; neither reads the other's internals.

## 0. Transport

The fleet plugin invokes the board's CLI as a subprocess and parses JSON on stdout.
No HTTP, no shared Python import, no direct SQLite writes from the fleet side.

```
hermes [--board <slug>] kanban <verb> [flags] --json
```

- **Auth: none.** Same host, same user, DB file mode 0600. Board comes from `--board`, else
  `HERMES_KANBAN_DB` / `HERMES_KANBAN_BOARD`, else the `current` board.
- **Env hygiene (required).** Launch the child with `HERMES_DELEGATED_CHILD_CONTEXT`
  removed. With it present the kernel fails closed
  (`PermissionError: delegate_task child contexts cannot mutate Kanban tasks or boards`)
  on every `write_txn`, so a fleet that inherits it from a delegate run claims nothing.
- **Exit codes:** `0` success, including `task:null` (no work); `2` usage, unknown task, or
  not the lease owner, do not retry; `3` board unreachable or DB locked, retry with
  backoff. Errors print `{"ok":false,"reason":"..."}` on stdout and the human line on stderr.
- **Remote later:** the same verb names become `POST /kanban/<verb>`, flags as the JSON
  body, this same JSON back. Nothing else here changes.

**Worker id** `--worker ID` is any stable, opaque id. The board stores it **verbatim** in
`tasks.claim_lock` and returns it in the claim payload, so every mutating verb is a
compare-and-set on `(task_id, status, claim_lock)`. Pass a per-instance id (`bc:fleet`) on
`claim-next` and then reuse the `claim_lock` the payload returned for every later verb on
that task, the two must be byte-identical or the verb answers exit 2. Nothing parses the
id: `<host>:fleet:<session>` is a convention, not a format. The board never appends the
session name: the claim payload echoes back exactly the value that was passed.

**Claim payload** (JSON, key names exactly):

```json
{"ok":true,"lease_expires":1791522186,
 "task":{"id":"t_1a3440b3","title":"…","body":"…","assignee":"default","status":"running",
   "priority":3,"workspace_kind":"scratch","workspace_path":"…/workspaces/t_1a3440b3",
   "branch_name":null,"project_id":null,"created_at":1791521286,"current_run_id":1,
   "claim_lock":"bc:fleet","claim_expires":1791522186,"skills":[],
   "model_override":null,"provider_override":null,"max_runtime_seconds":null,
   "completion_contract":"local-only","body_ref":"…/workspaces/t_1a3440b3/.kanban/task.json"}}
```

## 1. Claim API

| # | verb | status |
|---|---|---|
| E1 | `claim-next --worker ID [--ttl S] [--status ready\|review] [--json]` | NEW |
| E2 | `heartbeat <task_id> --worker ID [--ttl S] [--note N] [--json]` | EXISTS (`heartbeat_claim`); add `--worker`, `--ttl` |
| E3 | `release <task_id> --worker ID --reason R [--json]` | NEW (operator `reclaim` exists) |
| E4 | `complete <task_id> --worker ID [--summary S] [--result R] [--metadata JSON] [--json]` | EXISTS; add `--worker` |
| E5 | `fail <task_id> --worker ID --error E [--retryable] [--json]` | NEW |
| E6 | `show <task_id> --json`, `list --status ready --json` | EXISTS (reconcile only) |

```
E1 → {"ok":true,"task":{…}|null,"lease_expires":<epoch>|null}
E2 → {"ok":true,"lease_expires":<epoch>} | {"ok":false,"reason":"lost_claim"}
E3 → {"ok":true,"status":"ready"|"review","already_terminal":false}
E4 → {"ok":true,"status":"done","already_terminal":false}
E5 → {"ok":true,"status":"ready"|"blocked","already_terminal":false}
```

`claim-next` picks the highest `priority`, then oldest `created_at`, among rows with
`status='ready'`, satisfied parents, non-empty assignee, and no live `claim_lock`. It
resolves and creates the workspace and persists `workspace_path` before answering, so
`body_ref` is always a real path. **One claimer per board:** where the fleet runs, the
board's own dispatcher/daemon must be stopped (`hermes kanban daemon` off), otherwise both
sides race for the same pool and the loser's workers start with no lease.

## 2. States and transitions

Board statuses are the kernel's: `triage | todo | scheduled | ready | running | blocked |
review | done | archived`.

| from → to | trigger | who may |
|---|---|---|
| triage → todo | specify / decompose | board, operator |
| todo → ready | `recompute_ready` once all parents are `done` | board only |
| ready → running | E1 claim-next (CAS) | fleet worker |
| running → running | E2 heartbeat (extends lease) | lease owner |
| running → done | E4 complete | lease owner |
| running → ready | E3 release, E5 fail `--retryable`, or lease expiry | lease owner; board on expiry |
| running → blocked | E5 fail without `--retryable` (incl. needs-input), or the failure breaker | lease owner; board |
| running → review | `request-review` | lease owner |
| running → todo / blocked / scheduled | operator `reclaim` / `reassign` / `archive` | operator only |
| blocked / done → ready | `unblock`, `reopen-review` | operator only |

A fleet worker may act only on a task whose `claim_lock` equals its worker id; anything
else exits 2. `done`, `blocked` and `archived` are terminal for the worker, never re-report
into them (E4/E5 are idempotent and answer `already_terminal:true`).

## 3. Lease semantics

- **TTL** `900 s` (`DEFAULT_CLAIM_TTL_SECONDS`), overridable per claim with `--ttl`.
- **Heartbeat** every `TTL/3` (300 s), plus once immediately after claim and after each
  progress event; bumps `tasks.claim_expires` and `tasks.last_heartbeat_at`.
  **The renewal must carry the same `--ttl` the claim was made with** (`heartbeat … --ttl S`,
  added with the bridge): the board's renewal otherwise falls back to its own default, so a
  claimer that asked for a short lease gets it widened silently on the first heartbeat.
- **Expiry** is board-side: the `release_stale_claims` tick (dispatcher/daemon) returns the
  task to `ready`, closes the run with outcome `reclaimed`, and increments
  `consecutive_failures` so the breaker eventually blocks a task that can never run.
- **No duplicate dispatch.** A task re-enters the pool only through that reclaim CAS, by
  which time the lease is dead. The board must **not** try to kill a fleet worker (its
  `worker_pid` is a tmux pane, not a child of the board); the fleet's own heartbeat is the
  kill signal.
- A live host-local worker with a fresh heartbeat gets its claim *extended*, not reclaimed:
  a slow model inside one long tool-free call must not be yanked.

## 4. Atomicity

Claim is a compare-and-set inside one SQLite `BEGIN IMMEDIATE` transaction
(`write_txn`, `kanban_db_connect.py`):

```sql
UPDATE tasks SET status='running', claim_lock=:worker, claim_expires=:exp,
                 worker_pid=:pid, last_heartbeat_at=:now, started_at=COALESCE(started_at,:now)
 WHERE id=:id AND status='ready' AND claim_lock IS NULL;
-- rowcount == 1 → winner; 0 → lost the race, try the next candidate
```

Two racing claimers serialize on the write lock; exactly one sees `rowcount == 1`. E2–E5 use
the same shape with `AND claim_lock = :worker`, so a worker whose lease was reclaimed mid-run
cannot report into the task its successor now owns. Every transition appends a `task_events`
row (`claimed`, `heartbeat`, `reclaimed`, `completed`, `failed`) and mirrors the lease onto
`task_runs.claim_expires`.

## 5. Session mapping and handoff

- **Session** `kb-<task_id>` → `kb-t_1a3440b3`; window `worker`. Always address the exact
  target `=<session>:worker`, tmux resolves targets by prefix, and an unqualified name can
  read, type into, or kill a different session.
- **A session already wearing that name is replaced, not adopted.** The name is deterministic
  per task, so a live `kb-<task_id>` at spawn time is a leftover from a lease that is dead now
  (the claim that got us here is a CAS on a released claim). `tmux new-session` refuses a
  duplicate name, so the spawner kills the stale session first, otherwise the task is claimed
  and never run. Symmetrically, a worker that loses its lease kills its session **only while
  the pane's `HERMES_KANBAN_CLAIM_LOCK` is still its own** (`tmux show-environment -t
  =<session>:`, the value the session was created with): by then a successor may be running
  under the same name, and killing by name would kill *its* worker.
- **cwd** = the claimed task's `workspace_path`.
- **Handoff:** write the claim record to `<workspace>/.kanban/task.json` (mode 0600), plus
  `summary.md` and `metadata.json` for the report; export the env below; then inject a
  **short pointer line** into the pane ("Read `<workspace>/.kanban/task.json` and complete
  the task; report via `hermes kanban complete`"). Never paste the body: a long paste
  truncates the front of the buffer. Verify the paste's first line landed before trusting it.
- **Env:** `HERMES_KANBAN_TASK`, `HERMES_KANBAN_DB`, `HERMES_KANBAN_BOARD`,
  `HERMES_KANBAN_WORKSPACE`, `HERMES_KANBAN_RUN_ID`, `HERMES_KANBAN_CLAIM_LOCK`,
  `HERMES_KANBAN_WORKER_ID`.
- **Concurrency:** `max_held` (default 4) tasks per fleet instance; a claim past the cap is
  released immediately. Poll 5 s after a successful claim, backing off 5→10→30→60 s cap
  while `task:null`.

## 6. Completion reporting

| worker outcome | report |
|---|---|
| exit 0 | `complete <id> --worker ID --summary <last line or summary.md> --result <text> --metadata <metadata.json>` |
| exit ≠ 0 | `fail <id> --worker ID --error "exit=<code>: <last stderr line>" --retryable` |
| `NEEDS-INPUT-<NAME>` sentinel | `fail <id> --worker ID --error "needs-input: <text>"` (no `--retryable` → `blocked`) |
| session killed by operator | `release <id> --worker ID --reason "session killed"` |

Both report verbs are idempotent: a repeat answers `already_terminal:true` without reopening
or double-counting the run. A worker may report directly (it holds `HERMES_KANBAN_TASK` and
`HERMES_KANBAN_CLAIM_LOCK`); the fleet then sees a terminal task on exit and skips its own
report. If the board is unreachable at report time (exit 3), keep the
report in `<workspace>/.kanban/pending-report.json` and retry at the next heartbeat tick; the
lease expiry is the backstop if the fleet never returns.

## 7. Error cases

| case | detection | required behaviour |
|---|---|---|
| worker crash, no report | session gone + heartbeat stale | fleet calls E3 release; if the board is unreachable, let the lease expire and the board reclaim |
| tmux session dies mid-run | `tmux has-session -t =<name>` fails | release with `--reason "session died"`, kill leftover panes; do not relaunch the same task in the same run |
| board unreachable | exit 3 / `hermes` missing | keep the worker and the pane untouched; retry heartbeat 5→10→30→60 s; after 3 consecutive failures mark the task `unmanaged` and raise one alert; on recovery a `lost_claim` answer means kill the session and do not report |
| task cancelled while running | E2 answers `lost_claim` (operator archived / reclaimed / reassigned) | kill the session **if it is still ours** (its `HERMES_KANBAN_CLAIM_LOCK` equals our lock), drop any pending report, log the task id; never report into a task you no longer own. A successor under the same session name must be left running |
| lease expired and re-claimed while our worker still runs | our session name is now worn by the successor's session | leave it alone: killing by name would kill the successor's worker. The spawner of the successor has already replaced the session we were in |
| claim race lost | `task:null` on E1 | back off and poll; never retry the same id in a tight loop |
| lease lost then regained | heartbeat `ok` after a `lost_claim` blip | treat as lost, the CAS already reassigned it; stop (kill only what is still ours) |

## 8. Delta to implement

- **Board:** `claim-next`, `release`, `fail`; `--worker` on `heartbeat`/`complete` (today's
  default `<host>:<pid>` stays); `body_ref` in the claim JSON; the exit-code convention
  above (the CLI currently returns `1` for every error). Persistence, CAS and the lease
  columns already exist, add no columns.
- **Fleet:** the claim loop, the `kb-<task_id>` spawner, the per-task heartbeat timer, the
  exit-code reporter, and the four error paths in §7.

## 9. Board-side notes (implemented)

Decisions §1–§7 left implicit, recorded here because the fleet depends on them:

- **`claim-next` expires leases itself.** Where the fleet runs the board dispatcher is off
  (§1), so the tick that would reclaim an expired claim never fires. `claim-next` sweeps
  expired claims before it picks, so the fleet is self-sufficient; the sweep books a failure
  through the normal reclaim path, so the breaker, not the fleet, is what stops a task
  that can never run.
- **`fail --retryable` counts toward the breaker; `release` does not.** A retryable failure
  closes the run as `failed` and increments `consecutive_failures`, so a worker that dies on
  every claim trips the limit and the card parks `blocked` instead of looping forever. An
  explicit release (E3) is the lease owner choosing to stop, so it leaves the counter alone.
- **`release` emits a `reclaimed` event** carrying `released: true` and the reason, so a
  released run and an expired run stay distinguishable on the event log while both land
  `ready` (or `review`, for a reviewer run). History is never deleted.
- **`fail` without `--retryable` is sticky.** The card lands `blocked` with
  `block_kind=needs_input` plus a `blocked` event, that event is what holds it against
  `recompute_ready`. Only a human `unblock` revives it.
- **Idempotency** is answered from the task's terminal status and from a run this same
  worker id closed. A claim now held by *another* worker is always exit 2, even if this
  worker owned the task earlier: never report into a task a successor owns.
- **`body_ref` is written by the board** (`<workspace>/.kanban/task.json`, mode 0600), so it
  is a real path the moment `claim-next` answers. The fleet may overwrite it with its own
  claim record.
- **Scope of the exit-code convention:** 0/2/3 applies to the fleet verbs (`claim-next`,
  `heartbeat --worker`, `release`, `fail`, `complete --worker`). Every other `hermes kanban`
  verb keeps its historical exit 1, so existing scripts are unaffected.
- **`heartbeat --worker` takes `--ttl`** (`renew_claim` already did; the CLI now passes it
  through). Without it the renewal falls back to `DEFAULT_CLAIM_TTL_SECONDS`, so the TTL the
  caller asked for on `claim-next` applied only to the first claim. Omitted = board default,
  which keeps every pre-existing heartbeat call unchanged.

## 10. Running the end-to-end check

`tests/e2e_bridge_real.py` drives the whole loop against the REAL board CLI and REAL tmux,
on throwaway boards in a temp dir and a private tmux server (`tmux -L`), so a live fleet is
never touched. It needs no arguments and prints a pass/fail report:

```
python3 tests/e2e_bridge_real.py          # add --keep to leave the boards behind
```

Scenarios, one throwaway board each:

1. **happy path**: claim, the `kb-<task_id>` session appears, the pointer line and the
   `HERMES_KANBAN_*` env reach the pane, the worker reads the body, exits 0, the task lands
   `done` with the worker's own summary as the result.
2. **no duplicate dispatch**: two claimers race five tasks against the real CAS (each task
   claimed exactly once), then two fleet instances race one task (exactly one session).
3. **mid-run kill**: the session is killed under a running task: released to `ready`, not
   left `running`, and claimable again.
4. **lease expiry mid-run**: A's lease expires while its worker runs; B reclaims, replaces
   the stale session, and A's `lost_claim` leaves B's session alone.

`tests/e2e_worker.py` is the worker stand-in: it records what it saw to
`<workspace>/.kanban/worker-receipt.json`, so the run asserts on evidence rather than on the
fleet's own logs.
