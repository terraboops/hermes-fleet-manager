#!/usr/bin/env python3
"""Kanban dispatcher reliability harness (negative cases on a SCRATCH board).

Runs the real hermes kanban dispatcher code (hermes_cli.kanban_db /
kanban_db_dispatch) against a throwaway kanban.db with fake workers, kills
processes with SIGKILL at chosen moments, and asserts three invariants:

  I1 no card lost        every card ends in a known status and has a run history
  I2 no concurrent dup   two live workers never hold the same card at once
  I3 lease reclaim       a card whose holder is gone is reclaimed after expiry
                         (and NOT before it)

Usage (see README.md):
    HARNESS_PY=~/.hermes/hermes-agent/venv/bin/python
    $HARNESS_PY harness.py [--only S1,S2b] [--out DIR]

The live board is never touched: every path is pinned into a temp directory
and the runner refuses to start if the resolved DB sits under ~/.hermes.
SIGKILL is not a power cut: the OS page cache survives it. See README
"What this cannot prove".
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import sqlite3
import subprocess
import sys
import tempfile
import time
import traceback
from pathlib import Path

HERE = Path(__file__).resolve()
AGENT_DIR = Path(os.environ.get("HERMES_AGENT_DIR", Path.home() / ".hermes" / "hermes-agent"))
LIVE_HOME = Path.home() / ".hermes"


# --------------------------------------------------------------------------
# isolation
# --------------------------------------------------------------------------
def pin_scratch(root: Path) -> Path:
    """Point every kanban path at ``root`` and refuse anything near the live board."""
    root.mkdir(parents=True, exist_ok=True)
    db = root / "kanban.db"
    # Drop every inherited HERMES_* var. Run from inside a kanban worker, the delegated-child
    # marker would fence the scratch board, and a stale pin could aim at the live one.
    for k in [k for k in os.environ if k.startswith(("HERMES_", "_HERMES_"))]:
        del os.environ[k]
    for k in ("HERMES_KANBAN_HOME", "HERMES_HOME"):
        os.environ[k] = str(root)
    os.environ["HERMES_KANBAN_DB"] = str(db)
    os.environ["HERMES_KANBAN_WORKSPACES_ROOT"] = str(root / "workspaces")
    os.environ["HERMES_KANBAN_BOARD"] = "default"
    os.environ["HERMES_KANBAN_CRASH_GRACE_SECONDS"] = "0"
    for k in ("HERMES_KANBAN_TASK", "HERMES_KANBAN_RUN_ID", "HERMES_KANBAN_CLAIM_LOCK"):
        os.environ.pop(k, None)
    live = (LIVE_HOME / "kanban.db").resolve()
    if db.resolve() == live or str(db.resolve()).startswith(str(LIVE_HOME / "kanban")):
        raise SystemExit("refusing to run against the live board")
    sys.path.insert(0, str(AGENT_DIR))
    return db


def _imports():
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc
    from hermes_cli import kanban_db_dispatch as kbd
    return kb, kbc, kbd


# --------------------------------------------------------------------------
# role: worker  (fake long-running agent)
# --------------------------------------------------------------------------
def xlog(path: str, line: str) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
    try:
        os.write(fd, (line + "\n").encode())
        os.fsync(fd)
    finally:
        os.close(fd)


def read_xlog(path: str) -> list[list[str]]:
    if not os.path.exists(path):
        return []
    return [ln.split() for ln in Path(path).read_text().splitlines() if ln.strip()]


def pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    # a zombie still answers kill 0
    try:
        out = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True).stdout.strip()
    except Exception:
        return True
    return bool(out) and not out.startswith("Z")


def role_worker() -> None:
    kb, kbc, kbd = _imports()
    tid = os.environ["HERMES_KANBAN_TASK"]
    run_id = int(os.environ["HERMES_KANBAN_RUN_ID"])
    log = os.environ["H_XLOG"]
    modes = os.environ.get("H_MODES", "finish").split(",")
    dur = float(os.environ.get("H_DUR", "3"))
    me = os.getpid()
    prior = [r for r in read_xlog(log) if r[0] == "START" and r[1] == tid]
    mode = modes[min(len(prior), len(modes) - 1)]
    peers = [r[2] for r in prior if pid_alive(int(r[2])) and not any(
        e[0] in ("END", "ABORT") and e[1] == tid and e[2] == r[2] for e in read_xlog(log))]
    xlog(log, f"START {tid} {me} peers={','.join(peers) or '-'} mode={mode} run={run_id}")
    conn = kbc.connect()
    if os.environ.get("H_ADOPT") == "1":
        if not kbd.adopt_worker_pid(conn, tid, run_id, me):
            xlog(log, f"ABORT {tid} {me} reclaimed_before_adopt")
            return
    if mode == "hb_then_hang":
        kbd.heartbeat_worker(conn, tid, note="harness hb", expected_run_id=run_id)
    if mode in ("hang", "hb_then_hang"):
        while True:
            time.sleep(1)
    time.sleep(dur)
    try:
        ok = kb.complete_task(conn, tid, summary="harness worker done", expected_run_id=run_id)
        xlog(log, f"END {tid} {me} complete={ok}")
    except Exception as exc:  # reclaimed under us, etc.
        xlog(log, f"END {tid} {me} complete_rejected={type(exc).__name__}")


# --------------------------------------------------------------------------
# role: dispatcher  (stand-in for the gateway-embedded dispatcher)
# --------------------------------------------------------------------------
def make_spawn_fn(fault: str):
    def spawn(task, workspace, board=None):
        if fault == "die_before_popen":
            os.kill(os.getpid(), signal.SIGKILL)
        env = dict(os.environ)
        env.update({
            "HERMES_KANBAN_TASK": task.id,
            "HERMES_KANBAN_RUN_ID": str(task.current_run_id),
            "HERMES_KANBAN_CLAIM_LOCK": task.claim_lock or "",
        })
        logf = open(os.path.join(os.environ["H_OUT"], f"worker-{task.id}.log"), "ab")
        p = subprocess.Popen([sys.executable, str(HERE), "worker"], env=env, stdin=subprocess.DEVNULL,
                             stdout=logf, stderr=subprocess.STDOUT, start_new_session=True)
        if fault == "die_after_popen":
            time.sleep(0.2)  # let the child start, then die before the pid is recorded
            os.kill(os.getpid(), signal.SIGKILL)
        return p.pid
    return spawn


def one_tick(spawn_fn, ttl=None):
    kb, kbc, kbd = _imports()
    import contextlib
    with contextlib.closing(kbc.connect()) as conn:
        return kbd.dispatch_once(conn, spawn_fn=spawn_fn, ttl_seconds=ttl, max_in_progress=50,
                                 default_assignee="default")


def role_dispatcher() -> None:
    kb, kbc, kbd = _imports()
    if os.environ.get("H_HB_STALE"):
        kb.DEFAULT_CLAIM_HEARTBEAT_MAX_STALE_SECONDS = int(os.environ["H_HB_STALE"])
    fault = os.environ.get("H_FAULT", "none")
    ttl = int(os.environ["H_TTL"]) if os.environ.get("H_TTL") else None
    spawn = make_spawn_fn(fault)
    while True:
        try:
            one_tick(spawn, ttl)
        except Exception:
            traceback.print_exc()
        time.sleep(float(os.environ.get("H_INTERVAL", "0.3")))


# --------------------------------------------------------------------------
# role: churn writer (abrupt DB close test)
# --------------------------------------------------------------------------
def role_churn() -> None:
    kb, kbc, kbd = _imports()
    conn = kbc.connect()
    i = 0
    while True:
        kb.create_task(conn, title=f"churn {i}", assignee="default")
        i += 1


# --------------------------------------------------------------------------
# scenario plumbing
# --------------------------------------------------------------------------
class Ctx:
    def __init__(self, name: str, root: Path, out: Path):
        self.name = name
        self.root = root
        self.out = out
        self.db = pin_scratch(root / name)
        os.environ["H_OUT"] = str(out)
        self.xlog = str(out / f"{name}.exec.log")
        os.environ["H_XLOG"] = self.xlog
        os.environ.pop("H_MODES", None)
        for k in ("H_DUR", "H_ADOPT", "H_FAULT", "H_TTL", "H_HB_STALE"):
            os.environ.pop(k, None)
        self.procs: list[subprocess.Popen] = []
        self.notes: list[str] = []
        kb, kbc, kbd = _imports()
        self.kb, self.kbc, self.kbd = kb, kbc, kbd
        kbc.init_db()

    def conn(self):
        return self.kbc.connect()

    def mk(self, n=1, **kw) -> list[str]:
        c = self.conn()
        ids = [self.kb.create_task(c, title=f"{self.name} card {i}", body="harness", assignee="default",
                                   **kw) for i in range(n)]
        c.close()
        return ids

    def row(self, tid):
        c = self.conn()
        r = c.execute("SELECT status, claim_lock, claim_expires, worker_pid, consecutive_failures, "
                      "last_heartbeat_at FROM tasks WHERE id=?", (tid,)).fetchone()
        c.close()
        return dict(r) if r else None

    def events(self, tid) -> list[str]:
        c = self.conn()
        ks = [r[0] for r in c.execute("SELECT kind FROM task_events WHERE task_id=? ORDER BY id", (tid,))]
        c.close()
        return ks

    def runs(self, tid):
        c = self.conn()
        rs = [(r["id"], r["status"], r["outcome"]) for r in
              c.execute("SELECT id,status,outcome FROM task_runs WHERE task_id=? ORDER BY id", (tid,))]
        c.close()
        return rs

    def starts(self, tid):
        return [r for r in read_xlog(self.xlog) if r[0] == "START" and r[1] == tid]

    def concurrent_dups(self):
        return [r for r in read_xlog(self.xlog) if r[0] == "START" and r[3] != "peers=-"]

    def wait(self, cond, timeout=30.0, step=0.2, tick=None):
        t0 = time.time()
        while time.time() - t0 < timeout:
            if tick:
                tick()
            if cond():
                return True
            time.sleep(step)
        return False

    def spawn_dispatcher(self, **env) -> subprocess.Popen:
        e = dict(os.environ)
        e.update({k: str(v) for k, v in env.items()})
        lf = open(self.out / f"{self.name}.dispatcher{len(self.procs)}.log", "ab")
        p = subprocess.Popen([sys.executable, str(HERE), "dispatcher"], env=e, stdout=lf,
                             stderr=subprocess.STDOUT, start_new_session=True)
        self.procs.append(p)
        return p

    def cleanup(self):
        for p in self.procs:
            try:
                os.kill(p.pid, signal.SIGKILL)
            except Exception:
                pass
            try:
                p.wait(timeout=2)
            except Exception:
                pass
        for r in read_xlog(self.xlog):
            if r[0] == "START":
                try:
                    os.kill(int(r[2]), signal.SIGKILL)
                except Exception:
                    pass

    def integrity(self, path=None) -> str:
        c = sqlite3.connect(str(path or self.db))
        try:
            return c.execute("PRAGMA integrity_check").fetchone()[0]
        finally:
            c.close()


class Result:
    def __init__(self, sid, title):
        self.sid, self.title = sid, title
        self.checks: list[tuple[str, bool, str]] = []
        self.expected_gap = False
        self.notes: list[str] = []

    def check(self, name, ok, detail=""):
        self.checks.append((name, bool(ok), str(detail)))

    @property
    def passed(self):
        return all(ok for _, ok, _ in self.checks)


def kill9(pid):
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def start_pids(ctx, tid):
    return [int(r[2]) for r in ctx.starts(tid)]


# --------------------------------------------------------------------------
# scenarios
# --------------------------------------------------------------------------
def s1_worker_sigkill(ctx: Ctx, res: Result):
    """(a) worker killed mid-run: crash detected, card requeued, re-run, finishes."""
    os.environ["H_MODES"] = "hang,finish"; os.environ["H_DUR"] = "1"; os.environ["H_ADOPT"] = "1"
    [tid] = ctx.mk()
    spawn = make_spawn_fn("none")
    tick = lambda: one_tick(spawn, ttl=900)
    res.check("first worker starts", ctx.wait(lambda: len(ctx.starts(tid)) == 1, 20, tick=tick))
    pid = start_pids(ctx, tid)[0]
    kill9(pid)
    res.check("crash detected: card leaves 'running' without waiting for TTL",
              ctx.wait(lambda: ctx.row(tid)["status"] != "running" or len(ctx.starts(tid)) > 1, 20, tick=tick),
              ctx.events(tid))
    res.check("card re-run and completed",
              ctx.wait(lambda: ctx.row(tid)["status"] == "done", 40, tick=tick), ctx.row(tid))
    res.check("I1 no card lost (done, 2 runs)", ctx.row(tid)["status"] == "done" and len(ctx.runs(tid)) == 2, ctx.runs(tid))
    res.check("I2 no concurrent duplicate", not ctx.concurrent_dups(), ctx.concurrent_dups())
    res.check("executions == 2 (crash + retry)", len(ctx.starts(tid)) == 2, len(ctx.starts(tid)))
    res.notes.append(f"events: {ctx.events(tid)}")


def s1b_worker_sigkill_twice(ctx: Ctx, res: Result):
    """(a') two kills trip the circuit breaker (failure_limit=2): card parked blocked, not lost."""
    os.environ["H_MODES"] = "hang"; os.environ["H_ADOPT"] = "1"
    [tid] = ctx.mk()
    spawn = make_spawn_fn("none")
    tick = lambda: one_tick(spawn, ttl=900)
    for n in (1, 2):
        ok = ctx.wait(lambda: len(ctx.starts(tid)) >= n, 40, tick=tick)
        res.check(f"worker #{n} started", ok, ctx.events(tid))
        if not ok:
            return
        kill9(start_pids(ctx, tid)[n - 1])
        ctx.wait(lambda: ctx.row(tid)["status"] != "running", 15, tick=tick)
    row = ctx.row(tid)
    res.check("I1 card not lost: parked 'blocked' after limit", row["status"] == "blocked", row)
    res.check("I2 no concurrent duplicate", not ctx.concurrent_dups())
    res.notes.append(f"events: {ctx.events(tid)}; a human must unblock it, nothing alerts by itself")


def s2_dispatcher_sigkill(ctx: Ctx, res: Result):
    """(b) gateway/dispatcher SIGKILLed mid-run; worker survives; new dispatcher must not duplicate."""
    os.environ["H_MODES"] = "finish"; os.environ["H_DUR"] = "6"; os.environ["H_ADOPT"] = "1"
    [tid] = ctx.mk()
    d1 = ctx.spawn_dispatcher()
    res.check("worker started under dispatcher #1", ctx.wait(lambda: len(ctx.starts(tid)) == 1, 25))
    kill9(d1.pid)
    time.sleep(0.5)
    res.check("worker survived dispatcher death (own session)", pid_alive(start_pids(ctx, tid)[0]))
    ctx.spawn_dispatcher(H_TTL=2)  # TTL 2s: expiry is hit while worker is still alive
    res.check("card completes", ctx.wait(lambda: ctx.row(tid)["status"] == "done", 40), ctx.row(tid))
    res.check("I2 exactly one execution (no respawn beside live worker)",
              len(ctx.starts(tid)) == 1 and not ctx.concurrent_dups(), read_xlog(ctx.xlog))
    res.check("I1 no card lost", ctx.row(tid)["status"] == "done")
    res.notes.append(f"events: {ctx.events(tid)}")


def s2b_die_before_spawn(ctx: Ctx, res: Result):
    """(b') dispatcher dies after the claim commit, before any worker exists. Lease must expire, then reclaim."""
    os.environ["H_MODES"] = "finish"; os.environ["H_DUR"] = "1"; os.environ["H_ADOPT"] = "1"
    ttl = 4
    [tid] = ctx.mk()
    d1 = ctx.spawn_dispatcher(H_FAULT="die_before_popen", H_TTL=ttl)
    ctx.wait(lambda: d1.poll() is not None, 25)
    r = ctx.row(tid)
    t_claim = time.time()
    res.check("claim committed, then dispatcher died (card 'running', no worker)",
              r["status"] == "running" and r["worker_pid"] is None and not ctx.starts(tid), r)
    ctx.spawn_dispatcher(H_TTL=ttl)
    time.sleep(1.0)
    res.check("NOT reclaimed before lease expiry", ctx.row(tid)["status"] == "running" and "reclaimed" not in ctx.events(tid),
              ctx.events(tid))
    ok = ctx.wait(lambda: "reclaimed" in ctx.events(tid), 30)
    res.check("reclaimed after lease expiry", ok, ctx.events(tid))
    res.notes.append(f"reclaim latency after crash ~{time.time() - t_claim:.1f}s with ttl={ttl}s (+ tick interval)")
    res.check("re-run and completed once", ctx.wait(lambda: ctx.row(tid)["status"] == "done", 30)
              and len(ctx.starts(tid)) == 1, ctx.row(tid))
    res.check("I2 no concurrent duplicate", not ctx.concurrent_dups())


def _s2c(ctx: Ctx, res: Result, adopt: str):
    os.environ["H_MODES"] = "finish"; os.environ["H_DUR"] = "9"; os.environ["H_ADOPT"] = adopt
    [tid] = ctx.mk()
    d1 = ctx.spawn_dispatcher(H_FAULT="die_after_popen", H_TTL=3)
    ctx.wait(lambda: d1.poll() is not None, 25)
    res.check("dispatcher died after Popen, before worker pid was recorded",
              ctx.row(tid)["status"] == "running" and len(ctx.starts(tid)) >= 1, ctx.row(tid))
    ctx.spawn_dispatcher(H_TTL=3)
    ctx.wait(lambda: ctx.row(tid)["status"] == "done", 45)
    time.sleep(1)
    res.check("I2 no concurrent duplicate execution", not ctx.concurrent_dups() and len(ctx.starts(tid)) == 1,
              read_xlog(ctx.xlog))
    res.check("I1 card done", ctx.row(tid)["status"] == "done", ctx.row(tid))
    res.notes.append(f"events: {ctx.events(tid)}")


def s2c_orphan_noadopt(ctx, res):
    """(b'') pid-record window, worker does NOT self-register (what a bare worker would do)."""
    res.expected_gap = True
    _s2c(ctx, res, "0")


def s2d_orphan_adopt(ctx, res):
    """(b''') pid-record window, worker self-registers via adopt_worker_pid (the shipped mitigation)."""
    _s2c(ctx, res, "1")


def s3_power_cut(ctx: Ctx, res: Result):
    """(c) dispatcher AND worker SIGKILLed together mid-run; DB files left as-is; recovery on restart."""
    os.environ["H_MODES"] = "hang,finish"; os.environ["H_DUR"] = "1"; os.environ["H_ADOPT"] = "1"
    [tid] = ctx.mk()
    d1 = ctx.spawn_dispatcher(H_TTL=900)
    res.check("worker started", ctx.wait(lambda: len(ctx.starts(tid)) == 1, 25))
    wpid = start_pids(ctx, tid)[0]
    kill9(d1.pid); kill9(wpid)  # no clean shutdown, no WAL checkpoint
    time.sleep(0.5)
    snap = ctx.root / ctx.name / "snapshot"
    snap.mkdir(exist_ok=True)
    for suffix in ("", "-wal", "-shm"):
        f = Path(str(ctx.db) + suffix)
        if f.exists():
            shutil.copy2(f, snap / f.name)
    res.check("integrity_check on files left by the kill", ctx.integrity(snap / "kanban.db") == "ok",
              ctx.integrity(snap / "kanban.db"))
    res.check("card still 'running' with a dead holder (state frozen at kill)",
              ctx.row(tid)["status"] == "running" and not pid_alive(wpid), ctx.row(tid))
    t0 = time.time()
    ctx.spawn_dispatcher(H_TTL=900)
    res.check("recovered: re-run and completed", ctx.wait(lambda: ctx.row(tid)["status"] == "done", 60), ctx.row(tid))
    res.notes.append(f"recovery ~{time.time() - t0:.1f}s after restart even with a 900s lease (pid liveness, not TTL)")
    res.check("I1 no card lost, 2 runs", len(ctx.runs(tid)) == 2, ctx.runs(tid))
    res.check("I2 no concurrent duplicate", not ctx.concurrent_dups())
    res.notes.append(f"events: {ctx.events(tid)}")


def s3b_abrupt_db_close(ctx: Ctx, res: Result):
    """(c') writer SIGKILLed mid-transaction, repeatedly: DB opens clean, no half-written cards."""
    bad = []
    total = 0
    for round_ in range(6):
        e = dict(os.environ)
        p = subprocess.Popen([sys.executable, str(HERE), "churn"], env=e, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, start_new_session=True)
        time.sleep(0.7 + 0.11 * round_)
        kill9(p.pid); p.wait()
        if ctx.integrity() != "ok":
            bad.append(("integrity", round_))
        c = ctx.conn()
        total = c.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
        orphans = c.execute("SELECT COUNT(*) FROM tasks t WHERE NOT EXISTS "
                            "(SELECT 1 FROM task_events e WHERE e.task_id=t.id AND e.kind='created')").fetchone()[0]
        c.close()
        if orphans:
            bad.append(("task_without_created_event", round_, orphans))
    res.check("integrity_check ok after 6 mid-write SIGKILLs", not any(b[0] == "integrity" for b in bad), bad)
    res.check("every card has its 'created' event (create is atomic)", not any(b[0] != "integrity" for b in bad), bad)
    res.notes.append(f"{total} cards survived; PRAGMA synchronous=FULL is set in connect()")


def s4a_stalled_no_heartbeat(ctx: Ctx, res: Result):
    """(d) hung worker, PID alive, never heartbeated, lease expired: spec wants reclaim."""
    res.expected_gap = True
    os.environ["H_MODES"] = "hang"; os.environ["H_ADOPT"] = "1"
    [tid] = ctx.mk()
    spawn = make_spawn_fn("none")
    tick = lambda: one_tick(spawn, ttl=2)
    ctx.wait(lambda: len(ctx.starts(tid)) == 1, 20, tick=tick)
    t0 = time.time()
    while time.time() - t0 < 12:
        tick(); time.sleep(0.5)
    evs = ctx.events(tid)
    res.check("lease expired (>=1 claim_extended)", "claim_extended" in evs, evs)
    res.check("stalled card reclaimed after lease expiry", "reclaimed" in evs or len(ctx.starts(tid)) > 1,
              f"status={ctx.row(tid)['status']} events={evs}")
    res.check("I2 no concurrent duplicate", not ctx.concurrent_dups())
    res.notes.append("a live PID that never heartbeats keeps its lease forever (extended every expiry); "
                     "only max_runtime_seconds, if set on the card, ends it")


def s4b_stalled_heartbeat_then_hang(ctx: Ctx, res: Result):
    """(d') worker heartbeats once then wedges: reclaimed once heartbeat age > HB max-stale (patched 4s)."""
    os.environ["H_MODES"] = "hb_then_hang,finish"; os.environ["H_DUR"] = "1"; os.environ["H_ADOPT"] = "1"
    [tid] = ctx.mk()
    kb, _, _ = ctx.kb, None, None
    kb.DEFAULT_CLAIM_HEARTBEAT_MAX_STALE_SECONDS = 4  # production value is 3600
    # _extend_live_stale_claim() ignores the per-claim ttl and re-reads the env/default (900s),
    # so the lease length must come from the env here or the second expiry is 15 minutes away.
    os.environ["HERMES_KANBAN_CLAIM_TTL_SECONDS"] = "2"
    spawn = make_spawn_fn("none")
    tick = lambda: one_tick(spawn, ttl=2)
    ok = ctx.wait(lambda: "reclaimed" in ctx.events(tid), 40, tick=tick)
    res.check("wedged worker reclaimed after lease expiry + heartbeat staleness", ok, ctx.events(tid))
    res.check("old worker killed (not left beside the successor)",
              ctx.wait(lambda: not pid_alive(start_pids(ctx, tid)[0]), 10, tick=tick))
    res.check("successor completes", ctx.wait(lambda: ctx.row(tid)["status"] == "done", 40, tick=tick), ctx.row(tid))
    res.check("I2 no concurrent duplicate", not ctx.concurrent_dups(), ctx.concurrent_dups())
    res.notes.append("production staleness window is 3600s (patched to 4s here); lease length forced via env "
                     "because a live-PID extension ignores the claim's own ttl (gap G4)")


def s4c_max_runtime(ctx: Ctx, res: Result):
    """(d'') max_runtime_seconds bounds a hung worker with no heartbeat at all."""
    os.environ["H_MODES"] = "hang,finish"; os.environ["H_DUR"] = "1"; os.environ["H_ADOPT"] = "1"
    [tid] = ctx.mk(max_runtime_seconds=3)
    spawn = make_spawn_fn("none")
    tick = lambda: one_tick(spawn, ttl=900)
    ok = ctx.wait(lambda: "timed_out" in ctx.events(tid), 40, tick=tick)
    res.check("timed_out after max_runtime_seconds", ok, ctx.events(tid))
    res.check("re-run completes", ctx.wait(lambda: ctx.row(tid)["status"] == "done", 40, tick=tick), ctx.row(tid))
    res.check("I2 no concurrent duplicate", not ctx.concurrent_dups())


def s5_racing_dispatchers(ctx: Ctx, res: Result):
    """Three dispatchers hammer one board: each card executes exactly once."""
    os.environ["H_MODES"] = "finish"; os.environ["H_DUR"] = "1"; os.environ["H_ADOPT"] = "1"
    ids = ctx.mk(12)
    for _ in range(3):
        ctx.spawn_dispatcher(H_TTL=900, H_INTERVAL=0.05)
    ok = ctx.wait(lambda: all(ctx.row(t)["status"] == "done" for t in ids), 120)
    res.check("all 12 cards done", ok, {t: ctx.row(t)["status"] for t in ids})
    counts = {t: len(ctx.starts(t)) for t in ids}
    res.check("I2 each card executed exactly once", all(v == 1 for v in counts.values()), counts)
    res.check("no concurrent duplicates", not ctx.concurrent_dups())


SCENARIOS = [
    ("S1", s1_worker_sigkill), ("S1b", s1b_worker_sigkill_twice),
    ("S2", s2_dispatcher_sigkill), ("S2b", s2b_die_before_spawn),
    ("S2c", s2c_orphan_noadopt), ("S2d", s2d_orphan_adopt),
    ("S3", s3_power_cut), ("S3b", s3b_abrupt_db_close),
    ("S4a", s4a_stalled_no_heartbeat), ("S4b", s4b_stalled_heartbeat_then_hang), ("S4c", s4c_max_runtime),
    ("S5", s5_racing_dispatchers),
]


def main():
    if len(sys.argv) > 1 and sys.argv[1] in ("worker", "dispatcher", "churn"):
        # child roles: env already pinned by the parent
        sys.path.insert(0, str(AGENT_DIR))
        {"worker": role_worker, "dispatcher": role_dispatcher, "churn": role_churn}[sys.argv[1]]()
        return
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", default="")
    ap.add_argument("--out", default=str(HERE.parent / "results"))
    a = ap.parse_args()
    out = Path(a.out); out.mkdir(parents=True, exist_ok=True)
    root = Path(tempfile.mkdtemp(prefix="kanban-harness-", dir=os.environ.get("TMPDIR")))
    only = {s for s in a.only.split(",") if s}
    results = []
    for sid, fn in SCENARIOS:
        _kb0 = None
        if only and sid not in only:
            continue
        # scenario state lives in a fresh scratch board; kb module state is process-global, so reset
        ctx = Ctx(sid, root, out)
        res = Result(sid, (fn.__doc__ or "").strip())
        t0 = time.time()
        try:
            fn(ctx, res)
        except Exception:
            res.check("scenario ran without harness error", False, traceback.format_exc()[-600:])
        finally:
            ctx.cleanup()
            ctx.kb.DEFAULT_CLAIM_HEARTBEAT_MAX_STALE_SECONDS = 3600
            os.environ.pop("HERMES_KANBAN_CLAIM_TTL_SECONDS", None)
        print(f"[{'PASS' if res.passed else 'FAIL'}] {sid} ({time.time() - t0:.0f}s) {res.title}")
        for name, ok, detail in res.checks:
            print(f"    {'ok  ' if ok else 'FAIL'} {name}" + ("" if ok else f"  -> {detail}"))
        results.append({"id": sid, "title": res.title, "passed": res.passed, "expected_gap": res.expected_gap,
                        "checks": [{"name": n, "ok": o, "detail": d} for n, o, d in res.checks], "notes": res.notes})
    (out / "results.json").write_text(json.dumps(results, indent=2))
    lines = ["| case | verdict | checks | note |", "|---|---|---|---|"]
    for r in results:
        failed = [c["name"] for c in r["checks"] if not c["ok"]]
        lines.append(f"| {r['id']} {r['title'].splitlines()[0]} | {'PASS' if r['passed'] else 'FAIL'} | "
                     f"{sum(c['ok'] for c in r['checks'])}/{len(r['checks'])} | "
                     f"{'; '.join(failed) if failed else '-'} |")
    (out / "results.md").write_text("\n".join(lines) + "\n")
    print("\n" + "\n".join(lines))
    print(f"\nscratch board: {root}  (logs: {out})")
    sys.exit(0 if all(r["passed"] or r["expected_gap"] for r in results) else 1)


if __name__ == "__main__":
    main()
