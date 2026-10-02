#!/usr/bin/env python3
"""Score the ESCALATION decision against the eval set, with the parked rule on and off.

Reports accuracy, the majority-class baseline, the confusion matrix, and the cost model the gate's
own checkpoint ships (missing an act costs 3.0, acting for nothing costs 0.5), because on a set
that is 57% "stay silent" the accuracy number alone rewards a gate that never speaks.

Rows are split by label_source: `observed` rows are real decisions from the log, `extrapolated`
rows are generated. A rule that scores well on generated rows and badly on observed ones has
learned the generator.

The decision path is exercised for real: `laya_gate.classify` is called with the row's event and
situation, and only `_newest_message` is stubbed (the eval carries the message; a live gate reads
it from the transcript). Quiet hours are forced off so the measurement is about the rule rather
than the clock.

Run:  python3 evals/score_escalation.py [--set evals/laya-escalation-evals.jsonl]
"""
import argparse
import collections
import json
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

import laya_gate  # noqa: E402

COST_MISS = 3.0    # expected an act, the gate stayed silent
COST_SPARE = 0.5   # expected silence, the gate acted


def message_of(situation):
    tail = situation.split("What the session last said:")[-1]
    return tail.split("Work in flight:")[0].strip()


def expecting_of(situation):
    return "none recorded" not in situation.split("Work in flight:")[-1]


def run(rows, parked_rule, quiet=False):
    laya_gate.PARKED_RULE = parked_rule
    laya_gate.in_quiet_hours = lambda now=None: quiet
    out = []
    for r in rows:
        msg = message_of(r["situation"])
        laya_gate._recent.clear()
        laya_gate._newest_message = lambda sess, m=msg: m
        v = laya_gate.classify(r["session"], "", r["event"], context=r["situation"],
                               expecting=expecting_of(r["situation"]))
        out.append((r, v))
    return out


def report(title, pairs):
    tp = sum(1 for r, v in pairs if r["expected"] == "interrupt her now" and v["escalate"])
    fn = sum(1 for r, v in pairs if r["expected"] == "interrupt her now" and not v["escalate"])
    fp = sum(1 for r, v in pairs if r["expected"] != "interrupt her now" and v["escalate"])
    tn = sum(1 for r, v in pairs if r["expected"] != "interrupt her now" and not v["escalate"])
    n = len(pairs)
    acc = (tp + tn) / n if n else 0.0
    maj = max(tp + fn, fp + tn) / n if n else 0.0
    cost = fn * COST_MISS + fp * COST_SPARE
    print(f"\n== {title}")
    print(f"   rows {n}   accuracy {acc:.1%}   majority-class baseline {maj:.1%}")
    print(f"   interrupt expected: {tp + fn}  ->  caught {tp}, missed {fn}")
    print(f"   silence expected  : {fp + tn}  ->  correct {tn}, spurious {fp}")
    print(f"   cost (miss 3.0 / spurious 0.5): {cost:.1f}")
    for src in ("observed", "extrapolated"):
        sub = [(r, v) for r, v in pairs if r["label_source"] == src]
        if not sub:
            continue
        s_tp = sum(1 for r, v in sub if r["expected"] == "interrupt her now" and v["escalate"])
        s_fn = sum(1 for r, v in sub if r["expected"] == "interrupt her now" and not v["escalate"])
        s_fp = sum(1 for r, v in sub if r["expected"] != "interrupt her now" and v["escalate"])
        s_tn = sum(1 for r, v in sub if r["expected"] != "interrupt her now" and not v["escalate"])
        print(f"   {src:<13} n={len(sub):<4} caught {s_tp}  missed {s_fn}  spurious {s_fp}  "
              f"correct-silence {s_tn}")
    rules = collections.Counter(v.get("rule") for _, v in pairs)
    print("   top rules: " + ", ".join(f"{r} x{c}" for r, c in rules.most_common(4)))
    return {"acc": acc, "fn": fn, "fp": fp, "cost": cost}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--set", default=os.path.join(REPO, "evals", "laya-escalation-evals.jsonl"))
    a = ap.parse_args()
    rows = [json.loads(l) for l in open(a.set) if l.strip()]
    print(f"eval set: {a.set}  ({len(rows)} rows)")

    off = report("parked rule OFF (the gate as it was)", run(rows, False))
    on = report("parked rule ON", run(rows, True))

    print("\n== delta")
    print(f"   missed  {off['fn']} -> {on['fn']}   (-{off['fn'] - on['fn']})")
    print(f"   spurious {off['fp']} -> {on['fp']}   (+{on['fp'] - off['fp']})")
    print(f"   cost    {off['cost']:.1f} -> {on['cost']:.1f}")


if __name__ == "__main__":
    main()
