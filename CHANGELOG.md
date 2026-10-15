# Changelog

All notable changes to **hermes-fleet-manager**.

## [Unreleased]

### Changed
- **An idle session is called wedged from failed nudges, not from the clock.** The fleet_watch
  daemon now sends the idle nudges (capped per stop, held in quiet hours) and hands the gate a
  nudge state: `episode_id`, `nudge_count`, `first_nudge_at`, `last_nudge_at`, `nudge_max`. The
  gate escalates an IDLE stall once the nudges are used up and the session stayed silent
  `LAYA_GATE_NUDGE_SILENCE_WINDOW` (default 30 min) after the last one. Quiet hours hold the
  escalation until morning, and a session that starts working again ends the episode with no
  further nudges. The fixed one-hour `IDLE_WEDGE_AFTER` is gone as a rule: it remains only as a
  fallback for a stall where no nudge landed (no nudge state, or delivery failing), and defaults
  to the time the nudge path would have taken (first stall + nudge intervals + silence window,
  about 56 min). `LAYA_GATE_IDLE_WEDGE_AFTER` overrides it in seconds.
- **Overwatch cron agents no longer nudge.** The overwatch prompt tells them to leave idle
  nudging to the daemon. `FLEET_OVERWATCH_NUDGES=1` (or `overwatch_nudges` in the fleet config)
  restores the old behaviour for use while the daemon is down. Jobs armed before this change keep
  the old prompt until they are disarmed and armed again.
- Added `tests/test_idle_wedge_flow.py`: one idle stop on an injectable clock, with the decision
  row sequence, resume mid-episode, the nudge cap, and quiet hours.

### Added
- **The kanban bridge has a regression suite, and the atomic claim is provably load-bearing**
  (`tests/run_bridge_regression.py`). One command drives the whole claim → spawn → complete loop
  against the stub board and real tmux with a fake worker: happy path, no work available, two
  claimers racing one pool, a lease that expires mid-run and is re-claimed with the stale session
  replaced, a worker exiting non-zero, a session killed externally, and a board unreachable at
  heartbeat time. `tests/test_atomic_claim_mutation.py` runs the claim-race check against a copy of
  the stub with its flock removed and asserts a duplicate IS observed, so the check goes red the
  moment the board's atomic claim is removed instead of passing forever without testing anything.
  The stub gained the two board behaviours a lease-expiry test needs: `claim-next` sweeps expired
  leases before it picks (§9), and a heartbeat renews for the TTL it was given rather than the
  default (§3).
- **The fleet can take its work from the kanban board** (`scripts/fleet_kanban.py`). The board owns
  task state; the fleet owns the tmux sessions; the interface is `docs/kanban-fleet-bridge.md` and
  nothing reads the other side's internals. `run` polls `claim-next`, and on a claim it writes the
  claim record to `<workspace>/.kanban/task.json`, generates a wrapper that records the harness's
  exit status, starts `kb-<task_id>` in the task's workspace with the board's environment handed over
  explicitly (`-e`: a new pane inherits the tmux SERVER's environment, not the caller's), and types a
  short pointer line into the pane instead of pasting the body. A per-task heartbeat renews the lease
  at TTL/3 while the session lives. When it ends, exit 0 completes, a non-zero status fails
  retryably, a `NEEDS-INPUT-` sentinel blocks, and a session that died without a status is RELEASED
  rather than failed, because nothing proved its work failed. A board outage leaves the pane alone,
  buffers the report and retries it at the next tick; three failed heartbeats raise one alert and
  mark the task unmanaged; a `lost_claim` kills the session and drops the report. `reconcile` adopts
  the sessions a restart left behind, so a restart cannot orphan a pane whose lease then expires
  under it. `tests/stub_kanban.py` implements the contract over one JSON state file with a real
  flock, so the fleet half is testable before the board half lands and a claim race in the tests is a
  claim race.
