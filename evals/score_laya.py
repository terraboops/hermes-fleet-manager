#!/usr/bin/env python3
"""Score the gate's decisions against the eval set built from the logs.

Reports accuracy AND the majority-class baseline, because a decision model that answers the
same way every time can look respectable on a skewed set: 104 of the 117 response rows expect a
nudge, so "always nudge" scores 89% and means nothing.

Also reports the split by lane, because a row the structural rules settle is a regression check
on the code, not a measurement of the model.

Run:  python3 evals/score_laya.py [--set evals/laya-evals.jsonl] [--limit N] [--quiet]
"""

import argparse
import collections
import json
import os
import statistics
import sys
import time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

import laya_gate  # noqa: E402


def score(rows, limit=None, progress=True):
    got = []
    for i, r in enumerate(rows):
        if limit and i >= limit:
            break
        q = r["question"]
        t0 = time.time()
        if q == "decide":
            v = laya_gate.classify(r["session"], "EVAL", r["provenance"].get("text", ""),
                                   r["situation"])
            answer = "interrupt her now" if v.get("escalate") else "log it and stay silent"
        else:
            v = laya_gate.respond(r["session"], r["provenance"].get("response", ""),
                                  r.get("state"), log=False)
            answer = v.get("response")
        got.append({**r, "got": answer, "lane": v.get("lane"),
                    "confidence": v.get("confidence"), "rule": v.get("rule"),
                    "probs": v.get("probabilities"), "ms": int((time.time() - t0) * 1000)})
        if progress and (i + 1) % 25 == 0:
            print(f"  ... {i + 1}/{min(len(rows), limit or len(rows))}", file=sys.stderr)
    return got


