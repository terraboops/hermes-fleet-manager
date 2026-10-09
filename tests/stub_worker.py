#!/usr/bin/env python3
"""stub_worker.py - the stand-in harness a test launches inside a tmux session.

It behaves like a long-lived agent harness in the two ways the fleet's spawner
depends on: it is interactive (it READS the pointer line the fleet types into
the pane, which is how a real session receives its brief) and it exits with a
status of its choosing (which is what the fleet maps to complete/fail).

Usage:  stub_worker.py <workspace> [--exit-code N] [--sleep S] [--sentinel TEXT]

On a line read from stdin it records the line to <workspace>/.kanban/pointer-received,
then writes <workspace>/.kanban/summary.md and exits with --exit-code.
"""
from __future__ import annotations

import argparse
import os
import sys
import time


def main(argv=None):
    p = argparse.ArgumentParser()
    p.add_argument("workspace")
    p.add_argument("--exit-code", type=int, default=0)
    p.add_argument("--sleep", type=float, default=0.0)
    p.add_argument("--sentinel", default=None)
    p.add_argument("--no-stdin", action="store_true",
                   help="exit without reading the pointer line (worker that never gets one)")
    args = p.parse_args(argv)

    kdir = os.path.join(args.workspace, ".kanban")
    os.makedirs(kdir, exist_ok=True)

    if not args.no_stdin:
        line = sys.stdin.readline().strip()
        with open(os.path.join(kdir, "pointer-received"), "w") as fh:
            fh.write(line + "\n")

    if args.sleep:
        time.sleep(args.sleep)

    summary = args.sentinel or "stub worker finished"
    with open(os.path.join(kdir, "summary.md"), "w") as fh:
        fh.write(summary + "\n")
    if args.exit_code:
        sys.stderr.write(f"stub worker failing: exit {args.exit_code}\n")
    return args.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
