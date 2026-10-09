#!/usr/bin/env python3
"""The claim-race check must fail when the atomic-claim guarantee is removed.

``tests/test_fleet_kanban_integration.py`` asserts that claimers racing one pool
claim each task exactly once. That assertion passes against the stub only
because the stub serializes its read-modify-write on an exclusive ``flock``.
This module removes that lock from a COPY of the stub and runs the very same
check against the mutant: it must observe a duplicate claim.

If a future change ever makes the race check insensitive to atomicity - it stops
comparing ownership, it reads the shared state file instead of each claimer's
own answer, it serializes the claimers itself - the mutant run here stops
producing duplicates and THIS test fails, so the guarantee cannot be quietly
dropped from the suite's coverage.

Run:  python3 -m unittest discover -s tests -t .
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))

from tests.claim_race import (  # noqa: E402
    add_task, claim_once_each, duplicate_claims, remove_atomicity,
)

STUB_KANBAN = os.path.join(HERE, "stub_kanban.py")
CLAIMERS = ["bc:fleet:a", "bc:fleet:b", "bc:fleet:c", "bc:fleet:d"]


class AtomicClaimIsLoadBearing(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="atomic-claim-")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _race(self, stub_path, task_id):
        state = os.path.join(self.tmp, "state.json")
        add_task(stub_path, state, task_id)
        return claim_once_each(stub_path, state, CLAIMERS)

    def test_the_real_stub_claims_a_contested_task_exactly_once(self):
        """The check the suite ships: four claimers, one task, one winner."""
        results = self._race(STUB_KANBAN, "t_contested")
        self.assertEqual(
            duplicate_claims(results), {},
            f"the stub handed one task to more than one claimer: {results}")
        winners = [w for w, tid in results.items() if tid]
        self.assertEqual(len(winners), 1,
                         f"expected exactly one winner, got {results}")
        self.assertEqual(results[winners[0]], "t_contested")

    def test_the_check_fails_once_the_atomic_guarantee_is_removed(self):
        """Same check, mutant stub: it MUST go red, or it is not a check."""
        mutant = remove_atomicity(STUB_KANBAN, os.path.join(self.tmp, "mutant_stub.py"))
        results = self._race(mutant, "t_contested")
        self.assertNotEqual(
            duplicate_claims(results), {},
            "the atomic-claim guarantee was removed and the race check still "
            f"passed; it is not testing atomicity any more: {results}")


if __name__ == "__main__":
    unittest.main()
