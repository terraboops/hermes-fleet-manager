# hermes-fleet-manager

Supervise a fleet of **Claude Code** sessions from Hermes (or any agent runtime): give each
session a shared *managed-member contract*, watch their transcripts for completion / failure /
needs-input signals with a small config-driven daemon, and relay those to a human (or a
supervisor agent) as one coherent digest instead of a firehose.

## Why this exists (key benefits)

1. **Stays inside Claude Code's terms of service.** It coordinates through Claude Code's
   *officially supported* automation surface, and nothing but it: the documented
   `--remote-control` / `--resume` / `--ax-screen-reader` flags, the session **remote-control
   console**, the **transcript** the CLI itself records, and a **skill injected into the
   profile** (`fleet-member`) that teaches sessions the shared contract. No binary
   reverse-engineering, no scraping private or undocumented surfaces, no headless screenshots
   of the app — it drives the app through its own remote-control + transcript channels.

2. **Remote-control any session from Claude Code.** Every managed session runs under
   `--remote-control` (the `/rc` console), so you can attach and steer any session — including
   fully remote ones — from the Claude Code app, while the supervisor agent (Hermes) dispatches
   work into them. One human, many sessions, all drivable from one place.

3. **Bidirectional, reliable interactivity between Claude and Hermes.** Two-way, not
   fire-and-forget:
   - *Claude → Hermes:* sessions raise events into their transcript — completion
     (`DONE-<slug>-<session>-<ms>`), blocked-on-you (`NEEDS-INPUT-<slug>-...`), failures
     (`traceback`) — which the daemon catches (role-filtered, exact-matched, deduped) and
     delivers to the supervisor/human as **one coherent digest** (debounced + batched, not a
     per-event firehose).
   - *Hermes → Claude:* the supervisor dispatches concrete tasks and status checks and gets
     back clean, machine-parseable JSON; the `fleet-member` contract tells sessions exactly how
     to ask for a human decision asynchronously and keep working meanwhile.
   - *Reliable:* buffered events survive daemon restarts, writes are atomic, a single-instance
     `flock` guard prevents double-processing, and webhook pushes are HMAC-signed. A session
     that goes quiet is surfaced — never silently dropped.

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
