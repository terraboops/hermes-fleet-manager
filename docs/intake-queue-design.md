# Intake queue: recommended architecture and spike list

Status: design note. Consolidates four spikes. Nothing in a production path was changed by the spikes except the reliability harness, which is test-only.

Evidence (spike outputs):

- S-capture: `~/.hermes/plans/spike-gateway-precapture-t_ca389c44.md` (+ `.proto.py`)
- S-reliability: `tests/kanban_reliability/README.md` and `results/` (commit d8a2f72)
- S-contracts: `~/.hermes/plans/kanban-completion-contracts-and-lanes.md`
- S-triage: `~/.hermes/plans/kanban-triage-and-delivery.md` and `~/.hermes/plans/triage-spike/`

## 1. Problem and constraints

Every inbound chat message must be recorded as a durable card before any model runs. Background workers do the work. Nothing waits behind a running turn, nothing is dropped, and no acknowledgement message is sent.

Constraints:

- One chat model, one profile (`default`) today.
- The GPU is shared with gaming, so the local lane runs one card at a time.
- Telegram drops messages over about 4096 chars. Quiet hours are 23:00-05:00.
- `kanban.db` schema belongs to upstream. Local data lives in separate files.
- Missing a task costs more than one extra interruption, so every gate must fail open for capture and fail closed for completion.

## 2. Current state

- Kanban: durable SQLite board (`~/.hermes/kanban.db`), statuses triage, todo, ready, running, review, blocked, done. Claim is a compare-and-set in one `BEGIN IMMEDIATE` transaction.
- Dispatcher: embedded in the gateway, one tick lock per DB, spawns `hermes chat -q` workers in their own session. Lease 900 s, renewed by heartbeat.
- Filing policy: skill `kanban-task-routing` tells the chat agent when to file a card. It depends on the model choosing to call `kanban_create`. It is not a classifier.
- Chat capture today: 10 cards from 169 inbound Telegram messages (5.9%), window 2026-10-08 to 2026-10-12.
- Fleet bridge (t_0a297cdd): blocked for 94 h. Its board verbs (`claim-next`, `release`, `fail`, `heartbeat --worker`) are not in the running hermes. Commits b71108d03e and 446a46870a were displaced by an update reset and exist only under `refs/hermes-update-backups/diverged-main-20261009-224625-446a46870abe`.
- Triage status: cards in `triage` do not run until promoted. A decomposer creates children from a parent card.
- Busy input: `display.busy_input_mode: interrupt`, `display.busy_ack_enabled: false`. An arriving message interrupts the running turn and no ack text is sent.
- Review lane: built (`review_dispatch: true`, `request_review`, `request_changes`) but never entered. Workers close their own cards with `kanban_complete`.

## 3. Recommended architecture

```
 Telegram / desktop message
          |
          v
 BasePlatformAdapter.handle_message          (single choke point, idle AND busy)
   new hook: message_received  (fail-open, no send path)
          |  skip: internal, unauthorized, control/slash, clarify and approval replies
          v
 durable TRIAGE card                          idempotency_key = intake:<platform>:<chat>:<msg_id>
   (written BEFORE the turn; redelivery returns the same card)
          |
          v
 classifier (after capture, never before)     rules -> cheap model if conf < 0.6
   decision logged to triage-decisions.db     decide: now | defer | human
          |
   +------+---------------------------+----------------+
   |now                               |defer           |human
   v                                  v                v
 chat agent answers inline        lane by profile    stays in triage,
 (card closed as answered)        + priority band    one question + default
                                      |
        urgent 8-10 -> fast   normal 3-7 -> default   slow 0-2 -> slow / local
                                      |
                                      v
                       worker: claim -> lease -> heartbeat
                       (crash/timeout -> reclaim, 2 failures -> blocked + page)
                                      |
                                      v
                       request_review + completion results (contract gate G2)
                                      |
                                      v
                       reviewer lane: different-model profile `reviewer`
                       approve -> done | request_changes (cap 3) -> blocked
                                      |
                                      v
                       DeliveryPolicy (pure function, in notifier)
                       ONE message: urgent -> Telegram, rest -> desktop batch
```

The chat agent never waits on a worker. Workers are separate processes. The chat session is busy only for its own turn.

## 4. The seven questions

### Q1. Intake: can a gateway hook create the card before any model runs?

Answer: yes, with one core change. Evidence: S-capture.

