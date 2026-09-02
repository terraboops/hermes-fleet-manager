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
import argparse, json, os, re, pathlib, sys, subprocess

def _cfg(key, default):
    p = os.environ.get("FLEET_CONFIG")
    if p:
        try:
            return os.path.expanduser(json.load(open(os.path.expanduser(p))).get(key) or default)
        except Exception:
            pass
    return os.path.expanduser(default)

REG = _cfg("registry_file", "~/.hermes/scripts/cc-watch/fleet_registry.json")
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

def _resolve_newest_uuid(e):
    """UUID of the newest session transcript for a cwd (class-fix: recover a session whose
    uuid wasn't captured at register time instead of registering a phantom)."""
    cfg = os.path.expanduser(e.get("config_dir") or CFG.get(e.get("profile", "work")))
    base = pathlib.Path(cfg) / "projects" / slug(e["cwd"])
    cand = sorted(base.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
    return (cand[0].stem if cand else None)

def _probe_live(name):
    """A managed session is ALIVE iff its tmux session exists (tmux IS the pane; when the
    claude proc dies the window closes and the session dies)."""
    try:
        return subprocess.run(["tmux", "has-session", "-t", name],
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0
    except Exception:
        return False


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list")
    reg = sub.add_parser("register")
    reg.add_argument("name"); reg.add_argument("--short", required=True)
    reg.add_argument("--profile", required=True, choices=list(CFG))
    reg.add_argument("--cwd", required=True); reg.add_argument("--uuid")
    reg.add_argument("--force", action="store_true",
                     help="register even if no session transcript resolves (you'll own the unrecoverable risk)")
    unreg = sub.add_parser("unregister"); unreg.add_argument("name")
    chk = sub.add_parser("check")
    chk.add_argument("--live", action="store_true", help="also probe every session's tmux liveness (ALIVE/DEAD)")
    a = ap.parse_args()
    d = load()
    if a.cmd == "list":
        for e in d["sessions"]:
            live = "ALIVE" if _probe_live(e["name"]) else "DEAD"
            print(f"{e['name']:22} {e['short']:14} {e['profile']:9} {e['cwd']}  [{live}]")
    elif a.cmd == "register":
        entry = {"name": a.name, "short": a.short, "profile": a.profile,
                 "config_dir": CFG[a.profile], "cwd": a.cwd}
        if a.uuid:
            entry["uuid"] = a.uuid
        else:
            # class-fix: recover a captured-uuid-not-never; don't register a phantom.
            found = _resolve_newest_uuid(entry)
            if found:
                entry["uuid"] = found
                print(f"[auto-recovered uuid {found} for {a.name}]")
        if not transcript_path(entry) and not a.force:
            print(f"REFUSED: no session jsonl resolves for {a.name} (would be unrecoverable at reboot). Pass --force to register anyway.")
            sys.exit(1)
        d["sessions"] = [e for e in d["sessions"] if e["name"] != a.name] + [entry]
        save(d); print(f"registered {a.name} -> {transcript_path(entry) or a.name+' (NO transcript -- unrecoverable)'}")
    elif a.cmd == "unregister":
        n0 = len(d["sessions"])
        d["sessions"] = [e for e in d["sessions"] if e["name"] != a.name]
        save(d); print(f"unregistered {a.name}" if len(d["sessions"]) < n0 else f"not found: {a.name}")
    elif a.cmd == "check":
        miss = 0
        for e in d["sessions"]:
            p = transcript_path(e)
            lv = "ALIVE" if _probe_live(e["name"]) else "DEAD"
            if p:
                print(f"OK   {e['short']:14} {lv:5} {p}")
            else:
                miss += 1; print(f"MISS {e['short']:14} {lv:5} no transcript ({e['name']})")
        ndead = sum(1 for e in d["sessions"] if not _probe_live(e["name"]))
        print(f"{len(d['sessions'])} registered, {miss} missing transcript, {ndead} not-alive")
        sys.exit(1 if (miss or ndead) else 0)

if __name__ == "__main__":
    main()
