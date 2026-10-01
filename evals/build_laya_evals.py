#!/usr/bin/env python3
"""Build an eval set for the gate's decisions out of what the fleet already recorded.

Nothing here invents a label. Every expected answer comes from one of three sources, and each
row says which:

  hindsight   the gate's own decision, judged by what the session did next. An escalation
              followed by the session carrying on by itself was probably not needed; an event
              held while the session then went quiet for hours probably was.
  behaviour   what the overwatch actually did on a wake, judged the same way. It nudged and the
              session produced output soon after, or it stayed silent and the session kept
              working, or the opposite of either.
  policy      the standing rule for a state or an event shape, which is ground truth by
              definition because it is the policy the model is being asked to apply.

A row without a defensible label is DROPPED and counted, not guessed. The output is one JSONL
row per decision:

  {"id", "question", "session", "at", "state", "situation", "expected", "label_source",
   "provenance"}

Run:  python3 evals/build_laya_evals.py [--hours 36] [--out evals/laya-evals.jsonl]
"""

import argparse
import collections
import glob
import json
import os
import re
import sys
import time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

import laya_gate  # noqa: E402

CRON_OUT = os.path.expanduser("~/.hermes/cron/output")
JOBS = os.path.expanduser("~/.hermes/cron/jobs.json")
DECISIONS = os.path.expanduser("~/.hermes/logs/laya-gate-decisions.jsonl")

# How soon after a wake the session has to produce output for the wake to count as having
# worked, and how long it has to stay silent for the silence to count as a miss. Both are the
# gate's own windows, so the labels agree with the review rather than inventing a second
# standard.
RESUME_WINDOW = laya_gate.RESUME_WINDOW
QUIET_AFTER = laya_gate.QUIET_AFTER

# Receipts and bookkeeping. A session that goes quiet after its DONE token has FINISHED, and
# one that goes quiet after a watch is satisfied has been answered: calling either a miss is the
# exact false alarm the review already learned to exclude. Family-matched, so the label is not
# applied to a genuine stall that merely mentions a token.
RECEIPT_PATTERNS = tuple(pat for pat, _why in laya_gate.LANE2)


def _is_receipt(text):
    return any(re.search(pat, text or "") for pat in RECEIPT_PATTERNS)


def _transcript_times(session):
    path = laya_gate._transcript_for(session)
    if not path:
        return []
    return sorted(t for t in (laya_gate._epoch(x) for x in laya_gate._tail_timestamps(path)) if t)


def _outcome(times, at):
    """What the session did after `at`. The same three verdicts the review uses."""
    after = [t for t in times if t > at]
    if not after:
        return "stayed_quiet" if time.time() - at >= QUIET_AFTER else "too_recent"
    delay = after[0] - at
    if delay <= RESUME_WINDOW:
        return "resumed"
    if delay >= QUIET_AFTER:
        return "stayed_quiet"
    return "unclear"


def _response_of(path):
    """The run's own response: the text after the LAST '## Response'. The files nest the
    previous run's output inside the prompt, so a naive search finds an older response."""
    try:
        text = open(path, errors="replace").read()
    except Exception:
        return None
    i = text.rfind("\n## Response")
    if i < 0:
        return None
    return text[i + len("\n## Response"):].strip()


def _state_of(path):
    try:
        text = open(path, errors="replace").read()
    except Exception:
        return None
    i = text.rfind("### Current output")
    if i < 0:
        return None
    block = text[i:].split("```")
    for part in block:
        for line in part.strip().splitlines():
            line = line.strip()
            if re.match(r"^[A-Z][A-Z-]*\|", line):
                return line.split("|")[0]
    return None