- Idle path: plugin hook `pre_gateway_dispatch` fires before any model and can create a card now. It runs before authorization, so the plugin must check authorization itself.
- Busy path: `run_busy.py` never calls `pre_gateway_dispatch` or `post_gateway_admission` (zero hits). Because `busy_input_mode=interrupt` cancels the in-flight turn, a model-side `kanban_create` can be cancelled before it runs. Fix: one new `message_received` hook in `BasePlatformAdapter.handle_message` (`base.py:4038`), before the busy/idle branch.
- Idempotency key: `intake:<platform>:<chat_id>:<message_id>`; fallback `...:h:<sha256(text)[:16]>`. Not `update_id`, not time. Prototype on a throwaway DB: retry returned the same card, a new message created a second card.
- Capture rate: baseline 10/169 = 5.9% (4.1% of 243 persisted rows). The hook gives 100% of messages that pass the filters, by construction. No ground truth for "should have been a card", so 5.9% is a floor on missed work, not a defect rate. A full week was not measurable: the board has no cards before 2026-10-08.
- Voice and media: 99 of 169 log lines have empty text at hook time (transcription runs later). Create the card on arrival and enrich it after transcription.
- Race: `idx_tasks_idempotency` is a plain index. Make it `UNIQUE ... WHERE idempotency_key IS NOT NULL AND status != 'archived'`, or wrap in `BEGIN IMMEDIATE`.
- Not verified: the production hook does not exist yet; only the prototype ran.

### Q2. Triage: who classifies, and how is it logged?

Answer: two stages, rules then a cheap model when rules are unsure. Evidence: S-triage.

- The routing skill stays as filing policy. The classifier runs after capture, outside any expensive turn.
- Output: `decide` (now|defer|human), `lane`, `model`, `size` (S/M/L), `urgent`, `conf`, `why`. Gate at conf 0.6. A `defer` verdict may not be downgraded to `now` by a later stage.
- Log: own file `~/.hermes/state/triage-decisions.db` (tables `triage_decision`, `triage_outcome`, views `v_size_calibration`, `v_conf_calibration`), joined to cards by `card_id`. Schema loads and the views return rows.
- Rules eval on 27 real messages: decide 24/27, lane 22/27, size 19/27, no heavy work run inline in 26/27. Labels are hand-assigned and the rules were written after seeing the data, so these numbers are an upper bound.
- Open: no LLM classifier has been run. The prompt is a draft. Size cutoffs must be refit from the first 100 outcomes (finished cards ran 102 s to 2327 s, median 256 s).

### Q3. Reliability: claim, lease, heartbeat, retry, recovery

Answer: sound for dead holders, with gaps for stalled holders and non-hermes workers. Evidence: S-reliability, 12 negative cases on a scratch board.

- 10 pass, 2 documented gaps. No card lost in any case. No concurrent duplicate with the shipped worker. Dead holders reclaimed (S1, S2b, S3). Three racing dispatchers executed 12 cards exactly once each (S5).
- Gaps, in priority order:
  - G1: fleet bridge verbs displaced from `main` (highest impact).
  - G3: alive but stalled worker with NULL `last_heartbeat` is never reclaimed (S4a).
  - G2: pid-record window duplicates workers that do not call `adopt_worker_pid` (S2c).
  - G4: live-PID lease extension ignores the claim's own TTL.
  - G6: a card that fails twice is blocked with no alert.
  - G7: retries repeat side effects.
- Not proven: real power loss (SIGKILL keeps the OS page cache; durability rests on `PRAGMA synchronous=FULL`), and a real launchd gateway restart. The harness models the gateway as a dispatcher process. Both are open spikes (section 7).

### Q4. Completion: done only on observed behaviour

Answer: add a machine-checkable completion contract and force entry to the reviewer lane. Evidence: S-contracts.

- Cause of the two fast false-done cards (t_7bab2e99 at 102 s, t_1e2ba1fd at 141 s): the worker calls `kanban_complete` from `running`; the only gate is a non-blank summary; `completion_contract` accepts only `local-only`, `OWNER/REPO` or a PR URL; review approval is exempt from evidence; only one model exists, so no different-model review is possible.
- Design: JSON contract per card (acceptance ids, check type, evidence slot, forbidden status values such as `not_tested`). Mechanical gate in `complete_task` and `request_review`. Direct complete refused on contract cards. Same-model reviewer refused. Round cap 3, then `blocked`. Full YAML, flow diagram and 9-item code change list are in the S-contracts doc.
- Interim, no code: the card body tells workers to finish with `kanban_request_review(reviewer="reviewer")`. Honour system only.
- Not verified: no live card ran through the reviewer lane. The flow is designed, not exercised. A GLM-5.3 flash reviewer profile was read from `fallback_providers`, not probed. Whether the dispatcher may re-run worker commands is a safety decision that needs a second opinion.

### Q5. Lanes and board layout

Answer: one board, profiles as lanes, priority bands inside a lane. Evidence: S-contracts (dispatcher code read).

| lane | profile | model | priority | cap |
|---|---|---|---|---|
| urgent | fast | hosted flash | 8-10 | 1 reserved |
| normal | default | claude-sonnet-5-5 | 3-7 | 2 |
| slow | slow | cheap tier / nightly | 0-2 | 1 |
| local | local | local-qwen | 0-2 | 1 hard |
| review | reviewer | GLM-5.3 flash | inherits | 1 plus reserved review slot |

