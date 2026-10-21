#!/usr/bin/env python3
"""Run the Laya parity fixtures through the CPU backend and print drift, load time and peak RSS.

    LAYA_BACKEND=cpu [LAYA_QUANT_LEVEL=none] [LAYA_NUM_THREADS=4] python3 scripts/laya_cpu_parity.py \\
        [--fixtures DIR] [--limit N] [--out report.json] [--proposed-bound]

Exit 0 when the bounds pass, 1 when they fail, 2 on a setup error. Default fixtures are v1-mlxref (MLX
scores on the harness input), the set the drift bound is defined on. Without --proposed-bound no bound
is enforced and the numbers are only reported.
"""
import argparse
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
sys.path.insert(0, ROOT)
import laya_parity as lp  # noqa: E402

REF = os.path.join(ROOT, "tests", "fixtures", "laya_parity", "v1-mlxref")
PROPOSED = {"min_agreement": 0.990, "min_near_agreement": 0.96, "max_delta": 0.005, "max_mean_delta": 0.002}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--fixtures", default=REF)
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--out", default=None)
    ap.add_argument("--proposed-bound", action="store_true")
    args = ap.parse_args(argv)
    if os.environ.get("LAYA_BACKEND", "").strip().lower() != "cpu":
        print("laya_cpu_parity: set LAYA_BACKEND=cpu", file=sys.stderr)
        return lp.EXIT_ERROR
    import laya_backends as lb
    import laya_parity_cpu as cpu
    options = lb.BackendOptions.resolve()
    try:
        report = lp.run_parity("cpu", args.fixtures, batch_size=options.batch_size, limit=args.limit,
                               bounds=PROPOSED if args.proposed_bound else {},
                               fn=cpu.score_rows)
        facts = cpu.run_report()
    except (lp.ParityError, RuntimeError, lb.BackendLoadError) as e:
        print("laya_cpu_parity: error: %s" % e, file=sys.stderr)
        return lp.EXIT_ERROR
    report["cpu_run"] = facts
    print(lp.format_summary(report))
    print("cpu run: " + json.dumps(facts))
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            json.dump(report, fh, indent=2)
    return lp.EXIT_OK if report["bounds"]["passed"] else lp.EXIT_BOUNDS


if __name__ == "__main__":
    sys.exit(main())
