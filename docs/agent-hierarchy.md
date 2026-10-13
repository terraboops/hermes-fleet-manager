# Goal-directed agent hierarchies: design spec (draft 1)

Status: proposal. Nothing here is built. Scope: evolve the fleet manager from flat
sessions into goal-directed hierarchies.

Ladder: implementer -> manager -> director -> department, with a chief of staff facing
the human. Companion docs: `contract.md` (messaging, ack, tokens), `decisions.md`
(escalation gate), `kanban-fleet-bridge.md` (board contract).

## 1. Summary

- Today every session is a peer. The operator (the chief-of-staff role) dispatches to
  each one, watches each one, and verifies each result.
- This spec adds ownership: a manager session owns one goal and a team of implementer
  sessions. It decomposes, dispatches, reads tokens back, verifies, and answers up.
- Depth is capped at 2 delegation levels below the chief of staff (manager, then
  implementer) until measured. A director is specified (section 3.3) but stays disabled
  while the cap is 2, because director -> manager -> implementer is depth 3.
- Everything proven on the flat fleet is kept and restated per level (section 5).
  The hierarchy adds links and ownership. It does not replace the gate, the ack, or the
  transcript as ground truth.

## 2. Sources and what each one decided

| Source | What it gave | Decision it drove |
| --- | --- | --- |
| Paperclip (README, primary) | Org chart with roles and reporting lines; issues with parent links and goal ancestry; heartbeats; budgets with hard stops; review and approval gates | Parent/child links, goal tracing, budget per goal, wake-on-assignment |
| Anthropic cookbook, notebook 01 "chief of staff agent" (primary) | One coordinating agent, subagents with separate context via a Task tool, deterministic hooks for audit, output styles per audience, plan mode | The human-facing role is one agent that delegates and speaks once; audit by deterministic hooks, not by the model |
| arXiv 2508.12683, Moore (primary) | Five-axis taxonomy (control hierarchy, information flow, role and task delegation, temporal hierarchy, communication structure); contract-net as the canonical allocation pattern | The trade-off table in section 7; fixed roles and a static tree first |
| bb system overview (primary) | Thread kinds standard and manager; `parentThreadId`; immutable optional `lifecycleOwnerThreadId` set at creation only, with archive and delete cascading to dependents | Immutable owner link; ownership set at creation, never reassigned |
| Hermes kanban (this repo's bridge) | Durable board, parent links, claim lease with compare-and-set on `claim_lock`, review lane, completion contracts | Where team state lives (section 4.4) |

Caveats, checked against the sources on 2026-10-12:

- The Paperclip architecture write-up used in earlier notes (a personal blog analysis
  dated April 2026, AI-assisted) is SECOND-HAND. Anything below that cites the field
  name `reportsTo` comes from it. The Paperclip README itself says "reporting lines"
  and "parent links" and does not use that field name on the page read. Treat the name
  as unverified; the concept is confirmed.
- The same write-up listed deep planning as not yet done. The current README roadmap
  marks Deep Planning, Enforced Outcomes (watchdogs, recovery actions, review gates),
  Agent Reviews and Approvals, and Self-healing runs as done. Only Memory/Knowledge is
  in progress; Work Queues and Self-Organization are not started. The write-up is stale.
- Because the README lists review gates and watchdogs, the earlier note that Paperclip
  has no form-based escalation is withdrawn. It is unknown, not absent. Spike 2 settles it.
- The cookbook is a single-level pattern: one coordinator, flat subagents. It gives no
  evidence for deeper nesting. The arXiv paper is a taxonomy by one independent author;
  it frames trade-offs and gives no measurements on LLM hierarchies. Neither justifies
  depth beyond 2.
- The bb quotes come from bb's `system-overview` doc. Behaviour was probed only in
  part (spawn returned the fields). Cascade behaviour is documented, not tested here.

## 3. Roles and the fleet primitives they map to

### 3.1 Implementer

A fleet session working under a contract. Maps to an existing registered session plus
an armed watch.

Contract fields (all required):

- `contract_id`: unique, namespaced by chain (section 4.2).
- `goal`: one sentence, plus a pointer to the goal record.
- `boundary`: repos, paths, tools and systems it may touch; what it must not touch.
- `acceptance`: checks, each observable, with named negative cases.
- `done_token`, `blocked_token`: literal strings, derived from `contract_id`.
- `deadline`: absolute time; passing it raises a stall, not a silent extension.

The implementer emits the done token only after its own acceptance run. It never
verifies itself as the final word (section 5, lesson 5).

### 3.2 Manager

A session owning exactly ONE goal. It:

1. Writes acceptance criteria before decomposing.
2. Decomposes into implementer contracts, each narrower than its own grant.
3. Dispatches with the existing dispatch path and waits for the ack.
4. Reads tokens back from the transcripts (not from pasted replies).
5. Verifies each result against acceptance, by a reviewer that is a different session.
6. Emits its own done or blocked token upward, with pointers.

It takes over the idle nudge and stall check-in for its team; the daemon still detects
state. It holds no unique state: team state lives in the board and transcripts, so a
manager can be resumed by uuid or replaced without losing the team.

### 3.3 Director (specified, disabled at depth cap 2)

A manager of managers. Owns:

- a budget (tokens and wall-clock) split across its goals;
- a boundary (the union of what its managers may touch);
- standing rulings: decisions already made by the human that apply to every goal under
  it, stored as pointers and cited by id when applied.

It does not decompose implementer work and does not read implementer transcripts except
by sampling. Enabling it means raising the depth cap to 3, which needs the evidence in
section 9, phase 3.

### 3.4 Department

A named, persistent grouping under a director: standing rulings, budget, and a queue of
goals. A department of one person has no real director; open question 3.

### 3.5 Chief of staff

The one agent facing the human; the Hermes role today. Receives goals, routes them to a
manager (or to a flat session while hierarchy is off), and speaks to the human once per
event. Receipts never reach the human; the human-facing rules (quiet hours, "stopped
over 10 minutes is news", the three-lane gate) live only here.

## 4. Data model

### 4.1 Records

```
goal      id, title, owner_role, acceptance[], budget, deadline, state, human_ask_ptr
task      id, goal_id, parent_task_id|null, owner_session, contract_id, state
contract  contract_id, task_id, boundary, acceptance[], done_token, blocked_token, deadline
node      session, role(implementer|manager|director), parent_session|null,
          lifecycle_owner|null  (immutable), depth
```

- `parent_session` is the reporting line, comparable to Paperclip's reporting lines and
  bb's `parentThreadId`. It is the routing target for tokens and escalations.
- `lifecycle_owner` is comparable to bb's `lifecycleOwnerThreadId`: set once at
  creation, never updated, validated against a live owner, so cycles and reassignment
  cannot happen. Replacements of a dead worker inherit the same owner.
- `parent_session` may change (a manager is replaced; its team re-parents to the
  replacement). `lifecycle_owner` may not. Kept separate on purpose, as bb does.
- No cascade by default. bb cascades archive and delete to dependents; this fleet does
  not, because killing a manager must not kill running work with uncommitted state.
  A closed manager leaves its team parked and raises one event.

### 4.2 Token namespacing

`<KIND>-<goal_short>-<chain>-<seq>` where chain is the path of node ids from the goal,
for example `DONE-g41-m2-i3-1`. A parent matches only tokens whose prefix is its own
chain, so a child token cannot satisfy a different level.

### 4.3 Goal tracing

Every task carries `goal_id` and `parent_task_id`. Walking `parent_task_id` to null and
reading `goal_id` gives the goal from any task. Every contract renders the goal
sentence and a goal pointer into the implementer's first message, so the implementer
sees the why, as Paperclip does with goal ancestry. A task with no resolvable goal is
rejected at dispatch.

### 4.4 Where state lives

Open question 1. Default for the prototype: the Hermes kanban for goal, task, parent
links and verdicts (it already has parents, leases and review), and fleet registry
files for node and role. No new store until a gap is shown.

### 4.5 Depth cap

`max_depth` config, default 2, counted in delegation hops below the chief of staff:
manager = 1, implementer = 2. A dispatch that would create depth 3 is refused with a
reason, not queued. Raising it needs the phase 3 evidence. Spawn by an implementer is
refused outright: implementers do not create sessions.

## 5. The seven lessons, per level

| Lesson | Implementer | Manager | Director | Chief of staff |
| --- | --- | --- | --- | --- |
| 1. Nested tokens | Emits its own done or blocked token | Emits its done token only after its children's tokens are verified, never on their say-so | Same, over manager tokens | Gives the human one outcome per goal |
| 2. Delivery proof | Ack token embedded in each directive; proven by a real user turn | Same on each dispatch down; a miss raises not-delivered upward | Same | Same on goal intake |
| 3. Escalation lanes | Raises only blocked or needs-input | Absorbs by form what it can decide; the rest goes up | Same, applying standing rulings first | The only level that applies human-facing rules |
| 4. Authority narrowing | Works inside its boundary | Grants a subset of its own grant | Grants a subset of its own | Holds the human's grant |
| 5. Reviewer independence | Does not review its own work | Reviewer is a different session, preferably a different model | Samples reviews of reviews | Challenges a reported completion once |
| 6. Verification by behaviour | Runs acceptance, including named negative cases | Re-runs or inspects them before reporting | Checks that cases were named and run | Does not report done on committed, merged or CI-green alone |
| 7. Pointers | Writes artifact and transcript | Passes up pointers plus a short verdict | Same | Quotes a pointer, not a summary |

Rules that follow:

- Lesson 3: a classifier block or a choice UI is never resolved by an agent for another
  agent. It is raised to the human. A manager that approves its own team's gated action
  repeats the self-approval failure one level up.
- Lesson 4: the grant is a record. A dispatch whose boundary is not a subset of the
  parent's is refused at dispatch time by code, not by the manager's judgement.
- Lesson 6: each acceptance entry carries `positive` and `negative` cases. A contract
  with no negative case is rejected.

## 6. Risks and mitigations

| Risk | Mitigation |
| --- | --- |
| Lossy summaries compound upward (each level compresses the one below) | Lesson 7 pointers; verdict is a short label, not a retelling; reviewer samples the raw transcript each cycle; the human can always follow a pointer down |
| Cost multiplies per level (every manager is an extra long-lived context) | Budget per goal, set at intake, decremented per dispatch; hard stop raises an event; depth cap 2; managers use short contexts and re-read the board instead of carrying history |
| Manager drift (keeps working after the goal is met, or invents sub-goals) | Acceptance written before decomposition; deadline; a sub-task with no `goal_id` is rejected; manager done token ends its dispatch rights |
| Approval laundering (a gate passed because a lower level said so) | Lesson 3 and 4 rules; gate decisions logged with the level that made them |
| Deadlock (two managers wait on each other) | One owner per goal; explicit blocked tokens; a timeout that escalates; no peer-to-peer waits |
| Manager failure = team failure | State on the board and in transcripts; resume by uuid or replace and re-parent |
| False done from a confident manager | Reviewer independence; negative cases; the human-facing challenge-once rule |
| A layer that adds cost and no value | Spike 4 compares manager decomposition against what really happened; the cap is not raised without a measured gain |

## 7. Trade-offs from arXiv 2508.12683, applied

| Axis (paper) | Paper's trade-off | Choice here |
| --- | --- | --- |
| Control hierarchy | Centralized is efficient but a single point of failure; decentralized is robust but incoherent as it grows; hybrid is common | Hybrid: managers decide tactics inside a boundary, the human decides strategy and gates. Manager state is replaceable to soften the single point |
| Information flow | Top-down keeps directives consistent; bottom-up gives awareness but can overwhelm the leader; peer chatter confuses | Top-down contracts, bottom-up tokens plus pointers (not streams), no peer-to-peer between implementers in v1 |
| Role and task delegation | Fixed roles are simple but inflexible; emergent roles adapt | Fixed roles. Contract-net bidding is not used; the manager assigns. Revisit only with evidence |
| Temporal hierarchy | Upper layers plan over long horizons, lower layers react fast | Manager: goal horizon (hours to days). Implementer: a contract (minutes to hours). Chief of staff: event time |
| Communication structure | Static tree is analysable; dynamic adapts but may disconnect | Static tree. Re-parenting happens only on manager replacement, as an explicit event |

The paper also names open challenges that apply here: explainability to the human, and
safe integration of LLM agents. Its suggested pattern of an LLM in planning roles with
deterministic controllers in execution roles is the reason the gate, delivery proof and
authority checks are code, not model judgement.

## 8. Options for the host

- A) Build into the Hermes fleet manager on tmux sessions. Smallest; reuses everything proven.
- B) Build into bb using manager threads and lifecycle ownership. Depends on the bb spikes.
- C) Adopt Paperclip as the org layer (goals, budgets, org chart, approvals) with the fleet
  manager as the reliability substrate (delivery proof, tokens, lanes). Unknown whether
  Paperclip gives verifiable delivery or form-based escalation (its README lists review
  gates and watchdogs, which suggests partial overlap).

