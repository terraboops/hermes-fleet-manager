#!/usr/bin/env python3
"""Tests for the fleet half of the kanban <-> fleet bridge (scripts/fleet_kanban.py).

The failure each group guards:

  * no-work / race-lost  - a poll loop that turns "nothing to do" into a busy
    loop, or a claim race into two workers on one task.
  * spawn                - a session whose name is not derived from the task id
    (so two instances cannot agree on it), a body pasted instead of a pointer
    line, or board variables that never reach the pane.
  * heartbeat            - a lease that is never renewed (the board reclaims a
    task whose worker is still running), or one renewed for a lease we lost.
  * exit mapping         - a worker crash reported as success, or a killed
    session reported as a failure (we do not know that its work failed).
  * board unreachable    - a report thrown away, a pane killed while the board
    is down, or a silent outage nobody is told about.

Run:  python3 -m unittest discover -s tests -t .
"""
from __future__ import annotations

import json
import os
import shutil
import stat
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))

from scripts.fleet_kanban import (  # noqa: E402
    Board, BoardError, BoardUnreachable, HeldTask, Supervisor, Tmux, build_supervisor,
    parse_args, session_name,
)


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------
class FakeBoard:
    """The contract, in memory, with the same compare-and-set semantics."""

    def __init__(self):
        self.tasks = {}
        self.calls = []
        self.heartbeats = {}
        self.unreachable = set()   # verbs that answer exit 3
        self.fatal = set()         # verbs that answer exit 2
        self.lease = 10_000
        self.tmpdir = tempfile.gettempdir()
        self.last_response = {}

    def add(self, task_id, priority=0, workspace=None, status="ready"):
        self.tasks[task_id] = {
            "id": task_id, "title": f"task {task_id}", "body": "the body",
            "assignee": "default", "status": status, "priority": priority,
            "workspace_kind": "scratch",
            "workspace_path": workspace or os.path.join(self.tmpdir, "ws", task_id),
            "created_at": len(self.tasks), "current_run_id": 7, "claim_lock": None,
            "claim_expires": None,
        }
        return self.tasks[task_id]

    def _guard(self, verb):
        if verb in self.unreachable:
            raise BoardUnreachable(f"stub: {verb} unreachable")
        if verb in self.fatal:
            raise BoardError(f"stub: {verb} refused")
    def _owner(self, task_id, worker):
        task = self.tasks.get(task_id)
        return task if task and task.get("claim_lock") == worker else None

    def claim_next(self, worker, ttl=None, status=None):
        self.calls.append(("claim-next", worker))
        self._guard("claim-next")
        ready = [t for t in self.tasks.values()
                 if t["status"] == (status or "ready") and not t.get("claim_lock")]
        ready.sort(key=lambda t: (-t["priority"], t["created_at"]))
        if not ready:
            return {"ok": True, "task": None, "lease_expires": None}
        task = ready[0]
        lock = f"{worker}:kb-{task['id']}"
        task["status"] = "running"
        task["claim_lock"] = lock
        task["claim_expires"] = self.lease
        return {"ok": True, "task": dict(task), "lease_expires": self.lease}

    def heartbeat(self, task_id, worker, note=None, ttl=None):
        self.calls.append(("heartbeat", task_id, worker))
        self._guard("heartbeat")
        if not self._owner(task_id, worker):
            return {"ok": False, "reason": "lost_claim"}
        self.heartbeats[task_id] = self.heartbeats.get(task_id, 0) + 1
        self.tasks[task_id]["claim_expires"] = self.lease
        return {"ok": True, "lease_expires": self.lease}

    def release(self, task_id, worker, reason):
        self.calls.append(("release", task_id, worker, reason))
        self._guard("release")
        task = self.tasks.get(task_id)
        if task is None:
            raise BoardError("unknown task")
        if task["status"] in ("done", "archived"):
            return {"ok": True, "status": task["status"], "already_terminal": True}
        if task.get("claim_lock") != worker:
            raise BoardError("not the lease owner")
        task["status"] = "ready"
        task["claim_lock"] = None
        return {"ok": True, "status": "ready", "already_terminal": False}

    def complete(self, task_id, worker, summary=None, result=None, metadata=None):
        self.calls.append(("complete", task_id, worker, summary, result, metadata))
        self._guard("complete")
        task = self.tasks.get(task_id)
        if task is None:
            raise BoardError("unknown task")
        if task["status"] in ("done", "blocked", "archived"):
            resp = {"ok": True, "status": task["status"], "already_terminal": True}
        elif task.get("claim_lock") != worker:
            raise BoardError("not the lease owner")
        else:
            task["status"] = "done"
            task["claim_lock"] = None
            task["summary"] = summary
            resp = {"ok": True, "status": "done", "already_terminal": False}
        self.last_response = resp
        return resp

    def fail(self, task_id, worker, error, retryable=False):
        self.calls.append(("fail", task_id, worker, error, bool(retryable)))
        self._guard("fail")
        task = self.tasks.get(task_id)
        if task is None:
            raise BoardError("unknown task")
        if task["status"] in ("done", "blocked", "archived"):
            resp = {"ok": True, "status": task["status"], "already_terminal": True}
        elif task.get("claim_lock") != worker:
            raise BoardError("not the lease owner")
        else:
            task["status"] = "ready" if retryable else "blocked"
            task["claim_lock"] = None
            resp = {"ok": True, "status": task["status"], "already_terminal": False}
        self.last_response = resp
        return resp

    def show(self, task_id):
        return {"ok": True, "task": self.tasks.get(task_id)}

    def verbs(self):
        return [c[0] for c in self.calls]

    def calls_of(self, verb):
        return [c for c in self.calls if c[0] == verb]