def report(rows, out=None):
    by_q = collections.defaultdict(list)
    for r in rows:
        by_q[r["question"]].append(r)
    L = []
    for q, sel in sorted(by_q.items()):
        # Rows labelled by POLICY are structural coverage, not a measurement: the code produced
        # the verdict AND the label, so scoring them would report the rules agreeing with
        # themselves. They are counted, then set aside.
        coverage = [r for r in sel if r.get("label_source") == "policy"]
        sel = [r for r in sel if r.get("label_source") != "policy"]
        L.append(f"{q}: {len(sel)} scoreable rows"
                 + (f" (+{len(coverage)} structural-coverage rows, not scored)" if coverage else ""))
        if not sel:
            L.append("  nothing scoreable: every row in this window was settled by the structural"
                     " lanes, so the model was never asked.")
            L.append("")
            continue
        hits = sum(1 for r in sel if r["got"] == r["expected"])
        maj = collections.Counter(r["expected"] for r in sel).most_common(1)[0]
        lane3 = [r for r in sel if r.get("lane") == 3]
        lane3_hits = sum(1 for r in lane3 if r["got"] == r["expected"])
        confs = [r["confidence"] for r in lane3 if r.get("confidence") is not None]
        L.append(f"  accuracy            {hits}/{len(sel)} = {hits / len(sel) * 100:.1f}%")
        L.append(f"  majority baseline   {maj[1]}/{len(sel)} = {maj[1] / len(sel) * 100:.1f}%"
                 f"   (always '{maj[0]}')")
        if lane3:
            L.append(f"  reached the model   {len(lane3)} rows, accuracy "
                     f"{lane3_hits}/{len(lane3)} = {lane3_hits / len(lane3) * 100:.1f}%")
        if confs:
            below = sum(1 for c in confs if c < laya_gate.RESPOND_IF_AT_LEAST)
            L.append(f"  lane 3 confidence   median {statistics.median(confs):.3f}, "
                     f"min {min(confs):.3f}, max {max(confs):.3f}")
            L.append(f"  below threshold     {below}/{len(confs)} answers discarded in favour of "
                     f"the standing policy at {laya_gate.RESPOND_IF_AT_LEAST}")
        conf = collections.Counter((r["expected"], r["got"]) for r in sel)
        wrong = [(k, n) for k, n in conf.most_common() if k[0] != k[1]]
        L.append("  misses:")
        for (exp, g), n in wrong[:6]:
            L.append(f"    {n:4}  expected '{exp}' -> got '{g}'")
        if not wrong:
            L.append("    none")
        # The two errors that matter, named by what they cost rather than by sign. A false
        # negative is the expensive one: nothing happened, and something needed to.
        ACTS = {"nudge it with one concrete next increment", "challenge its completion claim",
                "re-align it to the authorized work", "answer it and let it continue",
                "escalate to the operator", "interrupt her now"}
        tp = fp = tn = fn = 0
        for r in sel:
            want, did = r["expected"] in ACTS, r["got"] in ACTS
            if want and did:
                tp += 1
            elif want and not did:
                fn += 1
            elif not want and did:
                fp += 1
            else:
                tn += 1
        L.append("  confusion, positive class = an act:")
        L.append(f"    true positive   {tp:4}  acted, and acting was right")
        L.append(f"    FALSE NEGATIVE  {fn:4}  nothing happened, and something was needed")
        L.append(f"    false positive  {fp:4}  acted, and nothing was needed")
        L.append(f"    true negative   {tn:4}  stayed out of the way, correctly")
        if tp + fn:
            L.append(f"    recall on the acts that were needed: {tp}/{tp + fn} = "
                     f"{tp / (tp + fn) * 100:.1f}%")
        if tn + fp:
            L.append(f"    restraint where nothing was needed:  {tn}/{tn + fp} = "
                     f"{tn / (tn + fp) * 100:.1f}%")
        distinct = len({(r["session"], r["expected"], r["got"], r["rule"]) for r in sel})
        if distinct < len(sel):
            L.append(f"    note: {len(sel)} rows collapse onto {distinct} distinct decisions, so a"
                     f" repeated wake of one session is counted more than once above")
        L.append("")
    # Rows dropped for want of a defensible label. They are NOT counted as errors, and they are
    # where a false negative would hide if one existed, so the count is reported rather than
    # quietly dropped.
    side = os.path.join(REPO, "evals", "laya-eval-dropped.json")
    if os.path.exists(side):
        try:
            dropped = json.load(open(side))
        except Exception:
            dropped = {}
        suspects = {k: v for k, v in dropped.items() if "suspect" in k}
        if suspects:
            L.append("Unproven, not counted as errors:")
            for k, v in sorted(suspects.items(), key=lambda kv: -kv[1]):
                L.append(f"  {v:4}  {k}")
            L.append("")
    # the subset that actually reaches a model, all questions together
    all3 = [r for r in rows if r.get("lane") == 3]
    if all3:
        h = sum(1 for r in all3 if r["got"] == r["expected"])
        L.append(f"TOTAL on the rows that reached a model: {h}/{len(all3)} = {h / len(all3) * 100:.1f}%")
    text = "\n".join(L)
    print(text)
    if out:
        with open(out, "w") as f:
            f.write(text + "\n")
    return text


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--set", default=os.path.join(REPO, "evals", "laya-evals.jsonl"))
    ap.add_argument("--limit", type=int)
    ap.add_argument("--out", default=os.path.join(REPO, "evals", "laya-baseline.txt"))
    ap.add_argument("--quiet", action="store_true")
    ap.add_argument("--from-runs", action="store_true",
                    help="re-report from the saved runs instead of calling the model again")
    args = ap.parse_args()

    if args.from_runs:
        scored = [json.loads(l) for l in open(os.path.join(REPO, "evals", "laya-baseline-runs.jsonl"))
                  if l.strip()]
        report(scored, args.out)
        return

    rows = [json.loads(l) for l in open(args.set) if l.strip()]
    if not args.quiet:
        print(f"scoring {len(rows)} rows against {args.set}", file=sys.stderr)
    scored = score(rows, args.limit, progress=not args.quiet)
    with open(os.path.join(REPO, "evals", "laya-baseline-runs.jsonl"), "w") as f:
        for r in scored:
            f.write(json.dumps({k: r[k] for k in ("id", "question", "session", "expected",
                                                  "got", "lane", "confidence", "rule", "probs",
                                                  "ms", "label_source")}) + "\n")
    report(scored, args.out)


if __name__ == "__main__":
    main()
