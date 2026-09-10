# Changelog

All notable changes to **hermes-fleet-manager**.

## [Unreleased]

### Added
- **`fleet_watch.py` — DELIVERY-ACK (2026-09-10).** A dispatch is only "delivered" once the payload
  appears as a real **USER turn** on the session's canonical transcript — pane-visibility / `LANDED`
  from `fleet_dispatch.sh` is NOT proof, because a busy auto-mode pane can swallow the paste before it
  submits (observed twice on wolfgang: a directive sat in the composer and never became a turn). Arm
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
