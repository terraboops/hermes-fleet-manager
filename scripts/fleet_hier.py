#!/usr/bin/env python3
"""Live entry point for the fleet hierarchy: CLI, real effects, persistence, event routing.

Spec: docs/agent-hierarchy.md. Wires fleet_contract / fleet_governance / fleet_manager /
fleet_review to the real fleet: the real dispatch path (fleet_dispatch.sh), real transcripts
(fleet_transcript), real liveness (fleet_state), and the daemon's event stream (route_event).

Every goal is rebuilt from a JSON file on each call, so the CLI is stateless between runs.
Flat sessions are untouched: a session absent from the hierarchy registry gets None from
route_event and the daemon keeps its own path.

Usage:
  fleet_hier.py open     <goal> <manager_session> --text T --boundary P[,P] --acceptance F.json
                         --deadline-min N --budget N --reviewer S
  fleet_hier.py assign   <goal> <session> --text T --boundary P[,P] --acceptance F.json --deadline-min N
  fleet_hier.py confirm  <goal> [contract_id]     # prove directive delivery from the child transcript
  fleet_hier.py tick     <goal>                   # one overwatch pass (real nudges / check-ins)
  fleet_hier.py collect  <goal>                   # read emitted tokens, run review, settle children
  fleet_hier.py result   <goal>                   # manager token, emission-proven, or nothing
  fleet_hier.py announce <goal>                   # have the manager session emit its derived token
  fleet_hier.py intake   <goal> <description> <path>...   # out-of-scope work is flagged, not taken
  fleet_hier.py status   [goal]
  fleet_hier.py route    <session> <event text> [--hour H]    # what the daemon would do
  fleet_hier.py gates                              # pending classifier blocks / choices
  fleet_hier.py approve  <event_id> --as <principal> --answer A   # only `human` may resolve
  fleet_hier.py close    <goal>                    # drop goal state and its registry nodes
"""
import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.realpath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))

import fleet_contract as fc          # noqa: E402
import fleet_governance as fg        # noqa: E402
import fleet_manager as fm           # noqa: E402
import fleet_review as fr            # noqa: E402

BASE = os.path.expanduser(os.environ.get(
    "FLEET_HIER_DIR", "~/.hermes/scripts/cc-watch/hierarchy"))
REGISTRY_PATH = os.path.expanduser(os.environ.get("FLEET_HIER_REGISTRY", fm.DEFAULT_PATH))
FLEET_REGISTRY = os.path.expanduser(os.environ.get(
    "FLEET_REGISTRY", "~/.hermes/scripts/cc-watch/fleet_registry.json"))
DISPATCH = os.environ.get("FLEET_HIER_DISPATCH", os.path.join(HERE, "fleet_dispatch.sh"))
STATE_CLI = os.path.join(HERE, "fleet_state.py")

# Same pattern the live gate treats as a classifier block (laya_gate.py); a test keeps them equal.
CLASSIFIER_BLOCK_RE = re.compile(
    r"\[Production Deploy\]|\[Security Weaken\]|\[Self-Approval\]|\[Git Destructive\]")
RECEIPT_RE = re.compile(r"^(ACK-OK|WATCH-SATISFIED|WATCH-EXPIRED)\b|^DONE[-_.]", re.I)
STALL_RE = re.compile(r"^STALL-\S+: no new transcript output for (\d+)s", re.I)


class HierCliError(RuntimeError):
    pass


# ---------------------------------------------------------------- real effects

def _fleet_entries():
    try:
        d = json.load(open(FLEET_REGISTRY))
    except Exception:
        return []
    return d.get("sessions", d if isinstance(d, list) else [])


def fleet_entry(session):
    for e in _fleet_entries():
        if session in (e.get("name"), e.get("short")):
            return e
    return None


def transcript_of(session):
    """Transcript path of a registered fleet session, or None. Never guessed from the cwd."""
    e = fleet_entry(session)
    if not e:
        return None
    import fleet_transcript as ft
    path, _src = ft.resolve(e, tmux=e.get("name"), use_cache=False)
    return path


