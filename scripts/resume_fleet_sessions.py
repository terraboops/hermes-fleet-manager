#!/usr/bin/env python3
"""Fix the botched restart: relaunch each blank session as a RESUME of its prior CLI
session (--resume <old-uuid> + --remote-control) so context + name come back."""
import json, os, subprocess, time, glob

REG = os.path.expanduser('~/.hermes/scripts/cc-watch/fleet_registry.json')
# Launch specs live in config (fleet_harness): no profile name, harness or flag is
# fixed in code, so any harness and any env vars/flags can be declared.
# Aliased so this block does not depend on where the file's own imports sit.
import os as _os, sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))
import fleet_harness as _harness

# name -> PRIOR session uuid (from registry backup; colliding/ghost cases pinned).
OLD = {
 'cc-p-example-a':  '00000000-0000-4000-8000-00000000000a',
 'cc-p-example-b':  '00000000-0000-4000-8000-00000000000b',
 'cc-p-example-c':      '00000000-0000-4000-8000-00000000000c',
 'cc-p-example-d':'00000000-0000-4000-8000-00000000000d',
 'cc-w-example-e':       '00000000-0000-4000-8000-00000000000e',
 'cc-w-example-f':        '00000000-0000-4000-8000-00000000000f',
 'cc-w-example-g':  '00000000-0000-4000-8000-000000000010',
 'cc-w-example-h':'00000000-0000-4000-8000-000000000011',
 'cc-w-example-i':     '00000000-0000-4000-8000-000000000012',
 'cc-w-example-j':          '00000000-0000-4000-8000-000000000013',
 'cc-p-example-k':  '00000000-0000-4000-8000-000000000014',
 'cc-p-example-l': '00000000-0000-4000-8000-000000000015',
}
SKIP = {'cc-w-example-1234', 'cc-w-example-5678'}  # working / already fixed
# blogresearch: NO prior session (the original was lost at reboot) -> relink to slopdetector's won't work; leave fresh
#   but map its uuid to blank-> it was a ghost anyway; just resume it as nothing -> fresh is fine.

def sh(*a): return subprocess.run(a, capture_output=True, text=True)


def main():
    d = json.load(open(REG))
    out = []
    for e in d['sessions']:
        name = e['name']
        if name in SKIP:
            out.append((name, 'SKIP'))
            continue
        old = OLD.get(name)
        cfg = _harness.resolve(e).get('config_dir')
        cwd = os.path.expanduser(e.get('cwd','')) or cfg
        # kill blank session
        sh('tmux','kill-session','-t',"=" + name + ":"); time.sleep(0.3)
        if old:
            cmd = _harness.shell_line(e, resume=old)
        else:  # no prior session -> fresh (ghost: original cc-p-example-m lost at reboot)
            cmd = _harness.shell_line(e)
        subprocess.Popen(['tmux','new-session','-d','-s',name,'-c',cwd, cmd])
        time.sleep(7)
        pane = (sh('tmux','capture-pane','-t',"=" + name + ":",'-p').stdout or '')
        if 'trust this folder' in pane.lower() or 'No, exit' in pane:
            sh('tmux','send-keys','-t',"=" + name + ":",'Down'); time.sleep(0.3)
            sh('tmux','send-keys','-t',"=" + name + ":",'Enter'); time.sleep(3)
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


if __name__ == '__main__':
    main()
