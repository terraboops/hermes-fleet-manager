#!/usr/bin/env python3
"""Rebuild the frozen v1 Laya parity fixtures from the gate decision log.

v1 is FROZEN: the log is append-only, so the build is pinned to a cutoff timestamp and
produces the same bytes every run. Any change to selection, tags or fields is a new
version directory (v2), never an edit to v1.

Usage: scripts/build_laya_parity_fixtures.py [--source PATH] [--out DIR] [--check]
"""
import argparse
import hashlib
import json
import os
import sys
from datetime import datetime, timezone

VERSION = "v1"
THRESHOLD = 0.59
BAND = 0.05
CUTOFF_AT = 1792240649          # last scored row in the log when v1 was cut
CREATED = "2026-10-21"
DEFAULT_SOURCE = "~/.hermes/logs/laya-gate-decisions.jsonl"
DEFAULT_OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..",
                           "tests", "fixtures", "laya_parity", VERSION)
RULE = "score >= 0.59 -> passes the gate (mlx_decision true = escalate = interrupt)"
PRIVACY = ("Kept verbatim and committed. The scored rows carry only a fixed STALL template, a "
           "fleet session id and an elapsed-seconds count: no operator free text. Session ids "
           "already appear in tracked files. The unrelated 'respond' rows (operator rulings, "
           "transcript fragments) are excluded entirely.")
SAMPLING = ("No sampling: every lane-3 'laya' classify row with at <= %d is kept. Rows with an "
            "identical (text, score, prior_24h, state_chars) are collapsed; no seed needed." % CUTOFF_AT)


def build(source):
    rows = [json.loads(l) for l in open(os.path.expanduser(source)) if l.strip()]
    scored = [r for r in rows if r.get("verdict", {}).get("rule") == "laya"
              and isinstance(r["verdict"].get("confidence"), (int, float)) and r["at"] <= CUTOFF_AT]
    scored.sort(key=lambda r: (r["at"], r["id"]))
    seen, out, dropped = set(), [], 0
    for r in scored:
        v = r["verdict"]
        key = (r["text"], v["confidence"], v.get("prior_24h"), v.get("state_chars"))
        if key in seen:
            dropped += 1
            continue
        seen.add(key)
        s = float(v["confidence"])
        dec = s >= THRESHOLD
        tags = ["high" if dec else "low"]
        if abs(s - THRESHOLD) <= BAND + 1e-9:
            tags.append("near_threshold")
        if bool(v.get("escalate")) != dec:
            tags.append("inconsistent")
        tags += ["prior_24h:%s" % v.get("prior_24h"), "state_chars:%s" % v.get("state_chars")]
        fid = hashlib.sha256(("%s|%s|%s" % (r["id"], r["at"], r["text"])).encode()).hexdigest()[:16]
        ts = datetime.fromtimestamp(r["at"], timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        out.append({"id": fid, "text": r["text"], "mlx_score": s, "mlx_decision": dec,
                    "source_ts": ts, "tags": tags})
    body = "".join(json.dumps(o, ensure_ascii=False, sort_keys=True) + "\n" for o in out)
    tc = {}
    for o in out:
        for t in o["tags"]:
            t = t.split(":")[0] if t.startswith(("prior_24h", "state_chars")) else t
            tc[t] = tc.get(t, 0) + 1
    manifest = {
        "version": VERSION, "source_path": DEFAULT_SOURCE, "source_row_count": len(rows),
        "fixture_count": len(out),
        "sha256_of_fixtures": hashlib.sha256(body.encode()).hexdigest(),
        "threshold": THRESHOLD, "decision_rule": RULE, "sampling": SAMPLING,
        "privacy": PRIVACY, "created": CREATED,
        "scored_rows_in_cutoff": len(scored), "duplicates_removed": dropped,
        "cutoff_at": CUTOFF_AT, "tag_counts": dict(sorted(tc.items())),
    }
    return body, manifest


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", default=DEFAULT_SOURCE)
    ap.add_argument("--out", default=DEFAULT_OUT)
    ap.add_argument("--check", action="store_true", help="verify the committed files, write nothing")
    a = ap.parse_args()
    body, manifest = build(a.source)
    out = os.path.abspath(a.out)
    mtxt = json.dumps(manifest, indent=2, sort_keys=True) + "\n"
    if a.check:
        ok = (open(os.path.join(out, "fixtures.jsonl")).read() == body
              and json.load(open(os.path.join(out, "manifest.json")))["sha256_of_fixtures"]
              == manifest["sha256_of_fixtures"])
        print("match" if ok else "MISMATCH", manifest["sha256_of_fixtures"])
        return 0 if ok else 1
    os.makedirs(out, exist_ok=True)
    open(os.path.join(out, "fixtures.jsonl"), "w").write(body)
    open(os.path.join(out, "manifest.json"), "w").write(mtxt)
    print(manifest["fixture_count"], manifest["sha256_of_fixtures"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
