#!/usr/bin/env python3
"""e2e_bridge_real.py - the end-to-end run of the kanban <-> fleet bridge.

Everything here is the REAL thing on both sides:

  * the board is the live ``hermes kanban`` CLI over a throwaway SQLite db
    (``HERMES_KANBAN_DB`` in a temp dir, never the user's board);
  * the sessions are real tmux sessions on a PRIVATE server (``tmux -L``), so a
    live fleet's sessions are never touched;
  * the worker is tests/e2e_worker.py, which records what it actually saw.

Four scenarios, each on its own throwaway board so they cannot contaminate one
another:

  1. happy path   - claim -> kb-<task_id> session -> the body, the env and the
                    pointer line reach the worker -> exit 0 -> task done with the
                    worker's own result payload.
  2. no duplicate - two claimers race one pool against the real CAS: every task
                    is claimed exactly once. Then two fleet instances race one
                    task: exactly one spawns a session.
  3. mid-run kill - the session is killed under a running task: the task is
                    RELEASED back to the pool (never left stuck running) and a
                    fresh claimer can take it.
  4. lease expiry - one instance's lease expires while its worker still runs; a
                    second instance reclaims the task and replaces the stale
                    session; the first is told lost_claim and must NOT kill the
                    session its successor is now using.

Run:  python3 tests/e2e_bridge_real.py
      python3 tests/e2e_bridge_real.py --keep     # keep the workdir in place
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from typing import Any

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from scripts.fleet_kanban import Board, Supervisor, Tmux, session_name  # noqa: E402

E2E_WORKER = os.path.join(HERE, "e2e_worker.py")
DEADLINE = 30.0
TERMINAL = {"done", "blocked", "archived"}
CLAIM_LOCK_KEY = "HERMES_KANBAN_CLAIM_LOCK"


# ---------------------------------------------------------------------------
# reporting
# ---------------------------------------------------------------------------
class Report:
    def __init__(self):
        self.rows = []
        self.failed = 0
        self.commands = []

    def check(self, name, ok, detail=""):
        ok = bool(ok)
        self.rows.append((ok, name))
        if not ok:
            self.failed += 1
        print(f"  [{'ok  ' if ok else 'FAIL'}] {name}", flush=True)
        if not ok and detail:
            print(f"         {detail}", flush=True)
        return ok

    def equal(self, name, got, want):
        return self.check(name, got == want, f"got={got!r} want={want!r}")

    def note(self, command):
        self.commands.append(command)

    def summary(self):
        print("")
        if self.failed:
            print(f"RESULT: FAIL - {self.failed} check(s) failed of {len(self.rows)}")
        else:
            print(f"RESULT: PASS - {len(self.rows)} checks, 0 failures")
        return 1 if self.failed else 0


# ---------------------------------------------------------------------------
# harness
# ---------------------------------------------------------------------------
class Harness:
    def __init__(self, workdir, keep=False):
        self.report = Report()
        self.keep = keep
        self.workdir = workdir
        self.ws_root = os.path.join(workdir, "workspaces")
        os.makedirs(self.ws_root, exist_ok=True)
        self.db = None
        self.env = self._base_env()
        self.socket = f"fk-e2e-{os.getpid()}"
        self.tmux = Tmux(socket_name=self.socket)
        self._seq = 0

    def _base_env(self):
        env = dict(os.environ)
        # The board CLI fails closed when this is inherited from a delegate run
        # (bridge spec section 0); a real deployment unsets it too.
        for key in ("HERMES_DELEGATED_CHILD_CONTEXT", "HERMES_KANBAN_BOARD",
                    "HERMES_KANBAN_TASK", "HERMES_KANBAN_RUN_ID",
                    "HERMES_KANBAN_CLAIM_LOCK", "HERMES_KANBAN_WORKER_ID",
                    "HERMES_KANBAN_WORKSPACE"):
            env.pop(key, None)
        env["HERMES_KANBAN_WORKSPACES_ROOT"] = self.ws_root
        return env

    # -- one throwaway board per scenario ------------------------------------
    def new_board(self, tag):
        self._seq += 1
        self.db = os.path.join(self.workdir, f"kanban-{self._seq}-{tag}.db")
        self.env["HERMES_KANBAN_DB"] = self.db
        out = self.cli("init", parse=False)
        self.report.check(f"[{tag}] board init", os.path.exists(self.db),
                          f"{out.stdout.strip()} {out.stderr.strip()}")
        return self.db

    # -- board CLI ----------------------------------------------------------
    def cli(self, *args, expect=0, parse=True) -> Any:
        """Run one board CLI verb; parsed JSON (or the raw process when not)."""
        argv = ["hermes", "kanban"] + [str(a) for a in args]
        self.report.note(" ".join(argv))
        proc = subprocess.run(argv, env=self.env, capture_output=True, text=True)
        if expect is not None and proc.returncode != expect:
            raise AssertionError(
                f"{' '.join(argv)} exited {proc.returncode} (wanted {expect}): "
                f"{proc.stderr.strip() or proc.stdout.strip()}")
        if not parse:
            return proc
        text = proc.stdout.strip()
        if not text:
            return None
        try:
            return json.loads(text)
        except ValueError:
            return None

    def create_task(self, title, body, workspace):
        task = self.cli("create", title, "--assignee", "default",
                        "--body", body, "--workspace", f"dir:{workspace}", "--json")
        self.report.equal(f"create {title!r}: status", task.get("status"), "ready")
        self.report.equal(f"create {title!r}: workspace", task.get("workspace_path"), workspace)
        return task["id"]

    def show(self, task_id):
        return self.cli("show", task_id, "--json") or {}

    def status_of(self, task_id):
        return self.show(task_id).get("task", {}).get("status")

    def events_of(self, task_id):
        return [e.get("kind") for e in (self.show(task_id).get("events") or [])]

    def runs_of(self, task_id):
        return self.show(task_id).get("runs") or []

    def claim_lock_of(self, task_id):
        """``tasks.claim_lock`` straight from the throwaway db.

        The CLI does not surface the column and the CAS is defined on it, so the
        e2e run reads it where it lives. Read-only, and only ever on a board this
        run created in its own temp dir.
        """
        conn = sqlite3.connect(self.db)
        try:
            row = conn.execute("SELECT claim_lock FROM tasks WHERE id = ?",
                               (task_id,)).fetchone()
        finally:
            conn.close()
        return row[0] if row else None

    # -- supervisors --------------------------------------------------------
    def board_client(self):
        return Board(argv_prefix=["hermes", "kanban"], env=self.env)

    def supervisor(self, worker_base, state_dir, ttl=900, worker_args=(), **kw):
        return Supervisor(
            self.board_client(), self.tmux, worker_base=worker_base,
            state_dir=state_dir, ttl=ttl, poll_schedule=(0.3,),
            worker_command=sys.executable,
            worker_args=[E2E_WORKER, *worker_args],
            board_env=self.env, log=lambda _m: None, **kw)

    def drive(self, sup, predicate, deadline=DEADLINE):
        end = time.time() + deadline
        while time.time() < end:
            sup.tick()
            if predicate():
                return True
            time.sleep(0.25)
        return predicate()

    def new_state_dir(self, tag):
        self._seq += 1
        path = os.path.join(self.workdir, f"state-{tag}-{self._seq}")
        os.makedirs(path, exist_ok=True)
        return path

    def teardown(self):
        subprocess.run(["tmux", "-L", self.socket, "kill-server"], capture_output=True)
        if not self.keep:
            shutil.rmtree(self.workdir, ignore_errors=True)


class SessionWatch:
    """Watch for a session appearing, from a thread.

    Every board CLI call costs seconds of interpreter start-up, so the claim path
    can easily outlast a short-lived worker: asking "is the session there?" after
    ``claim_once`` returns is a race the run loses. This polls while the claim is
    in flight instead, so "a session appeared" is an observation, not a guess.
    """

    def __init__(self, tmux, name):
        self.tmux = tmux
        self.name = name
        self.seen = False
        self._stop = threading.Event()
        self._thread = None

    def start(self):
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        return self

    def _loop(self):
        while not self._stop.is_set():
            if self.tmux.has_session(self.name):
                self.seen = True
                return
            time.sleep(0.05)

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)


# ---------------------------------------------------------------------------
# scenarios
# ---------------------------------------------------------------------------
def scenario_happy_path(h):
    r = h.report
    print("\n-- 1. happy path: claim -> session -> worker -> done --")
    h.new_board("happy")
    ws = os.path.join(h.ws_root, "happy")
    tid = h.create_task("e2e happy path", "BODY-MARKER-HAPPY", ws)
    # A worker that exits instantly leaves no session to look at: give it a
    # couple of seconds so "a session appeared" is observable, not a race.
    sup = h.supervisor("bc:fleet", h.new_state_dir("happy"), worker_args=("--sleep", "2"))

    watch = SessionWatch(h.tmux, session_name(tid)).start()
    held = sup.claim_once()
    watch.stop()
    if not r.check("claim-next returned the task",
                   held is not None and held.task_id == tid,
                   f"held={held and held.task_id}"):
        return
    r.equal("claim_lock is the worker id verbatim", h.claim_lock_of(tid), "bc:fleet")
    r.check(f"tmux session {session_name(tid)} appeared", watch.seen)
    r.equal("board status after claim", h.status_of(tid), "running")

    ok = h.drive(sup, lambda: h.status_of(tid) in TERMINAL)
    r.check("task reached a terminal status", ok, f"status={h.status_of(tid)}")
    r.equal("task landed done", h.status_of(tid), "done")

    kdir = os.path.join(ws, ".kanban")
    pointer = _read(os.path.join(kdir, "pointer-received")).strip()
    r.check("pointer line reached the pane", tid in pointer, f"pointer={pointer!r}")
    receipt = _json(os.path.join(kdir, "worker-receipt.json"))
    env_seen = receipt.get("env") or {}
    r.equal("worker env HERMES_KANBAN_TASK", env_seen.get("HERMES_KANBAN_TASK"), tid)
    r.equal("worker env HERMES_KANBAN_CLAIM_LOCK", env_seen.get(CLAIM_LOCK_KEY), "bc:fleet")
    r.equal("worker env HERMES_KANBAN_WORKSPACE",
            env_seen.get("HERMES_KANBAN_WORKSPACE"), ws)
    r.equal("worker env HERMES_KANBAN_DB is THIS board",
            env_seen.get("HERMES_KANBAN_DB"), h.db)
    r.check("worker env HERMES_KANBAN_RUN_ID is set",
            bool(env_seen.get("HERMES_KANBAN_RUN_ID")),
            f"run_id={env_seen.get('HERMES_KANBAN_RUN_ID')!r}")
    r.check("worker env carries no delegate marker",
            not env_seen.get("HERMES_DELEGATED_CHILD_CONTEXT"))
    r.equal("worker read the claim record body",
            (receipt.get("claim") or {}).get("body"), "BODY-MARKER-HAPPY")
    r.check("worker cwd is the workspace",
            os.path.realpath(receipt.get("cwd") or "") == os.path.realpath(ws),
            f"cwd={receipt.get('cwd')!r}")

    events = h.events_of(tid)
    for kind in ("claimed", "heartbeat", "completed"):
        r.check(f"board event {kind}", kind in events, f"events={events}")
    runs = h.runs_of(tid)
    r.equal("run outcome", runs[-1].get("outcome") if runs else None, "completed")
    result = h.show(tid).get("task", {}).get("result") or ""
    r.check("result payload is the worker's summary", "BODY-MARKER-HAPPY" in result,
            f"result={result!r}")
    r.equal("exit-code file was written by the wrapper",
            _read(os.path.join(kdir, "exit-code")).strip(), "0")
    r.check("session cleaned up after completion",
            not h.tmux.has_session(session_name(tid)))


def scenario_no_duplicate(h):
    r = h.report
    print("\n-- 2a. claim race: two claimers, one pool, real CAS --")
    h.new_board("race")
    ids = [h.create_task(f"e2e race {n}", f"BODY-RACE-{n}",
                         os.path.join(h.ws_root, f"race{n}")) for n in range(5)]
    boards = [h.board_client(), h.board_client()]
    claimed = {0: [], 1: []}

    def claim_loop(slot):
        while True:
            resp = boards[slot].claim_next(f"bc:fleet:racer{slot}", ttl=900)
            task = resp.get("task")
            if not task:
                return
            claimed[slot].append((task["id"], task["claim_lock"]))

    threads = [threading.Thread(target=claim_loop, args=(n,)) for n in (0, 1)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    all_claimed = claimed[0] + claimed[1]
    got = sorted(t for t, _ in all_claimed)
    r.equal("every ready task was claimed exactly once", got, sorted(ids))
    r.check("no task was claimed twice", len(got) == len(set(got)), f"claims={got}")
    locks = {lock for _, lock in all_claimed}
    r.check("each claim carries its own worker's lock",
            bool(locks) and all(lock.startswith("bc:fleet:racer") for lock in locks),
            f"locks={locks}")

    print("\n-- 2b. two fleet instances, one task: exactly one session --")
    h.new_board("once")
    ws = os.path.join(h.ws_root, "once")
    tid = h.create_task("e2e single dispatch", "BODY-ONCE", ws)
    sup_a = h.supervisor("bc:fleet:a", h.new_state_dir("dupe-a"), worker_args=("--sleep", "2"))
    sup_b = h.supervisor("bc:fleet:b", h.new_state_dir("dupe-b"), worker_args=("--sleep", "2"))
    watch = SessionWatch(h.tmux, session_name(tid)).start()
    held_a, held_b = sup_a.claim_once(), sup_b.claim_once()
    watch.stop()
    pairs = [(s, x) for s, x in ((sup_a, held_a), (sup_b, held_b)) if x is not None]
    if not r.equal("exactly one instance claimed the task", len(pairs), 1):
        return
    winner_sup, winner = pairs[0]
    loser = sup_b if winner_sup is sup_a else sup_a
    r.equal("the claim carries the winning instance's worker id",
            h.claim_lock_of(tid), winner.claim_lock)
    r.check("the winner's session appeared", watch.seen)
    r.equal("the loser is holding nothing", len(loser.held), 0)
    r.equal("board recorded one claimed event", h.events_of(tid).count("claimed"), 1)

    h.drive(winner_sup, lambda: h.status_of(tid) in TERMINAL)
    r.equal("task landed done", h.status_of(tid), "done")
    r.equal("still exactly one claimed event", h.events_of(tid).count("claimed"), 1)


def scenario_mid_run_kill(h):
    r = h.report
    print("\n-- 3. mid-run kill: released, not stuck running --")
    h.new_board("kill")
    ws = os.path.join(h.ws_root, "kill")
    tid = h.create_task("e2e mid-run kill", "BODY-KILL", ws)
    sup = h.supervisor("bc:fleet:kill", h.new_state_dir("kill"),
                       worker_args=("--sleep", "60"))
    held = sup.claim_once()
    if not r.check("task claimed", held is not None and held.task_id == tid):
        return
    r.equal("board status while running", h.status_of(tid), "running")
    r.equal("session alive before the kill", h.tmux.has_session(session_name(tid)), True)

    h.tmux.kill_session(session_name(tid))
    r.equal("session is gone", h.tmux.has_session(session_name(tid)), False)
    sup.tick()

    r.equal("task is back in the claimable pool", h.status_of(tid), "ready")
    r.check("claim_lock was cleared", h.claim_lock_of(tid) is None,
            f"lock={h.claim_lock_of(tid)!r}")
    runs = h.runs_of(tid)
    r.equal("run closed as reclaimed", runs[-1].get("outcome") if runs else None, "reclaimed")
    r.check("a reclaimed event was recorded", "reclaimed" in h.events_of(tid),
            f"events={h.events_of(tid)}")

    task = (h.board_client().claim_next("bc:fleet:second", ttl=900).get("task") or {})
    r.equal("a fresh claimer can take the released task", task.get("id"), tid)
    if task:
        h.cli("release", tid, "--worker", task["claim_lock"],
              "--reason", "kill cleanup", "--json")
        r.equal("released again", h.status_of(tid), "ready")


def scenario_lease_expiry(h):
    r = h.report
    print("\n-- 4. lease expiry mid-run: reclaimed once, no double dispatch --")
    h.new_board("expire")
    ws = os.path.join(h.ws_root, "expire")
    tid = h.create_task("e2e lease expiry", "BODY-EXPIRE", ws)
    sup_a = h.supervisor("bc:fleet:old", h.new_state_dir("expire-a"), ttl=2,
                         worker_args=("--sleep", "60"))
    held_a = sup_a.claim_once()
    if not r.check("instance A claimed the task",
                   held_a is not None and held_a.task_id == tid):
        return
    r.equal("A's session is alive", h.tmux.has_session(session_name(tid)), True)
    r.equal("A's session carries A's lock",
            h.tmux.session_env(session_name(tid), CLAIM_LOCK_KEY), "bc:fleet:old")
    r.equal("task running under A", h.claim_lock_of(tid), "bc:fleet:old")

    # A stops ticking: its 2s lease expires while A's worker keeps running.
    time.sleep(3.0)

    sup_b = h.supervisor("bc:fleet:new", h.new_state_dir("expire-b"), ttl=900,
                         worker_args=("--sleep", "60"))
    held_b = sup_b.claim_once()
    if not r.check("instance B reclaimed the expired task",
                   held_b is not None and held_b.task_id == tid,
                   f"held_b={held_b and held_b.task_id}"):
        return
    r.equal("the task now belongs to B", h.claim_lock_of(tid), "bc:fleet:new")
    r.equal("task is running again, not queued twice", h.status_of(tid), "running")
    outcomes = [run.get("outcome") for run in h.runs_of(tid)]
    r.check("A's run was closed as reclaimed", "reclaimed" in outcomes,
            f"outcomes={outcomes}")
    r.equal("B replaced the stale session (same deterministic name)",
            h.tmux.session_env(session_name(tid), CLAIM_LOCK_KEY), "bc:fleet:new")

    # A finally notices. It must drop the task WITHOUT killing the session its
    # successor is now running in.
    sup_a._heartbeat(held_a)
    r.check("A dropped the task", tid not in sup_a.held)
    r.equal("A did not kill B's session", h.tmux.has_session(session_name(tid)), True)
    r.equal("B's session is still B's",
            h.tmux.session_env(session_name(tid), CLAIM_LOCK_KEY), "bc:fleet:new")
    r.equal("B still owns the task", h.claim_lock_of(tid), "bc:fleet:new")

    h.tmux.kill_session(session_name(tid))
    sup_b.tick()
    r.equal("B released it when its session died", h.status_of(tid), "ready")


def _read(path):
    try:
        with open(path) as fh:
            return fh.read()
    except OSError:
        return ""


def _json(path):
    try:
        with open(path) as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


# ---------------------------------------------------------------------------
def main(argv=None):
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("--workdir", default=None,
                        help="where the throwaway boards + workspaces live")
    parser.add_argument("--keep", action="store_true",
                        help="keep the workdir (and the boards) after the run")
    args = parser.parse_args(argv)

    workdir = args.workdir or tempfile.mkdtemp(prefix="kbfleet-e2e-")
    print(f"workdir: {workdir}")
    print(f"boards:  {workdir}/kanban-*.db (throwaway, one per scenario)")
    print(f"tmux:    private server '-L fk-e2e-{os.getpid()}'")
    h = Harness(workdir, keep=args.keep)
    try:
        scenario_happy_path(h)
        scenario_no_duplicate(h)
        scenario_mid_run_kill(h)
        scenario_lease_expiry(h)
    finally:
        h.teardown()
    code = h.report.summary()
    print("\nboard commands exercised:")
    for line in sorted(set(h.report.commands)):
        print(f"  {line}")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
