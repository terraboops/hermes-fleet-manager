#!/usr/bin/env python3
"""run_bridge_regression.py - the kanban <-> fleet bridge regression suite.

One command, no arguments, no live board and no real agent:

    python3 tests/run_bridge_regression.py

It runs the three bridge modules and maps their result onto the scenarios the
bridge contract promises:

  tests.test_fleet_kanban             unit tests over fakes: transport, claim
                                      loop, spawn, heartbeat, exit mapping,
                                      outage, restart adoption
  tests.test_fleet_kanban_integration the same loop against the stub board
                                      (real flock CAS) and REAL tmux: happy
                                      path, no work, two claimers racing, lease
                                      expiry and re-claim, non-zero exit, a
                                      session killed mid-run, board unreachable
  tests.test_atomic_claim_mutation    proves the race check goes red once the
                                      board's atomic claim is removed, so the
                                      guarantee cannot silently lose coverage

Everything runs against throwaway state in a temp dir and a private tmux server
(`tmux -L`), so a live fleet and the user's board are never touched. It is also
picked up by the repo entrypoint, `python3 -m unittest discover -s tests -t .`.
"""
from __future__ import annotations

import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

MODULES = (
    "tests.test_fleet_kanban",
    "tests.test_fleet_kanban_integration",
    "tests.test_atomic_claim_mutation",
)


def main(argv=None):
    loader = unittest.TestLoader()
    suite = unittest.TestSuite(loader.loadTestsFromName(name) for name in MODULES)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    print("")
    if result.wasSuccessful():
        print(f"bridge regression: PASS - {result.testsRun} tests")
        if result.skipped:
            print(f"  {len(result.skipped)} skipped (tmux not installed?)")
        return 0
    print(f"bridge regression: FAIL - {len(result.failures)} failures, "
          f"{len(result.errors)} errors, of {result.testsRun} tests")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
