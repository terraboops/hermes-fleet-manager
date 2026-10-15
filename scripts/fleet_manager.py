#!/usr/bin/env python3
"""Manager role for the fleet hierarchy: one goal, a team, and the overwatch for it.

Spec: docs/agent-hierarchy.md sections 3.2, 4.1, 4.5. Builds on fleet_contract (contracts,
chain-namespaced tokens, ledger) and fleet_governance (independent review). Additive: the
flat path does not import this module, except through one guarded hook in fleet_watch.

Rules enforced in code, each with a test in tests/test_fleet_manager.py:

  1. ONE GOAL      A manager owns exactly one goal. Work whose paths fall outside the
                   goal's boundary is flagged to the parent and never dispatched.
  2. LINKS         Each node records parent_session (re-parentable) and lifecycle_owner
                   (set once, immutable). Owner must be a registered node.
  3. DEPTH CAP     Manager = depth 1, implementer = depth 2. Depth 3 is refused, and an
                   implementer may not create sessions.
  4. BUDGET        Each dispatch spends one unit of the goal's budget. A dispatch with no
                   budget left is refused with BudgetExhausted, never queued.
  5. OVERWATCH     For sessions a live manager owns, the idle nudge and the stall check-in
                   are issued by the manager (`tick`). The daemon defers
                   (`daemon_defers`) and keeps its own check-in only for unmanaged
                   sessions, or when the owning manager is dead.
  6. NO SAY-SO     Done is read from the child transcript by the Ledger and confirmed by
                   an independent reviewer; a child that claims done without meeting
                   acceptance is not counted.
"""
import json
import os
import time
from dataclasses import dataclass, field
from typing import Callable

import fleet_contract as fc

MAX_DEPTH = fc.MAX_DEPTH
MANAGER, IMPLEMENTER = "manager", "implementer"
ROLE_DEPTH = {MANAGER: 1, IMPLEMENTER: 2}

DEFAULT_PATH = os.path.expanduser("~/.hermes/scripts/cc-watch/hierarchy.json")


class HierarchyError(ValueError):
    """A hierarchy rule was broken. The message names the rule."""


class DepthCapExceeded(HierarchyError):
    pass


class BudgetExhausted(HierarchyError):
    pass


class OwnerImmutable(HierarchyError):
    pass


class OutOfScope(HierarchyError):
    pass


# ---------------------------------------------------------------- registry

class Registry:
    """Node records: session -> {role, parent_session, lifecycle_owner, depth, goal}.

    Persisted as JSON when `path` is given; in memory otherwise (tests).
    """

    def __init__(self, path=None, max_depth=MAX_DEPTH):
        self.path = path
        self.max_depth = max_depth
        self.nodes = {}
        if path and os.path.exists(path):
            with open(path) as fh:
                self.nodes = json.load(fh)

    def _save(self):
        if not self.path:
            return
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w") as fh:
            json.dump(self.nodes, fh, indent=1, sort_keys=True)
        os.replace(tmp, self.path)

    def register(self, session, role, parent_session=None, lifecycle_owner=None, goal=""):
        if role not in ROLE_DEPTH:
            raise HierarchyError("unknown role %r" % (role,))
        if session in self.nodes:
            raise HierarchyError("session %s is already registered" % session)
        depth = ROLE_DEPTH[role]
        if parent_session is not None:
            parent = self.nodes.get(parent_session)
            if parent is None:
                raise HierarchyError("parent %s is not registered" % parent_session)
            if parent["role"] == IMPLEMENTER:
                raise DepthCapExceeded("implementer %s may not create sessions"
                                       % parent_session)
            depth = parent["depth"] + 1
        elif role != MANAGER:
            raise HierarchyError("an implementer needs a parent manager")
        if depth > self.max_depth:
            raise DepthCapExceeded("depth %d exceeds cap %d" % (depth, self.max_depth))
        if lifecycle_owner is None:
            lifecycle_owner = parent_session
        elif lifecycle_owner not in self.nodes:
            raise HierarchyError("lifecycle owner %s is not registered" % lifecycle_owner)
        self.nodes[session] = {"role": role, "parent_session": parent_session,
                               "lifecycle_owner": lifecycle_owner, "depth": depth,
                               "goal": goal}
        self._save()
        return self.nodes[session]

    def reparent(self, session, new_parent):
        """The reporting line may move (a manager is replaced). The owner never does."""
        node = self._node(session)
        if new_parent not in self.nodes:
            raise HierarchyError("parent %s is not registered" % new_parent)
        node["parent_session"] = new_parent
        self._save()

    def set_lifecycle_owner(self, session, owner):
        node = self._node(session)
        if node["lifecycle_owner"] is not None:
            raise OwnerImmutable("lifecycle owner of %s is already set to %s and cannot change"
                                 % (session, node["lifecycle_owner"]))
        if owner not in self.nodes:
            raise HierarchyError("lifecycle owner %s is not registered" % owner)
        node["lifecycle_owner"] = owner
        self._save()

    def manager_of(self, session):
        node = self.nodes.get(session)
        if not node or node["role"] != IMPLEMENTER:
            return None
        return node["parent_session"]

    def _node(self, session):
        try:
            return self.nodes[session]
        except KeyError:
            raise HierarchyError("session %s is not registered" % session)


