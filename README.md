# hermes-fleet-manager

[![License](https://img.shields.io/badge/license-Apache--2.0-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.11%2B-3776ab.svg)](https://www.python.org/)
[![Tests](https://img.shields.io/badge/tests-103%20passing-brightgreen.svg)](#verification)
[![Hermes Agent](https://img.shields.io/badge/Hermes%20Agent-plugin-8a2be2.svg)](https://github.com/NousResearch/hermes-agent)
[![Harness](https://img.shields.io/badge/harness-Claude%20Code%2C%20any%20CLI-d97757.svg)](#launch-specs)

**Supervise a fleet of Claude Code sessions from one [Hermes Agent](https://github.com/NousResearch/hermes-agent), and only hear about the sessions that actually need you.**

Ten sessions working at once is not a problem. Ten sessions reporting at once is. This repo is
the layer that sits between them: a shared contract every session follows, a daemon that reads
their transcripts, and an escalation gate that decides, event by event, whether you need to be
told. A crash reaches you in seconds. A delivery receipt never reaches you at all.

## What you get

- **One digest, not a firehose.** Signals are deduped, debounced and batched into a single
  coherent message. Urgent events (a crash, a session blocked on your decision) flush
  immediately; bookkeeping stays silent.
- **A read model instead of pane-scraping.** State and content come from each session's JSONL
  transcript, never from the tmux pane. The pane lies about delivery and about state; the
  transcript does not. See [the read model](docs/read-model.md).
- **A model-gated escalation lane.** Everything ambiguous goes to a local decision model
  ([Laya](#laya-the-escalation-gate)) that answers "does this deserve the operator?" at 0.80
  confidence, with the answer logged rather than enforced until the threshold is calibrated.
- **Sessions that survive.** Named layouts, resume by UUID, crash recovery, power-loss resume,
  and CLI-version drift reporting.
- **A contract, not a convention.** Managed sessions emit exact, whole-token sentinels into
  their own transcript, so coordination needs no new API, no private surface and no scraping.

## How it works

```
Hermes Agent
  ├── cron          overwatch jobs: one per session, gated by a fleet_state.py monitor_script,
  │                 so the agent turn only runs when that session's state CHANGES
  ├── webhook       ONE HMAC-signed POST per digest -> /webhooks/fleet-sentinel
  │                 the adapter runs an agent turn and delivers it to your chat
  ├── skills        fleet-member (injected into every managed Claude profile)
  │                 fleet-operator (the controller-side playbook)
  └── terminal      the CLIs below, symlinked into ~/.hermes/scripts/cc-watch/

fleet_watch.py --daemon            (launchd on macOS, systemd on Linux)
  ├── polls registered transcripts every 5s, role-filtered to model-emitted lines
  ├── folds a per-session read model: state, last user turn, last assistant text
  ├── consults the Laya gate at 127.0.0.1:11436 before anything is delivered
  └── batches a burst into ONE digest, or appends to an events file when the webhook is down
```

### Why not Claude Code's stream-json / JSON-RPC channel

Claude Code can emit structured events (`claude -p --output-format stream-json --verbose`),
which would remove pane-scraping entirely. It was prototyped and then rejected as the
control channel, for one reason: **stream-json is a print-mode serializer, not an
interactive one.** It spawns a fresh process per call, so it returns a structured reply but
cannot steer a session that is already running.

Adopting it would mean trading live interactive sessions for one-shot print processes, and
giving up `--remote-control`: the channel every managed session is launched under, and the
one that lets the operator attach to any session from the app. Remote control is the feature
worth protecting here, so the tmux paste plus transcript-read transport stays.

Nothing in this repo uses the structured stream. A one-shot headless call (`claude -p`) is a
different tool with a different job: a reviewer in a multi-model review, a sample generator
in an eval, a small delegated task where a tmux session would be overkill. None of that is a
control channel for a running fleet, so none of it belongs in this repo's transport.
Prototype findings: [docs/json-rpc-prototype.md](docs/json-rpc-prototype.md).

## Quick start

```bash
git clone https://github.com/terraboops/hermes-fleet-manager.git
cd hermes-fleet-manager
cp config.example.yaml config.yaml        # registry path, profiles, relay target
```

Put the CLIs where Hermes sessions and skills can call them:

```bash
mkdir -p ~/.hermes/scripts/cc-watch
for f in scripts/*.py fleet_watch.py fleet_reg.py; do
  ln -sf "$PWD/$f" ~/.hermes/scripts/cc-watch/
done
```

Secrets live in `~/.hermes/.env` (`0600`), never in the repo:

```bash
echo 'FLEET_WEBHOOK_URL=http://localhost:8644/webhooks/fleet-sentinel' >> ~/.hermes/.env
echo "FLEET_WEBHOOK_SECRET=$(openssl rand -hex 32)"                    >> ~/.hermes/.env
```

Register a session and start the daemon:

```bash
python3 fleet_reg.py register cc-p-example --short example \
    --profile personal --cwd ~/Developer/example --uuid <session-uuid>
python3 fleet_watch.py --daemon --interval 5 --to telegram:<chat_id>
```

Nothing is watched until it is registered. There is no auto-discovery, on purpose: a fleet that
grows by accident is a fleet nobody can account for.

## How it plugs into Hermes

| Surface | What this repo contributes | Where it lands |
| --- | --- | --- |
| **Webhook platform** | One HMAC-signed digest per burst | `http://localhost:<port>/webhooks/fleet-sentinel` (port from `platforms.webhook.extra.port`) |
| **Cron** | Monitor-gated overwatch jobs, one per armed session | `~/.hermes/cron/jobs.json`, created by `fleet_overwatch.py arm` |
| **Skills** | `fleet-member` (managed-session contract) and `fleet-operator` (controller playbook) | Injected into the Claude profile, and read by the supervising agent |
| **Terminal** | Every CLI in `scripts/`, symlinked into `~/.hermes/scripts/cc-watch/` | Called by the agent, by skills, and by the daemon |
| **Local service** | The Laya gate on `127.0.0.1:11436` | Consulted by the daemon via `FLEET_GATE_URL` |

The daemon keeps its own interpreter and dependencies, and the gate runs as a local HTTP
service, so the fleet never reaches into Hermes' process and Hermes never reaches into the
fleet's. They meet at two narrow, auditable seams: a signed webhook and a loopback decision
endpoint.

`manifest.yaml` declares the tools, skill and daemon this repo intends to expose. Registering
the fleet verbs as native Hermes tools (`plugin.yaml` + `register(ctx)`) is on the
[roadmap](#roadmap); today they are invoked as CLIs, which is why the symlink step above exists.

## Laya: the escalation gate

Not every event deserves a human, and deciding which is exactly the judgement a rule engine
gets wrong. `laya_gate.py` splits the problem into three lanes, and only the third one asks a
model.

| Lane | Decided by | Examples | Behaviour |
| --- | --- | --- | --- |
| **1** | Code | `traceback`, `SESSION-DEAD-`, `USAGE-LIMIT-`, `NEEDS-INPUT-`, a blocked production action, money | Escalate. Hard items break through quiet hours |
| **2** | Code | `ACK-OK`, `WATCH-SATISFIED`, `DONE-`, `SENTINEL-MISSED-`, daemon bookkeeping | Silent. The log and the transcript remain the record |
| **3** | Laya | Everything else: a stall, a quiet session, an ambiguous completion | Classified. Logged, not enforced, until the threshold is calibrated |

Lane 1 exists because of a measurement, not a preference: given a `traceback`, Laya scored
`0.43` escalate, meaning "do not tell her". A classifier that can sit on a crash is worse than
no classifier, so a crash never reaches the model.

**The model.** [Laya](https://huggingface.co/convaiinnovations/laya) is Convai Innovations'
Apache-2.0 open-weights "System 1" decision model, served here through an MLX build
(`LAYA_GATE_MODEL`, default `aac6fef/laya-mlx`). It is loaded lazily on the first lane-3
decision, so a quiet fleet costs nothing.

**Knobs** (all environment or `config.yaml`, all with defaults that fail safe):

```bash
LAYA_GATE_PORT=11436              # loopback only
LAYA_GATE_THRESHOLD=0.80          # deliberately conservative while uncalibrated
LAYA_GATE_ENFORCE=0               # 0 = log the lane-3 answer, never act on it
LAYA_GATE_QUIET_HOURS=23:00-05:00 # soft items held overnight, reported once at morning
LAYA_GATE_REPEAT_WINDOW=21600     # the same event family, same session, once per 6h
```

**It is reviewable, which is the point.** A gate nobody can audit is a gate nobody should trust:

```bash
python3 laya_gate.py --review 1     # daily: were any decisions wrong?
python3 laya_gate.py --label <id> good|bad [note]
python3 laya_gate.py --digest       # the morning rollup of what was held overnight
python3 laya_gate.py --selftest     # lane assignment, repeat suppression, the idle-stall fix
curl -s localhost:11436/health      # {"ok":true,"model_loaded":true,"enforce":false,...}
```

The review is hindsight, not bookkeeping. Each decision resolves its session's transcript by
UUID and reads what the session actually did next:

- **Escalated, and the session carried on by itself within the resume window.** The
  interruption probably was not needed. Counted against precision.
- **Held, and the session then went quiet for hours.** Something may have deserved the operator
  and did not get her. Counted against recall.
- **Younger than the quiet threshold.** Reported as too recent, because an event minutes old has
  not had the chance to be followed by anything, and calling it silent would be a false alarm.

Completion receipts are excluded from the second list: a session going quiet after its `DONE-`
token means the work finished, not that a signal was missed.

## The sentinel contract

Managed sessions raise signals into their own transcript. Matching is **exact,
case-sensitive and whole-token**, and only on lines the model itself emitted, so a dispatcher
quoting a token can never fire one.

| Token | Meaning |
| --- | --- |
| `DONE-<slug>-<session>-<ms>` | The dispatched task finished |
| `NEEDS-INPUT-<slug>-<session>-<what>` | Blocked on a human decision |
| `PROGRESS-<task>-<pct>` | Non-terminal forward motion |
| `traceback` | A failure (caught automatically, never signalled by hand) |
| `MESSAGE-RECEIVED` | Optional handshake, never a substitute for `DONE-` |

A per-daemon-run random slug (written to `fleet_watch.slug`) namespaces every token, so
transcripts cannot collide and a stale or partial token never fires. A status ask returns one
line of JSON and the session resumes its work:

```json
{"schema":"fleet/1","session":"cc-p-example","state":"running","summary":"...","last":"...","current":"...","next":"...","eta":"...","concerns":"...","blockers":"..."}
```

The full protocol, including the agent state machine and the dispatch hard rules, is in
[docs/contract.md](docs/contract.md).

## The CLIs

| Command | What it does |
| --- | --- |
| `fleet_reg.py register, unregister, list, check, spawn, hygiene` | The registry. Explicit lifecycle, no auto-discovery |
| `fleet_watch.py --daemon` | The watcher: read model, sentinel matching, digest batching |
| `fleet_watch.py watch add, list, cancel` | Arm a completion watch with a deadline, so a hung agent is caught by its deadline |
| `fleet_state.py` | Deterministic state fingerprint for one session, used as the overwatch gate |
| `fleet_last.py`, `fleet_transcript.py` | What a session actually said, without reading a pane |
| `fleet_input.py` | Reads ONLY the composer, for the draft-stash path |
| `fleet_dispatch.sh` | Guarded dispatch: waits for a clean prompt, bracketed paste, verifies the first line landed, sends a `Read <path>` pointer for large payloads |
| `fleet_ack.py` | ACK-verified dispatch: arms a delivery watch, reports `LANDED` only on a real user turn |
| `fleet_answer.py` | Answers a session's own interactive choice UI |
| `fleet_overwatch.py arm, status, last-report, disarm` | Arms an overnight overwatch as a monitor-gated Hermes cron job |
| `fleet_layout.py save, resume, close, list, show` | Named layouts: snapshot the live session set, resume the same names by UUID |
| `fleet_mcp.py status, ensure` | Provisions the MCP servers a profile needs, before the launch |
| `fleet_version.py latest, of <session>, report` | Which CLI version each session is running, and which is newest |
| `laya_gate.py` | The escalation gate (service, `--review`, `--label`, `--digest`, `--selftest`) |
| `crash_recover_fleet.py`, `resume_after_powerloss.py`, `restart_fleet_sessions.py`, `kick_fleet.py` | Recovery paths |

## Launch specs

A profile is a name plus *how* to launch a session for it. No harness name, flag or environment
variable is fixed in code:

```yaml
profiles:
  - name: example
    command: claude                # any harness executable
    args: ["--remote-control"]     # extra flags, always passed
    env:
      CLAUDE_CONFIG_DIR: ~/.claude-example
    resume_flag: "--resume"        # how this harness resumes by id
    servers: {}                    # MCP servers this profile needs
```

A registry entry may carry the same keys, and per-session values win over the profile's, so one
session can add a flag or an env var without inventing a profile.

## Why not just drive tmux, or run a process supervisor

| | Raw tmux scripts | Process supervisor | This repo |
| --- | --- | --- | --- |
| Where state comes from | The pane, which lies about delivery and state | Process exit codes | The session transcript, folded into a read model |
| Delivery proof | None | N/A | An armed watch satisfied by a real user turn, or `ACK-MISSED` at the deadline |
| Notification shape | One message per event | One alert per crash | One deduped digest, urgent events flushed |
| Does a quiet session wake anything | No | No | Only on a state change, via the monitor gate |
| Escalation judgement | Hardcoded | Hardcoded | Code for the decidable lanes, a calibrated model for the rest |
| Resume after a crash or power cut | Manual | Process restarts, not sessions | `fleet_layout.py resume <name>`, by UUID |

The design rule underneath all of it: **tmux is write-only.** The controller sends to the pane
and never reads it for state, content or proof of delivery.

## What a digest looks like

```
FLEET DIGEST (4 events, 3 sessions)

cc-p-alpha    DONE-OW-ALPHA-0926G
  Migration finished, suite green, pushed.

cc-p-bravo    NEEDS-INPUT-PICK-A-REPO
  Two candidate repos, neither obviously right. Waiting on you.

cc-p-charlie  traceback (ValueError in tools.py:88)
  Session stopped. Not relaunched.
```

Everything else that happened in those five seconds was a receipt, and you did not need it.

## Configuration

Everything environment-specific lives in `config.yaml` (see
[`config.example.yaml`](config.example.yaml) for the annotated version):

| Key | Purpose |
| --- | --- |
| `registry.path` | Where managed sessions are tracked |
| `profiles[]` | Launch specs, per profile |
| `relay.target` | Where digests are pushed (`telegram:<chat_id>`, or `local` to log only) |
| `relay.webhook_secret_env` | The env var holding the HMAC secret |
| `watch.interval_seconds` | Poll interval (default 5) |
| `watch.stall_window_seconds` | Silence before a `STALL` check-in fires |
| `watch.ack_file`, `watch.user_msgs_file` | Delivery watches and the last hour of real user turns |
| `dedupe.keep_per_session` | Content-hash dedupe depth, because Claude rewrites transcripts in place |

## Verification

```bash
python3 -m unittest discover -s tests -t .   # 103 tests
python3 laya_gate.py --selftest              # lane assignment and repeat suppression
python3 fleet_version.py report              # CLI drift across the fleet
python3 fleet_overwatch.py status            # which overwatches are armed
curl -s localhost:11436/health               # the gate is up and which model is loaded
```

## Troubleshooting

**A dispatch reported `NOT-SUBMITTED` but the session answered it.** The pane keeps only its
last ten lines, so a delivered message that the session already replied to scrolls out of view.
A dispatch with no receipt is now retried exactly once (safe: nothing was received, so a retry
cannot double-post) and only then reported.

**A dispatch to a busy session reads `NOT-SUBMITTED` and the daemon logs `ACK-OK` seconds
later.** A busy session takes a paste as a queued turn, which the transcript records as a
queue operation rather than a user turn. The receipt check counts either form.

**The overwatch went silent on a session that was waiting on you.** A false `QUEUED` reading
used to swallow the escalation, because `QUEUED` means "leave it alone". The rule is now a
shape test on short indicator chrome, not a substring scan over the live region. A missed queue
costs nothing; a false one costs the escalation.

**A "nothing to report" run still reached the chat.** The silence marker is a literal token.
A translated or localised one does not match the delivery filter, so the template now says to
copy it character for character.

**`fleet_version.py` says a session is behind and a fresh install looks older.** Never resolve
"newest" through a `stable` dist-tag. At the time of writing `stable` was `2.1.274` while the
fleet was already on `2.1.282`, so resolving through it moves an install backwards. The
running version is read from the live process's mapped executable, never from a pane or a
transcript.

## Roadmap

- Register the fleet verbs as native Hermes tools (`plugin.yaml` + `register(ctx)`), so the
  symlink step becomes an install step.
- Calibrate the lane-3 threshold on labelled decisions, then turn `LAYA_GATE_ENFORCE` on.
- Publish the gate's precision and recall, from the daily review, as numbers rather than prose.

## Contributing

Issues and PRs are welcome. The house rules, in order of how often they matter:

1. **State comes from the transcript.** A change that reads the pane for state, content or
   delivery proof will be sent back.
2. **A fix names the failure it prevents.** Comments and commit messages say which bug class
   the code exists to stop, not what the diff does.
3. **Tests assert invariants**, not snapshots, and `python3 -m unittest discover -s tests -t .`
   stays green.
4. **Config over code.** Anything environment-specific belongs in `config.yaml`.

## License

[Apache-2.0](LICENSE)
