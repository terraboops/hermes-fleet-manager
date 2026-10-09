#!/usr/bin/env python3
"""fleet_kanban.py - the FLEET side of the kanban <-> fleet bridge.

The contract is docs/kanban-fleet-bridge.md; the board side (hermes kanban
claim-next/release/fail) is the other half. This module implements §8's fleet
delta: the claim loop, the `kb-<task_id>` spawner, the per-task heartbeat
scheduler, the exit-code reporter, and the error paths in §7.

WHY IT EXISTS
A Hermes kanban board can already hold and dispatch work, but its dispatcher
spawns one headless worker per task inside the gateway process. A fleet of
long-lived Claude Code sessions wants the same board as its QUEUE: the board
owns task state, the fleet owns the tmux sessions. Neither side reads the
other's internals - the fleet shells out to `hermes kanban <verb> --json` and
parses the contract's JSON, exactly as a remote implementation would.

DESIGN DECISIONS (this side's, recorded here because the other side must agree)

1. WORKER ID vs CLAIM LOCK. The contract's worker id embeds the session name
   (`<host>:fleet:kb-<task_id>`), but `claim-next` needs a worker id BEFORE the
   task id is known. So the fleet passes a BASE id (`<host>:fleet`) to
   claim-next and then uses the `claim_lock` the board returns for every later
   verb. The board is free to derive `"<base>:kb-<task_id>"` from the base; the
   fleet never re-derives it, so the two can never disagree about who owns the
   lease. The same value is exported to the worker as HERMES_KANBAN_CLAIM_LOCK.
2. BOARD SELECTION goes through the environment (HERMES_KANBAN_BOARD /
   HERMES_KANBAN_DB), not `--board`, because the flag's position in the CLI
   (`hermes kanban --board X <verb>` vs `hermes --board X kanban <verb>`) is
   not stable across versions and env is the contract's documented fallback.
3. EXIT DETECTION is the exit-code FILE, not the pane. The pane command is a
   generated wrapper (`.kanban/worker.sh`) that runs the harness and writes its
   status to `.kanban/exit-code`. tmux does not report a pane's exit status, and
   a killed session leaves no file - which is exactly the "died without a
   report" case (§7), so a missing file means RELEASE, never FAIL.
4. ONE TMUX SERVER, EXPLICIT ENV. `tmux new-session` inherits the tmux SERVER's
   environment, not the caller's, so the HERMES_KANBAN_* variables are passed
   with `-e` (tmux >= 3.2) rather than relied upon to leak through.
5. HEARTBEATS ARE A SCHEDULER, NOT A THREAD PER TASK. One tick loop with a
   per-task due time: no thread races, no leaked threads when a task ends, and
   a single place where "the board is unreachable" is counted.

Usage:
    fleet_kanban.py run [--once] [--ticks N] [--max-held N] [--ttl S]
    fleet_kanban.py status
    fleet_kanban.py reconcile

Run the tests:  python3 -m unittest discover -s tests -t .
"""
from __future__ import annotations

import argparse
import json
import os
import shlex
import socket
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.realpath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

# --- exit-code convention from the contract (§0) -----------------------------
EXIT_OK = 0          # success, including task:null
EXIT_FATAL = 2       # usage / unknown task / not the lease owner - do NOT retry
EXIT_UNREACHABLE = 3  # board unreachable or DB locked - retry with backoff

DEFAULT_TTL = 900            # DEFAULT_CLAIM_TTL_SECONDS
HEARTBEAT_DIVISOR = 3        # heartbeat every TTL/3
DEFAULT_POLL = (5, 10, 30, 60)
DEFAULT_MAX_HELD = 4
MAX_BOARD_FAILURES = 3       # §7: 3 consecutive failures -> unmanaged + one alert


def host_name() -> str:
    return socket.gethostname().split(".")[0]


def default_worker_base() -> str:
    return f"{host_name()}:fleet"


def session_name(task_id: str) -> str:
    """Deterministic tmux session for a task (§5)."""
    return f"kb-{task_id}"