- **The operator skill now documents the overwatch and its focus file (§5.5).** `fleet_overwatch.py`
  shipped 2026-09-17 as a first-class capability and was written up in this changelog and the README,
  but `claude-skill-fleet-operator/SKILL.md` — the document the next operator actually loads — never
  mentioned overwatch or focus at all: a grep for either word across its twelve kilobytes returned
  nothing. §5.5 now covers what an overwatch is (a monitor-gated cron, one per armed session, woken
  by a STATE CHANGE rather than a clock), the `arm` / `status` / `last-report` / `budget` / `disarm`
  surface, and the four rules that were learned the hard way. The focus file IS the per-session goal,
  and it is LOCAL: the mechanism is in this repo, the operator's steering is not, because a brief
  holds their own words and positions. An edit alone changes nothing, because the brief is rendered at
  ARM time, so changing a goal is edit → re-arm → verify, and what gets verified is the RENDER (the
  `focus:` line, then the heading inside the stored prompt) rather than the exit code. `--heartbeat`
  is what covers a session that never changes state. It also records that a superseded decision is
  RETIRED explicitly rather than deleted, and that a reporting obligation does not belong in a brief.
- **The gate reports a session that is parked on the operator** (`laya_gate.py`, `PARKED_RULE`). A
  STALL, SENTINEL or WATCH event on a session whose newest messages say it waits on a person
  ("parked with you", "waits on a person", "cannot proceed") is lane 1, once per episode
  (`LAYA_GATE_PARKED_REPEAT`, 6h), held to morning in quiet hours. Five such episodes (58 raw
  events) were held on 2026-10-01 while the sessions waited. The family is matched by prefix
  (`_is_stop`), because the daemon's family carries a suffix (`SENTINEL-MISSED`).
- **Silence on a stopped session is bounded at ten minutes, and an unacknowledged report is sent
  again.** The operator's number (2026-10-03). The daemon now repeats a STALL every `stall_repeat`
  seconds (300) until the transcript grows, and each one says how long the session has actually
  been quiet. The gate swallows a stop under `LAYA_GATE_SILENCE_ALERT_AFTER` (600s), reports it
  once past it (hard, through quiet hours), then reminds every `LAYA_GATE_SILENCE_REMIND` (30 min),
  doubling up to `LAYA_GATE_SILENCE_REMIND_MAX` (4h); reminders wait for morning. Transcript growth
  is the acknowledgement: the operator typing into the session, or the session resuming.
- **A client-visible surface always reaches the operator** (hard lane-1 rule), alongside production,
  security and money. It matches the words, not the act, for now.
- **The daemon polls the gate's health and tells the operator when it cannot be fixed.**
  `gate_watch()` checks `/health` every `FLEET_GATE_CHECK_EVERY` (60s) rather than only at startup,
  restarts a gate that is silent, cannot load its model, or is stuck loading past
  `FLEET_GATE_LOAD_GRACE`, and sends one urgent `GATE-DOWN` event if it is still unusable after
  `FLEET_GATE_ALERT_AFTER` (600s), then `GATE-RECOVERED` when it comes back. The gate's own outage
  bypasses the overwatch scope and the gate, since the gate is the thing that is down.

### Changed
- **The escalation cost asymmetry is 10:1, not 3:1** (`evals/score_escalation.py`,
  `evals/calibrate_laya.py`): a swallowed event that mattered costs ten times an unnecessary
  interruption. The operator's number.
- **Lane 3 escalates at confidence 0.59, not 0.80** (`LAYA_GATE_CHOICE_THRESHOLD`), the p75 of 337
  recorded decisions; the old threshold sat above all but the top decile. Not yet re-swept against
  a cost model.
- **The gate loads its model at startup**, in a thread after the port binds, so `/health` reports a
  broken model (`model_error`) or `model_loading: true` before the first decision instead of
  `model_error: null` until a lane-3 event happened to arrive. A checkpoint that fails to load, not
  only a failed import, now sets `model_error`. The startup banner prints the threshold actually in
  use (it printed the unused `LAYA_GATE_THRESHOLD`).
