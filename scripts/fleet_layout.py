#!/usr/bin/env python3
"""Named fleet LAYOUTS: snapshot the current live Claude-Code session set and
resume the EXACT sessions + names later, by layout name.

Layout = a named snapshot of the fleet registry entries that are currently
ALIVE in tmux: {name, short, profile, config_dir, cwd, uuid}. Saving captures
that set NOW; resuming <layout> recreates each tmux session with the SAME name +
cwd + `claude --remote-control --resume <uuid>` so it reattaches the exact
session (never blind --continue). Closing <layout> kills those tmux sessions
(transcripts persist; the layout can resume them again later).

Usage:
  fleet_layout.py save <name>            # snapshot current ALIVE registry set
  fleet_layout.py list                   # show saved layouts
  fleet_layout.py show <name>            # what's in a layout
  fleet_layout.py resume <name>          # recreate tmux sessions by name+uuid
  fleet_layout.py close <name>           # kill the tmux sessions in the layout
  fleet_layout.py close-all-except <keep_short...>  # close every ALIVE tmux session not in the keep list
"""

import argparse, json, os, subprocess, sys, time, datetime

# Launch specs live in config (fleet_harness): no profile name, harness or flag is
# fixed in code, so any harness and any env vars/flags can be declared.
# Aliased so this block does not depend on where the file's own imports sit.
import os as _os, sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))
import fleet_harness as _harness

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
try:
    from fleet_mcp import ensure_for  # provision required MCP servers per profile
except ImportError:                   # keep launcher working if the module is absent
    def ensure_for(cfg, quiet=True):
        return []
# STATE (registry + layouts) lives in the cc-watch runtime dir, NOT next to this
# script. This file is a symlink into the OSS repo, so resolving state relative to
# __file__ silently looks in the WRONG place when the script is invoked via its
# repo path (repo/scripts/fleet_layouts) instead of the symlink — `list` then
# reports "no layouts yet" even though layouts exist. Keep state anchored.
STATE_DIR = os.path.expanduser("~/.hermes/scripts/cc-watch")
if not os.path.isdir(STATE_DIR):
    STATE_DIR = HERE
REG = os.path.join(STATE_DIR, "fleet_registry.json")
LAYOUTS = os.path.join(STATE_DIR, "fleet_layouts")

def load_registry():
    return json.load(open(REG))["sessions"]

def tmux_alive(name):
    return subprocess.run(["tmux", "has-session", "-t", name], capture_output=True).returncode == 0

def layout_path(name):
    os.makedirs(LAYOUTS, exist_ok=True)
    return os.path.join(LAYOUTS, name + ".json")

def atomic_write(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=1)
        f.flush(); os.fsync(f.fileno())
    os.replace(tmp, path)

def snapshot_alive(reg):
    return [dict(e) for e in reg if tmux_alive(e["name"])]  # copy, don't mutate registry

def save(name):
    reg = load_registry()
    alive = snapshot_alive(reg)
    payload = {
        "layout": name,
        "saved_at": datetime.datetime.now().isoformat(timespec="seconds"),
        "count": len(alive),
        "sessions": alive,
        "_dead_also_registered": [e["short"] for e in reg if not tmux_alive(e["name"])],
    }
    atomic_write(layout_path(name), payload)
    print(f"saved layout '{name}': {len(alive)} sessions")
    for e in alive:
        print(f"  - {e['name']}  [{e['short']}] {e['profile']} @ {e['cwd']}  uuid={e.get('uuid','(none yet)')}")

def load(name):
    p = layout_path(name)
    if not os.path.exists(p):
        print(f"no layout '{name}' (have: {[ os.path.splitext(x)[0] for x in os.listdir(LAYOUTS) ] if os.path.isdir(LAYOUTS) else [] })")
        sys.exit(2)
    return json.load(open(p))

def resume(name):
    lay = load(name)
    # Provision the MCP servers each profile needs BEFORE launching anything.
    # A missing server is SILENT: the session just can't reach the tool and no
    # error surfaces (repro: a session had no MCP server during an
    # end-to-end test pass and had to ask the human for the add commands).
    for cfg in sorted({os.path.expanduser(e["config_dir"]) for e in lay["sessions"]}):
        added = ensure_for(cfg)
        if added:
            print(f"  + provisioned MCP for {cfg}: {', '.join(added)}")
    for e in lay["sessions"]:
        if tmux_alive(e["name"]):
            print(f"  ! {e['name']} already alive — skip")
            continue
        cwd = os.path.expanduser(e["cwd"])
        # infra/new sessions may have no uuid yet -> fresh session in cwd
        if e.get("uuid"):
            cmd = _harness.shell_line(e, resume=e["uuid"])
            r = f"(--resume {e['uuid']}"
        else:
            cmd = _harness.shell_line(e)
            r = "(fresh"
        subprocess.Popen(["tmux", "new-session", "-d", "-s", e["name"], "-c", cwd, cmd])
        print(f"  resumed {e['name']} {r}, cwd={cwd})")
    print(f"resume '{name}' issued. Verify each pane comes back to project context.")
    time.sleep(6)
    for e in lay["sessions"]:
        print(f"  {e['name']}: {'ALIVE' if tmux_alive(e['name']) else 'not-up-yet'}")

def close(name):
    lay = load(name)
    for e in lay["sessions"]:
        if tmux_alive(e["name"]):
            subprocess.run(["tmux", "kill-session", "-t", e["name"]])
            print(f"  closed {e['name']}")
        else:
            print(f"  {e['name']}: already closed")
    print(f"closed layout '{name}' ({lay['count']} sessions). transcripts persist; resume '{name}' to bring them back.")

def close_all_except(keep):
    reg = load_registry()
    kept = set(keep)
    killed = 0
    for e in reg:
        if e["short"] in kept:
            print(f"  KEEP {e['name']} [{e['short']}]")
            continue
        if tmux_alive(e["name"]):
            subprocess.run(["tmux", "kill-session", "-t", e["name"]])
            print(f"  closed {e['name']} [{e['short']}]")
            killed += 1
        else:
            print(f"  {e['name']}: already dead, skip")
    print(f"closed {killed} sessions; kept {sorted(kept)}.")

def list_layouts():
    if not os.path.isdir(LAYOUTS):
        print("no layouts yet"); return
    for f in sorted(os.listdir(LAYOUTS)):
        if f.endswith(".json"):
            d = json.load(open(os.path.join(LAYOUTS, f)))
            print(f"  {d['layout']:<14} {d['count']:>2} sessions  saved {d['saved_at']}")

def show(name):
    lay = load(name)
    print(f"layout '{name}' ({lay['count']} sessions) saved {lay['saved_at']}")
    for e in lay["sessions"]:
        print(f"  - {e['name']} [{e['short']}] {e['profile']} @ {e['cwd']}  uuid={e.get('uuid','(none yet)')}")

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("action", choices=["save","list","show","resume","close","close-all-except"])
    ap.add_argument("arg", nargs="*", help="layout name / keep-shorts for close-all-except")
    a = ap.parse_args()
    if a.action == "save": save(a.arg[0] if a.arg else "default")
    elif a.action == "list": list_layouts()
    elif a.action == "show": show(a.arg[0])
    elif a.action == "resume": resume(a.arg[0])
    elif a.action == "close": close(a.arg[0])
    elif a.action == "close-all-except": close_all_except(a.arg)

if __name__ == "__main__":
    main()
