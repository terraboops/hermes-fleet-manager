# The gate's decisions

The gate answers three questions, in this order of cost. Each one is asked only when the
previous one has not already settled the matter, and each one splits into lanes: what can be
decided by structure is decided by structure, and only what is genuinely a judgement reaches a
model.

| # | Question | Endpoint | Verb | Default answer when unsure |
| --- | --- | --- | --- | --- |
| 1 | Does this event deserve the operator? | `POST /decide` | `classify()` | escalate |
| 2 | What should happen to this session? | `POST /respond` | `respond()` | the standing policy for the state |
| 3 | May this action be affirmed without her? | `POST /affirm` | `affirm()` | escalate (maximum risk) |

Every answer carries `lane`, `rule`, `confidence` and `enforced`. `enforced: false` means the
answer was recorded and reported but not acted on, which is how a threshold is calibrated
without changing behaviour while it is being calibrated.

## 1. Does this event deserve the operator

Decided by the daemon for every event, before delivery. Lane 1 escalates in code (a crash, a
dead session, a usage limit, a blocked classifier, money, a session asking for input); lane 2
stays silent in code (receipts, bookkeeping, a version notice, a quiet session with nothing in
flight, a repeat); lane 3 goes to Laya with the event's situation.

Lane 1 exists because of a measurement: given a `traceback`, the model scored 0.43 escalate,
meaning "do not tell her". A classifier that can sit on a crash is worse than no classifier.

## 2. What should happen to this session

The escalation question only decides whether the operator is interrupted. It does not answer
what the fleet does next, and that is the decision that actually keeps sessions moving:

```
let it continue | nudge it with one concrete next increment | challenge its completion claim |
re-align it to the authorized work | answer it and let it continue | escalate to the operator
```

**Lane 1, settled by structure:**

| Condition | Action | Why |
| --- | --- | --- |
| state `WORKING` or `QUEUED` | let it continue | a nudge would land mid-turn |
| state `DEAD` | escalate to the operator | a dead session is reported, never nudged |
| the session is showing its own choice UI | escalate to the operator | a picker is a checkpoint someone put there on purpose |
| blocked by the auto-mode classifier | escalate to the operator | only the operator authorizes |
| production deploy or client-visible in the probe | escalate to the operator | the blast radius is hers |

**Lane 2, settled by history:** the same response already given for this session inside the
repeat window is not given again. A repeat nudge is noise, and noise is what stops the nudges
being read.

**Lane 3:** Laya, against the doctrine in `RESPOND_DOCTRINE`. Below `LAYA_GATE_RESPOND_THRESHOLD`
(0.50) the answer is too close to call, and the gate falls back to the **standing policy for
that state** rather than to the model's guess. An uncalibrated model must never be a silent
policy change: unsure has to mean "do what we did before", and before, `IDLE` got nudged.

## 3. May this action be affirmed without her

Affirming is the one response where being wrong is expensive, because the fleet proceeds on the
operator's behalf. So the question returns a **risk score**, and the score is the threshold:
`risk = 1 - P(safe to affirm)`, and the action is affirmed only under `LAYA_GATE_AFFIRM_RISK`
(0.20).

**Lane 1, never scored by a model:**

| Shape | Why |
| --- | --- |
| `rm -rf`, `DROP TABLE`, `git push --force`, `push origin --delete`, `terraform destroy`, `kubectl delete`, `git reset --hard` | destructive and irreversible |
| money words: spend, charge, invoice, payment, purchase, subscribe | money |
| credentials: secret, credential, API key, private key, access token | credentials |
| a blocked auto-mode classifier label | only the operator authorizes |

Asking a small model whether `git push --force` is safe is a way of losing the argument, so
these are asserted with the model stubbed to raise.

**Lane 3:** Laya scores it against `AFFIRM_DOCTRINE`, which says what safe means (reversible or
trivial; no money, credentials, production surface, client-visible change, or deletion of
anything not reproduced elsewhere) and says plainly that being asked to affirm is not evidence
that the action is safe, and neither is the session describing its own action as routine.

An unanswered question is not consent: if the model is unavailable, the risk is 1.0.

## The situation each decision is given

A decision made on one line of event text is a guess. `situation()` assembles what the model
actually needs, and names what it could not find rather than dropping it silently:

- who the session is: registry name, profile, working directory
- the operator's **standing ruling** for that session, read from its overwatch focus file
- the computed state, and whether work is in flight (armed watches, pending dispatches)
- what the session last said, and the most recent instruction that actually reached it
- how many decisions have already been made about this session in the last 24 hours
- any extra context the caller passes, and the event under decision

It is capped by `LAYA_GATE_SITUATION_CHARS` (4000), and every decision records the size of the
situation it was made on (`situation_chars`), so a decision made on a thin situation is visible
in the review instead of being invisible.

## Calibration

Enforcement is per question, and only a question with a calibration run behind it acts:

| Question | Threshold | Enforced |
| --- | --- | --- |
| escalate | 0.80 | no: zero model-facing rows in the first 36h window |
| respond | 0.55, calibrated 2026-10-01 | yes |
| affirm | risk < 0.20 | no: no data, and affirming cannot be undone |

The live read, and the offline one:

```bash
python3 laya_gate.py --review 1        # escalations judged by hindsight; the other two by split
python3 laya_gate.py --label <id> good|bad [note]
python3 laya_gate.py --selftest        # every lane, including the model answers
python3 laya_gate.py --respond <session> --state IDLE --no-log
python3 laya_gate.py --affirm <session> "delete the merged feature branch" --no-log
curl -s localhost:11436/health         # thresholds and which of them are enforced
```

```bash
python3 evals/build_laya_evals.py --hours 36   # label a window out of the fleet's own logs
python3 evals/score_laya.py                    # score the model, against the majority baseline
python3 evals/calibrate_laya.py                # the cost-minimising threshold, and its cost
```

The threshold is picked to minimise **expected cost**, using the costs the checkpoint was trained
under (a wrong act 3.0, an unnecessary act 0.5), not to maximise accuracy. On a set where most
wakes need no act, accuracy is maximised by never acting, which is the one behaviour the fleet
cannot have.

### What the first run over 36 hours found

- 143 rows labelled, 217 events considered, the rest dropped with a reason (a receipt followed by
  quiet is a finished session; an event held while a session with nothing in flight goes quiet is
  a suspect, not an error; a wake on a WORKING session is settled by policy and cannot be labelled
  by behaviour).
- The escalation question had **zero** rows that reached the model: the structural lanes covered
  every event in the window. Its threshold is therefore unchanged and still uncalibrated, which is
  a fact about the window rather than about the model.
- The response question reached the model on 60 rows and agreed with the behaviour-derived label
  on 50 of them, but 59 of the 60 answers sit below the 0.50 threshold, so the standing policy
  answers nearly every wake and the model is effectively unused. Moving the threshold to 0.55 cut
  the cost on the labelled set from 88.0 to 10.0.
- The affirmation question had no data at all.

Provisional: 0.55 rests on 60 rows that reached the model, with labels skewed toward acting. The
direction is sound because the cost model is asymmetric, the exact number is not, and it moves as
the window grows.

### Two honest caveats

1. The checkpoint warns that some answer buckets ship temperatures outside the range that keeps
   confidence meaningful, so treat lane-3 confidence as uncalibrated. That is why the escalation
   and affirmation thresholds are logged rather than enforced.
2. With `LAYA_GATE_AFFIRM_RISK=0.20`, the gate refuses most affirmations, including plainly safe
   ones. Deliberate for an uncalibrated threshold: an unnecessary question costs one round trip,
   and an affirmed destructive action cannot be taken back.