- **The gate's restarts have one owner.** When the `com.terra.laya-gate` LaunchAgent is loaded
  (`FLEET_GATE_LABEL`), the daemon restarts the gate with `launchctl kickstart -k` and never starts a
  second copy; it spawns one itself only when no LaunchAgent owns it.
- **The escalation scorer and audit refuse to report a score the model did not produce.** `classify`
  fails open, so on an interpreter without laya-mlx the fallback's escalations were scored as the
  gate's (32 rows of one run) and the script exited 0. Both now exit 2 and name the interpreter;
  `--allow-fallback` scores anyway. Under the real model the parked-rule-OFF baseline misses 41, not
  24, and the ON run's spurious count is 7, not 16.

### Fixed
- **The gate's self-heal now starts the replacement it stopped.** `ensure_gate` stamped its throttle
  before stopping a gate with a broken model, then hit that same throttle and returned without
  starting one; only the LaunchAgent's KeepAlive brought it back. It also SIGTERMed every process
  with a connection to the port, not just the listener (`lsof -sTCP:LISTEN` now).
- **The ten-minute silence rule fired on the wrong stops and never on the right ones.** The daemon
  sent one STALL per stop, at 360s, so the gate swallowed it and a session that stayed stopped was
  never reported (210 swallowed on 2026-10-03). The gate's stop clock reset only on an event of
  another kind, so a session that resumed and worked quietly carried its old stamp: the rule's only
  firing reported "still stopped 30 min" for a six-minute stop. The clock is now the daemon's.
- **Hindsight reads the whole transcript, not its tail** (`_all_timestamps`, cached by size), so a
  decision more than the tail's span old can be scored against what the session did next.
- **The parked rule no longer reads `fleet_last`'s error output as the session's words.** Without
  the `last N message(s)` marker it returns no signal, and it accepts any N (a young session prints
  fewer than 3).
- `test_a_successful_load_clears_a_previous_error` never called `load_model()` and could not fail; it
  now loads against a stand-in `laya_mlx`, alongside a test for a checkpoint that will not load.
- `fleet_input.py` no longer reads an UNLOCATED composer as a draft. With no box found the reader
  reported the pane's cursor row (`❯`) as `text` with `empty: false`, and that phantom occupant
  propagated into both directions of the dispatch path: the stash wrote the glyph over the saved
  draft, so `restore_draft` saw a non-empty composer, mismatched it, and skipped putting the
  operator's real text back (`DRAFT-NOT-RESTORED-COMPOSER-OCCUPIED` three dispatches running, each
  logging `❯` as the preserved draft); and `clear_decision()` read the same glyph as "the box holds
  text" and sent Ctrl-C into a pane it had never read, which cancels a prompt or ends the session on
  a permission or trust dialog. The rule is now one pure function, `draft_text(info)`: an unlocated
  box saves nothing and a prompt-glyph-only read saves nothing, and the clear refuses with "composer
  not located; refuse to send Ctrl-C blind". Covered by `tests/test_fleet_input.py::DraftStash`.
- `fleet_ack.py` no longer silently swaps a contract's own done token for the wrapper token when
  the payload writes it in a free-form shape. `contract_token()` recognised only `DONE-…` and
  `FW<hex>-…`, so a contract labelling its token any other way armed the completion watch on the
  wrapper token while the payload told the session to emit the other one — the completion then read
  as a false `SENTINEL-MISSED`. A cue line is now authoritative: when neither known shape appears on
  it or the two lines under it, a whole-line uppercased-hyphenated token is used and the caller is
  warned to prefer the `DONE-` form. Prose and label-only lines still yield no token, and the
  placeholder template still yields none.
