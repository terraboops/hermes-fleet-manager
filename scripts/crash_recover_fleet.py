#!/usr/bin/env python3
"""Crash-recovery relaunch of all managed Claude Code tmux sessions, PRESERVING each session
via --resume <registry-uuid> + --remote-control. the example session first. Verifies liveness after."""
import json, os, subprocess, time

REG = os.path.expanduser('~/.hermes/scripts/cc-watch/fleet_registry.json')
PROFILES = {'personal': os.path.expanduser('~/.claude-example-b'),
            'work': os.path.expanduser('~/.claude-example-a')}
FIRST = 'cc-w-example-1111'   # relaunch the example session first (the operator priority)

def sh(*a): return subprocess.run(a, capture_output=True, text=True)

d = json.load(open(REG))
order = [e for e in d['sessions'] if e['name'] == FIRST] + \
        [e for e in d['sessions'] if e['name'] != FIRST]

for e in order:
    name = e['name']
    uuid = e.get('uuid')
    cfg = os.path.expanduser(PROFILES.get(e['profile'], e.get('config_dir','')))
    cwd = os.path.expanduser(e.get('cwd','')) or cfg
    sh('tmux','kill-session','-t',name)   # no-op if already gone
    time.sleep(0.3)
    if uuid:
        cmd = f'CLAUDE_CONFIG_DIR={cfg} claude --remote-control --resume {uuid}'
    else:
        cmd = f'CLAUDE_CONFIG_DIR={cfg} claude --remote-control'
    subprocess.Popen(['tmux','new-session','-d','-s',name,'-c',cwd, cmd])
    time.sleep(5)
    pane = (sh('tmux','capture-pane','-t',name,'-p').stdout or '')
    if 'trust this folder' in pane.lower() or 'No, exit' in pane:
        sh('tmux','send-keys','-t',name,'Down'); time.sleep(0.3)
        sh('tmux','send-keys','-t',name,'Enter'); time.sleep(3)
    print(f'{name:<20} relaunched (resume={bool(uuid)})')

time.sleep(4)
print('--- liveness ---')
for e in order:
    r = sh('tmux','has-session','-t',e['name'])
    print(f"{e['name']:<20} {'ALIVE' if r.returncode==0 else 'DEAD'}")
