#!/usr/bin/env python3
"""Move ONE registered Codex session back to Claude Code, keeping its context.

Stages (each writes receipts into --bundle; run from the fleet runtime dir):

  prepare   snapshot source + clone the project tree, launch a read-only Claude
            candidate on a fresh native UUID, wait for the mechanical gate
  cutover   pause the session's jobs, park + close source and candidate, promote the
            candidate under the stable name with the operational launch args, resume
  engage    one post-cutover native tools/context check, then /rc
  finalize  refresh the paused overwatch prompt in place, resume the jobs, read back

Never edits sources it did not park; never marks a session `up` unless it was up
before. Wraps claude_handoff.py and fleet_reg.promote; adds only the glue between them.
"""
import argparse
import datetime
import hashlib
import json
import os
import pathlib
import re
import subprocess
import sys
import time

HERE = pathlib.Path(__file__).resolve().parent
RT = pathlib.Path(os.path.expanduser('~/.hermes/scripts/cc-watch'))
for p in (str(RT), str(HERE)):
    if p not in sys.path:
        sys.path.insert(0, p)
JOBS = pathlib.Path(os.path.expanduser('~/.hermes/cron/jobs.json'))
FOCUS_START = 'ALREADY DECIDED - do NOT re-ask these:\n'
FOCUS_END = '\nEACH RUN:'


def extract_focus(prompt):
    """The standing focus block of a rendered overwatch prompt ('' when absent)."""
    if FOCUS_START not in prompt:
        return ''
    return prompt.split(FOCUS_START, 1)[1].split(FOCUS_END, 1)[0].strip()


def _cmd(args, cwd=None):
    p = subprocess.run(args, cwd=cwd or RT, text=True, capture_output=True)
    if p.returncode:
        raise RuntimeError('%s failed: %s %s' % (args, p.stdout, p.stderr))
    return p.stdout


def _entry(fleet_reg, name):
    rows = [e for e in fleet_reg.load()['sessions'] if e.get('name') == name]
    if len(rows) != 1:
        raise SystemExit('name must match exactly one registry row')
    return rows[0]


def _save(root, name, obj):
    (root / name).write_text(json.dumps(obj, indent=2, default=str))


def _jobs_for(entry):
    jobs = json.loads(JOBS.read_text())['jobs']
    return [j for j in jobs if entry['name'] in json.dumps(j) or (entry.get('uuid') and entry['uuid'] in json.dumps(j))]


def _live(name):
    return subprocess.run(['tmux', 'has-session', '-t', '=' + name], capture_output=True).returncode == 0


def prepare(a):
    import fleet_reg
    import claude_handoff
    src = _entry(fleet_reg, a.name)
    if src.get('provider') != 'codex':
        raise SystemExit('source is not a Codex session')
    bundle = pathlib.Path(a.bundle).expanduser()
    cwd = pathlib.Path(src['cwd'])
    state = _cmd(['python3', 'fleet_state.py', a.name])
    if not state.startswith(('IDLE', 'NEEDS-INPUT')):
        raise SystemExit('source not quiescent: ' + state.strip())
    record = claude_handoff.prepare(fleet_reg.transcript_path(src), src['uuid'], bundle,
                                    pathlib.Path(a.instruction_file).read_text())
    plan = claude_handoff.plan_candidate(record, src, a.candidate_name, a.model, cwd / a.document,
                                         json.loads(pathlib.Path(a.facts_file).read_text()), str(fleet_reg.REG))
    git = subprocess.run(['git', '-C', str(cwd), 'status', '--porcelain', '-z'], capture_output=True)
    (bundle / 'git-before.bin').write_bytes(git.stdout)
    backup = bundle.parent / (bundle.name + '-workspace-backup')
    if backup.exists():
        raise SystemExit('workspace backup already exists: ' + str(backup))
    subprocess.run(['cp', '-cR', str(cwd), str(backup)], check=True)
    checks = []
    for item in git.stdout.split(b'\0'):
        if not item:
            continue
        rel = os.fsdecode(item[3:])
        p, q = cwd / rel, backup / rel
        if p.is_file():
            h = hashlib.sha256(p.read_bytes()).hexdigest()
            if hashlib.sha256(q.read_bytes()).hexdigest() != h:
                raise SystemExit('backup differs from tree: ' + rel)
            checks.append({'path': rel, 'sha256': h})
    _save(bundle, 'workspace-checks.json', checks)
    print(json.dumps(claude_handoff.launch_candidate(plan)), flush=True)
    name = plan['target']['name']
    end = time.monotonic() + 30
    while time.monotonic() < end:
        pane = _cmd(['tmux', 'capture-pane', '-p', '-t', '=' + name + ':'])
        if 'Quick safety check:' in pane and str(cwd) in pane and '❯ No, exit' in pane and 'Yes, I trust this folder' in pane:
            _cmd(['tmux', 'send-keys', '-t', '=' + name + ':', 'Down'])
            if '❯ Yes, I trust this folder' not in _cmd(['tmux', 'capture-pane', '-p', '-t', '=' + name + ':']):
                raise SystemExit('trust menu not on the expected option')
            _cmd(['tmux', 'send-keys', '-t', '=' + name + ':', 'Enter'])
            break
        try:
            claude_handoff.native_path(plan)
            break
        except ValueError:
            time.sleep(.5)
    print(json.dumps(claude_handoff.wait_candidate(plan, timeout=a.timeout), indent=2), flush=True)