- `fleet_dispatch.sh` no longer reports a delivery failure on a PANE verdict. A pane keeps only its
  ten-line tail, so a delivered message that the session answered scrolled out of it and a good
  dispatch was reported `NOT-SUBMITTED` — four times in one status sweep. When the transcript shows no
  receipt, the paste is now retried ONCE (safe because nothing was received, so a retry cannot
  double-post) and the verdict is only reported after that retry. New outcomes: `LANDED-RETRY`,
  `NOT-SUBMITTED-AFTER-RETRY`, `NOT-LANDED-AFTER-RETRY`, so a rescued dispatch is distinguishable
  from a first-pass one.
- The overwatch template now demands the LITERAL silence token. A run with nothing to report answered
  with a translated marker instead of `[SILENT]`; the delivery filter matches only the known literals,
  so a "nothing to report" path reached the operator's phone. The instruction now names it as a token
  to copy character for character, states that it is never translated or localized, and adds that a
  translated marker does not suppress delivery.
- Dispatch receipt now accepts a QUEUED paste. A busy session takes a paste as a queued turn,
  which its transcript records as a `queue-operation` / `attachment` record instead of a
  `message.role == "user"` line, and only becomes a user turn when the queue drains. The
  receipt check required a user turn, so dispatches to a working session reported
  `NOT-SUBMITTED` while the daemon logged `ACK-OK` seconds later — three times in a row on
  2026-09-26, which is how a genuinely swallowed Enter gets ignored. `delivered()` now counts
  either form, takes an optional transcript path (so it is testable), and the dispatcher's
  comment states the receipt rule. Tests: `tests/test_fleet_ack.py::Delivered`.

### Added
- `fleet_version.py` — which CLI version each session is **running**, and which is newest. The running
  version is read from the live process's mapped executable, the only source unaffected by symlink churn
  or pane width (a status bar drops the version on a narrow pane, and a transcript only records it when
  that session next writes). The newest is resolved across the registry's `latest` tag, the local install
  store and a cache, taking the highest and **reporting which source won**, so one stale source cannot
  fake a downgrade and a network failure cannot either. It never consults the `stable` dist-tag: at the
  time of writing `stable` was `2.1.274` while the fleet was already on `2.1.282`, so resolving through it
  moves an install BACKWARDS. Verbs: `latest`, `of <session>`, `report`.
  Launches now resolve the newest installed binary **explicitly**, so a repointed `claude` symlink cannot
  start a new session behind the fleet. `FLEET_CLAUDE_BIN` pins a specific binary and warns loudly when it
  points at nothing rather than silently substituting one.
- The daemon reports **`VERSION-BEHIND-<SESSION>`** once per version it observes a session on, so CLI drift
  is visible instead of assumed. Throttled to one process lookup per session per five minutes, silent when
  the version cannot be read, and inert when the version module is absent — a reporting nicety must never
  take the daemon down. Tested for the positive case (18 drift events with a forced-newest release), the
  current case (zero false alarms on a current fleet) and fire-once (18 → 0 across scans).
- `fleet_input.py <session>` — reads ONLY the input box, the one piece of state the log
  cannot carry. Locates the composer by expanding from the cursor to the enclosing border
  rows, so scrollback above and status chrome below cannot leak in (checked against a live
  pane). Distinguishes a MENU from a draft: a numbered option uses the same `❯` marker, and
  treating it as input is what let the dispatcher Ctrl-C a live permission prompt.
  `--clear` empties a draft that holds text and refuses to touch a menu or an empty box.
- Armed watches accept **any token form**: a watch token is now matched literally against the
  session's own (model) lines, not only against the slug/generic sentinel patterns. A contract that
  names its done-token in its own words (`FW6F-POLL-LANDED`) could previously be armed but never
  satisfied, so the watch could only ever false-fire `SENTINEL-MISSED` at its deadline.
  The operator skill documents the arm-verbatim rule.
- `fleet_last.py <session>` — the session's most recent messages, read from its own transcript
  (tail-read, so it stays fast on a several-hundred-MB file), plus the newest sentinel token it
  actually emitted. `fleet_state.py` answers *which state* a session is in; this answers *what it
  said*, so reading a session's own account no longer needs a throwaway transcript parser.

