#!/usr/bin/env python3
"""claim_race.py - the claim-race check, and the mutation that proves it bites.

The bridge's whole safety story is one sentence: two claimers racing one pool
claim each task exactly once, because the claim is a compare-and-set. A check
that asserts that is only worth having if it goes RED when the guarantee is
taken away, so this module carries both halves:

  * ``claim_once_each`` - drive N ``claim-next`` processes at one stub board
    concurrently and collect what each came away with. Concurrency here is real
    processes against a real state file, not threads against a fake.
  * ``remove_atomicity`` - write a copy of the stub with its ``flock`` swapped
    for a sleep: the same read-modify-write with no serialization, which is
    precisely the guarantee removed.

``tests/test_fleet_kanban_integration.py`` runs the check against the real stub
(it must pass). ``tests/test_atomic_claim_mutation.py`` runs it against the
mutant (it must fail), so the check can never quietly stop testing anything.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys

ATOMICITY_NEEDLE = "fcntl.flock(lock, fcntl.LOCK_EX)\n        data = load(path)"
TMP_NEEDLE = "    tmp = path + \".tmp\""
MUTANT_MARKER = "# MUTANT: atomicity removed"


def add_task(stub_path, state_path, task_id, priority=0):
    """Seed one ready task into the stub board."""
    argv = [sys.executable, stub_path, "--state", state_path,
            "add", "--id", task_id, "--title", f"task {task_id}",
            "--priority", str(priority)]
    p = subprocess.run(argv, capture_output=True, text=True)
    if p.returncode != 0:
        raise AssertionError(f"stub add failed ({p.returncode}): {p.stderr}")


def claim_once_each(stub_path, state_path, workers, timeout=30):
    """Every worker's ``claim-next`` starts at the same time; return
    ``{worker: task_id_or_None}`` from each process's own stdout.

    Each claimer's answer is read from its OWN output, never from the shared
    state file: with the guarantee removed the writers clobber one another, so
    the state file would show only the last writer's claims and hide the very
    duplicate this check exists to catch.
    """
    procs = []
    for worker in workers:
        argv = [sys.executable, stub_path, "--state", state_path,
                "claim-next", "--worker", worker, "--json"]
        procs.append((worker, subprocess.Popen(argv, stdout=subprocess.PIPE,
                                               stderr=subprocess.PIPE, text=True)))
    results = {}
    for worker, proc in procs:
        stdout, stderr = proc.communicate(timeout=timeout)
        if proc.returncode != 0:
            raise AssertionError(f"stub claim-next ({worker}) failed: {stderr}")
        task_id = None
        for line in reversed([ln for ln in stdout.splitlines() if ln.strip()]):
            try:
                value = json.loads(line)
            except ValueError:
                continue
            if isinstance(value, dict):
                task_id = (value.get("task") or {}).get("id")
                break
        results[worker] = task_id
    return results


def duplicate_claims(results):
    """Task ids that more than one claimer came away with. Empty is the pass."""
    counts = {}
    for task_id in results.values():
        if task_id:
            counts[task_id] = counts.get(task_id, 0) + 1
    return {task_id: n for task_id, n in counts.items() if n > 1}


def remove_atomicity(src_path, dest_path, delay=1.5):
    """Write a copy of the stub with its exclusive lock removed.

    Two edits, because a board with no serialization needs both to be a fair
    mutant rather than a crashing one:

      * the flock goes, and a sleep takes its place BETWEEN the read and the
        write (the lock used to sit just before the read), so every claimer sees
        the same ready list before any of them writes and all of them win the
        same task;
      * the temp file each writer renames gets a per-process name, so two
        concurrent writers stop clobbering one another's temp and the mutant can
        actually finish its non-atomic claim instead of dying on a rename.

    A plain deletion of the lock would race by luck, and a flaky proof is no
    proof.
    """
    with open(src_path) as fh:
        src = fh.read()
    for needle in (ATOMICITY_NEEDLE, TMP_NEEDLE):
        if needle not in src:
            raise AssertionError(
                f"the stub no longer contains {needle!r}; update this mutation "
                f"or the check it proves has stopped tracking the guard")
    mutated = src.replace(
        ATOMICITY_NEEDLE,
        f"data = load(path)\n        time.sleep({delay})  {MUTANT_MARKER}")
    mutated = mutated.replace(
        TMP_NEEDLE, f'    tmp = f"{{path}}.{{os.getpid()}}.tmp"  {MUTANT_MARKER}')
    if "fcntl.flock" in mutated:
        raise AssertionError("the mutation left another lock behind")
    os.makedirs(os.path.dirname(os.path.abspath(dest_path)), exist_ok=True)
    with open(dest_path, "w") as fh:
        fh.write(mutated)
    return dest_path