class FakeTmux:
    """The session surface, with the same exact-target discipline."""

    def __init__(self):
        self.sessions = {}
        self.lines = []
        self.killed = []

    def has_session(self, name):
        return name in self.sessions

    def new_session(self, name, cwd, command, env=None):
        self.sessions[name] = {"cwd": cwd, "command": command, "env": dict(env or {})}
        return name

    def kill_session(self, name):
        self.sessions.pop(name, None)
        self.killed.append(name)

    def session_env(self, name, key):
        """What the session was CREATED with (tmux `-e`), like the real server."""
        session = self.sessions.get(name)
        return session["env"].get(key) if session else None

    def send_line(self, name, text, verify=None, tries=3, settle=0.4):
        self.lines.append((name, text))
        return True

    def die(self, name):
        """The pane's process exited: the session is simply gone."""
        self.sessions.pop(name, None)


class Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t

    def advance(self, dt):
        self.t += dt


class Sleeps:
    def __init__(self):
        self.intervals = []

    def __call__(self, seconds):
        self.intervals.append(seconds)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="fleet-kanban-test-")
        self.board = FakeBoard()
        self.board.tmpdir = self.tmp
        self.tmux = FakeTmux()
        self.clock = Clock()
        self.sleeps = Sleeps()
        self.alerts = []

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def supervisor(self, **kw):
        kw.setdefault("worker_base", "testhost:fleet")
        kw.setdefault("worker_command", "true")
        return Supervisor(self.board, self.tmux, state_dir=self.tmp,
                          clock=self.clock, sleep=self.sleeps,
                          alert=self.alerts.append, log=lambda _m: None, **kw)

    def workdir(self, task_id):
        return os.path.join(self.tmp, "ws", task_id)

    def finish_worker(self, task_id, exit_code=0, summary=None, stderr=None):
        """Write what a finished wrapper leaves behind."""
        kdir = os.path.join(self.workdir(task_id), ".kanban")
        os.makedirs(kdir, exist_ok=True)
        with open(os.path.join(kdir, "exit-code"), "w") as fh:
            fh.write(str(exit_code))
        if summary is not None:
            with open(os.path.join(kdir, "summary.md"), "w") as fh:
                fh.write(summary + "\n")
        if stderr is not None:
            with open(os.path.join(kdir, "worker.log"), "a") as fh:
                fh.write(stderr + "\n")
        self.tmux.die(session_name(task_id))


