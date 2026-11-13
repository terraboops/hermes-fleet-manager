# Director layer: design addendum

Addendum to `agent-hierarchy.md` (phase 3). Status: design only, nothing built. The
cap stays 2 until every scenario in `tests/e2e_director_pending.py` passes.

## Decision

A director is a manager of managers. It owns a department goal, a pooled budget, a
reviewer pool and a standing-rulings store. It adds one level. It adds no new authority:
every gate still ends at the human, and only the top level filters news.

Depth becomes 3 only for a registry created with `max_depth=3`. The default registry
stays at 2, so a flat or manager-only fleet cannot grow a director by accident.

## Changes by gap

1. Roles and depth
   - `fleet_manager`: add `DIRECTOR`; `ROLE_DEPTH = {DIRECTOR: 1, MANAGER: 1, IMPLEMENTER: 2}`
     stays the root-depth map. Depth below a parent is `parent depth + 1`, as today.
   - A director registers only on a registry with `max_depth >= 3`.
   - Chains: director `d1`, manager `d1.m1`, implementer `d1.m1.i1`. `fc.MAX_DEPTH`
     becomes a per-registry value; `new_contract` already takes `max_depth`.
   - Only a director may call `assign_manager`. A manager calling it raises
     `HierarchyError`. A manager's `assign` of a manager child stays refused.
   - `test_depth_three` in `test_e2e_hierarchy.py` becomes depth four on this change.

2. Budget roll-up
   - `Budget(units, parent=None)`. `spend(n)` checks the whole chain first, then
     commits to every level. A refused spend changes nothing.
   - Manager budgets are carved from the department budget, so their sum may exceed it.
     The department cap binds first. The error text names "department".
   - `raise_cap(n, principal)` accepts the human only.

3. Standing rulings (`scripts/fleet_rulings.py`, new)
   - `RulingStore(clock)`: `add / edit / extend` take a principal; non-human raises
     `RulingError`. An expiry is required. The store is append-only: an edit writes a
     new version and keeps the old.
   - `director.rule(topic, asked_by)`: one active match, apply and cite
     (`Ruling.cite()` returns the id). No match, expired, or two different active
     decisions: return `None` and escalate (`needs-ruling` or `ruling-conflict`). The
     director never picks between conflicting rulings.
   - Every call writes an audit entry: `ruling_id, asked_by, applied_by, result, at`.
     Results: `applied, expired, none, conflict`.
   - Gates are out of scope by construction. A topic prefixed `gate:` is refused at
     `add`, and `rule_event` on a gate event raises `RulingError`. Precedent never
     answers a classifier block or a choice UI.

4. Arbitration
   - `assign_manager` refuses a boundary that overlaps a sibling manager's
     (`BoundaryOverlap`, names the shared paths).
   - `director.arbitrate(new_manager, boundary, owner, reason, ...)` names which
     manager keeps the overlap; the other boundary is trimmed. Recorded in
     `director.arbitrations`. No owner named: escalate `boundary-conflict` to the
     human and raise. A manager cannot arbitrate its own boundary.

5. Reviewer pool
   - `director.reviewer_pool`: one pool, shared across the department's managers.
     `pick(producer)` returns a member that `is_independent(member, producer)`.
   - Independent means: not the producer, not any ancestor of the producer (its
     manager, the director), and not a descendant of an ancestor that produced the
     same child. A session in a sibling manager's subtree qualifies.
   - Prefer a different model, as `fg.pick_reviewer` does. With no independent member,
     raise `ReviewerNotIndependent`: no review beats a fake one.
   - `verify_for(manager)` returns the manager's `Ledger.verify`; its verdicts are
     checked against the same independence rule, so a hand-picked reviewer is refused.

6. Gates and news through three levels
   - Levels chain implementer, manager, director, chief. Director level is
     `Level(parent=chief)`, not top. The existing routing already forwards gates
     unchanged; the scenario locks that in.
   - Quiet hours and the 10-minute stopped threshold exist only in
     `Level._human_facing`. Director and manager levels forward and never hold or drop.
   - Add `Level.seen` (events passed through, for assertions only). It carries no
     behaviour.

7. Entry point
   - `ChiefOfStaff.take_department(goal, text, boundary, acceptance, deadline,
     budget, director_session, effects, clock)` returns a `Director`. It validates
     first and undoes registration on refusal, as `take_goal` does.

## Out of scope

Departments as a persistent grouping, goal queues, and local-model reviewers stay in
phase 4. A director for a department of one has no distinct holder; that is an open
question in the parent spec and is not settled here.

## Entry condition

The parent spec requires measured phase 2 data (cost per goal, summary loss, manager
drift) before the cap lifts. These scenarios define the build; they do not replace
that data.

## Scenarios (29, all red today)

Run: `python3 tests/e2e_director_pending.py`. All 29 currently error on one cause:
`ChiefOfStaff.take_department` does not exist. Expect the failures to move to the
individual gaps as the entry point lands.

- Depth: director role and cap 3; depth four refused; manager-to-manager refused,
  director allowed; default registry stays 2.
- Budget: department cap binds first with nothing half-created; failed spend rolls
  nothing up; only the human raises a cap.
- Rulings: applied, cited, audited; expired escalates; no match escalates; conflict
  escalates; agents cannot add, edit or extend; no expiry refused; never resolves a gate.
- Arbitration: overlap refused; arbitration assigns and records; no owner goes to the
  human; manager cannot arbitrate itself.
- Reviewers: shared pool; producer chain excluded; sibling subtree independent; no
  candidate raises; ledger refuses a non-independent verdict.
- Gates: level chain; block crosses three levels once and unchanged; no agent can
  approve; quiet hours only at the top; stopped threshold only at the top; receipts
  absorbed at the first level.
