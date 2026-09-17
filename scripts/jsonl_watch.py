#!/usr/bin/env python3
"""cc-watch: tail a Claude Code session transcript JSONL and emit lines matching regexes.

Canonical-output watcher for long/unattended waits (the "JSONL watch" pattern):
- Resolves a session-id to its transcript (searches ~/.claude-{personal,work}/projects, or accept a path).
- Reads only NEW bytes (offset-tracked, handles rotation), extracts the canonical message text
  (Claude's message.content string or block array) from each JSONL event, and tests regexes.
- Watchdog contract: prints NOTHING when nothing matches; prints ONE line per match.
  Use as a background process (watch its stdout), or as the source for a Hermes cron
  monitor_script (its output is byte-hashed: a NEW match = CHANGED = fires the agent).
Default patterns (unless --regex given):  done-  ·  \b(error|failed|exit code)\b  ·  message received
"""
import argparse, json, os, re, sys, time, pathlib

def extract_text(obj):
    m = obj.get("message") or {}
    c = m.get("content")
    if isinstance(c, str):
        return c
    if isinstance(c, list):
        return "\n".join(b.get("text", "") for b in c if isinstance(b, dict))
    return ""

def person_path(session_id, profile):
    base = os.path.expanduser(f"~/.claude-{profile}" if profile else "~/.claude")
    proot = pathlib.Path(base, "projects")
    if not proot.is_dir():
        return None
    for proj in proot.iterdir():
        if proj.is_dir():
            f = proj / (session_id + ".jsonl")
            if f.exists():
                return str(f)
    # prefix fallback: match the most-recently-modified jsonl whose id starts with the given prefix
    cands = []
    for proj in proot.iterdir():
        if not proj.is_dir():
            continue
        for f in proj.glob(session_id + "*.jsonl"):
            if f.is_file():
                cands.append(f)
    if cands:
        return str(sorted(cands, key=lambda p: p.stat().st_mtime, reverse=True)[0])
    return None

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("target", help="session-id OR path to a .jsonl transcript")
    ap.add_argument("--profile", default=None, choices=["personal", "work"], help="needed when target is a session-id")
    ap.add_argument("--regex", action="append", default=[], help="regex(es) to match (repeatable); overrides the defaults")
    ap.add_argument("--case", action="store_true", help="case-sensitive match (default: insensitive)")
    ap.add_argument("--all", action="store_true", help="emit every line that has extractable text (debug)")
    ap.add_argument("--from-start", action="store_true", help="scan from file start (default: tail-only, offset-tracked)")
    ap.add_argument("--interval", type=float, default=1.0, help="poll seconds")
    ap.add_argument("--once", action="store_true", help="scan once then exit")
    a = ap.parse_args()

    if os.path.exists(a.target):
        path, session = a.target, os.path.basename(a.target).replace(".jsonl", "")
    else:
        path = person_path(a.target, a.profile)
        session = a.target
        if not path:
            print(f"ERR: no transcript for session {a.target} under ~/.claude-{a.profile or ''}/projects", file=sys.stderr)
            sys.exit(2)

    pats = [re.compile(p, 0 if a.case else re.I) for p in a.regex]
    if not pats:
        pats = [re.compile(x, re.I) for x in [r"done-", r"\b(error|failed|exit code)\b", r"message received"]]

    size = (0 if a.from_start else os.path.getsize(path))
    while True:
        try:
            cur = os.path.getsize(path)
            if cur < size:
                size = 0                      # rotated -> rescan from new start
            with open(path, "rb") as fh:
                fh.seek(size)
                data = fh.read(cur - size).decode("utf-8", "replace")
                size = cur
            for line in data.splitlines():
                if not line.strip():
                    continue
                try:
                    obj = json.loads(line)
                except Exception:
                    obj = {}
                txt = extract_text(obj) if not a.all else line
                if not txt:
                    continue
                hay = txt if a.case else txt.lower()
                hit = None
                for p in pats:
                    m = p.search(hay)
                    if m:
                        hit = m.group(0)
                        break
                if a.all or hit:
                    print(f"[{session}] {hit or ''} :: {txt[:300].strip()}")
                    sys.stdout.flush()
        except Exception as e:
            print(f"ERR: {e}", file=sys.stderr)
            sys.stdout.flush()
        if a.once:
            break
        time.sleep(a.interval)

if __name__ == "__main__":
    main()