# ---------------------------------------------------------------------------
# Transport: the board CLI is a subprocess, and the contract's exit codes mean
# something. Getting this wrong is how a usage error becomes an infinite retry.
# ---------------------------------------------------------------------------
class TestBoardClient(unittest.TestCase):
    def board(self, rc, out, capture=None):
        def runner(argv, env, timeout):
            if capture is not None:
                capture["argv"] = argv
                capture["env"] = env
            return rc, out, ""

        return Board(argv_prefix=["hermes", "kanban"], runner=runner)

    def test_json_is_requested_and_the_verb_is_passed(self):
        seen = {}
        board = self.board(0, '{"ok":true,"task":null,"lease_expires":null}', seen)
        board.claim_next("bc:fleet", ttl=900)
        self.assertEqual(seen["argv"][:3], ["hermes", "kanban", "claim-next"])
        self.assertEqual(seen["argv"][-1], "--json")
        self.assertIn("bc:fleet", seen["argv"])

    def test_exit_3_is_unreachable_and_exit_2_is_fatal(self):
        with self.assertRaises(BoardUnreachable):
            self.board(3, '{"ok":false,"reason":"db locked"}').claim_next("bc:fleet")
        with self.assertRaises(BoardError):
            self.board(2, '{"ok":false,"reason":"not the lease owner"}').claim_next("bc:fleet")

    def test_delegated_child_context_is_stripped_from_the_child_env(self):
        seen = {}
        os.environ["HERMES_DELEGATED_CHILD_CONTEXT"] = "1"
        try:
            self.board(0, '{"ok":true,"task":null}', seen).claim_next("bc:fleet")
        finally:
            os.environ.pop("HERMES_DELEGATED_CHILD_CONTEXT", None)
        self.assertNotIn("HERMES_DELEGATED_CHILD_CONTEXT", seen["env"])

    def test_unparseable_stdout_is_a_fatal_refusal_not_a_crash(self):
        with self.assertRaises(BoardError):
            self.board(0, "Traceback (most recent call last):").claim_next("bc:fleet")


# ---------------------------------------------------------------------------
# Claim loop
# ---------------------------------------------------------------------------
class TestClaimLoop(Base):
    def test_no_work_backs_off_and_spawns_nothing(self):
        sup = self.supervisor(poll_schedule=(5, 10, 30, 60))
        sup.run(max_ticks=4)
        self.assertEqual(self.tmux.sessions, {})
        self.assertEqual(self.sleeps.intervals, [5, 10, 30])
        self.assertEqual(len(self.board.calls_of("claim-next")), 4)

    def test_claim_race_lost_never_spawns_a_second_worker(self):
        self.board.add("t_1")
        first = self.supervisor()
        second = Supervisor(self.board, FakeTmux(), worker_base="testhost:fleet",
                            state_dir=self.tmp, clock=self.clock, sleep=self.sleeps,
                            log=lambda _m: None)
        self.assertIsNotNone(first.claim_once())
        self.assertIsNone(second.claim_once())
        self.assertEqual(list(first.tmux.sessions), ["kb-t_1"])
        self.assertEqual(second.tmux.sessions, {})
        self.assertEqual(second.held, {})
        self.assertEqual(second._null_streak, 1)

    def test_a_fatal_claim_refusal_does_not_busy_loop(self):
        self.board.fatal.add("claim-next")
        sup = self.supervisor(poll_schedule=(5, 10))
        sup.run(max_ticks=3)
        self.assertEqual(len(self.board.calls_of("claim-next")), 3)
        self.assertEqual(self.tmux.sessions, {})

    def test_board_unreachable_at_claim_is_not_a_task_outcome(self):
        self.board.add("t_1")
        self.board.unreachable.add("claim-next")
        sup = self.supervisor()
        sup.run(max_ticks=2)
        self.assertEqual(self.tmux.sessions, {})
        self.assertEqual(self.board.tasks["t_1"]["status"], "ready")

    def test_capacity_is_never_exceeded_and_a_claim_past_the_cap_is_released(self):
        self.board.add("t_1")
        sup = self.supervisor(max_held=2)
        sup.claim_once()
        self.assertEqual(len(sup.held), 1)

        # Capacity consumed while the claim was in flight: the task comes back
        # after the pre-check, and must be handed straight back.
        self.board.add("t_2")
        real = self.board.claim_next

        def sneak(worker, ttl=None, status=None):
            sup.held["t_other"] = HeldTask("t_other", "kb-t_other", "/tmp", "x", 900)
            return real(worker, ttl=ttl, status=status)

        self.board.claim_next = sneak
        self.assertIsNone(sup.claim_once())
        self.assertEqual(self.board.tasks["t_2"]["status"], "ready")
        self.assertNotIn("kb-t_2", self.tmux.sessions)
        self.assertEqual(self.board.calls_of("release")[-1][3], "concurrency cap reached")


