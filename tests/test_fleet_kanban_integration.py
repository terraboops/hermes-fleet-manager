#!/usr/bin/env python3
"""End-to-end tests for the fleet half of the bridge, against REAL tmux.

The unit tests use fakes; these use the real thing, because the parts that
actually break in production are the ones a fake cannot have: a session name
that tmux resolves to something else, environment variables that never reach the
pane (tmux gives a new pane the SERVER's environment, not the caller's), a
pointer line that does not land, and an exit status that is lost because the
process that would have recorded it was killed with the pane.

The board is tests/stub_kanban.py - the contract's JSON and exit codes over one
JSON state file, with a real flock, so a claim race between two processes here is
a claim race.

Every tmux call goes to a PRIVATE server (`tmux -L <socket>`): this machine runs
a live fleet, and a test must never touch its sessions.

Run:  python3 -m unittest discover -s tests -t .
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))

from scripts.fleet_kanban import Board, Supervisor, Tmux, session_name  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
STUB_KANBAN = os.path.join(HERE, "stub_kanban.py")
STUB_WORKER = os.path.join(HERE, "stub_worker.py")
DEADLINE = 25.0


class TmuxIntegration(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not shutil.which("tmux"):
            raise unittest.SkipTest("tmux is not installed")

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="fleet-kanban-e2e-")
        self.socket = f"fk-e2e-{os.getpid()}-{len(self._testMethodName)}"
        self.state = os.path.join(self.tmp, "stub-state.json")
        self.ws = os.path.join(self.tmp, "ws")
        self.tmux = Tmux(socket_name=self.socket)
        self.alerts = []

    def tearDown(self):
        subprocess.run(["tmux", "-L", self.socket, "kill-server"], capture_output=True)
        shutil.rmtree(self.tmp, ignore_errors=True)

    # -- helpers ------------------------------------------------------------
    def stub(self, *args, expect=0):
        argv = [sys.executable, STUB_KANBAN, "--state", self.state] + [str(a) for a in args]
        p = subprocess.run(argv, capture_output=True, text=True)
        self.assertEqual(p.returncode, expect, f"stub {args} exited {p.returncode}: {p.stderr}")
        return json.loads(p.stdout) if p.stdout.strip() else {}

    def board(self):
        return Board(argv_prefix=[sys.executable, STUB_KANBAN, "--state", self.state])

    def add_task(self, task_id="t_1", priority=0):
        workspace = os.path.join(self.ws, task_id)
        self.stub("add", "--id", task_id, "--title", f"task {task_id}",
                  "--body", "the body of the task", "--priority", priority,
                  "--workspace", workspace)
        return workspace

    def supervisor(self, task_id="t_1", exit_code=0, ttl=2, sentinel=None, worker_sleep=1,
                   **kw):
        return Supervisor(
            self.board(), self.tmux, worker_base="testhost:fleet",
            state_dir=self.tmp, ttl=ttl, poll_schedule=(0.2,),
            worker_command=sys.executable,
            worker_args=[STUB_WORKER, os.path.join(self.ws, task_id),
                         "--exit-code", str(exit_code), "--sleep", str(worker_sleep)] +
                        (["--sentinel", sentinel] if sentinel else []),
            alert=self.alerts.append, **kw)

    def wait_until(self, sup, predicate, deadline=DEADLINE):
        """Tick the supervisor until a condition holds."""
        end = time.time() + deadline
        while time.time() < end:
            sup.tick()
            if predicate():
                return True
            time.sleep(0.2)
        return predicate()

    def wait_for_settlement(self, sup, deadline=DEADLINE):
        """Tick until the fleet holds nothing, or the deadline passes."""
        end = time.time() + deadline
        while sup.held and time.time() < end:
            time.sleep(0.2)
            sup.tick()
        return not sup.held

    def session_exists(self, name):
        p = subprocess.run(["tmux", "-L", self.socket, "has-session", "-t", f"={name}:"],
                           capture_output=True, text=True)
        return p.returncode == 0

    def calls(self, verb):
        return [c for c in self.stub("calls").get("calls", []) if c["verb"] == verb]

    # -- the acceptance path -------------------------------------------------
    def test_claim_spawn_heartbeat_and_complete(self):
        self.add_task()
        sup = self.supervisor(ttl=1, worker_sleep=3)
        held = sup.claim_once()
        self.assertIsNotNone(held, "the fleet did not claim a ready task")

        # A VISIBLE session, named from the task id, in the task's workspace.
        self.assertTrue(self.session_exists("kb-t_1"), "no tmux session kb-t_1")
        windows = subprocess.run(["tmux", "-L", self.socket, "list-windows", "-t", "=kb-t_1:"],
                                 capture_output=True, text=True).stdout
        self.assertIn("worker", windows)
        panes = subprocess.run(["tmux", "-L", self.socket, "list-panes", "-t", "=kb-t_1:",
                                "-F", "#{pane_current_path}"],
                               capture_output=True, text=True).stdout.strip()
        self.assertEqual(os.path.realpath(panes), os.path.realpath(os.path.join(self.ws, "t_1")),
                         "the pane did not start in the workspace")

        self.assertTrue(self.wait_for_settlement(sup), "the worker never settled")

        state = self.stub("state")["tasks"]["t_1"]
        self.assertEqual(state["status"], "done")
        self.assertEqual(state["summary"], "stub worker finished")
        self.assertGreaterEqual(len(self.calls("heartbeat")), 2,
                                "the lease must be renewed at least once while the session lives")
        self.assertEqual(len(self.calls("claim-next")), 1)

        # The brief arrived as a pointer line, not a pasted body.
        pointer = open(os.path.join(self.ws, "t_1", ".kanban", "pointer-received")).read()
        self.assertIn("task.json", pointer)
        self.assertIn("t_1", pointer)
        self.assertNotIn("the body of the task", pointer)
        self.assertEqual(
            open(os.path.join(self.ws, "t_1", ".kanban", "exit-code")).read().strip(), "0")

    def test_board_variables_reach_the_pane(self):
        self.add_task()
        sup = self.supervisor()
        sup.claim_once()
        env = subprocess.run(["tmux", "-L", self.socket, "show-environment", "-t", "=kb-t_1:"],
                             capture_output=True, text=True).stdout
        self.assertIn("HERMES_KANBAN_TASK=t_1", env)
        self.assertIn("HERMES_KANBAN_CLAIM_LOCK=testhost:fleet:kb-t_1", env)
        self.wait_for_settlement(sup)

    def test_a_crashing_worker_fails_retryably(self):
        self.add_task()
        sup = self.supervisor(exit_code=3)
        sup.claim_once()
        self.assertTrue(self.wait_for_settlement(sup))
        self.assertEqual(self.stub("state")["tasks"]["t_1"]["status"], "ready")
        fail = self.calls("fail")[-1]
        self.assertTrue(fail["retryable"])
        self.assertTrue(fail["error"].startswith("exit=3:"), fail["error"])

    def test_a_worker_that_needs_input_blocks(self):
        self.add_task()
        sup = self.supervisor(exit_code=0, sentinel="NEEDS-INPUT-credentials")
        sup.claim_once()
        self.assertTrue(self.wait_for_settlement(sup))
        self.assertEqual(self.stub("state")["tasks"]["t_1"]["status"], "blocked")
        self.assertFalse(self.calls("fail")[-1]["retryable"])

    def test_an_outage_at_report_time_buffers_and_retries_after_recovery(self):
        self.add_task()
        sup = self.supervisor()
        sup.claim_once()
        self.stub("unreachable-set", "--verb", "complete")
        buffered = self.wait_until(
            sup, lambda: "t_1" in sup.held and bool(sup.held["t_1"].pending_report), deadline=15)
        self.assertTrue(buffered, "the report was not buffered during the outage")
        self.assertEqual(self.stub("state")["tasks"]["t_1"]["status"], "running")
        self.assertTrue(os.path.exists(
            os.path.join(self.ws, "t_1", ".kanban", "pending-report.json")))

        self.stub("unreachable-set", "--verb", "complete", "--off")
        self.assertTrue(self.wait_for_settlement(sup, deadline=15))
        self.assertEqual(self.stub("state")["tasks"]["t_1"]["status"], "done")
        self.assertEqual(sup.held, {})

    def test_an_outage_does_not_touch_a_running_pane(self):
        self.add_task()
        sup = self.supervisor(ttl=1, worker_sleep=8)
        sup.claim_once()
        self.stub("unreachable-set", "--verb", "heartbeat")
        for _ in range(3):
            time.sleep(0.4)
            sup.tick()
        self.assertTrue(self.session_exists("kb-t_1"), "the pane must survive a board outage")
        self.assertEqual(len(self.alerts), 1)
        self.stub("unreachable-set", "--verb", "heartbeat", "--off")
        self.assertTrue(self.wait_for_settlement(sup, deadline=25))
        self.assertEqual(self.stub("state")["tasks"]["t_1"]["status"], "done")

    def test_two_instances_never_dispatch_the_same_task_twice(self):
        self.add_task()
        first = self.supervisor()
        second = Supervisor(self.board(), Tmux(socket_name=self.socket),
                            worker_base="otherhost:fleet", state_dir=self.tmp,
                            ttl=2, poll_schedule=(0.2,),
                            worker_command=sys.executable,
                            worker_args=[STUB_WORKER, os.path.join(self.ws, "t_1"),
                                         "--sleep", "1"],
                            log=lambda _m: None)

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda s: s.claim_once(), [first, second]))
        winners = [s for s, r in zip([first, second], results) if r is not None]
        self.assertEqual(len(winners), 1, "a claim race produced two workers for one task")
        self.assertEqual(len(self.calls("claim-next")), 2, "both instances must have polled")
        self.assertTrue(self.wait_for_settlement(winners[0]))
        self.assertEqual(self.stub("state")["tasks"]["t_1"]["status"], "done")

    def test_a_restart_adopts_the_live_session_and_settles_it(self):
        self.add_task()
        first = self.supervisor()
        first.claim_once()
        second = self.supervisor()
        second.reconcile()
        self.assertIn("t_1", second.held)
        self.assertEqual(self.calls("claim-next"), [c for c in self.calls("claim-next")],
                         "reconcile must not re-claim")
        self.assertEqual(len(self.calls("claim-next")), 1)
        self.assertTrue(self.wait_for_settlement(second))
        self.assertEqual(self.stub("state")["tasks"]["t_1"]["status"], "done")


if __name__ == "__main__":
    unittest.main()
