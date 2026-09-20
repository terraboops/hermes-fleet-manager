# Changelog

All notable changes to **hermes-fleet-manager**.

## [Unreleased]

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