- Separate boards rejected: the host cap is shared, links and parents cannot cross boards, and the urgent lane would still share profiles.
- Priority alone rejected: no preemption, and nothing keeps local-model cards off a cloud model.
- Code needed: `max_in_progress_per_profile` takes a map, because today it is one integer for every profile.

### Q6. Delivery: one short message when it matters

Answer: a pure `DeliveryPolicy.decide` between `_notifier_collect` and `adapter.send` in `gateway/kanban_watchers_notifier.py`. Evidence: S-triage.

- Send only when the card needs her, a deliverable she asked for is ready, work stopped after retries, or it was flagged urgent. Never send claimed, spawned, heartbeat, status, acks, child completions of an open parent, or no-op results.
- Urgent goes to Telegram, immediately. Everything else goes to the desktop, batched up to 10 min or 5 events. Quiet hours hold non-urgent to 05:00. Dedupe key `(card_id, event_kind, sha(first 200 chars))`, TTL 24 h.
- When the gateway is down, urgent pages use the direct Bot API sender from `fleet_watch.py` (decision t_1e2ba1fd).
- Six negative tests are named in the S-triage doc and are required before this counts as live. None are written yet.

### Q7. What the chat agent does while workers run

Answer: it stays available. Evidence: S-capture (code read, not exercised under load).

- Workers are separate processes claimed through `kanban.db` (lock `host:pid`). They do not hold the chat session slot.
- A message that arrives mid-turn interrupts the turn (`busy_input_mode=interrupt`). With the hook, the card already exists, so the interrupt loses no work. `busy_ack_enabled=false` keeps it silent, and the hook adds no send path.
- Worker completions arrive as internal events and must be excluded from capture via `event.internal`.
- Open: no test with a live worker plus a live second message. Owner and spike listed below.

## 5. Open risks

- R1. The bridge commits can be lost again: any `hermes update` that resets `main` displaces local commits. Carry the bridge as a plugin or upstream it.
- R2. Board noise: about 70-90% of captured messages are conversation. Without the classifier lane, triage fills with junk and she stops reading it. Capture and classifier must ship together.
- R3. The same-model reviewer problem. Until a second profile and model are verified, a "different model reviews" claim is false.
- R4. Interrupt semantics: capturing before the turn is only safe if the hook cannot raise. It is fail-open and must be tested with a raising handler.
- R5. Re-running worker commands in the dispatcher is a trust call. Read-only allowlist only.
- R6. Classifier numbers are an upper bound. Do not tune thresholds from them.
- R7. Reliability numbers come from a modelled gateway on a scratch board, not the live one.

## 6. First slice

Name: capture-only intake with a negative-case suite. No classifier model, no reviewer lane.

In scope:

1. `message_received` hook in `BasePlatformAdapter.handle_message`, fail-open, no send path.
2. Capture handler: filters (internal, unauthorized, control, clarify/approval), idempotent triage card, voice enrichment after transcription.
3. Unique partial index on `tasks.idempotency_key`.
4. Rules-only classifier writing `triage-decisions.db`. Cards it marks `now` or `defer` are logged, not yet promoted.
5. Re-apply bridge commits b71108d03e and 446a46870a on `main`, with a check that fails when `claim-next` is missing.

Negative cases that must pass (observed on a throwaway gateway instance, never the live one):

- message arrives while a turn runs: card exists, turn interrupted, no ack sent;
- same message delivered twice: one card;
- gateway killed between receipt and card write: redelivery yields exactly one card;
- internal event, unauthorized sender, slash command, clarify answer: zero cards;
- raising hook: message still processed;
- voice message with empty text: card created, then enriched.

Done means the cases above are observed passing on a throwaway gateway, and a week of live capture shows the rate against the 5.9% baseline.

## 7. Remaining spikes

| # | spike | question | owner |
|---|---|---|---|
| SP1 | Real gateway restart and power cut | Q3: launchd ExitTimeOut 25 s against a ~70 s drain; S3 inside a VM with a hard power-off; throwaway gateway only | default (worker) |
| SP2 | LLM classifier run | Q2: cheap-model pass over the same 27 messages plus 100 more; refit size cutoffs | default (worker) |
| SP3 | Reviewer lane live run | Q4: create `reviewer` profile, probe GLM-5.3 flash, run one contract card end to end | default (worker), model choice needs the user |
| SP4 | Contract re-run safety | Q4: decide the read-only command allowlist | multi-model second opinion, then the user |
| SP5 | Chat availability under load | Q7: live worker plus second message on a throwaway gateway | default (worker) |
| SP6 | Stalled worker backstop | Q3 G2, G3, G4: NULL heartbeat treated as start time, default `max_runtime_seconds`, pid intent row | default (worker) |
| SP7 | DeliveryPolicy and its six negative tests | Q6 | default (worker) |
| SP8 | Per-profile cap map and reserved urgent slot | Q5 | default (worker) |

Owner "default" is the kanban profile that runs these cards. Each spike becomes a card with a `completion_contract` once the contract gate exists.
