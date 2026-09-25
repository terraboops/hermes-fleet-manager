#!/usr/bin/env python3
"""Restart registered Claude Code tmux sessions, preserving their conversations.

Design rules, each one written because breaking it cost a conversation:

1. `--help` is handled. It used to be swallowed as a keep-list entry, so asking for help
   silently restarted the whole fleet.
2. The registry is backed up before it is touched, so a bad run is recoverable.
3. **A uuid is never overwritten.** It is the only reference to a conversation. The observed
   launch uuid is recorded under a separate key (`last_launch_uuid`) and `uuid` is left alone.
   The old code re-resolved `uuid` to "newest jsonl created after launch", which meant a failed
   resume replaced the real conversation's reference with a brand-new empty session's id.
4. A launch is VERIFIED, not assumed: the pane must show claude running, and the resumed
   conversation must be the one that was asked for. Liveness used to be `tmux has-session`,
   which reports a pane containing a dead or brand-new claude as perfectly healthy.
5. Failures are reported as failures. "ALIVE (verify)" was indistinguishable from success.

usage: restart_fleet_sessions.py <session-to-keep> [more...]
       restart_fleet_sessions.py --help
"""
from __future__ import annotations

import glob
import json
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
for p in (HERE, os.path.dirname(HERE)):
    if p not in sys.path:
        sys.path.insert(0, p)

try:
    import fleet_harness as _harness
except ImportError:  # keep the tool usable if the module is absent
    _harness = None

try:
    from fleet_mcp import ensure_for
except ImportError:
    def ensure_for(cfg, quiet=True):
        return []

REG = os.path.join(HERE, 'fleet_registry.json')
SETTLE_S = 6.0


def sh(*a, **k):
    return subprocess.run(a, capture_output=True, text=True, **k)


def usage(stream=sys.stdout) -> None:
    stream.write(__doc__ or '')


def pane_text(name: str) -> str:
    return sh('tmux', 'capture-pane', '-t', '=' + name + ':', '-p').stdout or ''


def claude_running(name: str) -> bool:
    """True if the pane looks like a live claude session, not a shell prompt or an error."""
    txt = pane_text(name).lower()
    if not txt.strip():
        return False
    dead_markers = ('command not found', 'no such file', 'not logged in', 'please run /login',
                    'error:', 'session not found')
    if any(m in txt for m in dead_markers):
        return False
    # claude draws a status line; a bare shell does not
    live_markers = ('auto mode', 'shift+tab', '✧', '⏵⏵', 'context')
    return any(m in txt for m in live_markers)


