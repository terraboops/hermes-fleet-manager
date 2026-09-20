#!/usr/bin/env python3
"""Resume the fleet after an ungraceful host death (power loss / hard crash).

Session set = every Claude Code process registry file present in the profile's
`sessions/<pid>.json` (Claude Code DELETES that file on a clean exit, so a
surviving file == the process was killed by the host going down). Each entry's
`sessionId` + `cwd` + `tmux` target is the ground truth for the resume, so we
never guess with --continue.

Usage: python3 resume_after_powerloss.py [--dry-run]
"""
import json, glob, os, subprocess, sys, time

PROFILE_DIRS = {
    'work': '/Users/yourname/.claude-example-a',
    'personal': '/Users/yourname/.claude-example-b',
}
FIRST = 'cc-w-example-1234'          # operator priority: this session comes back first
DRY = '--dry-run' in sys.argv
HERE = os.path.dirname(os.path.abspath(__file__))
MANIFEST = os.path.join(HERE, 'fleet_powerloss_manifest.json')



def sh(*a):
    return subprocess.run(a, capture_output=True, text=True)


def discover():
    """Union of (a) surviving live process-registry entries and (b) the pinned
    manifest captured at 2026-09-16 21:4x. The registry files are ephemeral: the
    FIRST new claude process of a profile GCs entries whose pid is gone, so the
    manifest is the durable record. Dedupe by session id."""
    out, seen = [], set()
    cands = []
    if os.path.exists(MANIFEST):
        cands += json.load(open(MANIFEST))['sessions']
    for prof, d in PROFILE_DIRS.items():
        for f in glob.glob(f'{d}/sessions/*.json'):
            try:
                j = json.load(open(f))
            except Exception:
                continue
            name = (j.get('tmux') or '').split(':')[0]
            if name.startswith('cc-'):
                cands.append({'name': name, 'profile': prof, 'config_dir': d,
                              'cwd': j.get('cwd'), 'uuid': j.get('sessionId'),
                              'status': j.get('status'), 'last_write': os.path.getmtime(f)})
    for e in cands:
        if e['uuid'] in seen:
            continue
        seen.add(e['uuid'])
        e.setdefault('config_dir', PROFILE_DIRS[e['profile']])
        # integrity: the transcript must exist or --resume silently starts fresh
        import re
        slug = re.sub(r'[^A-Za-z0-9]+', '-', e['cwd'])
        tr = sorted(glob.glob(f"{e['config_dir']}/projects/{slug}/{e['uuid']}.jsonl"))
        e['transcript'] = tr[0] if tr else None
        e['transcript_mtime'] = os.path.getmtime(tr[0]) if tr else 0
        out.append(e)
    out.sort(key=lambda e: (e['name'] != FIRST, e['name']))
    return out


def main():
    sessions = discover()
    print(f'discovered {len(sessions)} sessions that died with the host')
    for e in sessions:
        print(f"  {e['name']:<22} {e['profile']:<8} {e['uuid']}  {e['cwd']}  ({e.get('status') or e.get('last_status')})"
              f"  transcript={'OK' if e['transcript'] else 'MISSING'}")
    if DRY:
        return
    sh('tmux', 'set-option', '-g', 'remain-on-exit', 'on')   # keep a failed pane readable
    for e in sessions:
        sh('tmux', 'kill-session', '-t', e['name'])
        time.sleep(0.2)
        cmd = (f"CLAUDE_CONFIG_DIR={e['config_dir']} CLAUDE_AX_STARTUP_QUIET_MS=0 "
               f"claude --ax-screen-reader --remote-control --resume {e['uuid']}")
        subprocess.Popen(['tmux', 'new-session', '-d', '-s', e['name'], '-c', e['cwd'], cmd],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        print(f"launched {e['name']}")
        time.sleep(6)
    print('--- settle 30s ---')
    time.sleep(30)
    for e in sessions:
        pane = (sh('tmux', 'capture-pane', '-t', e['name'], '-p').stdout or '').strip()
        tail = ' | '.join(pane.splitlines()[-3:])[:220]
        print(f"=== {e['name']}\n    {tail}")


if __name__ == '__main__':
    main()
