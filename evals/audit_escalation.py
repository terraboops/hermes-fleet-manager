#!/usr/bin/env python3
"""Audit the escalations the parked rule gets wrong, row by row.

A score is not an audit. This prints every row the rule answered differently from the label, with
the matched phrase and the event, so the false positives can be judged rather than counted.

Run:  python3 evals/audit_escalation.py [--set evals/laya-escalation-evals.jsonl]
"""
import argparse
import json
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
sys.path.insert(0, os.path.join(REPO, "evals"))

import laya_gate  # noqa: E402
from score_escalation import expecting_of, message_of  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--set", default=os.path.join(REPO, "evals", "laya-escalation-evals.jsonl"))
    a = ap.parse_args()
    rows = [json.loads(l) for l in open(a.set) if l.strip()]

    laya_gate.PARKED_RULE = True
    laya_gate.in_quiet_hours = lambda now=None: False

    wrong = []
    for r in rows:
        msg = message_of(r["situation"])
        laya_gate._recent.clear()
        laya_gate._newest_message = lambda s, m=msg: m
        v = laya_gate.classify(r["session"], "", r["event"], context=r["situation"],
                               expecting=expecting_of(r["situation"]))
        got = "interrupt her now" if v["escalate"] else "log it and stay silent"
        if got != r["expected"]:
            wrong.append((r, v, msg, got))

    print(f"rows answered differently from the label: {len(wrong)}\n")
    for r, v, msg, got in wrong:
        kind = "MISSED" if r["expected"] == "interrupt her now" else "SPURIOUS"
        print(f"{kind:<9} {r['label_source']:<13} {r['id']:<18} lane={v['lane']} "
              f"rule={str(v['rule'])[:52]}")
        print(f"          event: {r['event'][:100]}")
        print(f"          last : {msg[:180]}")
        print()


if __name__ == "__main__":
    main()
