# hermes-fleet-manager

A **Hermes plugin** to supervise a fleet of Claude Code sessions. It gives each session a shared *managed-member contract*, watches them for completion/failure/needs-input signals via a config-driven daemon, and relays those to the human (or a supervisor agent) through a push model.

> **Status: PRIVATE REVIEW.** Scaffold for your review before release. The living deployment currently lives in `~/.hermes/scripts/cc-watch/`; this repo is the generalized, config-driven, installable form.

## What it does
1. **Registry** – tracks which sessions are managed (`register` / `unregister` / `list` / `check`). The watcher watches *only* what's registered.
2. **Regex signal patterns** – the sentinel protocol Claude sessions emit in their transcripts:
   - `DONE-<NAME>` → task finished (case-sensitive, uppercase)
   - `NEEDS-INPUT-<NAME>` → the agent is blocked and the human must decide/act
   - `[tT]raceback` → failure marker (auto-caught)
3. **Watcher daemon** – polls registered transcripts (~5s), dedupes fired signals by content (Claude compacts/replays in place), and pushes events to a webhook / relay.
4. **`fleet-member` skill** – canonical `SKILL.md` injected into configured Claude profiles so every session knows the contract (see below).

## The managed-member contract (version 1)
A managed Claude session should:
- End every completed task by emitting `DONE-<NAME>` as the final line.
- On a status-check ask, reply with **one line of JSON**:
  ```json
  {"session":"<name>","state":"working|idle|parked","summary":"...","last":"...","current":"...","next":"...","eta":"...","concerns":"...","blockers":"..."}
  ```
- When blocked on a human decision, say it in plain words **and** emit `NEEDS-INPUT-<NAME>`.
- Report real status only; never echo template placeholders or fabricate a `DONE`.
- Not self-register (registry lifecycle is the supervisor's job).

## Layout
```
manifest.yaml            # Hermes dir-plugin manifest
config.example.yaml      # all config knobs (registry, transcript roots, chat target, patterns)
fleet_watch.py           # config-driven watcher daemon (webhook push + events file fallback)
fleet_reg.py             # register/unregister/list/check
claude-skill-fleet-member/SKILL.md   # injected managed-member skill for Claude sessions
README.md
```

## Install (post-release)
- Drop into Hermes's plugin dir (dir-plugin), set `config.yaml` (or env) for registry path / transcript roots / chat target / webhook secret, register your sessions, and start the `fleet_watch` daemon.

## Security
- Long-lived daemon, HMAC-signed webhook push. All secrets come from env/config (never in git — see `.gitignore`).