# ---------------------------------------------------------------------------
# Spawn / handoff
# ---------------------------------------------------------------------------
class TestSpawn(Base):
    def test_session_name_is_derived_from_the_task_id_and_workdir_is_the_workspace(self):
        self.board.add("t_1")
        sup = self.supervisor()
        held = sup.claim_once()
        self.assertIsNotNone(held)
        self.assertEqual(held.session, "kb-t_1")
        self.assertEqual(list(self.tmux.sessions), ["kb-t_1"])
        self.assertEqual(self.tmux.sessions["kb-t_1"]["cwd"], self.workdir("t_1"))

    def test_board_variables_reach_the_pane_and_carry_the_lease_owner(self):
        self.board.add("t_1")
        sup = self.supervisor()
        sup.claim_once()
        env = self.tmux.sessions["kb-t_1"]["env"]
        self.assertEqual(env["HERMES_KANBAN_TASK"], "t_1")
        self.assertEqual(env["HERMES_KANBAN_CLAIM_LOCK"], "testhost:fleet:kb-t_1")
        self.assertEqual(env["HERMES_KANBAN_WORKER_ID"], env["HERMES_KANBAN_CLAIM_LOCK"])
        self.assertEqual(env["HERMES_KANBAN_RUN_ID"], 7)

    def test_claim_record_is_private_and_the_pointer_is_a_line_not_the_body(self):
        self.board.add("t_1")
        sup = self.supervisor()
        sup.claim_once()
        kdir = os.path.join(self.workdir("t_1"), ".kanban")
        record = os.path.join(kdir, "task.json")
        self.assertTrue(os.path.exists(record))
        mode = stat.S_IMODE(os.stat(record).st_mode)
        self.assertEqual(mode, 0o600, f"task.json mode {oct(mode)} is world-readable")
        payload = json.loads(open(record).read())
        self.assertEqual(payload["id"], "t_1")
        self.assertEqual(payload["worker_id"], "testhost:fleet:kb-t_1")

        session, line = self.tmux.lines[-1]
        self.assertEqual(session, "kb-t_1")
        self.assertIn(record, line)
        self.assertIn("t_1", line)
        self.assertNotIn("the body", line, "the task body must never be pasted into the pane")

    def test_the_pane_command_runs_the_harness_and_records_its_exit_status(self):
        self.board.add("t_1")
        sup = self.supervisor(worker_command="my-harness", worker_args=["--flag"])
        sup.claim_once()
        script = open(os.path.join(self.workdir("t_1"), ".kanban", "worker.sh")).read()
        self.assertIn("'my-harness' '--flag'", script)
        self.assertIn(".kanban/exit-code", script)
        self.assertTrue(self.tmux.sessions["kb-t_1"]["command"].startswith("sh "))

    def test_stale_exit_code_from_a_previous_run_is_cleared(self):
        self.board.add("t_1")
        kdir = os.path.join(self.workdir("t_1"), ".kanban")
        os.makedirs(kdir, exist_ok=True)
        with open(os.path.join(kdir, "exit-code"), "w") as fh:
            fh.write("1")
        sup = self.supervisor()
        sup.claim_once()
        self.assertFalse(os.path.exists(os.path.join(kdir, "exit-code")))