def cutover(a):
    import fleet_reg
    import fleet_harness
    import fleet_input
    src = _entry(fleet_reg, a.name)
    bundle = pathlib.Path(a.bundle).expanduser()
    T = bundle / 'cutover'
    T.mkdir(mode=0o700, exist_ok=False)
    target = json.loads((bundle / 'candidate.json').read_text())
    _save(T, 'source-before.json', src)
    jobs = _jobs_for(src)
    _save(T, 'jobs-before.json', jobs)
    _save(T, 'armed-before.json', json.loads((RT / 'overwatch/armed.json').read_text()))
    for j in jobs:
        if j.get('enabled'):
            _cmd(['hermes', 'cron', 'pause', j['id']])
    state = _cmd(['python3', 'fleet_state.py', a.name])
    if not state.startswith(('IDLE', 'NEEDS-INPUT')) and src.get('desired') != 'parked':
        raise SystemExit('source not quiescent: ' + state.strip())
    _save(T, 'source-state.json', {'state': state})
    cwd = pathlib.Path(src['cwd'])
    backup = bundle.parent / (bundle.name + '-workspace-backup')
    git = subprocess.run(['git', '-C', str(cwd), 'status', '--porcelain', '-z'], capture_output=True)
    checks = []
    for item in git.stdout.split(b'\0'):
        if not item:
            continue
        rel = os.fsdecode(item[3:])
        p = cwd / rel
        if p.is_file():
            h = hashlib.sha256(p.read_bytes()).hexdigest()
            if backup.exists() and (backup / rel).is_file() and hashlib.sha256((backup / rel).read_bytes()).hexdigest() != h:
                raise SystemExit('dirty file changed since the backup: ' + rel + '; take a new backup first')
            checks.append({'path': rel, 'sha256': h})
    _save(T, 'dirty-verified.json', checks)
    _cmd(['python3', 'fleet_boot.py', 'park', src['name']])
    if _live(src['name']):
        _cmd(['tmux', 'kill-session', '-t', '=' + src['name']])
    if _live(target['name']):
        _cmd(['tmux', 'kill-session', '-t', '=' + target['name']])
    p = pathlib.Path(target['source_provenance']['transcript'])
    old = (bundle / 'source-transcript.jsonl').read_bytes()
    now = p.read_bytes()
    if not now.startswith(old):
        raise SystemExit('source transcript rewrote history; reconcile first')
    (T / 'source-delta.jsonl').write_bytes(now[len(old):])
    _save(T, 'source-hashes.json', {'old': hashlib.sha256(old).hexdigest(),
                                    'final': hashlib.sha256(now).hexdigest(), 'delta_bytes': len(now) - len(old)})
    import fleet_autonomy
    args = json.loads(pathlib.Path(a.args_file).read_text())
    try:
        fleet_autonomy.check_unowned_restrictions(src, args, a.allow_restricted)
    except ValueError as exc:
        raise SystemExit(str(exc))
    target['args'] = fleet_autonomy.carry_over(src, args)
    _save(T, 'autonomy-carry.json', {'args_file': args, 'carried': fleet_autonomy.auto_mode_allow(target['args']),
                                     'final_args': target['args']})
    if src.get('auto_mode_allow'):
        target['auto_mode_allow'] = src['auto_mode_allow']
    _save(T, 'operational-candidate.json', target)
    entry = fleet_reg.promote(src['name'], target, src['uuid'], str(T / 'promotion'))
    _save(T, 'published-row.json', entry)
    launch = fleet_harness.shell_line(entry, resume=entry['uuid'])
    _cmd(['tmux', 'new-session', '-d', '-s', src['name'], '-c', src['cwd'], launch])
    deadline = time.monotonic() + 90
    seen = []
    inp = {}
    while time.monotonic() < deadline:
        inp = fleet_input.read_input(src['name'])
        if inp.get('menu'):
            _save(T, 'startup-blocker.json', inp)
            raise SystemExit('startup menu; preserved, never auto-answered: ' + json.dumps(inp))
        if inp.get('box_found') and inp.get('empty'):
            seen.append(inp)
            if len(seen) >= 2:
                break
        time.sleep(1)
    if len(seen) < 2:
        raise SystemExit('no prompt-free composer: ' + json.dumps(inp))
    _save(T, 'prompt-free.json', seen)
    print(a.name, 'published and resumed', entry['uuid'], '(desired=parked until finalize)')


