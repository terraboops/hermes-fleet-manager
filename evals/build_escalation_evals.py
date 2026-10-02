#!/usr/bin/env python3
"""Build the ESCALATION eval set: should this event be raised to the operator?

The gate's held decisions are the evidence. Every row here comes from the real decision log, or is
an extrapolation along the axes the real rows vary on, so the set tests the rule rather than the
eight sentences that happened to appear.

Positive  = a held event that should have interrupted her. The measured class is a session that
            STOPPED while its own last word was that it waits on a person: "parked with you",
            "waits on a person", "cannot proceed". 58 held events sat on exactly that.
Negative  = a held event that was right to hold: a session that went quiet with nothing waiting,
            finished its work, or is waiting on a machine (a deploy, CI, a build) rather than a
            person. That last one is the trap the rule must not fall into.

Rows carry `label_source` so the set is auditable: `observed` rows are real log entries, and
`extrapolated` rows are generated. A set made only of generated rows measures the generator.

Run:  python3 evals/build_escalation_evals.py
"""
import json
import os
import random

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "laya-escalation-evals.jsonl")
SOURCE = "/tmp/laya_false_negatives.json"
SEED = 20261001

# Event shapes the daemon actually emits, so a generated row is a shape the gate really sees.
EVENTS = {
    "STALL": "STALL-{sess_upper}: no new transcript output for {mins}s (alive, not producing) — check",
    "SENTINEL": ("SENTINEL-MISSED-{sess_upper}: expected {tok} by {dl} — check in on this session "
                 "(re-estimate ETA + re-arm the watch)"),
    "WATCH": "WATCH-EXPIRED {sess} {tok} deadline={dl}",
    "ACK": ("ACK-MISSED-{sess_upper}: dispatch '{marker}' NOT received as a user turn by deadline "
            "— re-deliver (it was swallowed / never submitted)"),
}

# Phrases that mean the session is waiting on a PERSON. Observed in the real transcripts.
BLOCKED = [
    "both items now wait only on a person's approval past their agents' flags",
    "J9 hasn't moved; it's still parked with you",
    "the change is parked with the operator until she decides",
    "this waits on a person, not on the pipeline",
    "the two PRs are parked with you for sign-off",
    "I cannot proceed without your approval on the schema change",
    "the migration is awaiting your decision before it runs",
    "the remaining step needs a person to approve the release",
    "one human-only step left: the approval on the deploy",
    "it is pending your approval, so I have stopped here",
    "blocked on a decision only you can make",
    "for you to decide which tenant gets the demo grant",
]

# Phrases that mean waiting on a MACHINE. The rule must hold these: nothing is owed to the operator
# yet, and interrupting her for a deploy in flight is the false positive this set exists to catch.
NOT_BLOCKED = [
    "the deploy is still running; I'll read the result when it lands",
    "waiting on CI to finish the test job before I push",
    "the two-client walk is still running in the background",
    "the build is queued behind the registry push",
    "no new transcript output while the migration runs",
    "I finished the sweep; nothing is outstanding",
    "all tests pass and the branch is merged; nothing left to do",
    "I have parked the idea in the backlog for later, unrelated to this run",
    "the gate is green and the ring is live, so this task is complete",
]

SESSIONS = [
    "cc-w-wolfgang-1c2b", "cc-p-terratauri-a4d1", "cc-w-bcprod", "cc-w-kploy",
    "cc-w-mine-4990", "cc-p-homeinfra-9f2c", "cc-w-bithub2", "cc-w-develop-b0bb",
    "cc-w-leadership-6398", "cc-p-akashic-5423", "cc-p-catbus99", "cc-p-npride2026-0e95",
    "cc-p-forma-fc3", "cc-p-natra", "cc-p-agentmob-rev",
]


def event_for(kind, sess, rng):
    return EVENTS[kind].format(
        sess=sess, sess_upper=sess.upper(), mins=rng.choice([360, 420, 600, 900, 1200]),
        tok=f"DONE-ow-{rng.randint(100000, 999999)}", dl=rng.randint(1790000000, 1790999999),
        marker=f"DISPATCH-{sess.split('-')[-2]}-{rng.randint(10**12, 10**13)}")


def situation(sess, message, expecting):
    return (f"Session: {sess} (profile work, cwd ~/Developer/{sess.split('-')[-1]}).\n"
            f"The operator's standing ruling for this session:\n"
            f"ROLE: build and ship the authorized work; raise anything you cannot decide.\n"
            f"Computed state: IDLE.\n"
            f"What the session last said:\n{message}\n"
            f"Work in flight: "
            + ("1 armed watch(es) or pending dispatch(es)." if expecting
               else "none recorded."))


