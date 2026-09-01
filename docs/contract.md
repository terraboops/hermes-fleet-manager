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
- `start` — relaunch a session by registry entry (resume `uuid`).
- `stop` — close a session (send finish → kill → unregister).
- `finish` — graceful end (agent emits `done`; controller closes + unregisters).

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