Recommendation: prove the model on A with a two-level prototype, then decide B or C on
spike evidence. Do not rebuild an org-chart product when one exists under MIT.

## 9. Phased rollout

Each phase is done only when validated by behaviour, including the negative cases listed.

### Phase 0: spikes (read-only or sandboxed)

1. Paperclip sandbox trial, telemetry off, no client data: how a heartbeat is delivered
   and whether delivery is verifiable; how adapters launch and resume sessions; what an
   approval looks like; whether it can sit on tmux sessions already running.
2. bb manager threads: whether a manager sees children's events, can send with proof of
   delivery, and what happens to children when the owner is archived.
3. Goal decomposition quality: three real past goals decomposed by a manager, compared
   against what actually happened.

### Phase 1: implementer contracts (no manager yet)

Add the contract record, namespaced tokens, boundary and acceptance fields to the
existing dispatch and watch. The chief of staff is still the only supervisor.
Exit: every dispatch carries a valid contract; contracts without a negative case are
refused. Negative cases: false done token; blocked token; missed ack; deadline passed;
duplicate dispatch.

### Phase 2: manager (depth 2)

One manager, two implementers, one real low-risk goal. Reviewer is a different session.
Exit: no lost task, no false done, the human told exactly once about a blocker.
Negative cases: implementer reports done falsely; implementer blocks; manager killed
mid-goal and resumed by uuid; a gated action that must reach the human; a dispatch that
would exceed depth 2 is refused; a boundary wider than the parent's is refused.