# ---------------------------------------------------------------------------
# Heartbeat
# ---------------------------------------------------------------------------
class TestHeartbeat(Base):
    def test_the_lease_is_renewed_on_claim_and_again_at_ttl_over_three(self):
        self.board.add("t_1")
        sup = self.supervisor(ttl=900)
        sup.claim_once()
        self.assertEqual(self.board.heartbeats["t_1"], 1, "claim must be followed by a heartbeat")

        self.clock.advance(900 / 3 - 1)
        sup.tick()
        self.assertEqual(self.board.heartbeats["t_1"], 1, "heartbeat too early")
        self.clock.advance(2)
        sup.tick()
        self.assertEqual(self.board.heartbeats["t_1"], 2)

    def test_the_heartbeat_carries_the_lease_owner_not_the_base_id(self):
        self.board.add("t_1")
        sup = self.supervisor()
        sup.claim_once()
        self.clock.advance(400)
        sup.tick()
        self.assertEqual(self.board.calls_of("heartbeat")[-1][2], "testhost:fleet:kb-t_1")

    def test_a_dead_session_is_not_heartbeated(self):
        self.board.add("t_1")
        sup = self.supervisor()
        sup.claim_once()
        self.tmux.die("kb-t_1")
        self.finish_worker("t_1", exit_code=0, summary="done")
        self.clock.advance(400)
        sup.tick()
        self.assertEqual(self.board.heartbeats["t_1"], 1)


# ---------------------------------------------------------------------------
# Exit mapping: the report is the whole point of holding the lease
# ---------------------------------------------------------------------------
class TestExitMapping(Base):
    def test_exit_zero_reports_complete_with_the_summary(self):
        self.board.add("t_1")
        sup = self.supervisor()
        sup.claim_once()
        self.finish_worker("t_1", exit_code=0, summary="implemented the thing")
        sup.tick()
        self.assertEqual(self.board.tasks["t_1"]["status"], "done")
        call = self.board.calls_of("complete")[-1]
        self.assertEqual(call[3], "implemented the thing")
        self.assertEqual(sup.held, {})

    def test_metadata_is_passed_through_when_the_worker_left_some(self):
        self.board.add("t_1")
        sup = self.supervisor()
        sup.claim_once()
        with open(os.path.join(self.workdir("t_1"), ".kanban", "metadata.json"), "w") as fh:
            fh.write('{"tests_run": 12}')
        self.finish_worker("t_1", exit_code=0, summary="ok")
        sup.tick()
        self.assertEqual(json.loads(self.board.calls_of("complete")[-1][5]), {"tests_run": 12})

    def test_nonzero_exit_reports_a_retryable_failure_with_the_last_stderr_line(self):
        self.board.add("t_1")
        sup = self.supervisor()
        sup.claim_once()
        self.finish_worker("t_1", exit_code=3, stderr="Error: could not reach the API")
        sup.tick()
        call = self.board.calls_of("fail")[-1]
        self.assertEqual(call[3], "exit=3: Error: could not reach the API")
        self.assertTrue(call[4])
        self.assertEqual(self.board.tasks["t_1"]["status"], "ready")

    def test_a_needs_input_sentinel_blocks_instead_of_retrying(self):
        self.board.add("t_1")
        sup = self.supervisor()
        sup.claim_once()
        self.finish_worker("t_1", exit_code=0, summary="NEEDS-INPUT-credentials")
        sup.tick()
        call = self.board.calls_of("fail")[-1]
        self.assertIn("needs-input", call[3])
        self.assertFalse(call[4], "needs-input must not be retryable")
        self.assertEqual(self.board.tasks["t_1"]["status"], "blocked")

    def test_a_session_that_died_without_an_exit_status_is_released_not_failed(self):
        self.board.add("t_1")
        sup = self.supervisor()
        sup.claim_once()
        self.tmux.die("kb-t_1")   # killed: the wrapper never wrote a status
        sup.tick()
        self.assertEqual(self.board.verbs().count("complete"), 0)
        self.assertEqual(self.board.verbs().count("fail"), 0)
        call = self.board.calls_of("release")[-1]
        self.assertEqual(call[3], "session died")
        self.assertEqual(self.board.tasks["t_1"]["status"], "ready")
        self.assertEqual(sup.held, {})

    def test_a_worker_that_reported_itself_is_not_double_counted(self):
        self.board.add("t_1")
        sup = self.supervisor()
        sup.claim_once()
        lock = "testhost:fleet:kb-t_1"
        self.board.complete("t_1", lock, summary="the worker reported")
        self.finish_worker("t_1", exit_code=0, summary="the worker reported")
        sup.tick()
        # The fleet still asks, because asking is how it learns the task is
        # already terminal - the verbs are idempotent by contract, so the second
        # report must be a no-op, not a second close.
        self.assertTrue(self.board.last_response.get("already_terminal"))
        self.assertEqual(self.board.tasks["t_1"]["status"], "done")
        self.assertEqual(sup.held, {})


