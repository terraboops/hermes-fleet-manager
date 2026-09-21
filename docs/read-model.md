# The read model: tmux is write-only, the log is the source of truth

**Rule.** The controller SENDS to tmux and never READS it for state, content, or proof of
delivery. Everything the controller knows about a session comes from that session's JSONL
transcript, folded into a projection that the daemon maintains and everyone else reads.

Two reasons this is not a style preference:

1. **The pane lies about delivery.** An unsubmitted draft sits in the pane looking exactly
   like a delivered message. Confirming delivery from the pane reported `LANDED` for
   messages a session never received.
2. **The pane lies about state.** A `· done` from the previous turn lingers in the visible
   region, so "is it working" answered from the pane is wrong in a way that is invisible.
   A session mid-turn that has written nothing for longer than the activity window reads
   `IDLE`, and a nudge pasted on that reading lands in a running turn.

## What the daemon projects (per session)

Folded from the transcript, appended to, never re-derived from scratch:

| field | derived from | used for |
| --- | --- | --- |
| `state` | spinner-free log activity: recent writes, sentinel tokens, armed watches | the overwatch gate |
| `last_user_msgs[]` | `role == "user"` events, newest first, capped | delivery proof; what the session was actually told |
| `last_assistant_msgs[]` | `role == "assistant"` text | "what is it doing / saying" without reading a pane |
| `deliveries{}` | dispatch marker seen as a user turn, with timestamps | confirming a dispatch landed, and how fast |
| `pending_tool_calls[]` | `tool_use` events with no matching `tool_result` | **do not send while non-empty** |
| `watches{}` | armed watch tokens + deadlines | satisfied / missed, reported |
| `tokens_seen[]` | sentinel tokens on their own line | MATCH events |
| `offsets`, `_fired[]` | byte offsets and line uuids | resume without double-firing |

`state` is a reading of the projection, not a fresh guess. `IDLE` means quiet, never
finished; the heartbeat exists so a quiet session still gets asked whether it is done.

## The two permitted tmux reads

**Liveness.** Whether the pane still exists has no log equivalent — when the process dies
nothing more is written. That read is owned by the daemon alone, in one place, and
published as the `SESSION-DEAD` event. Every other component learns it from the
projection. No second reader, no ad-hoc `has-session` scattered through the tooling.

**The input box** — `fleet_input.py`. An unsent draft exists nowhere else, so this is the
one piece of state the log cannot carry. It is read narrowly: take the cursor row, expand
to the enclosing border rows, and return what lies between them. Scrollback above and
status chrome below cannot leak in — verified against a live pane, where the done token,
the previous output and the status bar were all excluded.

The same read separates a MENU from a draft, and that distinction is the point. A numbered
choice (a permission prompt, the trust dialog) renders with the same `❯` marker the
composer uses, so a naive read calls it input — and the dispatcher then clears it with
Ctrl-C, cancelling the prompt or ending the session. `clear_decision` refuses to clear
anything that is not a draft holding text, which is what makes "always clear before
sending" safe to say.

## Consequences, and what they fix

- **Never send while `pending_tool_calls` is non-empty.** A tool call awaiting a result
  means the session is mid-turn, so a paste lands inside a running turn; if the pending
  call is a permission or trust prompt, the dispatcher's own Ctrl-C cancels the prompt —
  and on the trust dialog can end the session. Today the dispatcher decides this from
  pane glyphs (`^\s*❯\s+\S` matches the highlighted option of a permission menu), which is
  how it came to interrupt live prompts while reporting success.
- **Delivery is a projection fact**, not a pane observation: the dispatch marker appears in
  `last_user_msgs`, or it did not. Measured over 99 confirmed deliveries: median 3.6s,
  p90 8.2s, worst 23.2s, so a 60s confirmation deadline is ~2.5x the worst observed case.
- **State stops flapping.** One fold over the log replaces per-caller pane inspection, so
  two components can no longer disagree about the same session — today the daemon and the
  dispatcher do exactly that.

## Migration status

`fleet_state.py` still captures the pane as its primary input, and `fleet_dispatch.sh`
still uses three pane reads as guards. Both are tracked here until the projection replaces
them; the daemon already folds `userturns`, `modeltext`, `watches`, `ring` and offsets, so
the projection exists and needs publishing rather than inventing.
