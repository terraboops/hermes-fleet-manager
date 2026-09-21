#!/usr/bin/env python3
"""Restart all registered Claude Code tmux sessions with --remote-control, preserving
session names, re-resolving each session's new uuid, and verifying liveness.
Skips any name in KEEP (e.g. an actively-working the example session)."""
import json, os, subprocess, time, glob, sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
try:
    from fleet_mcp import ensure_for  # provision required MCP servers per profile
except ImportError:                   # keep launcher working if the module is absent
    def ensure_for(cfg, quiet=True):
        return []

REG = os.path.expanduser('~/.hermes/scripts/cc-watch/fleet_registry.json')
# Launch specs live in config (fleet_harness): no profile name, harness or flag is
# fixed in code, so any harness and any env vars/flags can be declared.
# Aliased so this block does not depend on where the file's own imports sit.
import os as _os, sys as _sys
import sys
_d = _os.path.dirname(_os.path.abspath(__file__))
_sys.path.insert(0, _d)
# Its own dir AND the parent: the harness sits beside the top-level plugin scripts, and a
# scripts/ file run in place from the repo would otherwise not find it.
_sys.path.insert(0, _os.path.dirname(_d))
import fleet_harness as _harness
# Refuse to run without an explicit keep list: the default used to be a placeholder, so a
# bare run bounced every session including one mid-work.
KEEP = set(sys.argv[1:])
if not KEEP:
    sys.stderr.write("refusing to restart the whole fleet with no keep list.\n"
                     "usage: restart_fleet_sessions.py <session-to-keep> [more...]\n")
    raise SystemExit(2)

def sh(*a, **k): return subprocess.run(a, capture_output=True, text=True, **k)


def main():
    d = json.load(open(REG))
    launch_start = time.time()

    # Provision the MCP servers each profile needs BEFORE relaunching anything.
    # A missing server is SILENT — the session just can't reach the tool.
    for cfg in sorted({
        _harness.resolve(e).get('config_dir') or ''
        for e in d['sessions']
    }):
        added = ensure_for(cfg)
        if added:
            print(f"  + provisioned MCP for {cfg}: {', '.join(added)}")

    out = []
    for e in d['sessions']:
        name = e['name']
        if name in KEEP:
            out.append((name, 'KEPT (working)'))
            continue
        cfg = _harness.resolve(e).get('config_dir')
        cfg = os.path.expanduser(cfg or '')
        cwd = os.path.expanduser(e.get('cwd','')) or cfg
        slug = cwd.replace('/','-').strip('-')
        # kill old
        sh('tmux','kill-session','-t',"=" + name + ":")
        time.sleep(0.3)
        # relaunch with --remote-control FLAG (starts RC control server at boot)
        # resume= is what preserves context: without it every session returns as a
        # fresh uuid and the conversation is gone.
        launch_cmd = _harness.shell_line(e, resume=e.get('uuid'))
        subprocess.Popen(['tmux','new-session','-d','-s',name,'-c',cwd, launch_cmd])
        time.sleep(5)
        # trust-prompt discipline (selector; default 'No, exit' kills the session)
        pane = (sh('tmux','capture-pane','-t',"=" + name + ":",'-p').stdout or '')
        if 'trust this folder' in pane.lower() or 'No, exit' in pane:
            sh('tmux','send-keys','-t',"=" + name + ":",'Down'); time.sleep(0.3)
            sh('tmux','send-keys','-t',"=" + name + ":",'Enter'); time.sleep(3)
        # resolve the NEW uuid: newest jsonl in this cwd's project dir, created after launch
        base = f'{cfg}/projects/-{slug}'
        cand = sorted(glob.glob(f'{base}/*.jsonl'), key=os.path.getmtime, reverse=True)
        newuuid = None
        for pth in cand:
            if os.path.getmtime(pth) >= launch_start - 2:   # created by this launch
                newuuid = os.path.basename(pth).replace('.jsonl',''); break
        if newuuid:
            e['uuid'] = newuuid
        out.append((name, f'relaunched -> {newuuid or "uuid?"}'))
        time.sleep(1)

    tmp = f"{REG}.tmp"
    with open(tmp, "w") as fh:
        json.dump(d, fh, indent=2)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, REG)

    time.sleep(4)
    for e in d['sessions']:
        if e['name'] in KEEP: continue
        r = sh('tmux','has-session','-t',"=" + e['name'] + ":")
        out.append((e['name'], ('ALIVE' if r.returncode==0 else 'DEAD') + ' (verify)'))

    for n, s in out: print(f'{n:<20} {s}')


if __name__ == '__main__':
    main()
