#!/usr/bin/env python3
"""Fix the botched restart: relaunch each blank session as a RESUME of its prior CLI
session (--resume <old-uuid> + --remote-control) so context + name come back."""
import json, os, subprocess, time, glob

REG = os.path.expanduser('~/.hermes/scripts/cc-watch/fleet_registry.json')
PROFILES = {'personal': os.path.expanduser('~/.claude-personal'),
            'work': os.path.expanduser('~/.claude-work')}

# name -> PRIOR session uuid (from registry backup; colliding/ghost cases pinned).
OLD = {
 'cc-p-agentmob-rev':  '9aa201a6-3437-48c2-aecc-e9a3476517f7',
 'cc-p-akashic-5423':  '54236fec-dba1-4825-bca2-ae01c332245b',
 'cc-p-catbus99':      '48e5b604-8a91-4b01-a15c-d440f9031267',
 'cc-p-npride2026-0e95':'0e95d1db-8ff2-4d12-baf1-e6e474ac1bba',
 'cc-w-bithub2':       'b14190a1-980c-468a-b6e2-8439fc977764',
 'cc-w-bitmap':        'c77bf182-1ae0-4527-8d00-94c26cbe8cd6',
 'cc-w-develop-b0bb':  'b0bba989-c744-473c-8622-ad7f5b22abbd',
 'cc-w-leadership-6398':'6398cb3c-2c94-4781-9610-2da184bb7fbd',
 'cc-w-mine-4990':     '49909de2-286b-4a0f-b876-e53b5a2e7c5e',
 'cc-w-home':          'd4169e55-f21a-497a-9082-52bee5f1709f',
 'cc-p-slopdetector':  '0095a45f-9ac6-4537-b924-20834b47cd15',
 'cc-p-blogresearch2': '0e796a26-e732-4877-b552-d9bb368c8106',
}
SKIP = {'cc-w-wolfgang-1c2b', 'cc-w-bcprod-44ea'}  # working / already fixed
# blogresearch: NO prior session (the original was lost at reboot) -> relink to slopdetector's won't work; leave fresh
#   but pin its uuid to blank-> it was a ghost anyway; just resume it as nothing -> fresh is fine.

def sh(*a): return subprocess.run(a, capture_output=True, text=True)

d = json.load(open(REG))
out = []
for e in d['sessions']:
    name = e['name']
    if name in SKIP:
        out.append((name, 'SKIP'))
        continue
    old = OLD.get(name)
    cfg = os.path.expanduser(PROFILES.get(e['profile'], e.get('config_dir','')))
    cwd = os.path.expanduser(e.get('cwd','')) or cfg
    # kill blank session
    sh('tmux','kill-session','-t',name); time.sleep(0.3)
    if old:
        cmd = f'CLAUDE_CONFIG_DIR={cfg} claude --remote-control --resume {old}'
    else:  # no prior session -> fresh (ghost: original cc-p-blogresearch lost at reboot)
        cmd = f'CLAUDE_CONFIG_DIR={cfg} claude --remote-control'
    subprocess.Popen(['tmux','new-session','-d','-s',name,'-c',cwd, cmd])
    time.sleep(7)
    pane = (sh('tmux','capture-pane','-t',name,'-p').stdout or '')
    if 'trust this folder' in pane.lower() or 'No, exit' in pane:
        sh('tmux','send-keys','-t',name,'Down'); time.sleep(0.3)
        sh('tmux','send-keys','-t',name,'Enter'); time.sleep(3)
    # resolve current uuid
    slug = cwd.replace('/','-').strip('-')
    cand = sorted(glob.glob(f'{cfg}/projects/-{slug}/*.jsonl'), key=os.path.getmtime, reverse=True)
    cur = os.path.basename(cand[0]).replace('.jsonl','') if cand else None
    if old: e['uuid'] = old
    elif cur: e['uuid'] = cur
    out.append((name, f'resume->{old or "fresh"} uuid={old or cur}'))
    time.sleep(1)

json.dump(d, open(REG,'w'), indent=2)
for n, s in out: print(f'{n:<20} {s}')
