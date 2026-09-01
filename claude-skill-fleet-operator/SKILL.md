---
name: fleet-operator
description: "Operate the fleet-manager plugin: spawn, take over, prompt, ack, monitor, and remote-control a fleet of local Claude Code sessions in tmux."
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
tmux paste-buffer -b <name> -t <session_name>
tmux send-keys -t <session_name> Enter
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
`tmux capture-pane -t <name> -p -S -50` to read progress. Indicators: `❯` =
waiting for input; `●` lines = actively using tools; `⏵⏵` = permission banner.
**Do not kill a slow session because it looks idle** — verify first. Watch
`grep -iE 'error|failed'` for silent failures. On high `/context`, prefer
`/compact` over `/clear` for long-lived sessions.

## 6. Remote-control mirroring
`--remote-control` (or `/rc` inside a live session) starts the server letting the
operator control/watch that session from the browser or mobile app, mirroring
the live pane. The session must stay running in tmux. If launched without the
flag, the operator can attach later by restarting the session with it. Keep the
remote route private + authenticated; never relay prompt content to third parties.

## 7. Cleanup
Graceful end: send finish, then kill the tmux session, then unregister from the
registry. Never leave an unregistered session orphaned.

## Adapting this skill (per the operator)
Substitute your own `<profile_dir>`, session-naming convention, and registry
path. Keep every REQUIRED step (profile-ask, `/rc` on takeover, register, file+
paste dispatch, ack handshake). The full transport + lifecycle protocol lives in
`docs/contract.md`; the agent side lives in `claude-skill-fleet-member/`.