def default_state_dir() -> str:
    return os.environ.get("FLEET_KANBAN_STATE") or os.path.expanduser("~/.hermes/fleet-kanban")


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------
class BoardError(Exception):
    """exit 2: a refusal that will not improve by retrying (usage, unknown task,
    or - the common one - we are no longer the lease owner)."""


class BoardUnreachable(Exception):
    """exit 3: board unreachable / DB locked. Retry with backoff; never treat as
    a task outcome."""


# ---------------------------------------------------------------------------
# The board, over the CLI (§0 Transport)
# ---------------------------------------------------------------------------
def _subprocess_runner(argv, env, timeout):
    """Default runner: argv -> (rc, stdout, stderr)."""
    try:
        p = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, env=env)
    except FileNotFoundError as exc:
        raise BoardUnreachable(f"cannot execute {argv[0]}: {exc}")
    except subprocess.TimeoutExpired:
        raise BoardUnreachable(f"{argv[0]} timed out after {timeout}s")
    return p.returncode, p.stdout, p.stderr


def _last_json_object(text: str):
    """The contract prints one JSON object on stdout; tolerate a stray human line
    by scanning backwards for the first parseable object."""
    for chunk in [text] + list(reversed([ln for ln in text.splitlines() if ln.strip()])):
        try:
            value = json.loads(chunk)
        except (ValueError, TypeError):
            continue
        if isinstance(value, dict):
            return value
    return None


class Board:
    """Thin client for `hermes kanban <verb> --json`.

    argv_prefix is injectable so the tests (and any other harness) can point the
    same code at a stub that speaks the contract's JSON and exit codes.
    """

    def __init__(self, argv_prefix=None, env=None, runner=None, timeout=30, log=None):
        self.argv_prefix = list(argv_prefix or ["hermes", "kanban"])
        self.env = env
        self.runner = runner or _subprocess_runner
        self.timeout = timeout
        self.log = log or (lambda _msg: None)

    def _child_env(self):
        env = dict(os.environ if self.env is None else self.env)
        # §0 env hygiene: inheriting this from a delegate run makes every board
        # write fail closed, silently, so the fleet would claim nothing.
        env.pop("HERMES_DELEGATED_CHILD_CONTEXT", None)
        return env

    def call(self, argv):
        full = self.argv_prefix + list(argv) + ["--json"]
        rc, out, err = self.runner(full, self._child_env(), self.timeout)
        if rc == EXIT_UNREACHABLE:
            raise BoardUnreachable((err or out or "").strip() or "board unreachable")
        if rc != EXIT_OK:
            payload = _last_json_object(out or "") or {}
            reason = payload.get("reason") or (err or out or "").strip() or f"exit {rc}"
            raise BoardError(str(reason))
        payload = _last_json_object(out or "")
        if payload is None:
            raise BoardError(f"unparseable JSON from {argv[0]}: {(out or '').strip()[:200]}")
        return payload

    # -- verbs (§1) --------------------------------------------------------
    def claim_next(self, worker, ttl=None, status=None):
        argv = ["claim-next", "--worker", str(worker)]
        if ttl:
            argv += ["--ttl", str(int(ttl))]
        if status:
            argv += ["--status", str(status)]
        return self.call(argv)

    def heartbeat(self, task_id, worker, note=None):
        argv = ["heartbeat", str(task_id), "--worker", str(worker)]
        if note:
            argv += ["--note", str(note)]
        return self.call(argv)

    def release(self, task_id, worker, reason):
        return self.call(["release", str(task_id), "--worker", str(worker), "--reason", str(reason)])

    def complete(self, task_id, worker, summary=None, result=None, metadata=None):
        argv = ["complete", str(task_id), "--worker", str(worker)]
        if summary:
            argv += ["--summary", str(summary)]
        if result:
            argv += ["--result", str(result)]
        if metadata:
            argv += ["--metadata", str(metadata)]
        return self.call(argv)

    def fail(self, task_id, worker, error, retryable=False):
        argv = ["fail", str(task_id), "--worker", str(worker), "--error", str(error)]
        if retryable:
            argv += ["--retryable"]
        return self.call(argv)

    def show(self, task_id):
        return self.call(["show", str(task_id)])