# ---------------------------------------------------------------------------
# Board unreachable / lost claim: the two ways a task gets stuck
# ---------------------------------------------------------------------------
class TestBoardOutage(Base):
    def test_an_outage_keeps_the_worker_and_alerts_exactly_once(self):
        self.board.add("t_1")
        sup = self.supervisor(ttl=900)
        sup.claim_once()
        self.board.unreachable.add("heartbeat")
        for _ in range(3):
            self.clock.advance(400)
            sup.tick()
        self.assertTrue(self.tmux.has_session("kb-t_1"), "the pane must not be touched")
        self.assertEqual(len(self.alerts), 1)
        self.assertTrue(sup.held["t_1"].alerted)
        for _ in range(2):
            self.clock.advance(400)
            sup.tick()
        self.assertEqual(len(self.alerts), 1, "one alert per condition, not one per tick")

    def test_recovery_clears_the_unmanaged_mark(self):
        self.board.add("t_1")
        sup = self.supervisor()
        sup.claim_once()
        self.board.unreachable.add("heartbeat")
        for _ in range(3):
            self.clock.advance(400)
            sup.tick()
        self.board.unreachable.discard("heartbeat")
        self.clock.advance(400)
        sup.tick()
        self.assertFalse(sup.held["t_1"].alerted)

    def test_a_lost_claim_kills_the_session_and_never_reports(self):
        self.board.add("t_1")
        sup = self.supervisor()
        sup.claim_once()
        self.board.tasks["t_1"]["claim_lock"] = "someone-else"   # reclaimed underneath us
        self.clock.advance(400)
        sup.tick()
        self.assertEqual(self.tmux.killed, ["kb-t_1"])
        self.assertEqual(sup.held, {})
        self.assertEqual(self.board.verbs().count("complete"), 0)
        self.assertEqual(self.board.verbs().count("fail"), 0)

    def test_a_fatal_heartbeat_refusal_is_treated_as_a_lost_claim(self):
        self.board.add("t_1")
        sup = self.supervisor()
        sup.claim_once()
        self.board.fatal.add("heartbeat")
        self.clock.advance(400)
        sup.tick()
        self.assertEqual(self.tmux.killed, ["kb-t_1"])
        self.assertEqual(sup.held, {})

    def test_an_unreachable_board_at_report_time_buffers_the_report_and_retries(self):
        self.board.add("t_1")
        sup = self.supervisor()
        sup.claim_once()
        self.finish_worker("t_1", exit_code=0, summary="finished")
        self.board.unreachable.add("complete")
        sup.tick()
        pending = os.path.join(self.workdir("t_1"), ".kanban", "pending-report.json")
        self.assertTrue(os.path.exists(pending), "the report must survive the outage")
        self.assertIn("t_1", sup.held, "a task with an undelivered report is still ours")
        self.assertEqual(self.board.tasks["t_1"]["status"], "running")

        self.board.unreachable.discard("complete")
        sup.tick()
        self.assertEqual(self.board.tasks["t_1"]["status"], "done")
        self.assertFalse(os.path.exists(pending))
        self.assertEqual(sup.held, {})

    def test_an_unreachable_board_at_release_time_leaves_the_lease_to_expire(self):
        self.board.add("t_1")
        sup = self.supervisor()
        sup.claim_once()
        self.tmux.die("kb-t_1")
        self.board.unreachable.add("release")
        sup.tick()   # must not raise, and must not invent a task outcome
        self.assertEqual(self.board.verbs().count("complete"), 0)
        self.assertEqual(self.board.verbs().count("fail"), 0)
        self.assertIn("lease expiry", open(sup.log_path).read())