def _overwatch_rows(hours, dropped):
    try:
        jobs = json.load(open(JOBS))["jobs"]
    except Exception:
        return []
    cut = time.time() - hours * 3600
    rows = []
    for job in jobs:
        name = job.get("name") or ""
        if not name.endswith("-overwatch"):
            continue
        mon = job.get("monitor_script") or ""
        m = re.search(r"overwatch/(.+)_state\.py$", mon)
        session = m.group(1) if m else None
        if not session:
            dropped["overwatch: no session in the monitor path"] += 1
            continue
        times = _transcript_times(session)
        for path in glob.glob(os.path.join(CRON_OUT, job["id"], "*")):
            if os.path.getmtime(path) < cut:
                continue
            resp = _response_of(path)
            if resp is None:
                dropped["overwatch: no response section"] += 1
                continue
            at = os.path.getmtime(path)
            state = _state_of(path)
            # A wake that lands on a WORKING or QUEUED session is settled by policy (leave it
            # alone), so a behaviour label cannot say anything about it. Only the states where
            # the response is genuinely open are worth a row.
            if state not in ("IDLE", "NEEDS-INPUT"):
                dropped[f"overwatch: state {state or 'unknown'} is settled by policy"] += 1
                continue
            spoke = not re.fullmatch(r"\[?SILENT\]?\.?", resp.strip(), re.I | re.M)
            outcome = _outcome(times, at)
            if outcome in ("too_recent", "unclear"):
                dropped[f"overwatch: outcome {outcome}"] += 1
                continue
            if spoke and outcome == "resumed":
                expected, src = "nudge it with one concrete next increment", "behaviour"
            elif spoke and outcome == "stayed_quiet":
                # It spoke and the session still went quiet: the wake was not the problem, and
                # nothing in this record says what the right response was. Do not guess.
                dropped["overwatch: spoke and the session still went quiet"] += 1
                continue
            elif not spoke and outcome == "resumed":
                expected, src = "let it continue", "behaviour"
            else:                                   # silent, and the session then went quiet
                expected, src = "nudge it with one concrete next increment", "hindsight"
            rows.append({
                "id": "ow-" + os.path.basename(path).replace(".md", ""),
                "question": "respond",
                "session": session,
                "at": int(at),
                "state": state,
                "situation": laya_gate.situation(session, f"overwatch wake, state {state}"),
                "expected": expected,
                "label_source": src,
                "provenance": {"kind": "overwatch-run", "job": name, "path": path,
                               "spoke": spoke, "outcome": outcome,
                               "response": resp[:300]},
            })
    return rows


def _gate_rows(hours, dropped):
    cut = time.time() - hours * 3600
    rows = []
    for rec in laya_gate.all_decisions():
        if rec.get("at", 0) < cut or rec.get("kind"):
            continue                                    # the other two questions have their own rows
        v = rec.get("verdict") or {}
        session, at = rec.get("session"), rec.get("at", 0)
        lane, rule = v.get("lane"), v.get("rule") or ""
        text = rec.get("text") or ""
        # Lane 1 and lane 2 are ground truth by policy: the model is never asked them, and a
        # row here is a regression check that the structural rule still holds.
        if lane in (1, 2):
            if _is_receipt(text):
                dropped["gate: receipt or bookkeeping, not judgeable by hindsight"] += 1
                continue
            expected = "interrupt her now" if v.get("escalate") else "log it and stay silent"
            rows.append({
                "id": rec.get("id"), "question": "decide", "session": session, "at": int(at),
                "state": None, "situation": laya_gate.situation(session, text),
                "expected": expected, "label_source": "policy",
                "provenance": {"kind": "gate-decision", "lane": lane, "rule": rule,
                               "text": text[:200], "enforced": v.get("enforced")},
            })
            continue
        if _is_receipt(text):
            dropped["gate: receipt or bookkeeping, not judgeable by hindsight"] += 1
            continue
        outcome = _outcome(_transcript_times(session), at)
        if outcome in ("too_recent", "unclear"):
            dropped[f"gate: outcome {outcome}"] += 1
            continue
        if outcome == "stayed_quiet":
            # Held, and the session then went quiet. The review calls this a SUSPECT, not an
            # error: a session with nothing in flight going quiet is the intended silence. It is
            # recorded so it can be looked at, and deliberately NOT turned into a label.
            dropped["gate: held and quiet, a suspect rather than a label"] += 1
            continue
        expected, src = "log it and stay silent", "hindsight"
        rows.append({
            "id": rec.get("id"), "question": "decide", "session": session, "at": int(at),
            "state": None, "situation": laya_gate.situation(session, text),
            "expected": expected, "label_source": src,
            "provenance": {"kind": "gate-decision", "lane": lane, "rule": rule,
                           "text": text[:200], "outcome": outcome},
        })
    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--hours", type=int, default=36)
    ap.add_argument("--out", default=os.path.join(REPO, "evals", "laya-evals.jsonl"))
    args = ap.parse_args()

    dropped = collections.Counter()
    rows = _overwatch_rows(args.hours, dropped) + _gate_rows(args.hours, dropped)
    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    with open(args.out, "w") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")

    by_q = collections.Counter(r["question"] for r in rows)
    by_src = collections.Counter(r["label_source"] for r in rows)
    by_exp = collections.Counter((r["question"], r["expected"]) for r in rows)
    print(f"wrote {len(rows)} rows to {args.out} (window {args.hours}h)")
    print("  by question:", dict(by_q))
    print("  by label source:", dict(by_src))
    print("  by expected answer:")
    for (q, e), n in sorted(by_exp.items()):
        print(f"    {q:8} {n:4}  {e}")
    print("  dropped (no defensible label):")
    for why, n in dropped.most_common():
        print(f"    {n:4}  {why}")


if __name__ == "__main__":
    main()
