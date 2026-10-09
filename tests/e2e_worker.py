#!/usr/bin/env python3
"""e2e_worker.py - the worker stand-in for the real-board end-to-end run.

Unlike tests/stub_worker.py (which only proves that a pointer line arrived), this
one PROVES the whole handoff, because the four things that silently break are all
invisible from the fleet side:

  * the pointer line actually reached the pane (it is read from stdin, which is
    the pane's tty);
  * the board's HERMES_KANBAN_* environment reached the pane (tmux gives a new
    pane the SERVER's environment, so these travel with `-e`);
  * the claim record at <workspace>/.kanban/task.json is readable and carries the
    task body;
  * the exit status is recorded where the fleet looks for it (the generated
    .kanban/worker.sh wrapper writes .kanban/exit-code).

It writes .kanban/worker-receipt.json with everything it saw, then summary.md
(which the fleet reports as the task result) and exits with the asked status.

Usage (launched by the fleet, cwd = the task workspace):
    e2e_worker.py [--sleep S] [--exit-code N] [--sentinel TEXT] [--no-stdin]
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time

ENV_KEYS = (
    "HERMES_KANBAN_TASK",
    "HERMES_KANBAN_DB",
    "HERMES_KANBAN_BOARD",
    "HERMES_KANBAN_WORKSPACE",
    "HERMES_KANBAN_RUN_ID",
    "HERMES_KANBAN_CLAIM_LOCK",
    "HERMES_KANBAN_WORKER_ID",
)


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--sleep", type=float, default=0.0)
    parser.add_argument("--exit-code", type=int, default=0)
    parser.add_argument("--sentinel", default=None)
    parser.add_argument("--no-stdin", action="store_true")
    args = parser.parse_args(argv)

    # The wrapper cds into the workspace; the env var is the fleet's own copy.
    ws = os.environ.get("HERMES_KANBAN_WORKSPACE") or os.getcwd()
    kdir = os.path.join(ws, ".kanban")
    os.makedirs(kdir, exist_ok=True)

    pointer = ""
    if not args.no_stdin:
        pointer = sys.stdin.readline().strip()
        with open(os.path.join(kdir, "pointer-received"), "w") as fh:
            fh.write(pointer + "\n")

    try:
        with open(os.path.join(kdir, "task.json")) as fh:
            claim = json.load(fh)
    except (OSError, ValueError):
        claim = {}

    receipt = {
        "pointer": pointer,
        "cwd": os.getcwd(),
        "env": {key: os.environ.get(key) for key in ENV_KEYS},
        "claim": claim,
    }
    with open(os.path.join(kdir, "worker-receipt.json"), "w") as fh:
        json.dump(receipt, fh, indent=2, sort_keys=True)

    if args.sleep:
        time.sleep(args.sleep)

    summary = args.sentinel or (
        f"worker completed {claim.get('id')}; body={claim.get('body')!r}"
    )
    with open(os.path.join(kdir, "summary.md"), "w") as fh:
        fh.write(summary + "\n")
    with open(os.path.join(kdir, "metadata.json"), "w") as fh:
        json.dump({"worker": "e2e_worker", "saw_task": claim.get("id")}, fh)
    if args.exit_code:
        sys.stderr.write(f"e2e_worker failing on purpose: exit {args.exit_code}\n")
    return args.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
