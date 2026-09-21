---
name: fleet-operator
description: "Drive the fleet-manager plugin: spawn, take over, prompt, ack, monitor, and remote-control a fleet of local Claude Code sessions in tmux."
version: 1.0.0
author: Hermes Agent
license: MIT
metadata:
  hermes:
    tags: [fleet, claude-code, tmux, remote-control, supervisor]
---

# Fleet Operator — supervise a fleet of Claude Code sessions

The controller-side companion to the `fleet-member` contract. This skill is a
**base to adapt**: it is deliberately environment-agnostic. Port it, then
substitute your own profiles, session names, and registry paths for the
`<...>` placeholders. Remove nothing marked REQUIRED — those are the safety
properties the fleet depends on.

## 0. Choose the profile FIRST — ALWAYS ASK
A fleet usually spans two (or more) Claude Code profiles, each a distinct
`CLAUDE_CONFIG_DIR` with its own model, permissions, hooks, plugins, and trusted
repos (e.g. personal vs work). **Ask the operator which profile a task belongs
to before spawning.** Do NOT guess from the repo name, path, or topic. Launch
with the matching `CLAUDE_CONFIG_DIR`.

## 1. Spawn a session
1. Confirm the profile (§0) + generate a unique session name (`cc-<project>-<short-id>`, prefix `p-`/`w-` by profile).
2. Launch in the target cwd, selecting the profile via `CLAUDE_CONFIG_DIR` (set it inside the tmux command string so `claude` inherits it):
   ```bash
   tmux new-session -d -s <name> -c <cwd> "CLAUDE_CONFIG_DIR=<profile_dir> claude --remote-control"
   ```
3. On first visit to a directory Claude shows a **workspace trust** prompt — answer it (`y` + Enter; a bare Enter is not always enough).
4. (If `--dangerously-skip-permissions` is used, also accept the bypass-permissions prompt — navigate Down + Enter.)

## 2. Take over an EXISTING session (not spawning)
`--resume` alone does NOT make a session the user opened elsewhere manageable:
1. Resolve the newest session id from `<profile_dir>/projects/*/*.jsonl` (by mtime) and/or the registry.
2. Launch under a managed tmux session: `tmux new-session -d -s <name> -c <cwd> "CLAUDE_CONFIG_DIR=<profile_dir> claude --resume <uuid>"`; answer any trust prompt.
3. **Send `/rc` to put it in remote-control mode** — without it the session will NOT surface as a steerable remote session in the Claude Code app. REQUIRED.
4. **Register it with the fleet watcher** (config-driven registry): verify with the registry `check` before treating it as managed. REQUIRED (register-with-watcher directive).
5. Then dispatch + verify delivery.

## 3. Prompt a session (send a task)
Use the **file + paste flow** for ANY prompt containing quotes/apostrophes or that is multi-line (inline `send-keys` breaks shell quoting). `send-keys` only for a short quote-free line.
```bash
# write the message to a file (not echo) -> /tmp/msg.txt
tmux set-buffer -b <name> "$(cat /tmp/msg.txt)"
tmux paste-buffer -b <name> -t =<session_name>
tmux send-keys -t =<session_name> Enter
sleep 3
tmux capture-pane -t <session_name> -p | grep -c '^you:'   # expect >= 1
```

**Dispatch pitfall (REQUIRED — real incident):** do NOT send `Escape` immediately
before the paste — it races and eats the FIRST character of the pasted block
(`TWO`→`WO`, `GO`→`O`) and can merge several pastes into one mangled blob, so the
agent looks idle/parked even though the paste "succeeded". Correct: `C-c` to
clear pending input first, then ONE fused buffer, then paste + Enter. Also
verify the **first line** landed intact (grep the opening word), not just that
`^you:` exists.

