# Fleet manager: one resolver, one daemon, the event-gate shape

Status: proposal. Nothing here is built. Findings are measured; the design is a recommendation.

## 1. Findings (measured 2026-10-21)

### Session identifiers are not interchangeable, and one reader is silently wrong
The registry (`fleet_registry.json`) holds, per session: `name` (cc-w-vibes), `short` (vibes), `uuid`, `cwd`, `profile`.
Probe of two read-only tools against one live session:

| id given | fleet_state.py | fleet_last.py |
|---|---|---|
| name `cc-w-vibes` | WORKING (correct) | resolved |
| short `vibes` | **DEAD (wrong: no error)** | resolved |
| full uuid | **DEAD (wrong: no error)** | not in the registry |
| uuid prefix | **DEAD (wrong: no error)** | not in the registry |

`fleet_ack.py` resolves the short name. Lookup is implemented separately in at least nine places:
`fleet_ack.resolve`, `fleet_last._entry`, `fleet_layout.load_registry`, `fleet_watch.load_registry`,
`fleet_boot.load_registry`, `fleet_transcript.resolve` / `by_uuid`, `fleet_reg._resolve_newest_uuid`,
`fleet_mcp.resolve` (unrelated: it resolves a Claude profile). A state tool that answers DEAD for an
unknown id is the dangerous case: a restart or nudge decision made on it acts on a live session.

### Entrypoints are scripts, not a service
27 scripts in `scripts/` plus `fleet_watch.py` (the daemon). Agents drive them by shelling out and parsing
text. `fleet_mcp.py` is NOT a fleet MCP server: it ensures other MCP servers exist in a session's profile.

## 2. The three changes

### A. One resolver (small, safe, do first)
`fleet_ident.py`: `resolve(any_id) -> Session | AmbiguousError | UnknownError`.
Accepts, case-sensitive and in this order: exact name, exact short, full uuid, unique uuid prefix (min 6 chars),
unique name prefix. Returns one record: name, short, uuid, cwd, profile, tmux target, transcript path.
Rules:
- An unknown or ambiguous id is an ERROR with the candidates listed. It is never DEAD, never empty, never a guess.
- Every CLI calls it first. The nine local lookups are deleted, not kept as fallbacks.
- Output shows the resolved name, so the caller sees what was matched.
Tests: each id form against each tool; ambiguous prefix; unknown id exits non-zero; a read-only tool never
dispatches.

### B. A single daemon that exposes the toolset as an MCP server (medium)
`fleet-watch` already runs as the one long-lived process. Give it a local MCP endpoint (stdio shim for Hermes,
Claude sessions and the desktop; loopback HTTP behind it). Tools mirror the CLIs one for one, all taking an `id`
that goes through the resolver: `state`, `last`, `dispatch`, `ack`, `resume`, `register`, `layout_*`, `funnel_status`,
`attempts`, `escalations`, `answer`, `escalate`. The CLIs stay as thin clients of the same functions so nothing
that works today breaks.
Gates carry over unchanged: the funnel's refusal of composed answers, the gated-shape screen, the single door
for pages. The MCP server adds no way around them. Dispatching tools are marked mutating so an approval policy
can sit in front of them.
Open design points: auth on the loopback port (token file, 0600); one writer for registry changes; whether Hermes
loads it as an MCP server or through the plugin that already exists (the fleet platform plugin).

### C. The event-gate shape, as a structure, not a dependency (design first)
Source: `~/Developer/mahdi/knowledge/the-event-gate-loop.md`. The loop: events in, a gate decides by escalation
form (rules, then a model for the ambiguous middle, then the human), effects out, the decision and its evidence
recorded, the record feeding the next decision.

**Ruling in force (OWNER-RULINGS, 2026-10-13): thos is bb-only; there is no Hermes port; Hermes is an independent
instance of the loop, kinship of shape only.** So this change imports no thos code and makes no thos dependency.
It uses the SHAPE to remove duplication inside the fleet manager.

Where the fleet manager already matches: three lanes (code escalate, code silent, Laya), default-escalate, silence
is never consent, an append-only ledger (`fleet-escalations.jsonl`, `fleet-attempts.jsonl`, `laya-gate-decisions.jsonl`).

Where it does not, from today's evidence:
1. Three ledgers with three schemas. A decision (page, handled, nudge, answer) should be ONE record type with the
   evidence that produced it, so "why did nothing happen to this session" is one query. Today it took three
   files and an afternoon (a stalled game session read as handled while 130 nudges landed unanswered).
2. A non-decision is recorded but not tied to its evidence. `handled: ladder in progress` names no rung, no
   outcome of the last rung, no next step. The record should carry what the gate saw.
3. The effect (a nudge) is not tied back to its outcome in the same record. `outcomes.evaluate` does it
   separately and the result is never read by the next decision. The next rung should be chosen from the last
   outcome (this is the owner-wait fix, done by hand in `ladder.py`; it should be the general mechanism).

From the decision-matrix patterns (`research/decision-matrix/07-patterns/PATTERNS.md`), the ones that map:
- 9 Deterministic first / 8 Evidence readers: already the lane 1-2 rule; extend by reading the transcript record
  type, not the session's words (the stall gate still reads wording in places).
- 7 Thresholds and abstaining: Laya should return `unsure` as a first-class answer that routes to the human.
- 11 Stopping the loop: a per-episode nudge budget and an oscillation check (a session answering every rung with the
  same text is a loop; `hermes-loop-guard` shows the detector).
- 5 Regenerate / patch / ask: define what a rejected nudge does next, per rung, instead of repeating rung 1.

## 3. Order
1. A (resolver). Hours. Fixes the silent-DEAD bug first.
2. C items 1-3 (one decision record, evidence in it, outcome feeding the next rung). Days. Replay the last 14
   days of ledger rows through it before restart, both directions.
3. B (MCP daemon). Days. Last, because it is the largest surface and everything above makes it smaller.

## 4. Not in scope
- Any thos code, schema or dependency.
- Changing the three-lane gate's safety rules or the single door for pages.
