#!/usr/bin/env python3
"""Manage the fleet-watch registry of Claude sessions (explicit, no auto-discovery).

The fleet-watch daemon tails ONLY sessions listed here. Register when you CREATE a
session; unregister when you CLOSE one. `check` verifies each registered session's
transcript exists — run it whenever the user asks for a status update, and re-register
any session whose transcript has gone stale (e.g. resumed to a new uuid).

Usage:
  fleet_reg.py list
  fleet_reg.py register <name> --short <s> --profile <p|w> --cwd <dir> [--uuid <id>]
  fleet_reg.py unregister <name>
  fleet_reg.py check          # prints per-session OK/MISSING + exit 1 if any missing
"""
import argparse, json, os, re, pathlib, sys

REG = os.path.expanduser("~/.hermes/scripts/cc-watch/fleet_registry.json")
CFG = {  # match claude-env oh-my-zsh wrappers
    "personal": "~/.claude-personal",
    "work": "~/.claude-work",
}

def load():
    return json.loads(pathlib.Path(REG).read_text())

def save(d):
    pathlib.Path(REG).write_text(json.dumps(d, indent=2) + "\n")

def slug(cwd):
    return re.sub(r"[^A-Za-z0-9]+", "-", os.path.expanduser(cwd))

def transcript_path(e):
    cfg = os.path.expanduser(e.get("config_dir") or CFG.get(e.get("profile", "work")))
    base = pathlib.Path(cfg) / "projects" / slug(e["cwd"])
    if e.get("uuid"):
        p = base / f"{e['uuid']}.jsonl"
        return str(p) if p.is_file() else None
    cand = sorted(base.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
    return os.fspath(cand[0]) if cand else None

def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list")
    reg = sub.add_parser("register")
    reg.add_argument("name"); reg.add_argument("--short", required=True)
    reg.add_argument("--profile", required=True, choices=list(CFG))
    reg.add_argument("--cwd", required=True); reg.add_argument("--uuid")
    unreg = sub.add_parser("unregister"); unreg.add_argument("name")
    chk = sub.add_parser("check")
    a = ap.parse_args()
    d = load()
    if a.cmd == "list":
        for e in d["sessions"]:
            print(f"{e['name']:22} {e['short']:14} {e['profile']:9} {e['cwd']}")
    elif a.cmd == "register":
        entry = {"name": a.name, "short": a.short, "profile": a.profile,
                 "config_dir": CFG[a.profile], "cwd": a.cwd}
        if a.uuid: entry["uuid"] = a.uuid
        d["sessions"] = [e for e in d["sessions"] if e["name"] != a.name] + [entry]
        save(d); print(f"registered {a.name} -> {transcript_path(entry) or a.name+' (no transcript yet)'}")
    elif a.cmd == "unregister":
        n0 = len(d["sessions"])
        d["sessions"] = [e for e in d["sessions"] if e["name"] != a.name]
        save(d); print(f"unregistered {a.name}" if len(d["sessions"]) < n0 else f"not found: {a.name}")
    elif a.cmd == "check":
        miss = 0
        for e in d["sessions"]:
            p = transcript_path(e)
            if p:
                print(f"OK   {e['short']:14} {p}")
            else:
                miss += 1; print(f"MISS {e['short']:14} no transcript ({e['name']})")
        print(f"{len(d['sessions'])} registered, {miss} missing")
        sys.exit(1 if miss else 0)

if __name__ == "__main__":
    main()
