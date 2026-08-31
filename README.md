# hermes-fleet-manager

Supervise a fleet of **Claude Code** sessions from Hermes (or any agent runtime): give each
session a shared *managed-member contract*, watch their transcripts for completion / failure /
needs-input signals with a small config-driven daemon, and relay those to a human (or a
supervisor agent) as one coherent digest instead of a firehose.

## What it does

1. **Session registry** — declare which sessions are managed (`register` / `unregister` /
   `list` / `check`). The watcher watches *only* what is registered.
2. **Sentinel protocol** — Claude sessions emit signals in their transcript, all matched
   **exact + case-sensitive + whole-token**:
   - `DONE-<slug>-<session>-<ms>` → a dispatched task finished.
   - `NEEDS-INPUT-<slug>-<session>-<what>` → the agent needs a human decision.
   - `traceback` → a failure marker (auto-caught).
   - Generic `DONE-`/`NEEDS-INPUT-`/`ERROR-`/`PROGRESS-` forms are also watched (used for
     session-lifetime notifications).
   A **per-daemon-run random slug** (written to `fleet_watch.slug`) namespaces every token,
   so transcripts can't collide and stale/partial tokens never fire.
3. **Watcher daemon** — polls registered transcripts (~5 s), **role-filters** (only lines the
   model *emitted* — never dispatcher/instruction text), dedupes by content (Claude
   compacts/replays in place), and **debounces into ONE batched webhook** so the handler turns
   the sentinel firehose into a single digest (not a message per event). Urgent signals
   (`NEEDS-INPUT-`/`traceback`) flush immediately.
4. **Hardening** — persistent buffered events (survive daemon restart), atomic writes, a
   single-instance `flock` guard, and (optionally) a signed HMAC webhook push.
5. **`fleet-member` skill** — a canonical `SKILL.md` injected into Claude profiles so every
   managed session knows the contract and how to raise errors / ask for input asynchronously.

## The managed-member contract

A managed Claude session:
- Ends every dispatched task with its exact `DONE-<slug>-<session>-<ms>` token.
- On a status-check ask, replies with **one line of JSON**:
  ```json
  {"session":"<name>","summary":"...","last":"...","current":"...","next":"...","eta":"...","concerns":"...","blockers":"..."}
  ```
- When blocked on a human decision, says it in plain words **and** emits `NEEDS-INPUT-<slug>-...`.
- Reports real status only — never template placeholders or fabricated completions.
- Does not self-register (registry lifecycle belongs to the supervisor).

## Layout

```
manifest.yaml            # Hermes dir-plugin manifest
config.example.yaml      # all config knobs (registry, profiles, relay, debounce, slug)
fleet_watch.py           # config-driven watcher daemon
fleet_reg.py             # register / unregister / list / check
claude-skill-fleet-member/SKILL.md   # managed-member skill injected into Claude profiles
model/fleet_state_machine.tla/.cfg   # formal model of the session state machine (TLA+/TLC)
docs/                    # contract + design notes
```

## Install

- Drop into Hermes's plugin dir (dir-plugin), set `config.yaml` (or env) for registry path,
  profile transcript roots, relay target and webhook secret, register your sessions, and start
  the `fleet_watch` daemon (`python3 fleet_watch.py --daemon --interval 5`).

## Security

- Loopback-daemon; optionally HMAC-signed webhook push. All secrets come from env/config —
  never from git (see `.gitignore`).
- The sentinel slug is generated per daemon run and written beside the state file; dispatch
  tokens use it so nothing stale can fire.

## Roadmap

The watcher (registry + sentinel digest + hardening) is the production-tested core. Planned:
an orchestration/controller layer (status collector, automated recovery, autostart template,
controller design doc).

## License

[Apache-2.0](LICENSE)