### Fixed
- **A sentinel token counts only when EMITTED as its own line — a mention no longer counts.**
  Token patterns were searched as substrings across the whole assistant message, so prose that
  quoted a done token matched exactly like an emission. A session that declined its contract wrote
  "not emitting `DONE-…`"; that sentence fired a `MATCH`, satisfied the armed watch, and retired it,
  so the overwatch went quiet on a contract that was never met — a reported completion that did not
  happen. Tokens now require exact line equality (markdown and quote decoration stripped). The same
  rule governs the watch sweep and the arm-guard (`token_emitted`), so the guard and the daemon
  cannot disagree. Strictness is the safe direction: a missed emission fires `SENTINEL-MISSED` (one
  check-in), a false emission silently retires the watch. Non-token patterns (the bare `traceback`
  probe) keep substring semantics, since a traceback is a block of text, not a line of its own.
  Covered by tests (`python3 -m unittest discover -s tests -t .`).
- **The `traceback` probe is case-insensitive.** It was compiled case-sensitive while Python writes
  "Traceback (most recent call last)", so it never fired for the errors it exists to surface.
  Slug-scoped sentinel tokens stay case-sensitive, where the strictness is deliberate.
- **A contract phrased with an unseen cue no longer arms the watch on the wrong token.**
  `contract_token()` only recognised a fixed set of cue words, and overwatch writes its contracts as
  `DONE CRITERION: …` followed by the bare token on the next line — so a real contract read as "no
  contract", the watch armed on the wrapper token the session was never told to prefer, and the
  deadline false-fired `SENTINEL-MISSED` on a session that was alive and mid-work. The cue list now
  includes the criterion form, and a token alone on its own line (optionally behind a short label
  such as `DONE:`) counts as a contract even when the wording is one this file has not seen.
- **A completion deadline under 300s is floored when the payload carries a sentinel contract.**
  A contract is a milestone-sized unit by construction, so a caller-supplied short deadline on one
  can only produce the false `SENTINEL-MISSED` the 1800s default exists to stop; the raise is
  printed rather than silent. Plain payloads keep whatever the caller asks for.
- **A dispatch that carries a sentinel contract no longer arms the watch on the wrong token.**
  `fleet_ack.py` invented a wrapper token and watched that, while the payload's own contract named a
  different done token — so the session emitted the contract token, the wrapper token never appeared,
  and the deadline fired `SENTINEL-MISSED` for work that had actually finished. The completion watch
  is now armed on the token the payload's contract names (`contract_token()`, exposed as
  `fleet_ack.py contract-token <payload>` to see what would be armed), and the appended instruction
  names that one token only, so a session is never told to emit two. Payloads without a contract keep
  the wrapper token unchanged. The contract text is built by a pure `wrap_payload()`, so the rule is
  covered by tests (`python3 -m unittest discover -s tests -t .`).
- `fleet_ack.py` completion deadline defaults to 1800s, not 180s. A milestone-sized unit cannot finish
  in three minutes, so the old default guaranteed the false `SENTINEL-MISSED` above; a deadline under
  300s now prints a warning, and the overwatch prompt no longer passes a short one.
- `overwatch status` prunes registry entries whose cron job no longer exists, and reports
  which ones it dropped. A job removed out-of-band left its entry behind for good, so the
  entry kept being presented as armed. Pruning is skipped when the job list cannot be
  obtained, so a failed listing cannot drop every entry at once.
- Config discovery no longer depends on an exported environment variable, so a daemon
  started by the service manager reads its config file rather than the built-in defaults.
- Modules that read the registry and drive tmux at import time are `__main__`-guarded, so
  importing one no longer kills and relaunches sessions.