# ---------------------------------------------------------------------------
# Restart
# ---------------------------------------------------------------------------
class TestReconcile(Base):
    def test_a_live_session_is_adopted_after_a_restart(self):
        self.board.add("t_1")
        first = self.supervisor()
        first.claim_once()
        second = self.supervisor()          # same tmux, same state dir
        second.reconcile()
        self.assertIn("t_1", second.held)
        self.clock.advance(400)
        second.tick()
        self.assertEqual(self.board.heartbeats["t_1"], 2)

    def test_a_session_that_died_during_the_restart_is_settled(self):
        self.board.add("t_1")
        first = self.supervisor()
        first.claim_once()
        self.finish_worker("t_1", exit_code=0, summary="done before the restart")
        second = self.supervisor()
        second.reconcile()
        self.assertEqual(self.board.tasks["t_1"]["status"], "done")
        self.assertEqual(second.held, {})

    def test_status_command_reports_what_is_held(self):
        self.board.add("t_1")
        sup = self.supervisor()
        sup.claim_once()
        data = json.loads(open(os.path.join(self.tmp, "held.json")).read())
        self.assertEqual(data["held"][0]["session"], "kb-t_1")


class TestWorkerCommandLine(Base):
    """The harness is launched with flags, so the flag that carries it must be
    able to hold a leading dash. A bare nargs="*" cannot."""

    def setUp(self):
        super().setUp()
        # Isolate from any ambient fleet config: these assertions are about the
        # command line, not about what a machine happens to have configured.
        patcher = mock.patch("scripts.fleet_kanban._config_section", lambda: {})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_a_full_command_line_is_split_into_command_and_args(self):
        args = parse_args(["run", "--once", "--state-dir", self.tmp,
                           "--worker-command", "claude --remote-control --model sonnet"])
        sup = build_supervisor(args)
        self.assertEqual(sup.worker_command, "claude")
        self.assertEqual(sup.worker_args, ["--remote-control", "--model", "sonnet"])

    def test_extra_args_are_appended_and_quotes_survive(self):
        args = parse_args(["run", "--once", "--state-dir", self.tmp,
                           "--worker-command", "/bin/sh -c 'echo $HERMES_KANBAN_TASK'",
                           "--worker-args=tail"])
        sup = build_supervisor(args)
        self.assertEqual(sup.worker_command, "/bin/sh")
        self.assertEqual(sup.worker_args, ["-c", "echo $HERMES_KANBAN_TASK", "tail"])

    def test_the_default_worker_is_the_harness_the_fleet_runs(self):
        sup = build_supervisor(parse_args(["run", "--once", "--state-dir", self.tmp]))
        self.assertEqual(sup.worker_command, "claude")
        self.assertEqual(sup.worker_args, [])
        self.assertEqual(sup.worker_base, f"{__import__('socket').gethostname().split('.')[0]}:fleet")


if __name__ == "__main__":
    unittest.main()
