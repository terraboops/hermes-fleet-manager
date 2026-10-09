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
  not the lease owner — do not retry; `3` board unreachable or DB locked — retry with
  backoff. Errors print `{"ok":false,"reason":"..."}` on stdout and the human line on stderr.
- **Remote later:** the same verb names become `POST /kanban/<verb>`, flags as the JSON
  body, this same JSON back. Nothing else here changes.

**Worker id** `worker = "<host>:fleet:<session>"`, e.g. `bc:fleet:kb-t_1a3440b3`, stable for
the life of the lease. It is written to `tasks.claim_lock`, so every mutating verb is a
compare-and-set on `(task_id, status, claim_lock)`.

**Claim payload** (JSON, key names exactly):

```json
{"ok":true,"lease_expires":1791522186,
 "task":{"id":"t_1a3440b3","title":"…","body":"…","assignee":"default","status":"running",
   "priority":3,"workspace_kind":"scratch","workspace_path":"…/workspaces/t_1a3440b3",
   "branch_name":null,"project_id":null,"created_at":1791521286,"current_run_id":1,
   "claim_lock":"bc:fleet:kb-t_1a3440b3","claim_expires":1791522186,"skills":[],
   "model_override":null,"provider_override":null,"max_runtime_seconds":null,
   "completion_contract":"local-only","body_ref":"…/workspaces/t_1a3440b3/.kanban/task.json"}}
```

## 1. Claim API

| # | verb | status |
|---|---|---|
| E1 | `claim-next --worker ID [--ttl S] [--status ready\|review] [--json]` | NEW |
| E2 | `heartbeat <task_id> --worker ID [--note N] [--json]` | EXISTS (`heartbeat_claim`); add `--worker` |
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
else exits 2. `done`, `blocked` and `archived` are terminal for the worker — never re-report
into them (E4/E5 are idempotent and answer `already_terminal:true`).

## 3. Lease semantics

- **TTL** `900 s` (`DEFAULT_CLAIM_TTL_SECONDS`), overridable per claim with `--ttl`.
- **Heartbeat** every `TTL/3` (300 s), plus once immediately after claim and after each
  progress event; bumps `tasks.claim_expires` and `tasks.last_heartbeat_at`.
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
  target `=<session>:worker` — tmux resolves targets by prefix, and an unqualified name can
  read, type into, or kill a different session.
- **cwd** = the claimed task's `workspace_path`.
- **Handoff:** write the claim record to `<workspace>/.kanban/task.json` (mode 0600), plus
  `summary.md` and `metadata.json` for the report; export the env below; then inject a
  **short pointer line** into the pane ("Read `<workspace>/.kanban/task.json` and complete
  the task; report via `hermes kanban complete`"). Never paste the body — a long paste
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
| task cancelled while running | E2 answers `lost_claim` (operator archived / reclaimed / reassigned) | kill the session immediately, drop any pending report, log the task id; never report into a task you no longer own |
| claim race lost | `task:null` on E1 | back off and poll; never retry the same id in a tight loop |
| lease lost then regained | heartbeat `ok` after a `lost_claim` blip | treat as lost — the CAS already reassigned it; kill and stop |

## 8. Delta to implement

- **Board:** `claim-next`, `release`, `fail`; `--worker` on `heartbeat`/`complete` (today's
  default `<host>:<pid>` stays); `body_ref` in the claim JSON; the exit-code convention
  above (the CLI currently returns `1` for every error). Persistence, CAS and the lease
  columns already exist — add no columns.
- **Fleet:** the claim loop, the `kb-<task_id>` spawner, the per-task heartbeat timer, the
  exit-code reporter, and the four error paths in §7.
