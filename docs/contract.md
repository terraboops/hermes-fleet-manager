# fleet-manager protocol — v1 (ACP-inspired)

A normalized contract for supervising a fleet of Claude Code sessions. Ideas ported
from Anthropic's **Agent Client Protocol** (JSON-RPC structure, agent lifecycle,
message-ids, permission surface) and used to *refine our own working system*.

## Transport
Today the controller talks to each session by inject-and-scrape (tmux paste /
capture-pane). **v2 (experimental)** will drive a structured JSON stream via
`claude --output-format stream-json --input-format stream-json`. The messaging
contract below is transport-agnostic so it holds for both.

### Dispatch hard rule (tmux inject)
When injecting a directive into a live session:
- **Do NOT send `Escape` immediately before the paste.** It races the paste and
  eats the **first character** of the pasted block (`TWO` → `WO`, `GO` → `O`),
  and lets several back-to-back pastes **merge into one mangled blob** the agent
  cannot cleanly act on. Symptom: the session looks idle, parked, or
  unresponsive even though the paste reported success.
- Correct sequence: `send-keys C-c` to clear any pending input, then
  `load-buffer` + `paste-buffer` + `Enter` — ideally **one fused buffer per
  directive** (do not stack separate pastes back-to-back).
- After injecting, `capture-pane` and verify the **first line (especially the
  opening word) landed intact** before trusting that the agent received it.
- Real incident (2026-09-01): a `Escape`-corrupted, merged dispatch plus a
  skipped sentinel left the wolfgang session parked and quiet. The **submit**,
  not the agent, was at fault.

### Delivery guarantee (ack + timeout) — REQUIRED
Every directive the controller injects MUST elicit a delivery ack, or the
controller treats the dispatch as NOT DELIVERED. This is what stops a failed
paste from going silently stale (dispatch reports "success" but the session
never acts → minutes of quiet).

- **Every inject embeds its ack token in the message text**: a unique
  `MESSAGE-RECEIVED-<slug>-<session>-<mid>` the agent must emit verbatim on its
  own next line as soon as it receives the directive. Put the token INSIDE the
  buffer (a token only printed to the controller's own log does not reach the
  agent — see the 2026-09-01 bc-prod miss where the agent refused to invent
  one).
- **The controller starts a timer at inject.** If the matching
  `MESSAGE-RECEIVED` ack has not been seen within the ack window (default
  ~15–30 s), the controller RAISES it as a `not-delivered` alert (surfaced to
  Terra, not silently retried): the session is flagged "dispatch not acked,"
  the directive is re-sent once via the clean path (C-c clear → fused buffer →
  verify first line), and a second miss raises again.
- **Do not reset the timeout on unrelated output.** Only the exact ack token
  counts; chat noise or even the actual work starting does not satisfy the ack.
- The ack guarantees *delivery to the agent's input*, not completion. Completion
  is the separate task sentinel (`DONE-/NEEDS-INPUT-`). Both are required for a
  dispatch to be considered closed: ack = received, task-sentinel = done.

## Message envelope (schema: 1)
Every fleet message is a JSON object:

```json
{
  "schema": "fleet/1",
  "schemaVersion": 1,
  "sessionId": "<registry name>",
  "mid": "<controller or agent message id>",
  "seq": 1,                 // per-recipient monotonic
  "ts": 1750000000,
  "type": "<message type>",
  "payload": { }
}
```

- `mid` is unique per message → the controller **dedupes by (sessionId, mid)**,
  not by similarity or content-hash (ACP message-ID RFD). Deprecates the content-hash state file (kept for back-compat).
- Streamed/partial chunks share a `mid`; a new `mid` marks a new message.

## Message types
| type | direction | purpose |
|---|---|---|
| `status` | both | request/reply one-line status (see below) |
| `steer` | controller→agent | steering instruction; "continue working after" = `resume:true` |
| `resume` | controller→agent | lifecycle: pick a parked session back up |
| `done` | agent→controller | terminal sentinel (`DONE-<NAME>`) |
| `needs_input` | agent→controller | blocked on human (`NEEDS-INPUT-<NAME>`) |
| `ack` | agent→controller | handshake (`MESSAGE-RECEIVED`) |
| `progress` | agent→controller | non-terminal update (`PROGRESS-<task>-<pct>`) |
| `error` | agent→controller | failure (`traceback` auto-caught) |

