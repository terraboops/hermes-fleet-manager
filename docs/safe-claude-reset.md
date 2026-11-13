# Safe Claude reset and native idle checks

Run lifecycle commands through the installed runtime directory, not the source checkout.

```sh
cd ~/.hermes/scripts/cc-watch
python3 fleet_reset.py --evidence "$PWD/reset-history/preview-unique"
python3 fleet_reset.py --apply --workers 3 --evidence "$PWD/reset-history/run-unique"
```

Use repeated `--only <exact-registry-name>` to limit a run. Evidence directories must be new. The command supports work-profile Claude sessions only. Parked sessions are excluded before discovery. Unregistered native sessions are inventoried but never stopped or registered by inference.

## Safety and recovery

The preflight checks the exact UUID, unique transcript, native process start time, pane, cwd, current executable, work authentication, launch constraints, genuine auto mode, input/menu state and child processes. Active tools, drafts and owner choices hold that target. Other targets continue through a sliding pool of at most three workers.

The boot-reconciler lock remains held across the batch. Each target gets a private transcript checkpoint and recovery manifest before `/exit`. There is no force kill. Resume uses the same UUID and current CLI through the harness. A failed launch leaves its pane readable. No global profile provisioning or gateway restart occurs.

Native verification requires a fresh maintenance-only dispatch, successful linked Bash `pwd`, final reply, unique replacement PID, exact executable and native Remote Control bridge metadata when previously present. Remote bridge metadata does not prove the owner opened the remote surface. Verification does not authorize productive work or resolve owner gates.

A stopped transaction that never attempted launch can recover through:

```sh
python3 fleet_reset.py --apply --recover /path/to/session.json \
  --evidence "$PWD/reset-history/recovery-unique"
```

`--recover` is repeatable. A recorded launch attempt cannot be retried through this path. Inspect the exact native process and transcript rather than creating a duplicate writer. Unknown shutdown suffixes remain held for inspection. Known exit receipts and unchanged backup metadata may be appended, but the original transcript must remain an exact prefix.

The command preserves registry, watch and cron configuration. It clears only the target's stale liveness/stall cache; it does not discard pending watches or acknowledgements. Manifests report per-phase elapsed times, dispositions and concurrency. A timeout is a ceiling, not a fixed delay.

## Native idle detection

```sh
python3 fleet_activity.py <session-or-short>
python3 fleet_overwatch.py cadence <exact-session-name> --minutes 5
```

The activity reader matches `tool_use` to `tool_result`, tracks background task IDs through native notifications and checks process liveness and child processes. Completed assistant turns start the idle clock. Housekeeping writes do not reset it. A truncated or malformed stream is unknown; an approval menu is held, not idle. Long-running or parallel unmatched tools are `WAITING_TOOL`, not idle.

Idle eligibility is 300 seconds and requires both desired-up state and an enabled armed watch. Cadence edits only the existing job's interval and armed metadata; it does not widen supervision or recreate the job. Existing heartbeat, tool deadlines, restart deadlines and approval policy are independent and unchanged. The 1,800-second stale-tool observation flag is diagnostic, never permission to stop a tool.

No cache keepalive, periodic inference ping, billing change or automatic task activation is implemented. Native usage records can show one-hour cache writes even when `promptCacheTtl` is unset.