def main():
    rng = random.Random(SEED)
    rows = []

    # ---- 1. real observed rows, from the gate's own decision log -------------------------
    observed_pos = observed_neg = 0
    if os.path.exists(SOURCE):
        src = json.load(open(SOURCE))
        seen_episode = set()
        for r in sorted(src, key=lambda x: x["at"]):
            sess, blocked = r["session"], r["blocked"]
            fam = "STALL" if "quiet session" in str(r["rule"]) else (
                "SENTINEL" if "sentinel miss" in str(r["rule"]) else None)
            if fam is None:
                continue
            # One row per (session, family, blocked-state) episode: the daemon repeats a stall
            # every few minutes and 58 raw rows are not 58 incidents.
            episode = (sess, fam, blocked)
            if episode in seen_episode:
                continue
            seen_episode.add(episode)
            rows.append({
                "id": f"obs-{r['at']}-{sess}",
                "question": "escalate",
                "session": sess,
                "at": r["at"],
                "event": event_for(fam, sess, rng),
                "situation": situation(sess, r.get("newest_msg") or "", r["next"] != "stayed_quiet"),
                "expected": "interrupt her now" if blocked else "log it and stay silent",
                "label_source": "observed",
                "provenance": f"decision log; rule={r['rule']}; next={r['next']}",
            })
            observed_pos += blocked
            observed_neg += (not blocked)

    # ---- 2. extrapolated rows: the axes the observed set cannot cover --------------------
    extra = []
    # (a) every blocked phrasing x a stall, so the rule is tested on wording it has not seen
    for i, msg in enumerate(BLOCKED):
        sess = SESSIONS[i % len(SESSIONS)]
        kind = ["STALL", "SENTINEL", "STALL"][i % 3]
        extra.append(("interrupt her now", sess, kind, msg, i % 2 == 0))
    # (b) every machine-waiting phrasing x a stall: the hard negatives
    for i, msg in enumerate(NOT_BLOCKED):
        sess = SESSIONS[(i + 3) % len(SESSIONS)]
        kind = ["STALL", "SENTINEL", "WATCH"][i % 3]
        extra.append(("log it and stay silent", sess, kind, msg, i % 2 == 1))
    # (c) the same blocked message but the session is WORKING with work in flight: still an
    #     interrupt, because the decision is waiting whether or not a watch is armed
    for i in range(10):
        extra.append(("interrupt her now", SESSIONS[i % len(SESSIONS)], "STALL",
                      rng.choice(BLOCKED), True))
    # (d) the same machine message across every STOP shape: shape must not change the answer.
    #     ACK-MISSED is deliberately excluded here -- the gate escalates a swallowed dispatch
    #     structurally and on purpose, so labelling one "stay silent" measures my generator, not
    #     the rule.
    for i, kind in enumerate(["STALL", "SENTINEL", "WATCH"]):
        for j in range(3):
            extra.append(("log it and stay silent", SESSIONS[(i * 3 + j) % len(SESSIONS)], kind,
                          rng.choice(NOT_BLOCKED), j % 2 == 0))
    # (h) a swallowed dispatch: escalated by policy whatever the session last said.
    for i in range(6):
        extra.append(("interrupt her now", SESSIONS[(i + 7) % len(SESSIONS)], "ACK",
                      rng.choice(NOT_BLOCKED + BLOCKED), i % 2 == 0))
    # (e) a completion claim: finished work is not a stop
    for i in range(8):
        extra.append(("log it and stay silent", SESSIONS[i % len(SESSIONS)], "STALL",
                      "DONE-ow-" + str(rng.randint(100000, 999999)) + " — work complete, "
                      "nothing outstanding and nothing waiting on a person.", i % 2 == 0))
    # (f) a question that the session then answered itself: not an operator dependency
    for i in range(6):
        extra.append(("log it and stay silent", SESSIONS[i % len(SESSIONS)], "STALL",
                      "Should I use the tenant table or the view? Answering myself: the view, "
                      "because it already carries the grant. Done, moving on.", False))
    # (g) fill to 100 with randomised combinations of the two message classes
    while len(extra) < 100:
        blocked = rng.random() < 0.5
        msg = rng.choice(BLOCKED if blocked else NOT_BLOCKED)
        extra.append(("interrupt her now" if blocked else "log it and stay silent",
                      rng.choice(SESSIONS), rng.choice(["STALL", "SENTINEL", "WATCH"]), msg, rng.random() < 0.5))

    for i, (expect, sess, kind, msg, expecting) in enumerate(extra, 1):
        rows.append({
            "id": f"gen-{i:03d}",
            "question": "escalate",
            "session": sess,
            "at": 1790900000 + i,
            "event": event_for(kind, sess, rng),
            "situation": situation(sess, msg, expecting),
            "expected": expect,
            "label_source": "extrapolated",
            "provenance": f"axis: kind={kind} expecting={expecting}",
        })

    with open(OUT, "w") as fh:
        for r in rows:
            fh.write(json.dumps(r) + "\n")

    pos = sum(1 for r in rows if r["expected"] == "interrupt her now")
    print(f"wrote {OUT}")
    print(f"  rows: {len(rows)}  ({pos} interrupt, {len(rows) - pos} stay silent)")
    print(f"  observed: {observed_pos + observed_neg} "
          f"({observed_pos} interrupt, {observed_neg} stay silent)")
    print(f"  extrapolated: {len(rows) - (observed_pos + observed_neg)}")


if __name__ == "__main__":
    main()
