#!/usr/bin/env python3
"""fleet_boot - declarative start/restart of the managed session fleet.

A host restart kills tmux, so every session dies while its transcript survives. This tool
makes "come back after a restart" a property of the system instead of a manual runbook:

  * Each registry entry carries `desired`: "up" or "parked". A session is only ever
    (re)started when it is desired-up and not alive. Missing `desired` means parked, so an
    entry nobody declared is never resurrected.
  * `reconcile` is idempotent and safe to run on a timer. It never touches a live session.
  * It starts sessions in priority order, a few at a
    time, verifying each pane shows a live harness and that the resumed transcript exists.
    It never starts a fresh conversation for an entry with no uuid.
  * A session that keeps dying is capped (`restart_cap` per hour) and reported instead of
    looping.
  * Once per boot: each restarted session gets one neutral note that the host restarted, and
    one summary report is sent (restored / never down / still open).

Subcommands (every one is explicit; unknown arguments are an error, never a silent run):

  reconcile [--dry-run] [--no-nudge] [--no-report]
  status
  up <name|short>...          mark desired-up (does not launch; the next reconcile does)
  park <name|short>...        mark parked (does not kill; closing a pane is a separate act)
  init --active-days N [--apply]   set desired from transcript activity (dry-run unless --apply)
  unfail <name|short>...      clear the crash-loop / failure mark

State: fleet_boot_state.json beside the registry. Config: optional `boot` object in the fleet
config: {priority: [names], max_parallel: 3, restart_cap: 3,
report: true}.
"""
from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import shlex
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
for _p in (HERE, os.path.dirname(HERE)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import fleet_harness as _harness  # noqa: E402

try:
    import fleet_transcript  # noqa: E402
except ImportError:  # keep the tool usable without it
    fleet_transcript = None

try:
    from fleet_mcp import ensure_for  # noqa: E402
except ImportError:
    def ensure_for(cfg, quiet=True):
        return []

STATE_DIR = os.path.expanduser('~/.hermes/scripts/cc-watch')
if not os.path.isdir(STATE_DIR):
    STATE_DIR = HERE
REG = os.path.join(STATE_DIR, 'fleet_registry.json')
STATE = os.path.join(STATE_DIR, 'fleet_boot_state.json')
LOCK = os.path.join(STATE_DIR, 'fleet_boot.lock')
LOG = os.path.expanduser('~/.hermes/logs/fleet-boot.log')
DISPATCH = os.path.join(STATE_DIR, 'fleet_dispatch.sh')
TG_MAX = 3500
SETTLE_DEADLINE_S = 1800

DEFAULTS = {'priority': [], 'max_parallel': 3, 'restart_cap': 3,
            'report': True}


# --------------------------------------------------------------------------- plumbing

def log(msg: str) -> None:
    line = '%s %s' % (time.strftime('%Y-%m-%d %H:%M:%S'), msg)
    print(line, flush=True)
    try:
        os.makedirs(os.path.dirname(LOG), exist_ok=True)
        with open(LOG, 'a') as fh:
            fh.write(line + '\n')
    except OSError:
        pass


def sh(*a, timeout=None):
    try:
        return subprocess.run(a, capture_output=True, text=True, timeout=timeout)
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as e:
        return subprocess.CompletedProcess(a, 124, '', str(e))


def atomic_json(path: str, data) -> None:
    tmp = path + '.tmp'
    with open(tmp, 'w') as fh:
        json.dump(data, fh, indent=2)
        fh.write('\n')
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def load_registry() -> dict:
    try:
        with open(REG) as fh:
            return json.load(fh)
    except FileNotFoundError:
        return {'sessions': []}


def boot_id() -> str:
    out = sh('/usr/sbin/sysctl', '-n', 'kern.boottime').stdout
    m = re.search(r'sec\s*=\s*(\d+)', out or '')
    return m.group(1) if m else 'unknown'


def boot_cfg() -> dict:
    cfg = dict(DEFAULTS)
    cfg.update((_harness.load() or {}).get('boot') or {})
    return cfg


def load_state() -> dict:
    try:
        with open(STATE) as fh:
            return json.load(fh)
    except (FileNotFoundError, ValueError):
        return {}


# ------------------------------------------------------------------- tmux / pane access

def tmux_alive(name: str) -> bool:
    return sh('tmux', 'has-session', '-t', '=' + name + ':').returncode == 0


def pane_text(name: str) -> str:
    return sh('tmux', 'capture-pane', '-t', '=' + name + ':', '-p').stdout or ''


def claude_running(name: str) -> bool:
    """True if the pane looks like a live harness, not a shell prompt or an error."""
    txt = pane_text(name).lower()
    if not txt.strip():
        return False
    if any(m in txt for m in ('command not found', 'no such file', 'not logged in',
                              'please run /login', 'error:', 'session not found')):
        return False
    return any(m in txt for m in ('auto mode', 'shift+tab', '✧', '⏵⏵', 'context'))


def answer_trust_prompt(name: str) -> bool:
    """The folder-trust dialog defaults to the option that EXITS the session, so Down first.
    Only acts when the selector is actually on screen."""
    tail = '\n'.join([l for l in pane_text(name).split('\n') if l.strip()][-6:]).lower()
    if 'trust this folder' in tail or 'no, exit' in tail:
        sh('tmux', 'send-keys', '-t', '=' + name + ':', 'Down')
        time.sleep(0.3)
        sh('tmux', 'send-keys', '-t', '=' + name + ':', 'Enter')
        return True
    return False


def transcript_for(e: dict):
    uuid = e.get('uuid')
    if not uuid:
        return None
    cfg = _harness.resolve(e).get('config_dir') or ''
    cwd = os.path.expanduser(e.get('cwd') or cfg)
    if fleet_transcript is not None:
        path, _src = fleet_transcript.resolve({'config_dir': cfg, 'cwd': cwd, 'uuid': uuid}, tmux=None)
        return path if path and os.path.isfile(path) else None
    return None


def launch_cmd(e: dict) -> str:
    """The harness command, run through the user's login shell so a session sees the same
    environment as one started from a terminal (a launchd job starts with almost none)."""
    line = _harness.shell_line(e, resume=e['uuid'], extra_args=e.get('extra_args'))
    return '/bin/zsh -lic %s' % shlex.quote('exec env ' + line)


def launch(e: dict) -> None:
    cwd = os.path.expanduser(e.get('cwd') or '~')
    sh('tmux', 'set-option', '-g', 'remain-on-exit', 'off')
    subprocess.Popen(['tmux', 'new-session', '-d', '-s', e['name'], '-c', cwd, launch_cmd(e)],
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def wait_batch(names: list[str], timeout: float = 120.0) -> dict:
    """Poll a batch concurrently until each pane shows a live harness. Returns name -> bool."""
    ok = {n: False for n in names}
    answered = set()
    deadline = time.time() + timeout
    while time.time() < deadline and not all(ok.values()):
        for n in names:
            if ok[n]:
                continue
            if n not in answered and answer_trust_prompt(n):
                answered.add(n)
            ok[n] = tmux_alive(n) and claude_running(n)
        if not all(ok.values()):
            time.sleep(2.0)
    return ok


# ------------------------------------------------------------------------ desired state

def find(reg: dict, key: str):
    for e in reg['sessions']:
        if key in (e['name'], e.get('short')):
            return e
    return None


def set_desired(keys: list[str], value: str) -> list[str]:
    """Used by the CLI and by the other launch/close tools. Returns what it could not find."""
    reg = load_registry()
    missing = []
    for k in keys:
        e = find(reg, k)
        if e is None:
            missing.append(k)
        else:
            e['desired'] = value
    atomic_json(REG, reg)
    return missing


def quiet(fn, *a, **k):
    """Best-effort hook for callers that must not fail because bookkeeping did."""
    try:
        return fn(*a, **k)
    except Exception:
        return None


# ---------------------------------------------------------------------- dependencies

# ------------------------------------------------------------------------- reconcile

def order(entries: list[dict], priority: list[str]) -> list[dict]:
    def key(e):
        pri = priority.index(e['name']) if e['name'] in priority else len(priority)
        return (pri, e['name'])
    return sorted(entries, key=key)


def reconcile(dry: bool = False, nudge: bool = True, report: bool = True) -> dict:
    cfg = boot_cfg()
    st = load_state()
    bid = boot_id()
    new_boot = st.get('boot_id') != bid
    if new_boot:
        st = {'boot_id': bid, 'first_seen': time.time(),
              'restarts': {}, 'failed': {}, 'restored': [], 'nudged': {}, 'reported': False}
    out = {'boot_id': bid, 'new_boot': new_boot, 'launched': [], 'ok': [], 'failed': {},
           'held': None, 'never_down': [], 'parked': 0}

    # sessions only: services need their own supervisor, not this loop
    reg = load_registry()['sessions']
    want = [e for e in reg if e.get('desired') == 'up']
    out['parked'] = sum(1 for e in reg if e.get('desired') != 'up')
    down = []
    for e in want:
        if tmux_alive(e['name']):
            if e['name'] not in st['restored']:
                out['never_down'].append(e['name'])
        elif e['name'] not in st['failed']:
            down.append(e)

    now = time.time()
    runnable = []
    for e in order(down, cfg['priority']):
        hist = [t for t in st['restarts'].get(e['name'], []) if now - t < 3600]
        st['restarts'][e['name']] = hist
        if not e.get('uuid'):
            st['failed'][e['name']] = 'no uuid on file; refusing to start a fresh conversation'
        elif not transcript_for(e):
            st['failed'][e['name']] = 'transcript for %s not found; resume would start fresh' % e['uuid'][:8]
        elif len(hist) >= cfg['restart_cap']:
            st['failed'][e['name']] = 'died %d times in an hour; not restarting' % len(hist)
        else:
            runnable.append(e)
    for e in want:
        if e['name'] in st['failed'] and not tmux_alive(e['name']):
            out['failed'][e['name']] = st['failed'][e['name']]

    if dry:
        out['would_launch'] = [e['name'] for e in runnable]
        return out

    for cfgdir in sorted({_harness.resolve(e).get('config_dir') or '' for e in runnable}):
        if cfgdir:
            quiet(ensure_for, cfgdir)

    n = max(1, int(cfg['max_parallel']))
    for i in range(0, len(runnable), n):
        batch = runnable[i:i + n]
        for e in batch:
            launch(e)
            st['restarts'].setdefault(e['name'], []).append(time.time())
            out['launched'].append(e['name'])
            time.sleep(1.0)
        res = wait_batch([e['name'] for e in batch])
        for e in batch:
            if res[e['name']]:
                out['ok'].append(e['name'])
                if e['name'] not in st['restored']:
                    st['restored'].append(e['name'])
                log('restored %s' % e['name'])
            else:
                st['failed'][e['name']] = 'harness not showing in the pane after 120s'
                out['failed'][e['name']] = st['failed'][e['name']]
                log('FAILED %s' % e['name'])
        atomic_json(STATE, st)

    # 3. one neutral note per restarted session per boot
    if nudge:
        for name in list(st['restored']):
            if st['nudged'].get(name) == bid or not tmux_alive(name):
                continue
            if send_nudge(name):
                st['nudged'][name] = bid
        atomic_json(STATE, st)

    # 4. one summary per boot, when settled or after the deadline
    pending = [e for e in want if not tmux_alive(e['name']) and e['name'] not in st['failed']]
    settled = not pending and not out['held']
    overdue = time.time() - st.get('first_seen', time.time()) > SETTLE_DEADLINE_S
    if report and cfg['report'] and not st['reported'] and (settled or overdue) and (st['restored'] or st['failed'] or new_boot):
        text = summary(st, want, out)
        if send_telegram(text):
            st['reported'] = True
        else:
            log('summary not delivered; will retry on the next pass')
        _write_full_report(text)
    atomic_json(STATE, st)
    return out


def send_nudge(name: str) -> bool:
    if not os.path.exists(DISPATCH):
        return False
    up = sh('uptime').stdout.strip()
    body = ('The host restarted and this session was relaunched on the same conversation. '
            'Shells and background tasks that were running before are gone. '
            'Re-read the live state of anything you deployed, started or were waiting on '
            'before relying on your notes. (%s)' % up)
    path = os.path.join(STATE_DIR, '.boot_nudge_%s.txt' % name)
    with open(path, 'w') as fh:
        fh.write(body + '\n')
    r = sh('bash', DISPATCH, name, path, '120', timeout=200)
    return 'LANDED' in (r.stdout or '')


def summary(st: dict, want: list[dict], out: dict) -> str:
    short = {e['name']: e.get('short') or e['name'] for e in want}
    restored = [short[n] for n in st['restored'] if n in short]
    never = [short[n] for n in out['never_down'] if n in short]
    lines = ['Host restart handled (boot %s).' % time.strftime('%H:%M', time.localtime(float(st['boot_id']))
                                                              if str(st['boot_id']).isdigit() else time.localtime())]
    lines.append('Restored (%d): %s' % (len(restored), ', '.join(restored) or 'none'))
    lines.append('Never down (%d): %s' % (len(never), ', '.join(never) or 'none'))
    open_items = ['%s: %s' % (short.get(n, n), why) for n, why in st['failed'].items()]
    lines.append('Still open (%d): %s' % (len(open_items), '; '.join(open_items) or 'none'))
    lines.append('Parked, left down on purpose: %d' % out['parked'])
    return '\n'.join(lines)[:TG_MAX]


def _write_full_report(text: str) -> None:
    try:
        with open(os.path.expanduser('~/.hermes/logs/fleet-boot-report.txt'), 'w') as fh:
            fh.write(text + '\n')
    except OSError:
        pass


def _env_value(key: str):
    if os.environ.get(key):
        return os.environ[key]
    try:
        for line in open(os.path.expanduser('~/.hermes/.env')):
            if line.startswith(key + '='):
                return line.split('=', 1)[1].strip().strip('"\'')
    except OSError:
        pass
    return None


def send_telegram(text: str) -> bool:
    import urllib.request
    token, chat = _env_value('TELEGRAM_BOT_TOKEN'), _env_value('TELEGRAM_HOME_CHANNEL')
    if not (token and chat):
        return False
    req = urllib.request.Request('https://api.telegram.org/bot%s/sendMessage' % token,
                                 data=json.dumps({'chat_id': chat, 'text': text}).encode(),
                                 method='POST', headers={'Content-Type': 'application/json'})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return bool(json.load(r).get('ok'))
    except Exception:  # noqa: BLE001
        return False


# ------------------------------------------------------------------------------- CLI

def cmd_init(days: float, apply: bool) -> int:
    reg = load_registry()
    cutoff = time.time() - days * 86400
    rows = []
    for e in reg['sessions']:
        tp = transcript_for(e)
        m = os.path.getmtime(tp) if tp else 0
        e_new = 'up' if m >= cutoff else 'parked'
        rows.append((e['name'], e.get('desired'), e_new, m))
        if apply:
            e['desired'] = e_new
    for n, old, new, m in sorted(rows, key=lambda r: -r[3]):
        age = (time.time() - m) / 3600 if m else float('inf')
        print('%-24s %-7s -> %-7s last transcript write %s' % (n, old or '(unset)', new,
              '%.1fh ago' % age if m else 'never'))
    print('%d up, %d parked%s' % (sum(r[2] == 'up' for r in rows), sum(r[2] == 'parked' for r in rows),
                                  '' if apply else '  (dry-run; pass --apply to write)'))
    if apply:
        atomic_json(REG, reg)
    return 0


def cmd_status() -> int:
    st = load_state()
    reg = load_registry()['sessions']
    print('boot_id %s  first_seen %s  reported %s' % (st.get('boot_id'), st.get('first_seen'), st.get('reported')))
    for e in reg:
        alive = tmux_alive(e['name'])
        flag = (st.get('failed') or {}).get(e['name'])
        print('  %-24s desired=%-7s %s%s' % (e['name'], e.get('desired') or '(unset)',
              'ALIVE' if alive else 'down ', '   FAILED: ' + flag if flag else ''))
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    sub = ap.add_subparsers(dest='cmd', required=True)
    r = sub.add_parser('reconcile')
    r.add_argument('--dry-run', action='store_true')
    r.add_argument('--no-nudge', action='store_true')
    r.add_argument('--no-report', action='store_true')
    sub.add_parser('status')
    for nme in ('up', 'park', 'unfail'):
        p = sub.add_parser(nme)
        p.add_argument('names', nargs='+')
    i = sub.add_parser('init')
    i.add_argument('--active-days', type=float, required=True)
    i.add_argument('--apply', action='store_true')
    a = ap.parse_args(argv)

    if a.cmd == 'status':
        return cmd_status()
    if a.cmd == 'init':
        return cmd_init(a.active_days, a.apply)
    if a.cmd in ('up', 'park'):
        miss = set_desired(a.names, 'up' if a.cmd == 'up' else 'parked')
        for n in a.names:
            print('%s %s' % ('not found:' if n in miss else a.cmd + ':', n))
        return 1 if miss else 0
    if a.cmd == 'unfail':
        st = load_state()
        reg = load_registry()
        for n in a.names:
            e = find(reg, n)
            key = e['name'] if e else n
            st.get('failed', {}).pop(key, None)
            st.get('restarts', {}).pop(key, None)
            print('cleared:', key)
        atomic_json(STATE, st)
        return 0

    fh = open(LOCK, 'w')
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        print('another reconcile is running; nothing to do')
        return 0
    res = reconcile(dry=a.dry_run, nudge=not a.no_nudge, report=not a.no_report)
    log('reconcile: boot=%s new=%s launched=%d ok=%d failed=%d held=%s never_down=%d' % (
        res['boot_id'], res['new_boot'], len(res['launched']), len(res['ok']),
        len(res['failed']), res['held'], len(res['never_down'])))
    if a.dry_run:
        print(json.dumps({k: res[k] for k in ('would_launch', 'failed', 'parked', 'held', 'never_down')
                          if k in res}, indent=1))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