# ---------------------------------------------------------------------------
# tmux
# ---------------------------------------------------------------------------
def _tmux_runner(argv, env, timeout):
    try:
        p = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, env=env)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 1, "", str(exc)
    return p.returncode, p.stdout, p.stderr


class Tmux:
    """The session surface. Every target is the exact form `=<session>:<window>`:
    tmux resolves a bare name by prefix, so an unqualified target can read, type
    into, or kill a different session (§5)."""

    def __init__(self, runner=None, socket_name=None, window="worker", log=None):
        self.runner = runner or _tmux_runner
        self.socket_name = socket_name
        self.window = window
        self.log = log or (lambda _msg: None)

    def _argv(self, *args):
        base = ["tmux"]
        if self.socket_name:
            base += ["-L", self.socket_name]
        return base + [str(a) for a in args]

    def _run(self, *args, timeout=15):
        return self.runner(self._argv(*args), None, timeout)

    def target(self, name):
        return f"={name}:{self.window}"

    def has_session(self, name):
        rc, _out, _err = self._run("has-session", "-t", f"={name}:")
        return rc == 0

    def new_session(self, name, cwd, command, env=None):
        args = ["new-session", "-d", "-s", name, "-n", self.window, "-c", cwd]
        for key, value in (env or {}).items():
            if value is None or value == "":
                continue
            # -e: the tmux SERVER's environment is what a new pane inherits, so
            # the worker's board variables have to be handed over explicitly.
            args += ["-e", f"{key}={value}"]
        args += [command]
        rc, _out, err = self._run(*args)
        if rc != 0:
            raise RuntimeError(f"tmux new-session {name} failed: {(err or '').strip()}")
        return name

    def kill_session(self, name):
        self._run("kill-session", "-t", f"={name}:")

    def send_line(self, name, text, verify=None, tries=3, settle=0.4):
        """Type one line into the pane and press Enter.

        The contract's handoff is a SHORT pointer line, never the body: a long
        paste truncates the front of the buffer. `verify` is a token that must
        appear in the pane afterwards; verification failing is reported, not
        fatal - the same path is also exported in the worker's environment.
        """
        for attempt in range(1, tries + 1):
            self._run("send-keys", "-t", self.target(name), "-l", "--", text)
            self._run("send-keys", "-t", self.target(name), "Enter")
            if not verify:
                return True
            time.sleep(settle)
            rc, out, _err = self._run("capture-pane", "-t", self.target(name), "-p")
            if rc == 0 and verify in (out or ""):
                return True
            self.log(f"pointer line not visible in {name} (attempt {attempt}/{tries})")
        return False


# ---------------------------------------------------------------------------
# Held task
# ---------------------------------------------------------------------------
class HeldTask:
    """One claimed task: its lease, its session, and its retry counters."""

    def __init__(self, task_id, session, workspace, claim_lock, ttl,
                 last_heartbeat=0.0, board_failures=0, alerted=False,
                 pending_report=None, title=""):
        self.task_id = task_id
        self.session = session
        self.workspace = workspace
        self.claim_lock = claim_lock
        self.ttl = ttl
        self.last_heartbeat = last_heartbeat
        self.board_failures = board_failures
        self.alerted = alerted
        self.pending_report = pending_report
        self.title = title

    def to_json(self):
        return {k: v for k, v in self.__dict__.items()}

    @classmethod
    def from_json(cls, d):
        known = {k: v for k, v in d.items() if k in cls("", "", "", "", 0).__dict__}
        return cls(**known)