### Changed
- **Launch is config, not code: arbitrary env vars and flags for any harness.** The launchers no
  longer keep a `personal`/`work` name-to-directory map, nor a hardcoded
  `CLAUDE_CONFIG_DIR=<cfg> claude --remote-control`. A profile is now a launch spec — `command`,
  `args`, `env`, `resume_flag`, `servers` — read from config, and any of those keys may also be set
  on a registry entry, where it wins over the profile's. Values expand `~`, `$VAR` and `${VAR}`, so
  a spec can reference the environment and stay portable. `config_dir` is still accepted at either
  level as a shortcut for `env.CLAUDE_CONFIG_DIR`, so registries written before this change keep
  working untouched. See `fleet_harness.py` and the README's launch-spec section.

### Added
- **`fleet_overwatch.py arm` is now RE-ARM SAFE (2026-09-17).** Re-arming a session removes its
  previous job before creating the new one, so it REPLACES the overwatch instead of stacking a second
  identically-named cron. This is the normal way to change an armed session's brief: edit the
  `--focus-file` and re-arm. Verified: re-arming leaves exactly one `example-session-overwatch` job and the
  stored prompt carries the new focus (5828 bytes incl. the plan path).
- **`fleet_overwatch.py` — OVERWATCH AS A FIRST-CLASS CAPABILITY (2026-09-17).** Arming an
  overnight overwatch is now one command instead of a hand-built cron that lived outside the
  manager: `fleet_overwatch.py arm <session> [--interval 15m] [--focus-file F]`, plus `status` and
  `disarm [--remove]`. It generates a per-session monitor wrapper (the cron `monitor_script` field
  takes no args), renders the task brief from `templates/overwatch-prompt.md`, and creates the Hermes
  agent cron gated on that monitor.
  The discipline now lives in the repo template rather than in one cron's prompt: wake on STATE
  CHANGE only (working -> idle / needs-input / queued / dead), nudge a parked session, answer
  obvious questions itself, and report to the human ONLY on a major milestone, a large blocker, or a
  major decision — otherwise reply exactly `[SILENT]` to suppress delivery. Engine: `fleet_state.py`
  (deterministic, timestamp-free fingerprint — anything volatile defeats the gate). Runtime state
  (generated wrappers + `armed.json`) lives in cc-watch, not the repo.
- **`fleet_mcp.py` — MCP PROVISIONING AT LAUNCH (2026-09-17).** The launchers
  (`fleet_layout.py resume`, `restart_fleet_sessions.py`) now ensure the MCP servers a profile
  requires exist **before** they launch anything. A missing server was previously SILENT — the
  session simply could not reach the tool and no error surfaced (repro: an end-to-end test pass
  was run with no MCP server registered, and the human had to be asked for the add
  commands). Registry is declarative (`PROFILES`: profile -> servers, with `env` for literals and
  `env_files` for secrets read from disk at add time — no secret is stored in this repo).
  Idempotent: an already-registered server is never re-added, so a working entry is never churned
  or overwritten. CLI: `fleet_mcp.py status|ensure [profile|config_dir]`. Verified end-to-end by
  removing a server from the work profile and confirming `ensure_for()` restored it with a valid
  token (`claude mcp list` -> Connected).
