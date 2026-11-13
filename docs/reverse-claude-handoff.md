# Reverse Codex → Claude handoff

Operational runbook for the authorized 2026-10-30 pilots:
`~/.hermes/scripts/cc-watch/migrations/reverse-claude-20261030/RUNBOOK.md`.

`claude_handoff.py` supports offline `prepare`, then explicit `run`, `verify`,
`resume`, and `verify-resume` actions. Run from the fleet runtime directory.
`prepare` without `--candidate-name` retains the original snapshot-only behavior.

A candidate is **unregistered, parked in its proposed row, and read-only**. The
launcher is private, uses the work Claude home explicitly, disables customizations
and MCP, and exposes only Read/Bash with automatic permission limited to Read and
`pwd`; other commands are denied. It never edits source registry rows, kills source
processes, switches routing, unpauses watches, or promotes automatically.

The fixed reset floor is `2026-10-30T02:00:00-07:00`. Do not bypass it. CLI plans
also bind the exact source registry row; source transcript changes invalidate a
launch. Launch attempts are exclusive and durable, even on subprocess failure.
An ambiguous launch must be inspected, not retried. Successful mechanical checks
require native UUID/cwd, linked successful Read/pwd tool results and a completed
final reply with source phrases not supplied in the prompt. This is **not** a
semantic review or a monitor/recovery/access qualification. Resume checks only new
native bytes after its recorded offset.

Promotion remains the separate existing `fleet_reg.py promote` transaction, after
all runbook gates. Its scoped rollback restores the saved parked source row; the
pre-park original row in the handoff plan remains separately preserved.

Tests:

```sh
cd ~/Developer/hermes-fleet-manager
TMPDIR=$HOME/.hermes/cache/scratch PYTHONPATH=tests \
  $HOME/.hermes/cache/scratch/fleet-test-venv/bin/python -m pytest \
  tests/test_claude_handoff.py tests/test_claude_handoff_runtime.py \
  tests/test_reverse_promotion.py tests/test_fleet_promotion.py \
  tests/test_registry_transaction.py tests/test_overwatch_refresh.py -q
```

Tests use isolated temporary directories and stub process boundaries; they do not
prove Claude inference, live tmux usability, account/model access, or remote control.