def state_of(session):
    """'dead' | 'idle' | 'working' from the same fingerprint the overwatch uses."""
    out = subprocess.run([sys.executable, STATE_CLI, session], capture_output=True,
                         text=True, timeout=30).stdout.strip()
    head = out.split("|", 1)[0].strip().upper()
    if head == "DEAD":
        return "dead"
    if head == "IDLE":
        return "idle"
    return "working"


def last_growth(session):
    p = transcript_of(session)
    try:
        return os.path.getmtime(p)
    except Exception:
        return 0.0


def _dispatch_text(session, text, timeout=90):
    """Deliver text through the real dispatch path. Raises unless it LANDED."""
    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False, dir=os.environ.get("TMPDIR")) as fh:
        fh.write(text)
        path = fh.name
    try:
        r = subprocess.run(["bash", DISPATCH, session, path, str(timeout)],
                           capture_output=True, text=True, timeout=timeout + 60)
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass
    out = (r.stdout + r.stderr).strip()
    if r.returncode != 0 or "LANDED" not in out:
        raise HierCliError("dispatch to %s did not land (exit %d): %s"
                           % (session, r.returncode, out[-300:]))
    return out


def real_effects(reviewer, deep=None, rate=0.3, seed=None, allow_same_model=True):
    """The injected callables, backed by the real fleet."""
    universe = []   # filled by the caller once the ledger exists

    def producer_of(child):
        return child.owner_session

    verify = fr.build_verify(reviewer, producer_of, transcript_of, lambda: list(universe),
                             deep=deep, rate=rate, seed=seed, allow_same_model=allow_same_model)
    return {
        "dispatch": _dispatch_text,
        "send": _dispatch_text,
        "state_of": state_of,
        "last_growth": last_growth,
        "transcript_of": transcript_of,
        "verify": verify,
    }, universe


# ---------------------------------------------------------------- persistence

def _goal_path(goal):
    if not fc.GOAL_RE.match(goal or ""):
        raise HierCliError("bad goal id %r (lowercase letters and digits, starts with a letter)" % goal)
    return os.path.join(BASE, "goals", goal + ".json")


