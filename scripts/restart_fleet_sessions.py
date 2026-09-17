#!/usr/bin/env python3
"""Restart all registered Claude Code tmux sessions with --remote-control, preserving
session names, re-resolving each session's new uuid, and verifying liveness.
Skips any name in KEEP (e.g. an actively-working the example session)."""
import json, os, subprocess, time, glob, sys

REG = os.path.expanduser('~/.hermes/scripts/cc-watch/fleet_registry.json')
PROFILES = {'personal': os.path.expanduser('~/.claude-example-b'),
            'work': os.path.expanduser('~/.claude-example-a')}
KEEP = set(sys.argv[1:]) or {'cc-w-example-1111'}

def sh(*a, **k): return subprocess.run(a, capture_output=True, text=True, **k)

d = json.load(open(REG))
launch_start = time.time()
out = []
for e in d['sessions']:
    name = e['name']
    if name in KEEP:
        out.append((name, 'KEPT (working)'))
        continue
    cfg = PROFILES.get(e['profile'], e.get('config_dir'))
    cfg = os.path.expanduser(cfg or '')
    cwd = os.path.expanduser(e.get('cwd','')) or cfg
    slug = cwd.replace('/','-').strip('-')
    # kill old
    sh('tmux','kill-session','-t',name)
    time.sleep(0.3)
    # relaunch with --remote-control FLAG (starts RC control server at boot)
    launch_cmd = f'CLAUDE_CONFIG_DIR={cfg} claude --remote-control'
    subprocess.Popen(['tmux','new-session','-d','-s',name,'-c',cwd, launch_cmd])
    time.sleep(5)
    # trust-prompt discipline (selector; default 'No, exit' kills the session)
    pane = (sh('tmux','capture-pane','-t',name,'-p').stdout or '')
    if 'trust this folder' in pane.lower() or 'No, exit' in pane:
        sh('tmux','send-keys','-t',name,'Down'); time.sleep(0.3)
        sh('tmux','send-keys','-t',name,'Enter'); time.sleep(3)
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

json.dump(d, open(REG,'w'), indent=2)

time.sleep(4)
for e in d['sessions']:
    if e['name'] in KEEP: continue
    r = sh('tmux','has-session','-t',e['name'])
    out.append((e['name'], ('ALIVE' if r.returncode==0 else 'DEAD') + ' (verify)'))

for n, s in out: print(f'{n:<20} {s}')