def engage(a):
    import fleet_reg
    import fleet_input
    e = _entry(fleet_reg, a.name)
    if e.get('provider') != 'claude':
        raise SystemExit('entry is not on claude')
    T = pathlib.Path(a.bundle).expanduser() / 'cutover'
    marker = 'CUTOVER-TOOLS-' + e['short'] + '-' + datetime.datetime.now().strftime('%Y%m%d%H%M%S')
    p = pathlib.Path(fleet_reg.transcript_path(e))
    start = p.stat().st_size
    prompt = ('Migration-only harmless validation, NOT a work nudge. The provider move has completed; this is a '
              'native tools/context check. Run exactly pwd and git status --short using Bash (separate calls), then Read '
              'one existing task-relevant local file and state one specific finding from it. Do not edit, install, run '
              'models, use the network or answer pending decisions. End the final response with the exact line ' + marker)
    f = T / 'engagement-prompt.txt'
    f.write_text(prompt)
    _save(T, 'engagement-dispatch.json', {'offset': start, 'marker': marker})
    print(_cmd(['bash', 'fleet_dispatch.sh', e['name'], str(f), '60']), flush=True)
    deadline = time.monotonic() + a.timeout
    while True:
        rows = [json.loads(x) for x in p.read_bytes()[start:].splitlines() if x.strip()]
        seg = [r for r in rows if r.get('type') in ('user', 'assistant')]
        finals = [r for r in seg if r['type'] == 'assistant' and r.get('message', {}).get('stop_reason') == 'end_turn'
                  and marker in json.dumps(r['message']['content'])]
        if finals:
            break
        if time.monotonic() > deadline:
            raise SystemExit('no final reply; inspect the transcript, never redispatch')
        time.sleep(1)
    calls = {b['id']: b for r in seg if r['type'] == 'assistant' and isinstance(r['message'].get('content'), list)
             for b in r['message']['content'] if b.get('type') == 'tool_use'}
    ok = {b['tool_use_id'] for r in seg if r['type'] == 'user' and isinstance(r['message'].get('content'), list)
          for b in r['message']['content'] if b.get('type') == 'tool_result' and not b.get('is_error')}
    good = [calls[k] for k in calls.keys() & ok]
    need = [any(x['name'] == 'Bash' and x['input'].get('command') == c for x in good) for c in ('pwd', 'git status --short')]
    if not all(need) or not any(x['name'] == 'Read' for x in good):
        raise SystemExit('tool gate failed: ' + json.dumps([x['name'] for x in good]))
    if not all(r.get('sessionId') == e['uuid'] and r.get('cwd') == e['cwd'] for r in seg):
        raise SystemExit('identity/cwd mismatch in segment')
    _save(T, 'engagement-proof.json', {'passed': True, 'uuid': e['uuid'], 'final': finals[-1], 'tools': good})
    print(json.dumps(finals[-1]['message']['content'])[:1500], flush=True)
    inp = fleet_input.read_input(e['name'])
    if not (inp.get('box_found') and inp.get('empty') and not inp.get('menu')):
        raise SystemExit('composer not clear for /rc: ' + json.dumps(inp))
    control = '/rc ' + e['short']
    _cmd(['tmux', 'send-keys', '-t', '=' + e['name'] + ':', '-l', control])
    end = time.monotonic() + 10
    while control not in json.dumps(fleet_input.read_input(e['name'])):
        if time.monotonic() > end:
            raise SystemExit('control composer not confirmed')
        time.sleep(.2)
    _cmd(['tmux', 'send-keys', '-t', '=' + e['name'] + ':', 'Enter'])
    end = time.monotonic() + 30
    while True:
        pane = _cmd(['tmux', 'capture-pane', '-p', '-t', '=' + e['name'] + ':'])
        urls = re.findall(r'https://claude.ai/code/session_[A-Za-z0-9]+', pane)
        if 'Remote Control' in pane and urls and 'Continue' in pane:
            break
        if time.monotonic() > end:
            raise SystemExit('RC not observed')
        time.sleep(.5)
    _save(T, 'rc-consumed-proof.json', {'uuid': e['uuid'], 'remote_url': urls[-1], 'browser_access': 'unverified'})
    _cmd(['tmux', 'send-keys', '-t', '=' + e['name'] + ':', 'Escape'])
    print('RC', urls[-1])


