# Changelog

All notable changes to **hermes-fleet-manager**.

## [Unreleased]

### Added
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
  emitted nothing for `stall_window` seconds (default 900) now fires a `STALL-<session>` urgent event —
  the agent is *up but not producing* (compaction, a parked prompt, or a hung loop all look identical to
  "went quiet"). Catches stalls the sentinel/token + SESSION-DEAD signals can't (process alive, no output).

### Fixed
- `fleet_dispatch.sh` busy gate: exclude recurring **scheduled-task / auto-update / `claude doctor`**
  banners and all completed-turn summary lines from the "is it busy?" check, so a finished agent no
  longer blocks readiness. (Blocking should only ever be a LIVE spinner.)