## Agent state machine (port from A2A/ACP lifecycle)
```
idle → running → awaiting_input → running → done
   └───────────────────────────────→ error
```
`state` field is one of: `idle | running | awaiting_input | done | error`.
**`awaiting_input` is first-class** — an agent that emits `needs_input` must move
to it, not stay `running`.

Status reply (one line, envelope + payload):
```json
{"sessionId":"<name>","state":"running","summary":"...","last":"...","current":"...","next":"...","eta":"...","concerns":"...","blockers":"..."}
```

## Agent lifecycle verbs (ACP `agent/list|start|stop|finish`)
Formalize today's ad-hoc tmux kill/relaunch:
- `list` — registry `check` → known + resolved sessions.
- `start` — launch or take over a session by registry entry (resume `uuid`).
- `stop` — close a session (send finish → kill → unregister).
- `finish` — graceful end (agent emits `done`; controller closes + unregisters).

### New / taken-over session (REQUIRED)
When the controller spawns a NEW session or TAKES OVER one Terra opened
elsewhere, it must, before treating the session as managed:
1. **Take over via the profile cleanly** — use the work or personal
   `CLAUDE_CONFIG_DIR`, `claude --resume <uuid>` (or fresh), inside a managed
   tmux session.
2. **Answer the "trust this folder?" onboarding prompt** if/when it appears
   (select trust) so the session reaches the prompt.
3. **Put it in remote-control mode** — send `/rc` (equivalently launch with
   `--remote-control`). A session that is NOT in `/rc` will NOT appear as a
   steerable remote session in the Claude Code app. A plain `--resume` is not
   enough to surface it there.
4. **Register it with the watcher**: `fleet_reg.py register <name> --short
   <short> --profile {personal,work} --cwd <cwd> --uuid <uuid>` so the watcher
   monitors it and the controller can dispatch to it. Verify with
   `fleet_reg.py check` before considering it managed.
5. Only then dispatch + verify delivery (see Dispatch hard rule + Delivery
   guarantee above).

## Permission / approval surface (ACP `permission/listForAgent` + approve/deny)
Replace hard-coded hooks with a relayed approval contract:
- Controller lists a session's pending permission requests.
- Terra approves/denies/auto in one place; tool gates (e.g. Slack-send) report to
  this surface instead of being bolted-on hooks.

## Capabilities (optional)
Each registry entry may declare `accepts: [status, steer, resume]` + project, so the
controller dispatches by capability.

## Migration
- Back-compat: today's sentinel tokens (`DONE-`, `NEEDS-INPUT-`, `MESSAGE-RECEIVED`, `traceback`)
  stay valid; v1 messages wrap them.
- `fleet_registry.json` gains `messageId`/sequence bookkeeping as `fleet_watch_state.json` drains.

## Liveness enforcement (operator-side, class-fix 2026-09-01)
A session being REGISTERED is not proof it is ALIVE. The fleet-watch daemon was
transcript-only: it read a registered session's jsonl for sentinels but never verified
the tmux session existed, so a dead session (trust-prompt "No, exit" kill, reboot,
crash) went SILENT and the operator kept dispatching into a dead pane.
- Liveness truth = `tmux has-session -t <name>` (tmux IS the pane; when claude exits the
  session dies). The daemon probes it and EMITS `SESSION-DEAD-<NAME>` on ALIVE→dead
  transitions, and `NO-TRANSCRIPT-<NAME>` for registered-but-unrecoverable sessions.
- The operator MUST verify-alive BEFORE any dispatch (tmux has-session / `fleet_reg check
  --live`). A dead target is relaunched or surfaced — never "idle".
- Dispatch result is tri-state: DELIVERED-WORKING / NO-ACK / SESSION-DEAD. "Text appeared
  in pane" is not proof of delivery to a live, responsive model.

## Large prompts = a pointer, not a raw paste (operator-side, 2026-09-01)
A raw tmux paste of a LONG prompt into a busy/live session truncates the FRONT of the buffer
(the session receives only the tail, never the opening; observed on cc-w-bcprod-44ea). For
anything longer than ~1-2 lines, write the FULL prompt to a file in the session's cwd and
dispatch a SHORT pointer line ("Read <abs path> and do X"). Verify BOTH the head and the tail of
the short dispatch appear in the pane. Large context belongs on disk, never in a paste.