def finalize(a):
    import fleet_reg
    import fleet_overwatch
    e = _entry(fleet_reg, a.name)
    T = pathlib.Path(a.bundle).expanduser() / 'cutover'
    if not json.loads((T / 'engagement-proof.json').read_text())['passed']:
        raise SystemExit('engagement not proven')
    old = json.loads((T / 'jobs-before.json').read_text())
    was_up = json.loads((T / 'source-before.json').read_text()).get('desired') == 'up'
    for j in old:
        if j.get('name', '').endswith('-overwatch'):
            focus = extract_focus(j['prompt']) + '\n' + (a.focus_note or '')
            fleet_overwatch.refresh_paused(e['name'], focus.strip())
        if j.get('enabled'):
            _cmd(['hermes', 'cron', 'resume', j['id']])
    now = json.loads(JOBS.read_text())['jobs']
    after = [next(x for x in now if x['id'] == j['id']) for j in old]
    for j, b in zip(old, after):
        for k in ('id', 'enabled', 'state', 'schedule', 'context_from', 'deliver', 'origin', 'monitor_script'):
            if j.get(k) != b.get(k):
                raise SystemExit('job field changed: %s %s' % (j['id'], k))
        if j.get('name', '').endswith('-overwatch') and ('CODEX PROVIDER OVERRIDE' in b['prompt']):
            raise SystemExit('overwatch prompt still codex-shaped')
    armed = json.loads((RT / 'overwatch/armed.json').read_text())
    before = json.loads((T / 'armed-before.json').read_text())
    if armed.get(e['name']) != before.get(e['name']):
        raise SystemExit('armed entry changed')
    if was_up:
        _cmd(['python3', str(RT / 'fleet_boot.py'), 'up', e['name']])
    pub = _entry(fleet_reg, e['name'])
    if pub['provider'] != 'claude' or pub['uuid'] != e['uuid'] or pub['desired'] != ('up' if was_up else 'parked'):
        raise SystemExit('published row readback mismatch')
    _save(T, 'jobs-after.json', after)
    _save(T, 'COMPLETE.json', {'at': datetime.datetime.now().astimezone().isoformat(), 'entry': pub,
                               'watch_ids': [j['id'] for j in after]})
    print(a.name, 'COMPLETE', pub['uuid'], 'desired=' + pub['desired'])


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    sub = ap.add_subparsers(dest='stage', required=True)
    for n in ('prepare', 'cutover', 'engage', 'finalize'):
        p = sub.add_parser(n)
        p.add_argument('name')
        p.add_argument('--bundle', required=True)
        p.add_argument('--timeout', type=float, default=180)
        if n == 'prepare':
            p.add_argument('--instruction-file', required=True)
            p.add_argument('--document', required=True, help='file inside the project, relative to its cwd')
            p.add_argument('--facts-file', required=True)
            p.add_argument('--candidate-name', required=True)
            p.add_argument('--model', default='opus')
        if n == 'cutover':
            p.add_argument('--args-file', required=True, help='JSON list: the operational claude launch args')
            p.add_argument('--allow-restricted', action='store_true',
                           help='permit --safe-mode/--restricted although the source had neither')
        if n == 'finalize':
            p.add_argument('--focus-note', default='')
    a = ap.parse_args(argv)
    globals()[a.stage](a)


if __name__ == '__main__':
    main()