- **`fleet_layout.py` — NAMED FLEET LAYOUTS (2026-09-14).** Snapshot the live session set and
  bring it back by name: `save <name>` records every ALIVE registry entry
  ({name, short, profile, config_dir, cwd, uuid}); `resume <name>` recreates each tmux session
  with the SAME name + cwd + `claude --remote-control --resume <uuid>` (never blind `--continue`);
  `close <name>` / `close-all-except <short...>` / `list` / `show`. The operator's daily rhythm: `save
  daily` → `close-all-except example-session` to free RAM → `resume daily` to bring the working set back.
  Verified: closing tmux does not lose the transcript, so a layout round-trips the exact sessions.
- **`scripts/resume_after_powerloss.py` — UNGRACEFUL-HOST-DEATH RESUME (2026-09-16).** Ground truth
  is the profile's `sessions/<pid>.json` (Claude Code deletes it on clean exit, so a surviving file
  == the host died) → resume each by its recorded `sessionId` + cwd + tmux target, never guessing.
- **Session-lifecycle helpers** — `scripts/restart_fleet_sessions.py` (relaunch all with
  `--remote-control`, preserving names, re-resolving uuids), `scripts/resume_fleet_sessions.py`
  (resume an accidentally-blanked restart via `--resume <uuid>`), `scripts/crash_recover_fleet.py`
  (post-crash relaunch, this session first), `scripts/kick_fleet.py` (dispatch a short post-crash status
  prompt to every session), and `scripts/jsonl_watch.py` (offset-tracked transcript watcher that
  prints one line per regex match, nothing when quiet — the JSONL-watch pattern).
- **`fleet_watch.py` — rolling 1-HOUR USER-MESSAGE BUFFER (2026-09-10).** The daemon now holds the last
  hour of USER-role text turns per session in a queryable store (`fleet_watch.py user-lines --session
  <s> [--minutes N]`), so "what was this session told?" has ONE authoritative answer instead of
  hand-parsing a huge jsonl — this surfaces the operator's remote-view feedback + dispatcher pastes + a
  session's own scheduled sweeps, and pane-swallowed dispatches never land here (which is itself the
  delivery truth). Store: `fleet_user_msgs.json` (config key `user_msgs_file`). Companion to the
  delivery-ACK below.
- **`fleet_watch.py` — DELIVERY-ACK (2026-09-10).** A dispatch is only "delivered" once the payload
  appears as a real **USER turn** on the session's canonical transcript — pane-visibility / `LANDED`
  from `fleet_dispatch.sh` is NOT proof, because a busy auto-mode pane can swallow the paste before it
  submits (observed twice on a session: a directive sat in the composer and never became a turn). Arm
  before dispatching: `fleet_watch.py watch ack --session <s> --marker <m> --deadline-min <0.7>`; the
  daemon satisfies the ack when the marker lands as a user turn (logged `ACK-OK`, removed), or fires an
  **urgent `ACK-MISSED-<session>`** at the deadline so the supervisor re-delivers instead of believing a
  false "landed". Store: `fleet_watch_acks.json` (config key `ack_file`).
- **`fleet_watch.py` — sentinel WATCH engine.** Arm a watch for a specific complet token with a
  deadline (`fleet_watch.py watch add --session <reg> --token DONE-... --deadline-min N`). The daemon
  satisfies the watch when the token appears as a **model-emitted** line on that session, or fires a
  `SENTINEL-MISSED-<session>` event the instant the deadline passes — waking the supervisor to check
  in, re-estimate the ETA, and re-arm. This is the **agent-to-agent task-coordination seam**: ask the
  controlled agent its ETA, use that as the watch deadline, and let the daemon catch a hung agent by
  deadline instead of it being silently missed. Also `watch list` / `watch cancel`.
- **`fleet_dispatch.sh` — LENGTH GUARD.** Payloads over ~160 lines / 6 KB are **never raw-pasted**
  into a tmux pane — the full text stays on disk and only a one-line `Read <path> then reply with
  <token>` pointer enters the input. Prevents the truncated/interleaved directives that happened when
  a long block was pasted into a busy auto-mode pane.
- **`fleet_watch.py` — STALL detection (alive-but-silent).** A live session whose transcript has
  emitted nothing for `stall_window` seconds (**fast, default 30s**) now fires a `STALL-<session>`
  check-in event. A STALL is a *prompt to check in and ask why*, not a blanket alarm: a legitimate
  external wait (API down, rate limit, waiting on a dependency/approval) is fine and stays quiet; the
  supervisor escalates to the user only when the agent has no good reason and there is further work
  available — because there is always more work.

### Fixed
- `fleet_dispatch.sh` busy gate: exclude recurring **scheduled-task / auto-update / `claude doctor`**
  banners and all completed-turn summary lines from the "is it busy?" check, so a finished agent no
  longer blocks readiness. (Blocking should only ever be a LIVE spinner.)