# ---------------------------------------------------------------------------
# The supervisor: claim loop + spawner + heartbeat + reporter
# ---------------------------------------------------------------------------
class Supervisor:
    def __init__(self, board, tmux, worker_base=None, max_held=DEFAULT_MAX_HELD,
                 ttl=DEFAULT_TTL, poll_schedule=DEFAULT_POLL, state_dir=None,
                 worker_command=None, worker_args=None, alert=None, clock=time.time,
                 sleep=time.sleep, log=None, settle=0.4, heartbeat_divisor=HEARTBEAT_DIVISOR):
        self.board = board
        self.tmux = tmux
        self.worker_base = worker_base or default_worker_base()
        self.max_held = int(max_held)
        self.ttl = int(ttl)
        self.poll_schedule = tuple(poll_schedule or DEFAULT_POLL)
        self.state_dir = state_dir or default_state_dir()
        self.worker_command = worker_command or os.environ.get("FLEET_KANBAN_WORKER_CMD") or "claude"
        self.worker_args = list(worker_args or [])
        self.clock = clock
        self.sleep = sleep
        self.settle = settle
        self.heartbeat_divisor = heartbeat_divisor
        self.held = {}
        self._null_streak = 0
        self._alert_hook = alert
        self.log_path = os.path.join(self.state_dir, "fleet-kanban.log")
        self.alerts_path = os.path.join(self.state_dir, "alerts.log")
        self.state_path = os.path.join(self.state_dir, "held.json")
        self._log = log

    # -- logging / alerts ---------------------------------------------------
    def log(self, msg):
        line = f"{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(self.clock()))} {msg}"
        if self._log:
            self._log(msg)
        else:
            print(line, file=sys.stderr)
        try:
            os.makedirs(self.state_dir, exist_ok=True)
            with open(self.log_path, "a") as fh:
                fh.write(line + "\n")
        except OSError:
            pass

    def alert(self, msg):
        """One alert per condition (§7). An alert is the operator's, not the
        task's: the pane is left alone and the task is marked unmanaged."""
        line = f"{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(self.clock()))} ALERT {msg}"
        try:
            os.makedirs(self.state_dir, exist_ok=True)
            with open(self.alerts_path, "a") as fh:
                fh.write(line + "\n")
        except OSError:
            pass
        if self._alert_hook:
            self._alert_hook(msg)
        self.log(f"ALERT {msg}")

    # -- durable state ------------------------------------------------------
    def persist(self):
        try:
            os.makedirs(self.state_dir, exist_ok=True)
            tmp = self.state_path + ".tmp"
            with open(tmp, "w") as fh:
                json.dump({"worker_base": self.worker_base,
                           "held": [h.to_json() for h in self.held.values()]}, fh, indent=2)
            os.replace(tmp, self.state_path)
        except OSError as exc:
            self.log(f"cannot persist held state: {exc}")

    def load_state(self):
        try:
            with open(self.state_path) as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            return []
        out = []
        for entry in data.get("held") or []:
            try:
                out.append(HeldTask.from_json(entry))
            except TypeError:
                continue
        return out

    # -- startup ------------------------------------------------------------
    def reconcile(self):
        """Adopt the sessions a previous run left behind, and settle the ones
        whose session is gone. Without this a fleet restart orphans every pane:
        nothing heartbeats it, the lease expires, and the board reclaims a task
        whose worker is still running."""
        for held in self.load_state():
            if self.tmux.has_session(held.session):
                held.board_failures = 0
                self.held[held.task_id] = held
                self.log(f"adopted live session {held.session} for {held.task_id}")
            else:
                self.held[held.task_id] = held
                self.log(f"session {held.session} is gone; settling {held.task_id}")
                self._settle(held)
        self.persist()

    # -- claim --------------------------------------------------------------
    def claim_once(self):
        """One claim attempt. Returns the HeldTask when work was taken."""
        if len(self.held) >= self.max_held:
            return None
        try:
            resp = self.board.claim_next(self.worker_base, ttl=self.ttl)
        except BoardUnreachable as exc:
            self.log(f"claim-next: board unreachable ({exc}); backing off")
            return None
        except BoardError as exc:
            # exit 2 is a refusal, not a hiccup: retrying the same verb in a loop
            # is how a broken contract turns into a busy loop.
            self.log(f"claim-next refused: {exc}")
            return None

        task = resp.get("task")
        if not task:
            self._null_streak += 1
            return None
        self._null_streak = 0

        if len(self.held) >= self.max_held:
            # Claimed past the cap (another holder won capacity meanwhile): hand
            # it straight back rather than run more workers than we promised.
            lock = task.get("claim_lock") or f"{self.worker_base}:{session_name(task['id'])}"
            self._release_ids(task["id"], lock, "concurrency cap reached")
            return None
        return self.spawn(task, resp)

    def _poll_interval(self):
        # The first empty poll already means "nothing yet": wait the shortest
        # interval, then back off. Indexing by the streak itself would skip 5s.
        idx = max(0, min(self._null_streak - 1, len(self.poll_schedule) - 1))
        return self.poll_schedule[idx]

    # -- spawn --------------------------------------------------------------
    def _worker_env(self, task, lock):
        return {
            "HERMES_KANBAN_TASK": task.get("id"),
            "HERMES_KANBAN_DB": os.environ.get("HERMES_KANBAN_DB"),
            "HERMES_KANBAN_BOARD": os.environ.get("HERMES_KANBAN_BOARD"),
            "HERMES_KANBAN_WORKSPACE": task.get("workspace_path"),
            "HERMES_KANBAN_RUN_ID": task.get("current_run_id"),
            "HERMES_KANBAN_CLAIM_LOCK": lock,
            "HERMES_KANBAN_WORKER_ID": lock,
        }

    def _write_private(self, path, text):
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as fh:
            fh.write(text)

    def worker_script(self, held_like):
        """The pane command: run the harness, record its status, exit.

        stdout stays on the pane so the session is watchable; stderr is copied to
        a file because the failure report wants its last line. `$?` is the
        harness's own status - no pipeline, so nothing can eat it."""
        ws = held_like.workspace
        harness = " ".join([_shquote(self.worker_command)]
                           + [_shquote(a) for a in self.worker_args])
        return (
            "#!/bin/sh\n"
            "# generated by fleet_kanban.py - runs the worker and records its exit status\n"
            f"cd {_shquote(ws)} || exit 127\n"
            f"{harness} 2>>\"$PWD/.kanban/worker.log\"\n"
            "rc=$?\n"
            'printf "%s" "$rc" > "$PWD/.kanban/exit-code"\n'
            'exit "$rc"\n'
        )

    def spawn(self, task, resp=None):
        task_id = task["id"]
        session = session_name(task_id)
        workspace = task.get("workspace_path") or os.path.join(self.state_dir, "workspaces", task_id)
        lock = task.get("claim_lock") or f"{self.worker_base}:{session}"
        kdir = os.path.join(workspace, ".kanban")
        os.makedirs(kdir, exist_ok=True)

        claim = dict(task)
        claim["lease_expires"] = (resp or {}).get("lease_expires")
        claim["worker_id"] = lock
        self._write_private(os.path.join(kdir, "task.json"),
                            json.dumps(claim, indent=2) + "\n")
        for name in ("summary.md", "metadata.json"):
            path = os.path.join(kdir, name)
            if not os.path.exists(path):
                self._write_private(path, "")
        exit_code = os.path.join(kdir, "exit-code")
        for stale in (exit_code, os.path.join(kdir, "pending-report.json")):
            if os.path.exists(stale):
                os.remove(stale)

        held = HeldTask(task_id, session, workspace, lock, self.ttl,
                        last_heartbeat=0.0, title=task.get("title") or "")
        script = os.path.join(kdir, "worker.sh")
        self._write_private(script, self.worker_script(held))

        self.tmux.new_session(session, workspace, f"sh {_shquote(script)}",
                              env=self._worker_env(task, lock))
        pointer = (f"Read {os.path.join(kdir, 'task.json')} and complete the task; "
                   f"report via: hermes kanban complete {task_id} --worker {lock}")
        verified = self.tmux.send_line(session, pointer, verify=task_id, settle=self.settle)
        if not verified:
            self.log(f"{task_id}: pointer line unverified in {session}; the claim record "
                     f"and env are still in place")

        self.held[task_id] = held
        self.persist()
        self.log(f"claimed {task_id} -> {session} (worker {lock})")
        # §3: heartbeat once immediately after the claim, so a claim made right
        # before a stall still has a full TTL ahead of it.
        self._heartbeat(held)
        return held

    # -- tick ---------------------------------------------------------------
    def tick(self):
        for held in list(self.held.values()):
            self._tick_one(held)

    def _tick_one(self, held):
        if held.pending_report:
            # The session is already gone; the only thing left is the report, and
            # a flush that lands drops the task itself.
            self._flush_pending(held)
            return
        if not self.tmux.has_session(held.session):
            self._settle(held)
            return
        if self.clock() - held.last_heartbeat >= self.ttl / self.heartbeat_divisor:
            self._heartbeat(held)

    def _heartbeat(self, held):
        try:
            resp = self.board.heartbeat(held.task_id, held.claim_lock,
                                        note=f"session {held.session}")
        except BoardUnreachable as exc:
            held.board_failures += 1
            self.log(f"{held.task_id}: heartbeat failed ({exc}); "
                     f"{held.board_failures}/{MAX_BOARD_FAILURES}")
            if held.board_failures >= MAX_BOARD_FAILURES and not held.alerted:
                held.alerted = True
                self.alert(f"board unreachable for {held.task_id}: the worker and its "
                           f"pane are left running, this task is unmanaged until the "
                           f"board answers")
            self.persist()
            return
        except BoardError as exc:
            # exit 2 from heartbeat = we are not the lease owner any more.
            self._lost_claim(held, f"heartbeat refused: {exc}")
            return
        held.board_failures = 0
        if not resp.get("ok", True) and str(resp.get("reason")) == "lost_claim":
            self._lost_claim(held, "heartbeat answered lost_claim")
            return
        if held.alerted:
            self.log(f"{held.task_id}: board reachable again")
        held.alerted = False
        held.last_heartbeat = self.clock()
        self.persist()

    def _lost_claim(self, held, why):
        """§7: the CAS already reassigned the task. Kill the session and do not
        report into a task we no longer own."""
        self.log(f"{held.task_id}: {why}; killing {held.session} and dropping any report")
        self.tmux.kill_session(held.session)
        self.held.pop(held.task_id, None)
        self.persist()

    # -- settle: the session is gone, decide the report ----------------------
    def _settle(self, held):
        rc = self._read_exit_code(held)
        if rc is None:
            # No exit status: the session was killed, or the wrapper never got to
            # write. That is a release, not a failure - we cannot claim a task
            # failed when we do not know that its work did.
            self._release_ids(held.task_id, held.claim_lock, "session died")
            self.held.pop(held.task_id, None)
            self.persist()
            return
        summary, tail = self._read_summary(held)
        sentinel = _find_sentinel(summary) or _find_sentinel(tail)
        if sentinel:
            self._report(held, "fail", error=f"needs-input: {sentinel}", retryable=False)
        elif rc == 0:
            self._report(held, "complete", summary=summary, result=summary)
        else:
            self._report(held, "fail", error=f"exit={rc}: {tail}", retryable=True)
        if held.pending_report:
            # §6: the board was unreachable at report time. The session is gone,
            # so keep the entry for the report alone - the next tick retries it -
            # and never leave the task looking finished while it is not.
            self.held[held.task_id] = held
            self.persist()
            return
        if self.tmux.has_session(held.session):
            self.tmux.kill_session(held.session)
        self.held.pop(held.task_id, None)
        self.persist()

    def _read_exit_code(self, held):
        try:
            return int(_read_text(os.path.join(held.workspace, ".kanban", "exit-code")).strip())
        except ValueError:
            return None

    def _read_summary(self, held):
        kdir = os.path.join(held.workspace, ".kanban")
        summary = _read_text(os.path.join(kdir, "summary.md")).strip()
        lines = [ln.strip() for ln in _read_text(os.path.join(kdir, "worker.log")).splitlines()
                 if ln.strip()]
        tail = lines[-1] if lines else ""
        return (summary or tail), tail

    def _metadata_text(self, held):
        text = _read_text(os.path.join(held.workspace, ".kanban", "metadata.json")).strip()
        try:
            value = json.loads(text) if text else None
        except ValueError:
            return None
        return json.dumps(value) if isinstance(value, dict) and value else None

    # -- report -------------------------------------------------------------
    def _report(self, held, kind, **kw):
        if kind == "complete":
            kw.setdefault("metadata", self._metadata_text(held))
        try:
            if kind == "complete":
                resp = self.board.complete(held.task_id, held.claim_lock,
                                           summary=kw.get("summary"), result=kw.get("result"),
                                           metadata=kw.get("metadata"))
            else:
                resp = self.board.fail(held.task_id, held.claim_lock, kw.get("error", "unknown"),
                                       retryable=bool(kw.get("retryable")))
        except BoardUnreachable as exc:
            held.pending_report = {"kind": kind, **kw}
            self._write_pending(held)
            self.log(f"{held.task_id}: board unreachable at report time ({exc}); "
                     f"buffered and retried at the next tick")
            self.persist()
            return
        except BoardError as exc:
            # Already terminal (idempotent verbs) or no longer ours: nothing to do.
            self.log(f"{held.task_id}: report refused: {exc}")
            return
        if resp.get("already_terminal"):
            self.log(f"{held.task_id}: already terminal; report skipped")
        else:
            self.log(f"{held.task_id}: reported {kind}")

    def _write_pending(self, held):
        try:
            path = os.path.join(held.workspace, ".kanban", "pending-report.json")
            self._write_private(path, json.dumps(held.pending_report, indent=2) + "\n")
        except OSError as exc:
            self.log(f"{held.task_id}: cannot buffer the report: {exc}")

    def _flush_pending(self, held):
        pending = held.pending_report
        held.pending_report = None
        self._report(held, pending.get("kind", "fail"),
                     **{k: v for k, v in pending.items() if k != "kind"})
        if held.pending_report is None:
            try:
                os.remove(os.path.join(held.workspace, ".kanban", "pending-report.json"))
            except OSError:
                pass
            self.held.pop(held.task_id, None)
            self.persist()

    def _release_ids(self, task_id, lock, reason):
        try:
            self.board.release(task_id, lock, reason)
            self.log(f"{task_id}: released ({reason})")
        except BoardUnreachable as exc:
            # §7: the lease expiry is the backstop. Never leave the task ours.
            self.log(f"{task_id}: release failed, board unreachable ({exc}); "
                     f"the lease expiry is the backstop")
        except BoardError as exc:
            self.log(f"{task_id}: release refused: {exc}")

    # -- loop ---------------------------------------------------------------
    def run(self, max_ticks=None, once=False):
        self.reconcile()
        ticks = 0
        while True:
            ticks += 1
            if len(self.held) < self.max_held:
                self.claim_once()
            self.tick()
            if once or (max_ticks is not None and ticks >= max_ticks):
                break
            self.sleep(self._poll_interval())
        self.persist()
        return self.held


