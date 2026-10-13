#!/usr/bin/env python3
"""Chief-of-staff entry point: a goal goes in, one human report comes out.

Spec: docs/agent-hierarchy.md. Sits on fleet_governance.Level(top=True) and
fleet_manager.Manager. It does three things and nothing else:

  1. take_goal   register a manager for the goal, build the goal contract, return
                 the Manager. Every outside effect stays an injected callable.
  2. mgr_level   the manager's governance level, parented to the top level, so
                 gate events and news travel up to the human and are never absorbed.
  3. report      compose the single human report: a verdict line plus one pointer
                 per child. Never a child body, never a transcript excerpt.
"""
import fleet_contract as fc
import fleet_governance as fg
import fleet_manager as fm


class ChiefOfStaff(object):
    def __init__(self, hour=12, registry=None):
        self.level = fg.Level("chief", top=True)
        self.hour = hour
        self.registry = registry if registry is not None else fm.Registry()

    def mgr_level(self):
        return fg.Level("manager", parent=self.level)

    def take_goal(self, goal_short, text, boundary, acceptance, deadline, budget,
                  manager_session, effects, clock=None, **manager_opts):
        """Create the manager and its contract for one goal.

        `effects` carries the injected callables: dispatch, send, state_of, last_growth,
        transcript_of, verify, and optionally escalate. `budget` is an int of units
        or a fm.Budget.
        """
        if not isinstance(budget, fm.Budget):
            budget = fm.Budget(int(budget))
        own = fc.new_contract(goal_short, ["m1"], text, boundary, acceptance,
                              deadline=deadline, owner_session=manager_session)
        # Validate before any side effect: Manager checks registration, so register
        # first, and undo if construction is refused.
        self.registry.register(manager_session, fm.MANAGER, goal=goal_short)
        kw = dict(effects)
        if clock is not None:
            kw["clock"] = clock
        kw.update(manager_opts)
        try:
            return fm.Manager(session=manager_session, registry=self.registry, own=own,
                              budget=budget, **kw)
        except Exception:
            self.registry.nodes.pop(manager_session, None)
            raise

    def report(self, goal, token, upward, ledger):
        """One message to the human: pointers plus verdict only. Returns the text."""
        lines = ["goal %s: %s (%s)" % (goal, upward.verdict if upward else "pending",
                                       token or "no token")]
        for cid, c in sorted(ledger.children.items()):
            st = ledger.state[cid]
            rep = st["report"]
            lines.append("  %s %s %s" % (cid, st["why"], rep.pointer if rep else "no pointer"))
        text = "\n".join(lines)
        needs_human = bool(upward and upward.verdict != "pass")
        self.level.route(fg.Event(fg.NEEDS_INPUT if needs_human else "goal_report",
                                  "manager", text), self.hour)
        return text
