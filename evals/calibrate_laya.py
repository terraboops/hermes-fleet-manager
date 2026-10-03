#!/usr/bin/env python3
"""Calibrate the gate's thresholds against the eval set, using the operator's cost model.

The checkpoint ships its costs in `rl_agent_config.json`: `cost_wrong_act: 3.0` and
`act_costs: {escalate: 0.5}`. Those are the objective the model was trained under, and they were
what this used until 2026-10-03, when the operator was asked directly which error the gate should
prefer and said a swallowed event that mattered is TEN times worse than an unnecessary
interruption. Her number replaces the checkpoint's, because she is the one who reads the
interruptions. So the threshold is chosen to minimise expected cost on the labelled set, not to
look accurate.

Two rules, applied to the rows that actually reach the model (lane 3). Rows the structural lanes
settle never see a threshold, so they are excluded:

  missing an act   expected an act, answered "let it continue"     -> cost 10.0
  acting for nothing  expected "let it continue", answered an act  -> cost 0.5

Run:  python3 evals/calibrate_laya.py
"""

import argparse
import collections
import json
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

import laya_gate  # noqa: E402

COST_WRONG_ACT = 10.0       # the operator's asymmetry (2026-10-03); was the checkpoint's 3.0
COST_ACT_UNNEEDED = 0.5     # ditto (act_costs.escalate)
INACTIVE = "let it continue"


def _load():
    evals = {}
    with open(os.path.join(REPO, "evals", "laya-evals.jsonl")) as f:
        for line in f:
            r = json.loads(line)
            evals[r["id"]] = r
    runs = []
    with open(os.path.join(REPO, "evals", "laya-baseline-runs.jsonl")) as f:
        for line in f:
            r = json.loads(line)
            r["state"] = (evals.get(r["id"]) or {}).get("state")
            runs.append(r)
    return runs


def _decide(probs, threshold):
    if not probs:
        return None
    best = max(probs.items(), key=lambda kv: kv[1])
    return best[0] if best[1] >= threshold else None


def _cost(expected, got, fallback):
    answer = got or fallback
    if answer == expected:
        return 0.0
    if answer == INACTIVE and expected != INACTIVE:
        return COST_WRONG_ACT
    if expected == INACTIVE and answer != INACTIVE:
        return COST_ACT_UNNEEDED
    return COST_WRONG_ACT          # a different act is still a wrong act


def sweep(rows, threshold_grid):
    out = []
    for t in threshold_grid:
        total = 0.0
        right = 0
        for r in rows:
            answer = _decide(r.get("probs"), t)
            fallback = laya_gate._default_response(r.get("state"))
            total += _cost(r["expected"], answer, fallback)
            right += 1 if (answer or fallback) == r["expected"] else 0
        out.append((round(total, 2), t, round(right / len(rows) * 100, 1)))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--grid", default="0.30,0.35,0.40,0.45,0.50,0.55,0.60,0.65,0.70")
    args = ap.parse_args()

    runs = _load()
    by_q = collections.defaultdict(list)
    for r in runs:
        by_q[r["question"]].append(r)

    print("Calibration against the eval set, costed the way the checkpoint was trained")
    print(f"  a missed act costs {COST_WRONG_ACT}, an unnecessary act costs {COST_ACT_UNNEEDED}")
    print()
    for q, sel in sorted(by_q.items()):
        lane3 = [r for r in sel if r.get("lane") == 3]
        print(f"{q}: {len(sel)} rows, {len(lane3)} of them reached the model")
        if not lane3:
            print("  nothing to calibrate: the structural lanes settled every row in this window,")
            print("  so the threshold had no effect on any of them. Collect more before moving it.")
            print()
            continue
        expected = collections.Counter(r["expected"] for r in lane3)
        print("  expected answers:", dict(expected))
        grid = [float(x) for x in args.grid.split(",")]
        table = sweep(lane3, grid)
        print("  threshold   total cost   accuracy")
        for cost, t, acc in sorted(table):
            print(f"    {t:.2f}       {cost:8.2f}   {acc:5.1f}%")
        best_cost, best_t, best_acc = min(table)
        print(f"  -> cost-minimising threshold {best_t:.2f} (cost {best_cost}, accuracy {best_acc}%)")
        current = laya_gate.RESPOND_IF_AT_LEAST
        cur = [x for x in table if abs(x[1] - current) < 1e-9]
        if cur:
            print(f"  -> the current default {current:.2f} costs {cur[0][0]} "
                  f"at {cur[0][2]}% accuracy")
        print(f"  apply with: LAYA_GATE_RESPOND_THRESHOLD={best_t:.2f}")
        print()
    print("Note: the escalation question had no lane-3 rows in this window, so its threshold is")
    print("unchanged and still uncalibrated. That is a fact about the window, not about the model.")


if __name__ == "__main__":
    main()
