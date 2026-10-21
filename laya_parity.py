"""Parity harness: run any scoring backend over the Laya parity fixtures and measure drift
against the recorded MLX baseline.

A backend is a plain callable ``score(texts: list[str]) -> list[float]``. Pure stdlib.

    python3 scripts/laya.py parity --backend replay --out report.json
    python3 scripts/laya.py parity --backend mypkg.mod:score --min-agreement 0.99 --max-delta 0.05
"""
import argparse
import hashlib
import importlib
import json
import math
import os
import platform
import re
import socket
import subprocess
import sys
import time
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.realpath(__file__))
DEFAULT_FIXTURE_DIR = os.path.join(ROOT, "tests", "fixtures", "laya_parity", "v1")
NEAR_TAG = "near_threshold"

EXIT_OK, EXIT_BOUNDS, EXIT_ERROR = 0, 1, 2


class ParityError(Exception):
    """Fixture, manifest or backend problem: the run cannot produce a meaningful report."""


# --------------------------------------------------------------------------- fixtures

def load_fixtures(fixture_dir=DEFAULT_FIXTURE_DIR, verify=True):
    """Return (rows, manifest). Verifies the manifest sha256 over the raw fixtures file."""
    mpath = os.path.join(fixture_dir, "manifest.json")
    fpath = os.path.join(fixture_dir, "fixtures.jsonl")
    try:
        with open(mpath, encoding="utf-8") as fh:
            manifest = json.load(fh)
        with open(fpath, "rb") as fh:
            raw = fh.read()
    except (OSError, ValueError) as exc:
        raise ParityError("cannot read fixtures in %s: %s" % (fixture_dir, exc))
    actual = hashlib.sha256(raw).hexdigest()
    if verify and actual != manifest.get("sha256_of_fixtures"):
        raise ParityError("fixtures sha256 mismatch: manifest %s, file %s"
                          % (manifest.get("sha256_of_fixtures"), actual))
    rows = []
    for n, line in enumerate(raw.decode("utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
            row["id"], row["text"], row["mlx_score"], row["mlx_decision"]
        except (ValueError, KeyError) as exc:
            raise ParityError("fixtures line %d is not a valid row: %r" % (n, exc))
        row["tags"] = list(row.get("tags") or [])
        rows.append(row)
    manifest["_actual_sha256"] = actual
    return rows, manifest


def filter_rows(rows, tags=None, limit=None):
    """Keep rows carrying ALL requested tags (exact match, or 'name' matching 'name:value')."""
    if tags:
        def has(row, tag):
            return any(t == tag or t.startswith(tag + ":") for t in row["tags"])
        rows = [r for r in rows if all(has(r, t) for t in tags)]
    if limit is not None:
        rows = rows[:max(0, int(limit))]
    return rows


def decide(score, threshold, rule):
    """Apply the manifest decision rule. 'score > t' is strict; anything else reads as '>='."""
    m = re.search(r"score\s*(>=|>)\s*[0-9.]+", rule or "")
    if m and m.group(1) == ">":
        return score > threshold
    return score >= threshold


# --------------------------------------------------------------------------- backends

def constant_backend(value=0.5):
    value = float(value)
    return lambda texts: [value] * len(texts)


class ReplayBackend:
    """Returns the recorded mlx_score. The log holds the same text with different scores, so
    scores are replayed in fixture order per text; callers must send texts in fixture order."""

    def __init__(self, rows):
        self._queues = {}
        for r in rows:
            self._queues.setdefault(r["text"], []).append(r["mlx_score"])
        self._pos = {}

    def __call__(self, texts):
        out = []
        for t in texts:
            q = self._queues.get(t)
            if not q:
                raise ParityError("replay backend has no recorded score for text %r" % t[:60])
            i = self._pos.get(t, 0)
            out.append(q[i % len(q)])
            self._pos[t] = i + 1
        return out


# name -> factory(rows, arg) -> score callable. A factory gets the filtered rows because
# test backends need them; real backends ignore them.
BUILTINS = {
    "constant": lambda rows, arg: constant_backend(0.5 if arg is None else arg),
    "dummy": lambda rows, arg: constant_backend(0.5 if arg is None else arg),
    "replay": lambda rows, arg: ReplayBackend(rows),
}


def resolve_backend(spec, rows=()):
    """'replay', 'constant:0.3', or an importable 'module:attr' resolving to a score callable
    (or to an object with a .score callable)."""
    spec = spec.strip()
    name, _, arg = spec.partition(":")
    if name in BUILTINS:
        try:
            return BUILTINS[name](list(rows), arg or None)
        except ValueError as exc:
            raise ParityError("bad backend argument in %r: %s" % (spec, exc))
    if ":" not in spec:
        raise ParityError("unknown backend %r (builtins: %s; or use module:attr)"
                          % (spec, ", ".join(sorted(BUILTINS))))
    mod_name, _, attr = spec.partition(":")
    try:
        obj = importlib.import_module(mod_name)
        for part in attr.split("."):
            obj = getattr(obj, part)
    except (ImportError, AttributeError) as exc:
        raise ParityError("cannot import backend %r: %s" % (spec, exc))
    if callable(obj):
        return obj
    if callable(getattr(obj, "score", None)):
        return obj.score
    raise ParityError("backend %r is not callable and has no .score" % spec)


def run_backend(fn, texts, batch_size):
    """Batched calls. Returns (scores, wall_seconds)."""
    if batch_size < 1:
        raise ParityError("batch size must be >= 1")
    scores = []
    t0 = time.perf_counter()
    for i in range(0, len(texts), batch_size):
        chunk = texts[i:i + batch_size]
        try:
            got = fn(chunk)
            got = [float(x) for x in got]
        except ParityError:
            raise
        except Exception as exc:  # backend failures are the harness's input error, not a crash
            raise ParityError("backend failed on batch at %d: %s: %s" % (i, type(exc).__name__, exc))
        if len(got) != len(chunk):
            raise ParityError("backend returned %d scores for %d texts (batch at %d)"
                              % (len(got), len(chunk), i))
        if any(math.isnan(x) for x in got):
            raise ParityError("backend returned NaN (batch at %d)" % i)
        scores.extend(got)
    return scores, time.perf_counter() - t0


# --------------------------------------------------------------------------- metrics

def percentile(values, q):
    """Linear-interpolated percentile (numpy default), q in [0, 100]."""
    if not values:
        return None
    s = sorted(values)
    k = (len(s) - 1) * q / 100.0
    lo, hi = int(math.floor(k)), int(math.ceil(k))
    return s[lo] + (s[hi] - s[lo]) * (k - lo)


def pearson(xs, ys):
    n = len(xs)
    if n < 2:
        return None
    if min(xs) == max(xs) or min(ys) == max(ys):
        return None
    mx, my = sum(xs) / n, sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    syy = sum((y - my) ** 2 for y in ys)
    if sxx == 0 or syy == 0:
        return None
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    return max(-1.0, min(1.0, sxy / math.sqrt(sxx * syy)))


def _ranks(values):
    order = sorted(range(len(values)), key=lambda i: values[i])
    ranks = [0.0] * len(values)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and values[order[j + 1]] == values[order[i]]:
            j += 1
        avg = (i + j) / 2.0 + 1
        for k in range(i, j + 1):
            ranks[order[k]] = avg
        i = j + 1
    return ranks


def spearman(xs, ys):
    return pearson(_ranks(xs), _ranks(ys)) if len(xs) >= 2 else None


def _agreement(pairs):
    """pairs: [(mlx_decision, backend_decision)] -> rate, or None when empty."""
    return sum(1 for a, b in pairs if a == b) / len(pairs) if pairs else None


def best_fit_threshold(backend_scores, mlx_decisions, reference=0.59):
    """Threshold t (decision = score >= t) maximising agreement with mlx_decisions. Candidates:
    every unique backend score, midpoints between neighbours, and a value above the maximum
    (all-false). Ties go to the candidate nearest `reference`, then the lower one."""
    uniq = sorted(set(backend_scores))
    if not uniq:
        return None
    cands = list(uniq)
    cands += [(a + b) / 2.0 for a, b in zip(uniq, uniq[1:])]
    cands.append(math.nextafter(uniq[-1], math.inf))
    # sweep in O(n log n): sort once, count agreement as the cut moves.
    pairs = sorted(zip(backend_scores, mlx_decisions))
    n = len(pairs)
    pos_total = sum(1 for _, d in pairs if d)
    best = None
    import bisect
    keys = [s for s, _ in pairs]
    pref_pos = [0]
    for _, d in pairs:
        pref_pos.append(pref_pos[-1] + (1 if d else 0))
    for t in sorted(set(cands)):
        i = bisect.bisect_left(keys, t)           # rows below t decide False, the rest True
        correct = (i - pref_pos[i]) + (pos_total - pref_pos[i])
        key = (-correct, abs(t - reference), t)
        if best is None or key < best[0]:
            best = (key, t, correct)
    _, t, correct = best
    return {"threshold": t, "agreement": correct / n, "flips_remaining": n - correct}


def compute_metrics(rows, backend_scores, threshold, rule, reference_threshold=None):
    n = len(rows)
    if n == 0:
        raise ParityError("no fixture rows selected")
    mlx = [r["mlx_score"] for r in rows]
    deltas = [b - m for b, m in zip(backend_scores, mlx)]
    absd = [abs(d) for d in deltas]
    bdec = [decide(b, threshold, rule) for b in backend_scores]
    pairs = [(bool(r["mlx_decision"]), d) for r, d in zip(rows, bdec)]

    flips = []
    for r, b, d, bd in zip(rows, backend_scores, deltas, bdec):
        if bool(r["mlx_decision"]) != bd:
            flips.append({"id": r["id"], "mlx_score": r["mlx_score"], "backend_score": b,
                          "delta": d, "mlx_decision": bool(r["mlx_decision"]),
                          "backend_decision": bd})
    near_idx = [i for i, r in enumerate(rows) if NEAR_TAG in r["tags"]]
    near_pairs = [pairs[i] for i in near_idx]
    best = best_fit_threshold(backend_scores, [p[0] for p in pairs],
                              reference=threshold if reference_threshold is None else reference_threshold)
    return {
        "n": n,
        "score_drift": {
            "mean_abs_delta": sum(absd) / n,
            "max_abs_delta": max(absd),
            "p95_abs_delta": percentile(absd, 95),
            "pearson": pearson(backend_scores, mlx),
            "spearman": spearman(backend_scores, mlx),
        },
        "agreement": {
            "threshold": threshold,
            "decision_rule": rule,
            "overall": _agreement(pairs),
            "near_threshold": _agreement(near_pairs),
            "near_threshold_n": len(near_pairs),
        },
        "flips": {
            "count": len(flips),
            "by_direction": {
                "pass_to_hold": sum(1 for f in flips if f["mlx_decision"]),
                "hold_to_pass": sum(1 for f in flips if not f["mlx_decision"]),
            },
            "cases": flips,
        },
        "best_fit": dict(best, decision_rule="score >= threshold"),
    }


# --------------------------------------------------------------------------- bounds

def check_bounds(metrics, min_agreement=None, max_delta=None, max_mean_delta=None,
                 min_near_agreement=None):
    """Return a list of violation strings. A bound on an undefined metric counts as violated."""
    out = []

    def need(name, value, op, limit):
        if value is None:
            out.append("%s is undefined (bound %s %s)" % (name, op, limit))
        elif (op == ">=" and value < limit) or (op == "<=" and value > limit):
            out.append("%s %.6g violates %s %.6g" % (name, value, op, limit))

    if min_agreement is not None:
        need("agreement", metrics["agreement"]["overall"], ">=", min_agreement)
    if min_near_agreement is not None:
        need("near_threshold agreement", metrics["agreement"]["near_threshold"], ">=", min_near_agreement)
    if max_delta is not None:
        need("max_abs_delta", metrics["score_drift"]["max_abs_delta"], "<=", max_delta)
    if max_mean_delta is not None:
        need("mean_abs_delta", metrics["score_drift"]["mean_abs_delta"], "<=", max_mean_delta)
    return out


# --------------------------------------------------------------------------- report

def _git_commit():
    try:
        r = subprocess.run(["git", "-C", ROOT, "rev-parse", "HEAD"], capture_output=True,
                           text=True, timeout=5)
        return r.stdout.strip() if r.returncode == 0 else None
    except (OSError, subprocess.SubprocessError):
        return None


def _package_version():
    try:
        with open(os.path.join(ROOT, "manifest.yaml"), encoding="utf-8") as fh:
            m = re.search(r"^version:\s*(\S+)", fh.read(), re.M)
        return m.group(1) if m else None
    except OSError:
        return None


def build_report(backend_name, manifest, metrics, wall, n, selection, batch_size, bounds, violations):
    return {
        "meta": {
            "backend": backend_name,
            "fixture_version": manifest.get("version"),
            "fixture_sha256": manifest.get("_actual_sha256") or manifest.get("sha256_of_fixtures"),
            "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "host": socket.gethostname(),
            "platform": platform.platform(),
            "python": platform.python_version(),
            "package": "hermes-fleet-manager",
            "package_version": _package_version(),
            "git_commit": _git_commit(),
            "batch_size": batch_size,
            "selection": selection,
        },
        "timing": {"wall_seconds": wall, "texts_per_second": (n / wall) if wall > 0 else None,
                   "n_texts": n},
        "metrics": metrics,
        "bounds": {"requested": {k: v for k, v in bounds.items() if v is not None},
                   "violations": violations, "passed": not violations},
    }


def _f(x, p=4):
    return "n/a" if x is None else ("%.*f" % (p, x))


def format_summary(report, max_flips=10):
    m, meta, tm = report["metrics"], report["meta"], report["timing"]
    d, a, fl, bf = m["score_drift"], m["agreement"], m["flips"], m["best_fit"]
    lines = [
        "laya parity: backend=%s fixtures=%s (%d rows) sha=%s" % (
            meta["backend"], meta["fixture_version"], m["n"], (meta["fixture_sha256"] or "")[:12]),
        "drift:     mean|d|=%s  max|d|=%s  p95|d|=%s  pearson=%s  spearman=%s" % (
            _f(d["mean_abs_delta"]), _f(d["max_abs_delta"]), _f(d["p95_abs_delta"]),
            _f(d["pearson"]), _f(d["spearman"])),
        "agreement: %s at %s overall; %s on near_threshold (%d rows)" % (
            _f(a["overall"]), a["threshold"], _f(a["near_threshold"]), a["near_threshold_n"]),
        "flips:     %d (pass->hold %d, hold->pass %d)" % (
            fl["count"], fl["by_direction"]["pass_to_hold"], fl["by_direction"]["hold_to_pass"]),
        "best fit:  threshold=%s agreement=%s flips remaining=%d" % (
            _f(bf["threshold"], 6), _f(bf["agreement"]), bf["flips_remaining"]),
        "timing:    %.3fs, %s texts/s" % (tm["wall_seconds"], _f(tm["texts_per_second"], 0)),
    ]
    for f in fl["cases"][:max_flips]:
        lines.append("  flip %s mlx=%.4f backend=%.4f delta=%+.4f %s->%s" % (
            f["id"], f["mlx_score"], f["backend_score"], f["delta"],
            "pass" if f["mlx_decision"] else "hold", "pass" if f["backend_decision"] else "hold"))
    if fl["count"] > max_flips:
        lines.append("  ... %d more in the JSON report" % (fl["count"] - max_flips))
    b = report["bounds"]
    if b["requested"]:
        lines.append("bounds:    " + ("PASS" if b["passed"] else "FAIL: " + "; ".join(b["violations"])))
    return "\n".join(lines)


# --------------------------------------------------------------------------- run / CLI

def run_parity(backend, fixture_dir=DEFAULT_FIXTURE_DIR, batch_size=32, limit=None, tags=None,
               bounds=None, verify=True, fn=None):
    """Library entry point. `backend` is a spec string, or pass `fn` (a callable) with `backend`
    as its display name. Returns the report dict."""
    rows, manifest = load_fixtures(fixture_dir, verify=verify)
    rows = filter_rows(rows, tags=tags, limit=limit)
    if not rows:
        raise ParityError("no fixture rows selected (tags=%r limit=%r)" % (tags, limit))
    if fn is None:
        fn = resolve_backend(backend, rows)
    scores, wall = run_backend(fn, [r["text"] for r in rows], batch_size)
    threshold = float(manifest.get("threshold", 0.59))
    rule = manifest.get("decision_rule", "score >= %s" % threshold)
    metrics = compute_metrics(rows, scores, threshold, rule)
    bounds = bounds or {}
    violations = check_bounds(metrics, **bounds)
    selection = {"tags": list(tags or []), "limit": limit, "rows": len(rows)}
    return build_report(backend, manifest, metrics, wall, len(rows), selection, batch_size,
                        bounds, violations)


def build_parser():
    p = argparse.ArgumentParser(prog="laya parity",
                                description="Measure a scoring backend's drift against the recorded MLX baseline.")
    p.add_argument("--backend", required=True,
                   help="replay | constant[:VALUE] | dummy | module:attr (a score(texts)->floats callable)")
    p.add_argument("--fixtures", default=DEFAULT_FIXTURE_DIR, help="fixture directory (default: v1)")
    p.add_argument("--batch-size", type=int, default=32)
    p.add_argument("--limit", type=int, default=None, help="first N rows after tag filtering")
    p.add_argument("--tags", default=None, help="comma-separated; rows must carry ALL of them")
    p.add_argument("--out", default=None, help="write the JSON report here")
    p.add_argument("--summary", default=None, help="write the text summary here instead of stdout")
    p.add_argument("--no-verify", action="store_true", help="skip the manifest sha256 check")
    p.add_argument("--min-agreement", type=float, default=None)
    p.add_argument("--min-near-agreement", type=float, default=None)
    p.add_argument("--max-delta", type=float, default=None, help="bound on max |score delta|")
    p.add_argument("--max-mean-delta", type=float, default=None)
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    tags = [t.strip() for t in args.tags.split(",") if t.strip()] if args.tags else None
    bounds = {"min_agreement": args.min_agreement, "max_delta": args.max_delta,
              "max_mean_delta": args.max_mean_delta, "min_near_agreement": args.min_near_agreement}
    try:
        report = run_parity(args.backend, args.fixtures, args.batch_size, args.limit, tags,
                            bounds, verify=not args.no_verify)
    except ParityError as exc:
        print("laya parity: error: %s" % exc, file=sys.stderr)
        return EXIT_ERROR
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            json.dump(report, fh, indent=2)
            fh.write("\n")
    text = format_summary(report)
    if args.summary:
        with open(args.summary, "w", encoding="utf-8") as fh:
            fh.write(text + "\n")
    else:
        print(text)
    return EXIT_OK if report["bounds"]["passed"] else EXIT_BOUNDS


if __name__ == "__main__":
    sys.exit(main())