def _atomic_write(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(obj, fh, indent=1, sort_keys=True)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def _contract_to_json(c):
    return {"contract_id": c.contract_id, "goal": c.goal, "boundary": list(c.boundary),
            "acceptance": [{"check": a["check"], "positive": list(a["positive"]),
                            "negative": list(a["negative"])} for a in c.acceptance],
            "done_token": c.done_token, "blocked_token": c.blocked_token,
            "deadline": c.deadline, "parent_contract_id": c.parent_contract_id,
            "chain": list(c.chain), "goal_short": c.goal_short, "owner_session": c.owner_session}


def _contract_from_json(d):
    return fc.Contract(
        contract_id=d["contract_id"], goal=d["goal"], boundary=tuple(d["boundary"]),
        acceptance=tuple(fc._freeze(a) for a in d["acceptance"]),
        done_token=d["done_token"], blocked_token=d["blocked_token"],
        deadline=float(d["deadline"]), parent_contract_id=d["parent_contract_id"],
        chain=tuple(d["chain"]), goal_short=d["goal_short"], owner_session=d["owner_session"])


def save_goal(goal, mgr, reviewer):
    state = {}
    for cid, st in mgr.ledger.state.items():
        s = dict(st)
        rep = s.get("report")
        s["report"] = None if rep is None else {"contract_id": rep.contract_id,
                                                "pointer": rep.pointer,
                                                "verdict": rep.verdict, "note": rep.note}
        state[cid] = s
    _atomic_write(_goal_path(goal), {
        "goal": goal, "manager": mgr.session, "reviewer": reviewer,
        "own": _contract_to_json(mgr.own),
        "budget": {"units": mgr.budget.units, "spent": mgr.budget.spent},
        "next": mgr._next,
        "children": [_contract_to_json(c) for c in mgr.ledger.children.values()],
        "state": state, "watch": mgr._watch, "issued": mgr.issued, "flagged": mgr.flagged,
    })


def load_goal(goal, effects=None):
    """Rebuild the Manager for `goal` from disk. Returns (manager, reviewer)."""
    path = _goal_path(goal)
    if not os.path.exists(path):
        raise HierCliError("no such goal %r" % goal)
    d = json.load(open(path))
    reviewer = d["reviewer"]
    eff, universe = effects if effects else real_effects(reviewer)
    reg = fm.Registry(REGISTRY_PATH)
    own = _contract_from_json(d["own"])
    mgr = fm.Manager(session=d["manager"], registry=reg, own=own,
                     budget=fm.Budget(d["budget"]["units"], d["budget"]["spent"]),
                     escalate=_escalator(goal, d["manager"]), **eff)
    mgr._next = d["next"]
    for cd in d["children"]:
        c = _contract_from_json(cd)
        mgr.ledger.add_child(c)
    for cid, st in d["state"].items():
        rep = st.get("report")
        st = dict(st)
        st["report"] = fc.Report(**rep) if rep else None
        mgr.ledger.state[cid] = st
    mgr._watch = d["watch"]
    mgr.issued = d["issued"]
    mgr.flagged = d["flagged"]
    universe[:] = list(mgr.ledger.children)
    return mgr, reviewer


def _escalator(goal, manager):
    """A manager's escalation: routed up the levels to the human, never resolved here."""
    def escalate(kind, text):
        ev = fg.Event(fg.NEEDS_INPUT, manager, "%s: %s" % (kind, text),
                      id=_event_id(manager, kind + text))
        _log("escalation", {"goal": goal, "kind": kind, "text": text, "event": ev.id})
        route_event(manager, "HIER-%s %s: %s" % (kind.upper(), goal, text), force_kind=fg.NEEDS_INPUT)
    return escalate


# ---------------------------------------------------------------- logs and events

def _log(kind, payload):
    os.makedirs(BASE, exist_ok=True)
    with open(os.path.join(BASE, "events.jsonl"), "a") as fh:
        fh.write(json.dumps({"at": int(time.time()), "kind": kind, **payload}) + "\n")


def _event_id(session, text):
    return hashlib.sha1(("%s|%s" % (session, text)).encode()).hexdigest()[:10]


def read_new_events(offset_name="daemon"):
    """Escalation lines not yet seen by the named reader, as (session, text). For the daemon."""
    path = os.path.join(BASE, "events.jsonl")
    off_path = os.path.join(BASE, "offset-%s" % offset_name)
    try:
        with open(off_path) as fh:
            off = int(fh.read().strip() or 0)
    except Exception:
        off = 0
    try:
        size = os.path.getsize(path)
    except OSError:
        return []
    if size < off:
        off = 0
    out = []
    held_path = os.path.join(BASE, "held.json")
    try:
        with open(held_path) as fh:
            held = [tuple(x) for x in json.load(fh)]
    except Exception:
        held = []
    with open(path, "rb") as fh:
        fh.seek(off)
        data = fh.read()
    for ln in data.decode("utf-8", "replace").splitlines():
        try:
            o = json.loads(ln)
        except ValueError:
            continue
        if o.get("kind") == "deliver":
            out.append((o["session"], o["text"]))
        elif o.get("kind") == "hold":
            held.append((o["session"], o["text"]))
    if held and not fg.in_quiet_hours(time.localtime().tm_hour):
        out.extend(held)       # a held escalation is delivered when quiet hours end, never lost
        held = []
    _atomic_write(held_path, held)
    with open(off_path, "w") as fh:
        fh.write(str(off + len(data)))
    return out


# ---------------------------------------------------------------- event routing

def classify_kind(text):
    t = (text or "").strip()
    if CLASSIFIER_BLOCK_RE.search(t):
        return fg.CLASSIFIER_BLOCK
    if RECEIPT_RE.match(t):
        return fg.RECEIPT
    if STALL_RE.match(t):
        return fg.STOPPED
    if t.startswith("NEEDS-INPUT-") or t.startswith("HIER-"):
        return fg.NEEDS_INPUT
    return None


def _levels(registry, session):
    """Chain of governance levels from `session` up to the chief. Returns (leaf, [names])."""
    chief = fg.Level("chief", top=True)
    chief.gates = _load_gates()
    chain = []
    node = registry.nodes.get(session)
    while node is not None:
        chain.append(session)
        parent = node["parent_session"]
        if parent is None:
            break
        session, node = parent, registry.nodes.get(parent)
    level = chief
    for name in reversed(chain):
        level = fg.Level(name, parent=level)
    return level, chief, [n for n in chain]


def _load_gates():
    path = os.path.join(BASE, "gates.json")
    try:
        with open(path) as fh:
            raw = json.load(fh)
    except Exception:
        return []
    gates = []
    for g in raw:
        ev = fg.Event(**g["event"])
        gate = fg.Gate(ev)
        gate.resolved_by, gate.answer = g.get("resolved_by"), g.get("answer")
        gates.append(gate)
    return gates


def _save_gates(gates):
    _atomic_write(os.path.join(BASE, "gates.json"), [
        {"event": {"kind": g.event.kind, "origin": g.event.origin, "detail": g.event.detail,
                   "stopped_seconds": g.event.stopped_seconds, "id": g.event.id},
         "resolved_by": g.resolved_by, "answer": g.answer} for g in gates])


def route_event(session, text, hour=None, registry=None, force_kind=None, queue=True):
    """Route one daemon event through the governance levels.

    Returns None when `session` is not in the hierarchy (the flat path is untouched) or the
    event has no hierarchy meaning. Otherwise a dict: action (absorb|up|deliver|hold|drop),
    where, kind, trail (the levels it passed), id. A gate kind is never absorbed or dropped:
    it ends at the human level as deliver or hold. `queue` writes a deliver line the daemon
    picks up (CLI escalations); the daemon itself passes queue=False. A failure here answers None, so the daemon
    keeps its fallback and nothing is swallowed.
    """
    try:
        reg = registry or fm.Registry(REGISTRY_PATH)
        if session not in reg.nodes:
            return None
        kind = force_kind or classify_kind(text)
        if kind is None:
            return None
        if hour is None:
            hour = time.localtime().tm_hour
        leaf, chief, trail = _levels(reg, session)
        secs = 0.0
        m = STALL_RE.match((text or "").strip())
        if m:
            secs = float(m.group(1))
        ev = fg.Event(kind, session, text[:300], stopped_seconds=secs,
                      id=_event_id(session, text))
        before = len(chief.gates)
        known = {g.event.id for g in chief.gates}
        if kind in fg.GATE_KINDS and ev.id in known:
            action, where = fg.HOLD, "duplicate of a pending gate"
        else:
            action, where = leaf.route(ev, hour)
        if len(chief.gates) != before:
            _save_gates(chief.gates)
        out = {"action": action, "where": where, "kind": kind, "trail": trail + ["chief"],
               "id": ev.id}
        _log("route", {"session": session, "text": text[:200], **out})
        if action == fg.HOLD and queue and where == "quiet hours":
            _log("hold", {"session": session, "text": "%s [via %s]" % (text, " > ".join(trail))})
        if action == fg.DELIVER and queue:
            _log("deliver", {"session": session,
                             "text": "%s [via %s]" % (text, " > ".join(trail))})
        return out
    except Exception as e:   # never let routing lose an event
        _log("route-error", {"session": session, "error": repr(e)})
        return None


# ---------------------------------------------------------------- commands

def _acceptance(path):
    return json.load(open(os.path.expanduser(path)))


def _paths(s):
    return [p for p in (s or "").split(",") if p]


def cmd_open(a):
    if os.path.exists(_goal_path(a.goal)):
        raise HierCliError("goal %s already exists" % a.goal)
    reg = fm.Registry(REGISTRY_PATH)
    for s in (a.manager, a.reviewer):
        if not fleet_entry(s):
            raise HierCliError("%s is not a registered fleet session" % s)
    if a.reviewer == a.manager:
        raise fg.ReviewerNotIndependent("the manager cannot be its own reviewer")
    own = fc.new_contract(a.goal, ["m1"], a.text, _paths(a.boundary), _acceptance(a.acceptance),
                          deadline=time.time() + 60 * a.deadline_min, owner_session=a.manager)
    reg.register(a.manager, fm.MANAGER, goal=a.goal)
    try:
        eff, universe = real_effects(a.reviewer)
        mgr = fm.Manager(session=a.manager, registry=reg, own=own, budget=fm.Budget(a.budget),
                         escalate=_escalator(a.goal, a.manager), **eff)
        save_goal(a.goal, mgr, a.reviewer)
    except Exception:
        reg.nodes.pop(a.manager, None)
        reg._save()
        raise
    print("OPENED goal=%s manager=%s done_token=%s blocked_token=%s budget=%d"
          % (a.goal, a.manager, own.done_token, own.blocked_token, a.budget))


def cmd_assign(a):
    mgr, rv = load_goal(a.goal)
    if not fleet_entry(a.session):
        raise HierCliError("%s is not a registered fleet session" % a.session)
    c = mgr.assign(a.session, a.text, _paths(a.boundary), _acceptance(a.acceptance),
                   time.time() + 60 * a.deadline_min)
    save_goal(a.goal, mgr, rv)
    print("ASSIGNED %s -> %s done_token=%s" % (c.contract_id, a.session, c.done_token))


def cmd_confirm(a):
    mgr, rv = load_goal(a.goal)
    ids = [a.contract] if a.contract else list(mgr.ledger.children)
    bad = 0
    for cid in ids:
        ok = mgr.confirm(cid)
        print("%s %s" % (cid, "DELIVERED" if ok else "NOT-DELIVERED"))
        bad += 0 if ok else 1
    save_goal(a.goal, mgr, rv)
    return 1 if bad else 0


def cmd_tick(a):
    mgr, rv = load_goal(a.goal)
    acts = mgr.tick()
    save_goal(a.goal, mgr, rv)
    print(json.dumps(acts))


def _emitted(mgr, c):
    path = transcript_of(c.owner_session)
    if fc.emitted_by_assistant(path, c.blocked_token) >= 1:
        return c.blocked_token, "blocked", path
    if fc.emitted_by_assistant(path, c.done_token) >= 1:
        return c.done_token, "pass", path
    return None, None, path


def cmd_collect(a):
    mgr, rv = load_goal(a.goal)
    for cid, c in mgr.ledger.children.items():
        if mgr.ledger.state[cid]["token"] is not None:
            print("%s settled: %s" % (cid, mgr.ledger.state[cid]["why"]))
            continue
        tok, verdict, path = _emitted(mgr, c)
        if tok is None:
            print("%s pending: no token emitted yet" % cid)
            continue
        rep = fc.Report(cid, "transcript:%s" % path, verdict, "collected from transcript")
        ok, why = mgr.receive(cid, rep)
        print("%s %s: %s" % (cid, "ACCEPTED" if ok else "REFUSED", why))
    save_goal(a.goal, mgr, rv)


def cmd_result(a):
    mgr, rv = load_goal(a.goal)
    tok, rep, why = mgr.result_proven()
    print(json.dumps({"token": tok, "verdict": rep.verdict if rep else None, "why": why,
                      "pointer": rep.pointer if rep else None,
                      "manager_token_if_done": mgr.own.done_token,
                      "manager_token_if_blocked": mgr.own.blocked_token}))


def cmd_announce(a):
    """Ask the manager session to emit the token the ledger derived. Nothing derived, nothing sent."""
    mgr, rv = load_goal(a.goal)
    tok, rep = mgr.result()
    if tok is None:
        print("NOTHING-TO-ANNOUNCE: children unsettled, no manager token derived")
        return 1
    lines = ["You are the manager of goal %s (%s). The ledger has settled every child contract"
             % (a.goal, mgr.own.goal),
             "from their own transcripts and an independent review. Outcome: %s (%s)." % (rep.verdict, rep.note),
             "Report it upward by putting this token alone on its own line, nothing else on it:",
             tok]
    _dispatch_text(mgr.session, "\n".join(lines))
    print("ANNOUNCED %s to %s; run `result` to prove emission" % (tok, mgr.session))


def cmd_intake(a):
    mgr, rv = load_goal(a.goal)
    try:
        mgr.intake(a.description, a.paths)
        print("ACCEPTED")
    except fm.OutOfScope as e:
        print("OUT-OF-SCOPE: %s" % e)
        return 1
    finally:
        save_goal(a.goal, mgr, rv)


def cmd_status(a):
    goals = [a.goal] if a.goal else [f[:-5] for f in sorted(os.listdir(os.path.join(BASE, "goals")))
                                     if f.endswith(".json")] if os.path.isdir(os.path.join(BASE, "goals")) else []
    for g in goals:
        d = json.load(open(_goal_path(g)))
        print("goal %s manager=%s reviewer=%s budget=%d/%d" % (
            g, d["manager"], d["reviewer"], d["budget"]["spent"], d["budget"]["units"]))
        for c in d["children"]:
            st = d["state"][c["contract_id"]]
            print("  %s %s delivered=%s token=%s verified=%s why=%s" % (
                c["contract_id"], c["owner_session"], st["delivered"],
                st["token"] or "-", st["verified"], st["why"]))


def cmd_route(a):
    r = route_event(a.session, a.text, hour=a.hour)
    print(json.dumps(r) if r else "FLAT (not in the hierarchy, or no hierarchy meaning)")


def cmd_gates(a):
    for g in _load_gates():
        print("%s %s %s resolved_by=%s" % (g.event.id, g.event.kind, g.event.origin, g.resolved_by))


def cmd_approve(a):
    gates = _load_gates()
    chief = fg.Level("chief", top=True)
    chief.gates = gates
    try:
        chief.approve(a.principal, a.event, a.answer)
    except fg.GovernanceError as e:
        print("REFUSED: %s: %s" % (type(e).__name__, e))
        return 1
    _save_gates(gates)
    _log("approve", {"event": a.event, "principal": a.principal, "answer": a.answer})
    print("RESOLVED %s by %s" % (a.event, a.principal))


def cmd_close(a):
    mgr, rv = load_goal(a.goal)
    reg = mgr.registry
    for c in mgr.ledger.children.values():
        reg.nodes.pop(c.owner_session, None)
    reg.nodes.pop(mgr.session, None)
    reg._save()
    os.unlink(_goal_path(a.goal))
    print("CLOSED %s" % a.goal)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sp = ap.add_subparsers(dest="cmd", required=True)

    def add(name, fn, *args, **opts):
        p = sp.add_parser(name)
        p.set_defaults(fn=fn)
        for x in args:
            p.add_argument(x) if not isinstance(x, tuple) else p.add_argument(x[0], **x[1])
        for k, v in opts.items():
            p.add_argument("--" + k.replace("_", "-"), **v)
        return p

    add("open", cmd_open, "goal", "manager", text=dict(required=True), boundary=dict(required=True),
        acceptance=dict(required=True), deadline_min=dict(type=float, required=True),
        budget=dict(type=int, required=True), reviewer=dict(required=True))
    add("assign", cmd_assign, "goal", "session", text=dict(required=True),
        boundary=dict(required=True), acceptance=dict(required=True),
        deadline_min=dict(type=float, required=True))
    add("confirm", cmd_confirm, "goal", ("contract", dict(nargs="?")))
    add("tick", cmd_tick, "goal")
    add("collect", cmd_collect, "goal")
    add("result", cmd_result, "goal")
    add("announce", cmd_announce, "goal")
    add("intake", cmd_intake, "goal", "description", ("paths", dict(nargs="*")))
    add("status", cmd_status, ("goal", dict(nargs="?")))
    add("route", cmd_route, "session", "text", hour=dict(type=int))
    add("gates", cmd_gates)
    add("approve", cmd_approve, "event", **{"as": dict(dest="principal", required=True),
                                           "answer": dict(default="")})
    add("close", cmd_close, "goal")
    a = ap.parse_args(argv)
    try:
        return a.fn(a) or 0
    except (HierCliError, fm.HierarchyError, fc.ContractError, fg.GovernanceError) as e:
        print("REFUSED: %s: %s" % (type(e).__name__, e))
        return 1


if __name__ == "__main__":
    sys.exit(main())