## 4. Acknowledgment protocol (ALWAYS use — REQUIRED)
Every dispatch must prove it was received + understood. Prepend to every prompt:
`Before doing anything else, reply exactly "MESSAGE RECEIVED" to acknowledge you got this. Then <task>.`
Then verify the literal ack echoes back within ~30–60s before reporting anything
in-flight. `^you:` proves delivery into the TUI; the echoed ack proves the model
is alive. No ack → assume failure (down, limit hit, unsubmitted, muted) — report
the session unreachable, never "in-flight". The fleet watcher separately enforces
a `MESSAGE-RECEIVED` ack + inject timeout (see docs/contract.md "Delivery
guarantee").

Useful mid-session slash commands: `/compact`, `/context`, `/effort`, `/model`, `/exit`.

## 5. Monitor & supervise

```bash
fleet_state.py <name>     # deterministic state fingerprint: WORKING / IDLE / STALLED / NEEDS-INPUT / DEAD
fleet_last.py  <name>     # what the session last SAID — its own most recent messages
fleet_reg.py   check      # every registered session: alive? transcript present? usage-limited?
```
`fleet_state.py` answers *which state*; `fleet_last.py` answers *what it said* (it tail-reads the
transcript, so it stays fast on a several-hundred-MB file). Prefer both over hand-rolled transcript
parsing.

**Armed watches — the ETA seam (`fleet_watch.py watch add|cancel|list`).** A watch names a session
and the exact token that ends its task; it is satisfied when the token appears on that session's own
(model) lines, and fires `SENTINEL-MISSED` if the deadline passes first. Arm the token **verbatim as
the dispatch contract words it** — the token form is free (`FW6F-POLL-LANDED` and
`DONE-fw…-wolfgang-…` both work); it is matched literally against the session's model lines, so a
token the slug/generic patterns cannot see still satisfies the watch. Never arm a token the session
was not told to emit: it can only ever false-fire at the deadline and cost the operator a check-in.
Cancel the old watch when a contract is superseded.

**tmux is the INPUT channel; the jsonl transcript is the OUTPUT channel.** Read tmux only to see the
input box: an unsent draft sitting between the borders, a parked `!` shell line, a blocking menu or
trust prompt, or an empty `❯` meaning it is ready to receive. NEVER read tmux for what the session is
doing or saying — the pane is a lossy render of the last few lines and it scrolls away mid-read.
`❯` = input box ready; `⏵⏵ auto mode on · N shell` = footer, not a spinner.

**Do not kill a slow session because it looks idle** — check `fleet_state.py` and `fleet_last.py`
first; it may be mid tool-loop. Silent failures appear in the transcript, not the pane. On high
context, prefer `/compact` over `/clear` for long-lived sessions.

## 6. Remote-control mirroring
`--remote-control` (or `/rc` inside a live session) starts the server letting the
operator control/watch that session from the browser or mobile app, mirroring
the live pane. The session must stay running in tmux. If launched without the
flag, the operator can attach later by restarting the session with it. Keep the
remote route private + authenticated; never relay prompt content to third parties.

## 6.5 Named layouts — save/restore the working set (fleet_layout.py)
A **layout** is a named snapshot of the currently-ALIVE registered sessions. Use it to free RAM
mid-day without losing anything, then bring the exact set back on demand:
```bash
fleet_layout.py save <name>              # snapshot the live set (name/short/profile/cwd/uuid)
fleet_layout.py resume <name>            # recreate each tmux session: same name + cwd +
                                         #   claude --remote-control --resume <uuid>  (never --continue)
fleet_layout.py close <name>             # kill those tmux sessions (transcripts persist)
fleet_layout.py close-all-except <short> # close everything but the named session(s)
fleet_layout.py list | show <name>
```
Verified: killing the tmux session does NOT delete the transcript jsonl, so a layout round-trips the
exact sessions with their context. The operator's daily rhythm is `save daily` → `close-all-except example-session`
→ `resume daily`. A session with no uuid yet (brand-new) resumes as a FRESH session in its cwd.

## 7. Cleanup
Graceful end: send finish, then kill the tmux session, then unregister from the
registry. Never leave an unregistered session orphaned.

## Adapting this skill (per the operator)
Substitute your own `<profile_dir>`, session-naming convention, and registry
path. Keep every REQUIRED step (profile-ask, `/rc` on takeover, register, file+
paste dispatch, ack handshake). The full transport + lifecycle protocol lives in
`docs/contract.md`; the agent side lives in `claude-skill-fleet-member/`.
