#!/usr/bin/env python3
"""Fix the botched restart: relaunch each blank session as a RESUME of its prior CLI
session (--resume <old-uuid> + --remote-control) so context + name come back."""
import json, os, subprocess, time, glob

REG = os.path.expanduser('~/.hermes/scripts/cc-watch/fleet_registry.json')
PROFILES = {'personal': os.path.expanduser('~/.claude-example-b'),
            'work': os.path.expanduser('~/.claude-example-a')}

# name -> PRIOR session uuid (from registry backup; colliding/ghost cases pinned).
OLD = {
 'cc-p-example-gggg':  '00000000-0000-4000-8000-000000000105',
 'cc-p-example-ffff':  '00000000-0000-4000-8000-000000000107',
 'cc-p-example-cccc':      '00000000-0000-4000-8000-000000000109',
 'cc-p-example-bbbb':'00000000-0000-4000-8000-000000000110',
 'cc-w-example-8888':       '00000000-0000-4000-8000-000000000103',
 'cc-w-example-7777':        '00000000-0000-4000-8000-000000000102',
 'cc-w-example-6666':  '00000000-0000-4000-8000-000000000104',
 'cc-w-example-4444':'00000000-0000-4000-8000-000000000106',
 'cc-w-example-3333':     '00000000-0000-4000-8000-000000000108',
 'cc-w-example-5555':          '00000000-0000-4000-8000-000000000101',
 'cc-p-example-aaaa':  '00000000-0000-4000-8000-000000000112',
 'cc-p-example-dddd': '00000000-0000-4000-8000-000000000111',
}
SKIP = {'cc-w-example-1111', 'cc-w-example-2222'}  # working / already fixed
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
    else:  # no prior session -> fresh (ghost: original cc-p-example-eeee lost at reboot)
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