def daemon_defers(session, registry, manager_alive):
    """True when the daemon must NOT issue its own idle nudge / stall check-in.

    Defers only for a registered implementer whose manager is alive. Unmanaged flat
    sessions, and sessions whose manager is dead, stay with the daemon so a dead manager
    cannot make its team silent.
    """
    mgr = registry.manager_of(session)
    if mgr is None:
        return False
    try:
        return bool(manager_alive(mgr))
    except Exception:
        return False   # cannot tell -> daemon keeps the fallback


# ---------------------------------------------------------------- budget

@dataclass
class Budget:
    units: int
    spent: int = 0

    def spend(self, n=1):
        if self.spent + n > self.units:
            raise BudgetExhausted("goal budget exhausted: %d of %d units spent, %d requested"
                                  % (self.spent, self.units, n))
        self.spent += n

    @property
    def left(self):
        return self.units - self.spent


# ---------------------------------------------------------------- manager

@dataclass
class Manager:
    """Owns one goal. Every outside effect is an injected callable.

      dispatch(session, directive) -> None   deliver the contract to the child
      send(session, text)          -> None   nudge / check-in message into a child
      state_of(session)            -> "working" | "idle" | "dead"
      last_growth(session)         -> epoch seconds of last transcript growth
      transcript_of(session)       -> path or None
      verify(child, report)        -> bool   independent reviewer
    """
    session: str
    registry: Registry
    own: fc.Contract
    budget: Budget
    dispatch: Callable
    send: Callable
    state_of: Callable
    last_growth: Callable
    transcript_of: Callable
    verify: Callable
    escalate: Callable = lambda kind, text: None
    nudge_interval: float = 300
    stall_window: float = 360
    stall_repeat: float = 300
    max_nudges: int = 5      # per stop episode; fleet_watch reads this as the daemon's nudge cap
    clock: Callable = time.time
    ledger: object = None
    issued: list = field(default_factory=list)
    flagged: list = field(default_factory=list)
    _next: int = 1
    _watch: dict = field(default_factory=dict)

    def __post_init__(self):
        node = self.registry.nodes.get(self.session)
        if not node or node["role"] != MANAGER:
            raise HierarchyError("%s is not a registered manager" % self.session)
        self.ledger = fc.Ledger(self.own, self.transcript_of, self.verify)

    # -- 1. one goal: out-of-scope work is flagged, not absorbed
    def intake(self, description, paths):
        """Accept new work only if every path lies inside the goal boundary."""
        stray = [p for p in paths if not fc._subset([p], self.own.boundary)]
        if stray or not paths:
            self.flagged.append({"description": description, "paths": list(paths)})
            self.escalate("out-of-scope", "%s: outside goal %s boundary: %s"
                          % (description, self.own.goal_short, ", ".join(stray) or "no paths"))
            raise OutOfScope("work is outside goal %s: %s"
                             % (self.own.goal_short, ", ".join(stray) or "no paths given"))
        return True

    # -- decompose + dispatch
    def assign(self, session, goal, boundary, acceptance, deadline):
        """Register an implementer, build its contract, spend budget, dispatch.

        Checks run before any side effect, so a refusal leaves nothing half-created.
        """
        if session in self.registry.nodes:
            raise HierarchyError("session %s is already registered" % session)
        if self.budget.left < 1:
            raise BudgetExhausted("goal budget exhausted: %d of %d units spent"
                                  % (self.budget.spent, self.budget.units))
        my_depth = self.registry.nodes[self.session]["depth"]
        if my_depth + 1 > self.registry.max_depth:
            raise DepthCapExceeded("manager %s at depth %d cannot dispatch: child depth %d "
                                   "exceeds cap %d" % (self.session, my_depth, my_depth + 1,
                                                       self.registry.max_depth))
        node_id = "i%d" % self._next
        contract = fc.new_contract(
            self.own.goal_short, list(self.own.chain) + [node_id], goal, boundary,
            acceptance, deadline, owner_session=session, parent=self.own,
            max_depth=self.registry.max_depth)
        self.registry.register(session, IMPLEMENTER, parent_session=self.session,
                               lifecycle_owner=self.session, goal=self.own.goal_short)
        self._next += 1
        self.ledger.add_child(contract)
        self.budget.spend(1)
        self.dispatch(session, self.directive(contract))
        self._watch[contract.contract_id] = {"nudges": 0, "last_nudge": 0.0,
                                             "last_checkin": 0.0, "given_at": self.clock()}
        return contract

    @staticmethod
    def directive(c):
        lines = ["GOAL: %s" % c.goal, "BOUNDARY: %s" % ", ".join(c.boundary), "ACCEPTANCE:"]
        for a in c.acceptance:
            lines.append("  - %s (negative: %s)" % (a["check"], "; ".join(a["negative"])))
        lines += ["When done, and only after your own acceptance run, put this alone on a line:",
                  c.done_token, "If blocked, put this alone on a line:", c.blocked_token]
        return "\n".join(lines)

    def confirm(self, contract_id):
        return self.ledger.confirm_delivery(contract_id, self.ledger.children[contract_id].done_token)

    # -- 5. overwatch: the manager issues the nudge and the check-in
    def tick(self):
        """One overwatch pass. Returns the list of actions issued this pass."""
        now = self.clock()
        acts = []
        for cid, c in self.ledger.children.items():
            st = self.ledger.state[cid]
            if st["token"] is not None:
                continue
            w = self._watch[cid]
            s = c.owner_session
            state = self.state_of(s)
            quiet = now - self.last_growth(s)
            if state == "dead":
                acts.append(self._issue("dead", s, cid, "session is not running", send=False))
                self.escalate("dead", "%s (%s) is not running" % (s, cid))
                continue
            if now > c.deadline:
                if now - w["last_checkin"] >= self.stall_repeat:
                    w["last_checkin"] = now
                    acts.append(self._issue("deadline", s, cid,
                                            "Deadline passed for %s. Report status or emit %s."
                                            % (cid, c.blocked_token)))
                continue
            if state == "idle":
                if w["nudges"] >= self.max_nudges:
                    if w["nudges"] == self.max_nudges:
                        w["nudges"] += 1
                        self.escalate("idle", "%s idle after %d nudges" % (s, self.max_nudges))
                    continue
                if now - w["last_nudge"] >= self.nudge_interval:
                    w["nudges"] += 1
                    w["last_nudge"] = now
                    acts.append(self._issue("nudge", s, cid,
                                            "You are idle. Continue %s, or emit %s if blocked."
                                            % (cid, c.blocked_token)))
            elif quiet >= self.stall_window and now - w["last_checkin"] >= self.stall_repeat:
                w["last_checkin"] = now
                acts.append(self._issue("checkin", s, cid,
                                        "No output for %ds. Status on %s, or emit %s if blocked."
                                        % (quiet, cid, c.blocked_token)))
            elif state == "working" and quiet < self.stall_window:
                w["nudges"] = 0   # progress resets the nudge budget
        return acts

    def _issue(self, kind, session, cid, text, send=True):
        if send:
            self.send(session, text)
        act = {"issuer": self.session, "kind": kind, "session": session,
               "contract": cid, "at": self.clock()}
        self.issued.append(act)
        return act

    # -- 6. results
    def receive(self, contract_id, report):
        c = self.ledger.children[contract_id]
        st = self.ledger.state[contract_id]
        token = c.blocked_token if report.verdict == "blocked" else c.done_token
        if not st["delivered"]:
            self.confirm(contract_id)
        ok, why = self.ledger.receive(contract_id, token, report)
        # A settled child that is not verified goes up now; the parent should not have to
        # poll result(). Settled children are refused on a repeat, so this fires once.
        if ok and self.ledger.state[contract_id]["verified"] is False:
            kind = "child-blocked" if why == "blocked" else "child-failed"
            self.escalate(kind, "%s (%s): %s" % (c.owner_session, contract_id, why))
        return ok, why

    def result(self):
        """(token, Report) for the parent, or (None, None) while work remains."""
        return self.ledger.upward_report()


    def result_proven(self):
        """(token, Report, why) for the parent. The manager's token must be emitted in the
        manager's own transcript; a derived token alone is not reported upward."""
        return self.ledger.upward_proven(self.transcript_of(self.session))
