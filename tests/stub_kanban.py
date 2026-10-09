#!/usr/bin/env python3
"""stub_kanban.py - a stand-in for the board's CLI, speaking the bridge contract.

docs/kanban-fleet-bridge.md is the contract; the board half of it (claim-next /
release / fail) is implemented separately. This stub is what lets the FLEET half
be exercised now, and by any end-to-end run later: same verb names, same request
flags, same JSON, same exit codes (0 ok / 2 refusal / 3 unreachable), and the
same compare-and-set on (task_id, claim_lock) that decides a claim race.

State is one JSON file (--state FILE, or FLEET_STUB_STATE). Every mutating verb
takes an exclusive flock on the state file for the whole read-modify-write, so
two stub processes racing for the same board really do serialize and exactly one
wins - a lost race in the tests is a lost race, not a simulation of one.

Two contract readings this stub fixes, so both sides can be checked against one
executable answer:
  * the claim derives the lease owner as "<worker>:kb-<task_id>" from the base
    worker id the fleet passes, and returns it as claim_lock;
  * a heartbeat for a lease we no longer hold answers exit 0 with
    {"ok":false,"reason":"lost_claim"} (the E2 row), while release/complete/fail
    answer exit 2 - a heartbeat is a question, a report is an assertion.

Usage (also the tests' API):
    stub_kanban.py [--state FILE] [--unreachable] [--fail-verb VERB] <verb> ...
    add --id t_1 --title "..." [--priority N] [--workspace PATH]
    state | calls | show <id> | list
"""
from __future__ import annotations

import argparse
import fcntl
import json
import os
import sys
import time

CONTRACT_STATUSES = ("triage", "todo", "scheduled", "ready", "running",
                     "blocked", "review", "done", "archived")
TTL_DEFAULT = 900


def state_path(args):
    return args.state or os.environ.get("FLEET_STUB_STATE") or "/tmp/fleet-stub-kanban.json"


def load(path):
    try:
        with open(path) as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {"tasks": {}, "calls": []}