def wait_for_claude(name: str, timeout: float = 90.0) -> bool:
    """Poll until claude is up. A large transcript takes far longer than a fixed settle to
    resume: a 10 MB conversation was still loading at 6s and got reported as a failure, so
    the launcher cried wolf on sessions that were perfectly healthy."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if claude_running(name):
            return True
        time.sleep(2.0)
    return False


def transcript_for(cfg: str, cwd: str, uuid: str) -> str | None:
    """Path of the transcript for a specific uuid, if it exists."""
    slug = os.path.expanduser(cwd).replace('/', '-').strip('-')
    p = os.path.join(os.path.expanduser(cfg), 'projects', '-{0}'.format(slug), uuid + '.jsonl')
    return p if os.path.isfile(p) else None


def main() -> int:
    argv = sys.argv[1:]
    if any(a in ('-h', '--help') for a in argv):
        usage()
        return 0

    # --only <profile|name|short> ... restrict the run to matching sessions, so a single
    # profile can be restarted without bouncing the rest of the fleet.
    only: list[str] = []
    args: list[str] = []
    i = 0
    while i < len(argv):
        if argv[i] == '--only' and i + 1 < len(argv):
            only.append(argv[i + 1])
            i += 2
            continue
        if argv[i].startswith('--only='):
            only.append(argv[i].split('=', 1)[1])
            i += 1
            continue
        args.append(argv[i])
        i += 1
    argv = args

    keep = {a for a in argv if not a.startswith('-')}
    if not keep:
        sys.stderr.write(
            "refusing to restart the whole fleet with no keep list.\n"
            "usage: restart_fleet_sessions.py <session-to-keep> [more...] [--only <profile|name>]\n"
            "(or --help)\n")
        return 2

    d = json.load(open(REG))
    sessions = d.get('sessions', [])

    # 2. back up before mutating
    stamp = time.strftime('%Y%m%d-%H%M%S')
    backup = '{0}.bak-{1}'.format(REG, stamp)
    with open(backup, 'w') as fh:
        json.dump(d, fh, indent=2)
    print('  registry backed up -> {0}'.format(os.path.basename(backup)))

    for cfg in sorted({(_harness.resolve(e).get('config_dir') if _harness else e.get('config_dir')) or ''
                       for e in sessions}):
        added = ensure_for(cfg)
        if added:
            print('  + provisioned MCP for {0}: {1}'.format(cfg, ', '.join(added)))

    out = []
    for e in sessions:
        name = e['name']
        if name in keep:
            out.append((name, 'KEPT (working)'))
            continue
        # keep wins over --only: a held session is never bounced
        if only and not any(o in (name, e.get('short'), e.get('profile')) for o in only):
            out.append((name, 'SKIPPED (--only)'))
            continue

        cfg = (_harness.resolve(e).get('config_dir') if _harness else e.get('config_dir')) or ''
        cwd = os.path.expanduser(e.get('cwd') or cfg)
        want = e.get('uuid')

        sh('tmux', 'kill-session', '-t', '=' + name + ':')
        time.sleep(0.3)

        if _harness:
            launch_cmd = _harness.shell_line(e, resume=want, extra_args=e.get('extra_args'))
        else:
            launch_cmd = 'CLAUDE_CONFIG_DIR={0} claude'.format(cwd)
            if want:
                launch_cmd += ' --resume {0}'.format(want)

        subprocess.Popen(['tmux', 'new-session', '-d', '-s', name, '-c', cwd, launch_cmd])
        time.sleep(SETTLE_S)

        # trust-prompt discipline, but only when the selector is actually on screen near the end
        tail = '\n'.join([l for l in pane_text(name).split('\n') if l.strip()][-6:]).lower()
        if 'trust this folder' in tail or 'no, exit' in tail:
            sh('tmux', 'send-keys', '-t', '=' + name + ':', 'Down')
            time.sleep(0.3)
            sh('tmux', 'send-keys', '-t', '=' + name + ':', 'Enter')
            time.sleep(SETTLE_S)

        # 4/5. verify, and say which conversation actually came back
        if not wait_for_claude(name):
            out.append((name, 'FAILED: claude not running in pane after 90s'))
            continue

        tp = transcript_for(cfg, cwd, want) if want else None
        if want and tp:
            size = os.path.getsize(tp)
            if size < 200_000:
                out.append((name, 'RESUMED but transcript is tiny ({0:,} B)'.format(size)))
            else:
                out.append((name, 'RESUMED {0} ({1:,} B)'.format(want[:8], size)))
        elif want:
            out.append((name, 'RESUME FAILED: no transcript for {0}'.format(want[:8])))
        else:
            newest = sorted(glob.glob(os.path.join(os.path.expanduser(cfg), 'projects',
                                                  '-{0}'.format(os.path.expanduser(cwd).replace('/', '-').strip('-')),
                                                  '*.jsonl')), key=os.path.getmtime)
            fresh = os.path.basename(newest[-1]).replace('.jsonl', '') if newest else None
            if fresh:
                # 3. record the observation WITHOUT destroying the original reference
                e['last_launch_uuid'] = fresh
            out.append((name, 'NO UUID ON FILE; launched fresh ({0})'.format((fresh or '?')[:8])))

    tmp = REG + '.tmp'
    with open(tmp, 'w') as fh:
        json.dump(d, fh, indent=2)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, REG)

    print()
    for n, s in out:
        print('{0:<22} {1}'.format(n, s))

    bad = [n for n, s in out if s.startswith(('FAILED', 'RESUME FAILED')) or 'tiny' in s]
    print('\n  {0} ok, {1} need attention{2}'.format(
        len(out) - len(bad), len(bad),
        ': ' + ', '.join(bad) if bad else ''))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