### Phase 3: director (raises the cap to 3)

Prerequisite: measured data from phase 2 on cost per goal, summary loss (sampled
disagreement between verdict and transcript), and manager drift incidents. Without it,
the cap stays 2 and this phase does not start.
Negative cases: a budget hard stop; a standing ruling applied and cited; a ruling
conflict escalated, not resolved by the director.

### Phase 4: departments

Persistent grouping, standing rulings store, goal queue per department.
Negative cases: a ruling edited by an agent is refused; a queue item with no goal is
refused.

## 10. Open questions

1. Where does the org state live: kanban, bb, Paperclip, or a new store? Default in 4.4
   is kanban plus registry files.
2. Which of the three hosts (A, B, C) wins on spike evidence?
3. Who is the director for a department of one person: a role with no distinct holder?
4. Can any lane run on local models (reviewer sampling, stall check-ins)?
5. Should a manager session be long-lived or re-created per cycle from the board?
6. How is the budget measured when one provider session spans several goals?
7. Is the reviewer model difference required or only preferred? How is it enforced?
8. Does contract-net bidding ever pay off for implementer selection, or are fixed roles enough?
9. How does the human see a goal's tree without being shown every level (the one-message rule)?
10. Paperclip field names (`reportsTo`) and cascade behaviour in bb: verify against source
    before any schema is copied.
