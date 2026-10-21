#!/usr/bin/env python3
"""Cut the MLX-reference fixture set from the v1 fixtures and the MLX baseline report.

Same rows and texts as v1, but `mlx_score` is what the MLX path scores on the harness input
(docs/laya-parity/baseline-mlx-v1.json, rows[].score), not the logged score. The decision and
the near_threshold tag are recomputed from that score. A backend measured against this set is
compared with MLX on identical input, so the missing logged Situation text does not count as drift.

    scripts/build_laya_mlx_reference_fixtures.py [--check]
"""
import argparse
import hashlib
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "tests", "fixtures", "laya_parity", "v1")
OUT = os.path.join(ROOT, "tests", "fixtures", "laya_parity", "v1-mlxref")
BASELINE = os.path.join(ROOT, "docs", "laya-parity", "baseline-mlx-v1.json")
THRESHOLD = 0.59
BAND = 0.05


def build():
    with open(os.path.join(SRC, "manifest.json"), encoding="utf-8") as fh:
        manifest = json.load(fh)
    with open(BASELINE, encoding="utf-8") as fh:
        base = {r["id"]: r["score"] for r in json.load(fh)["rows"]}
    lines, near = [], 0
    with open(os.path.join(SRC, "fixtures.jsonl"), encoding="utf-8") as fh:
        for line in fh:
            if not line.strip():
                continue
            row = json.loads(line)
            score = base[row["id"]]
            tags = [t for t in row["tags"] if t not in ("near_threshold", "high", "low")]
            is_near = abs(score - THRESHOLD) <= BAND
            near += is_near
            tags += ["near_threshold"] if is_near else []
            tags.append("high" if score >= THRESHOLD else "low")
            row.update(mlx_score=score, mlx_decision=score >= THRESHOLD, tags=sorted(tags))
            lines.append(json.dumps(row, sort_keys=True, ensure_ascii=False))
    raw = ("\n".join(lines) + "\n").encode("utf-8")
    manifest.update(version="v1-mlxref", sha256_of_fixtures=hashlib.sha256(raw).hexdigest(),
                    reference="docs/laya-parity/baseline-mlx-v1.json rows[].score",
                    tag_counts=dict(manifest["tag_counts"], near_threshold=near))
    return raw, manifest


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="verify the committed files, write nothing")
    args = ap.parse_args()
    raw, manifest = build()
    mtext = json.dumps(manifest, indent=2, sort_keys=True) + "\n"
    if args.check:
        ok = (open(os.path.join(OUT, "fixtures.jsonl"), "rb").read() == raw
              and open(os.path.join(OUT, "manifest.json"), encoding="utf-8").read() == mtext)
        print("v1-mlxref up to date" if ok else "v1-mlxref DIFFERS")
        return 0 if ok else 1
    os.makedirs(OUT, exist_ok=True)
    open(os.path.join(OUT, "fixtures.jsonl"), "wb").write(raw)
    open(os.path.join(OUT, "manifest.json"), "w", encoding="utf-8").write(mtext)
    print("wrote %s (%d rows, near_threshold %d)" % (OUT, manifest["fixture_count"], manifest["tag_counts"]["near_threshold"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