def _shquote(value):
    return "'" + str(value).replace("'", "'\\''") + "'"


def _read_text(path):
    """Read a small file, empty string when it is missing. The report path must
    never raise: a task with no summary is a task with an empty summary."""
    try:
        with open(path) as fh:
            return fh.read()
    except OSError:
        return ""


def _find_sentinel(text):
    """The worker's needs-input sentinel (§6). Whole token, any position."""
    if not text:
        return None
    for token in str(text).replace("\n", " ").split():
        if token.startswith("NEEDS-INPUT-"):
            return token[len("NEEDS-INPUT-"):].strip(":,") or token
    return None


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def _config_section():
    try:
        import fleet_harness
        cfg = fleet_harness.load() or {}
    except Exception:
        return {}
    section = cfg.get("kanban") or {}
    return section if isinstance(section, dict) else {}


def build_supervisor(args):
    section = _config_section()

    def pick(name, default):
        if getattr(args, name, None) is not None:
            return getattr(args, name)
        return section.get(name, default)

    env = dict(os.environ)
    if args.board:
        env["HERMES_KANBAN_BOARD"] = args.board
    if args.db:
        env["HERMES_KANBAN_DB"] = args.db

    prefix = args.hermes_cmd or section.get("hermes_cmd") or ["hermes", "kanban"]
    if isinstance(prefix, str):
        prefix = prefix.split()
    board = Board(argv_prefix=prefix, env=env, timeout=pick("timeout", 30) or 30)
    tmux = Tmux(socket_name=pick("tmux_socket", None))

    # The worker command is a COMMAND LINE, not just an executable: a harness is
    # normally launched with flags (`claude --remote-control`), and a bare
    # nargs="*" flag cannot carry a leading dash. One shell-quoted string can.
    raw = pick("worker_command", None) or "claude"
    if isinstance(raw, str):
        parts = shlex.split(raw) or ["claude"]
    else:
        parts = list(raw) or ["claude"]
    command, args_list = parts[0], parts[1:]
    args_list = list(args_list) + list(pick("worker_args", None) or [])

    return Supervisor(
        board, tmux,
        worker_base=pick("worker_id", None) or default_worker_base(),
        max_held=pick("max_held", DEFAULT_MAX_HELD),
        ttl=pick("ttl", DEFAULT_TTL),
        poll_schedule=pick("poll", DEFAULT_POLL),
        state_dir=pick("state_dir", None) or default_state_dir(),
        worker_command=command,
        worker_args=args_list,
    )