def save(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(data, fh, indent=2)
    os.replace(tmp, path)


def emit(payload, code=0):
    print(json.dumps(payload))
    return code


def record(data, verb, **fields):
    data.setdefault("calls", []).append(
        {"verb": verb, "at": time.time(), **fields})


def public(task):
    """The task object the contract's claim payload carries."""
    return {
        "id": task["id"], "title": task.get("title", ""), "body": task.get("body", ""),
        "assignee": task.get("assignee", "default"), "status": task["status"],
        "priority": task.get("priority", 0), "workspace_kind": task.get("workspace_kind", "scratch"),
        "workspace_path": task.get("workspace_path"),
        "branch_name": task.get("branch_name"), "project_id": task.get("project_id"),
        "created_at": task.get("created_at", 0), "current_run_id": task.get("current_run_id", 1),
        "claim_lock": task.get("claim_lock"), "claim_expires": task.get("claim_expires"),
        "skills": task.get("skills", []), "model_override": None, "provider_override": None,
        "max_runtime_seconds": None, "completion_contract": "local-only",
        "body_ref": os.path.join(task.get("workspace_path") or "", ".kanban", "task.json"),
    }


def main(argv=None):
    p = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    p.add_argument("--state")
    p.add_argument("--unreachable", action="store_true",
                   help="answer every verb with exit 3 (board unreachable)")
    p.add_argument("--fail-verb", action="append", default=[],
                   help="answer this verb with exit 3 (repeatable)")
    sub = p.add_subparsers(dest="verb")

    def jp(name, **kw):
        """Every verb takes --json, exactly as the real CLI's _json_flag does."""
        sp = sub.add_parser(name, **kw)
        sp.add_argument("--json", action="store_true", help="emit JSON (the stub always does)")
        return sp

    a = jp("add")
    a.add_argument("--id", required=True)
    a.add_argument("--title", default="")
    a.add_argument("--body", default="")
    a.add_argument("--priority", type=int, default=0)
    a.add_argument("--workspace", default=None)
    a.add_argument("--status", default="ready", choices=list(CONTRACT_STATUSES))

    c = jp("claim-next")
    c.add_argument("--worker", required=True)
    c.add_argument("--ttl", type=int, default=TTL_DEFAULT)
    c.add_argument("--status", default="ready")

    h = jp("heartbeat")
    h.add_argument("task_id")
    h.add_argument("--worker", required=True)
    h.add_argument("--ttl", type=int, default=None)
    h.add_argument("--note", default=None)

    r = jp("release")
    r.add_argument("task_id")
    r.add_argument("--worker", required=True)
    r.add_argument("--reason", default="")

    k = jp("complete")
    k.add_argument("task_id")
    k.add_argument("--worker", required=True)
    k.add_argument("--summary", default=None)
    k.add_argument("--result", default=None)
    k.add_argument("--metadata", default=None)

    f = jp("fail")
    f.add_argument("task_id")
    f.add_argument("--worker", required=True)
    f.add_argument("--error", default="")
    f.add_argument("--retryable", action="store_true")

    s = jp("show")
    s.add_argument("task_id")

    jp("list")
    jp("state")
    jp("calls")
    jp("unreachable-off")

    u = jp("unreachable-set")
    u.add_argument("--verb", dest="target_verb", required=True)
    u.add_argument("--off", action="store_true")

    args = p.parse_args(argv)
    path = state_path(args)

    # The stub honours --json like the real CLI: stdout stays parseable.
    if args.verb is None:
        p.print_help()
        return 2

    # A verb can be taken offline at runtime (unreachable-set) as well as at
    # launch (--unreachable / --fail-verb), so a test can simulate an outage and
    # a recovery against one running board.
    offline = set(args.fail_verb)
    try:
        with open(path) as fh:
            offline |= set((json.load(fh) or {}).get("unreachable_verbs") or [])
    except (OSError, ValueError):
        pass
    if args.unreachable or args.verb in offline:
        return emit({"ok": False, "reason": "board unreachable (stub)"}, 3)

    os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
    lock_path = path + ".lock"
    with open(lock_path, "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        data = load(path)
        tasks = data.setdefault("tasks", {})
        now = time.time()

        if args.verb == "state":
            return emit(data)
        if args.verb == "calls":
            return emit({"calls": data.get("calls", [])})
        if args.verb == "unreachable-off":
            data.pop("unreachable", None)
            save(path, data)
            return emit({"ok": True})
        if args.verb == "unreachable-set":
            verbs = set(data.get("unreachable_verbs") or [])
            if args.off:
                verbs.discard(args.target_verb)
            else:
                verbs.add(args.target_verb)
            data["unreachable_verbs"] = sorted(verbs)
            save(path, data)
            return emit({"ok": True, "unreachable_verbs": sorted(verbs)})

        if args.verb == "add":
            tasks[args.id] = {
                "id": args.id, "title": args.title, "body": args.body,
                "assignee": "default", "status": args.status, "priority": args.priority,
                "workspace_path": args.workspace or os.path.join("/tmp", "stub-ws", args.id),
                "created_at": now, "current_run_id": 1, "claim_lock": None,
                "claim_expires": None, "last_heartbeat_at": None,
            }
            record(data, "add", task_id=args.id)
            save(path, data)
            return emit({"ok": True, "task": public(tasks[args.id])})

        if args.verb == "claim-next":
            ready = [t for t in tasks.values()
                     if t["status"] == args.status and not t.get("claim_lock")]
            ready.sort(key=lambda t: (-t.get("priority", 0), t.get("created_at", 0), t["id"]))
            if not ready:
                record(data, "claim-next", worker=args.worker, task_id=None)
                save(path, data)
                return emit({"ok": True, "task": None, "lease_expires": None})
            task = ready[0]
            # the derived lease owner, exactly as the contract's example shows
            lock_id = f"{args.worker}:kb-{task['id']}"
            task["status"] = "running"
            task["claim_lock"] = lock_id
            task["claim_expires"] = now + args.ttl
            task["last_heartbeat_at"] = now
            record(data, "claim-next", worker=args.worker, task_id=task["id"], claim_lock=lock_id)
            save(path, data)
            return emit({"ok": True, "task": public(task), "lease_expires": task["claim_expires"]})

        task = tasks.get(args.task_id)
        if task is None:
            record(data, args.verb, task_id=args.task_id, error="unknown_task")
            save(path, data)
            return emit({"ok": False, "reason": "unknown task"}, 2)

        if args.verb == "heartbeat":
            if task.get("claim_lock") != args.worker:
                record(data, "heartbeat", task_id=args.task_id, error="lost_claim")
                save(path, data)
                return emit({"ok": False, "reason": "lost_claim"})
            task["claim_expires"] = now + TTL_DEFAULT
            task["last_heartbeat_at"] = now
            record(data, "heartbeat", task_id=args.task_id, worker=args.worker)
            save(path, data)
            return emit({"ok": True, "lease_expires": task["claim_expires"]})

        # every remaining verb is an assertion about a lease we must still hold
        if task.get("claim_lock") != args.worker and task["status"] != "done":
            record(data, args.verb, task_id=args.task_id, error="not_lease_owner")
            save(path, data)
            return emit({"ok": False, "reason": "not the lease owner"}, 2)

        if args.verb == "release":
            if task["status"] in ("done", "archived"):
                save(path, data)
                return emit({"ok": True, "status": task["status"], "already_terminal": True})
            task["status"] = "ready"
            task["claim_lock"] = None
            task["claim_expires"] = None
            record(data, "release", task_id=args.task_id, reason=args.reason)
            save(path, data)
            return emit({"ok": True, "status": "ready", "already_terminal": False})

        if args.verb == "complete":
            if task["status"] in ("done", "blocked", "archived"):
                save(path, data)
                return emit({"ok": True, "status": task["status"], "already_terminal": True})
            task["status"] = "done"
            task["claim_lock"] = None
            task["claim_expires"] = None
            task["summary"] = args.summary
            task["result"] = args.result
            task["metadata"] = args.metadata
            record(data, "complete", task_id=args.task_id, summary=args.summary,
                   result=args.result, metadata=args.metadata)
            save(path, data)
            return emit({"ok": True, "status": "done", "already_terminal": False})

        if args.verb == "fail":
            if task["status"] in ("done", "blocked", "archived"):
                save(path, data)
                return emit({"ok": True, "status": task["status"], "already_terminal": True})
            task["status"] = "ready" if args.retryable else "blocked"
            task["claim_lock"] = None
            task["claim_expires"] = None
            task["error"] = args.error
            record(data, "fail", task_id=args.task_id, error=args.error,
                   retryable=bool(args.retryable))
            save(path, data)
            return emit({"ok": True, "status": task["status"], "already_terminal": False})

        if args.verb == "show":
            return emit({"ok": True, "task": public(task), "status": task["status"]})
        if args.verb == "list":
            return emit({"ok": True, "tasks": [public(t) for t in tasks.values()]})

    return emit({"ok": False, "reason": f"unhandled verb {args.verb}"}, 2)


if __name__ == "__main__":
    raise SystemExit(main())
