#!/usr/bin/env python3
"""Crash-recovery relaunch of all managed Claude Code tmux sessions, PRESERVING each session
via --resume <registry-uuid> + --remote-control. The example session first. Verifies liveness after."""
import json, os, subprocess, time

REG = os.path.expanduser('~/.hermes/scripts/cc-watch/fleet_registry.json')
# Launch specs live in config (fleet_harness): no profile name, harness or flag is
# fixed in code, so any harness and any env vars/flags can be declared.
import os as _os, sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))
import fleet_harness as _harness
FIRST = 'cc-w-example-1234'   # relaunch this session first (operator priority)

def sh(*a): return subprocess.run(a, capture_output=True, text=True)

d = json.load(open(REG))
order = [e for e in d['sessions'] if e['name'] == FIRST] + \
        [e for e in d['sessions'] if e['name'] != FIRST]

for e in order:
    name = e['name']
    uuid = e.get('uuid')
    cfg = _harness.resolve(e).get('config_dir')
    cwd = os.path.expanduser(e.get('cwd','')) or cfg
    sh('tmux','kill-session','-t',name)   # no-op if already gone
    time.sleep(0.3)
    if uuid:
        cmd = _harness.shell_line(e, resume=uuid)
    else:
        cmd = _harness.shell_line(e)
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