def cmd_run(args):
    sup = build_supervisor(args)
    try:
        sup.run(max_ticks=args.ticks, once=args.once)
    except KeyboardInterrupt:
        sup.persist()
        print("interrupted; held sessions keep running and are adopted on the next start",
              file=sys.stderr)
    held = sup.held
    print(json.dumps({"held": sorted(held), "state": sup.state_path}))
    return 0


def cmd_status(args):
    state_dir = args.state_dir or default_state_dir()
    path = os.path.join(state_dir, "held.json")
    try:
        with open(path) as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        print(json.dumps({"held": [], "state": path}))
        return 0
    tmux = Tmux(socket_name=args.tmux_socket)
    rows = []
    now = time.time()
    for entry in data.get("held") or []:
        session = entry.get("session") or session_name(entry.get("task_id", ""))
        rows.append({"task_id": entry.get("task_id"), "session": session,
                     "alive": tmux.has_session(session),
                     "heartbeat_age": round(now - float(entry.get("last_heartbeat") or 0), 1),
                     "unmanaged": bool(entry.get("alerted")),
                     "pending_report": bool(entry.get("pending_report"))})
    print(json.dumps({"state": path, "held": rows}, indent=2))
    return 0


def cmd_reconcile(args):
    sup = build_supervisor(args)
    sup.reconcile()
    sup.tick()
    print(json.dumps({"held": sorted(sup.held)}))
    return 0


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    sub = parser.add_subparsers(dest="cmd")

    def common(p):
        p.add_argument("--board", help="kanban board slug (sets HERMES_KANBAN_BOARD)")
        p.add_argument("--db", help="kanban db path (sets HERMES_KANBAN_DB)")
        p.add_argument("--state-dir", help="where held.json / the log live")
        p.add_argument("--tmux-socket", help="tmux -L socket name (tests use their own server)")
        p.add_argument("--hermes-cmd", nargs="+", help="argv prefix for the board CLI")
        p.add_argument("--worker-id", help="the base worker id passed to claim-next")
        p.add_argument("--worker-command",
                       help="harness command line launched inside the session, "
                            "e.g. 'claude --remote-control'")
        p.add_argument("--worker-args", nargs="*",
                       help="extra args appended to the harness command "
                            "(write --worker-args=-x for a dash-leading value)")
        p.add_argument("--max-held", type=int, help="max tasks this instance holds")
        p.add_argument("--ttl", type=int, help="lease TTL in seconds")
        p.add_argument("--poll", type=int, nargs="+", help="poll backoff schedule, seconds")

    p_run = sub.add_parser("run", help="poll the board, claim, spawn, heartbeat, report")
    common(p_run)
    p_run.add_argument("--once", action="store_true", help="one claim + one tick, then exit")
    p_run.add_argument("--ticks", type=int, help="stop after N ticks")
    p_run.set_defaults(func=cmd_run)

    p_status = sub.add_parser("status", help="what this instance is holding")
    p_status.add_argument("--state-dir")
    p_status.add_argument("--tmux-socket")
    p_status.set_defaults(func=cmd_status)

    p_rec = sub.add_parser("reconcile", help="adopt or settle the sessions a restart left behind")
    common(p_rec)
    p_rec.set_defaults(func=cmd_reconcile)

    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    if not getattr(args, "func", None):
        print((__doc__ or "").strip().split("\n\n")[0])
        return 2
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
