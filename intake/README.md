# intake: pre-model message capture

Records every authorized inbound chat message as a `triage` kanban card before any model
runs. Promotion hands the card to the existing dispatcher (claim, lease, heartbeat, reclaim).
`guarded_complete` refuses completions whose output says the work was not tested.
Capture sends nothing to the chat: no acknowledgement.

Design: `docs/intake-queue-design.md`. Run log: `docs/intake-run-log.txt`.

## Enable

1. Install the plugin: `ln -s ~/Developer/hermes-fleet-manager/intake/plugin ~/.hermes/plugins/intake`
2. Enable it in the plugin list, then set the flag in `~/.hermes/config.yaml`:

       intake:
         enabled: true

   `HERMES_INTAKE_ENABLED=1` overrides the file. Default is off; an unreadable config means off.
3. Restart the gateway (gateway restarts kill cron work: pick a quiet moment).
4. Replay anything left over from a crash: `Intake(...).recover()` (idempotent).

## Roll back

Set `intake.enabled: false` (or unset the env var). The hook then returns at once. To remove
it fully, delete the `~/.hermes/plugins/intake` symlink and restart the gateway. Cards already
captured stay on the board as ordinary triage cards; the receipt journal is
`~/.hermes/state/intake/intake-receipts.jsonl` and can be deleted.

## Limits of this slice

- Only the idle path is hooked (`pre_gateway_dispatch`). A message that arrives while a turn
  runs is not captured until the `message_received` hook exists in `BasePlatformAdapter`
  (see the design note, Q1).
- No classifier: every captured card stays in `triage` until `Intake.promote` is called.
- `guarded_complete` is a wrapper. A worker that calls `kanban_complete` directly bypasses it
  until the check is moved into `complete_task` upstream (design note, Q4).
- Tests run on a scratch board with SIGKILL, not a real gateway or a power cut.

## Tests

    ~/.hermes/hermes-agent/venv/bin/python -m unittest tests.test_intake -v

Negative cases, by test name: `duplicate_message_delivery_no_duplicate_card`,
`gateway_killed_between_receipt_and_card_write`, `worker_killed_mid_run_card_reclaimed_no_lost_card`,
`gateway_restart_mid_run_no_duplicate_execution`, `worker_marks_done_with_not_tested_output_rejected`.
