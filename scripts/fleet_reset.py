#!/usr/bin/env python3
"""Fail-closed, same-conversation reset for live work-profile Claude sessions."""


import json
import os
from pathlib import Path
from typing import Optional


def save(path, data):
    tmp = path.with_suffix('.tmp')
    with open(tmp, 'w', opener=lambda p, flags: os.open(p, flags, 0o600)) as f:
        json.dump(data, f, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def reset_one(entry, directory, ops):
    started = time.monotonic()
    row = {'name': entry['name'], 'entry': entry, 'status': 'held', 'elapsed_s': {}}
    def measured(phase, fn, *args):
        tick = time.monotonic()
        try:
            return fn(*args)
        finally:
            row['elapsed_s'][phase] = round(time.monotonic() - tick, 3)
    path = directory / (entry['name'] + '.json')
    try:
        row['before'] = measured('preflight', ops.preflight, entry)
        save(path, row)
        if row['before']['reason']:
            row['reason'] = row['before']['reason']
        else:
            measured('stop', ops.stop, entry, row['before'])
            row['status'] = 'recovery-required'
            save(path, row)
            measured('launch', ops.launch, entry, row['before'])
            row['after'] = measured('verify', ops.verify, entry, row['before'])
            row['status'] = 'verified'
    except Exception as exc:
        row['reason'] = str(exc)
    row['elapsed_s']['total'] = round(time.monotonic() - started, 3)
    save(path, row)
    return row


def recover_one(row, directory, ops):
    if row['status'] != 'recovery-required' or row['before'].get('launch_attempted'):
        raise RuntimeError('not an unlaunched recovery checkpoint; inspect existing writer, do not retry')
    started = time.monotonic()
    row['name'] = row['entry']['name']
    try:
        ops.launch(row['entry'], row['before'])
        row['after'] = ops.verify(row['entry'], row['before'])
        row['status'] = 'verified'
        row.pop('reason', None)
    except Exception as exc:
        row['reason'] = str(exc)
    row['recovery_elapsed_s'] = round(time.monotonic() - started, 3)
    save(directory / (row['name'] + '.json'), row)
    return row


def scope_reason(entry):
    if entry.get('desired') != 'up':
        return 'parked'
    if entry.get('profile') != 'work' or entry.get('provider', 'claude') != 'claude':
        return 'not work Claude'
    return None


def gate(obs):
    checks = [
        (obs.get('transcript'), 'missing or invalid native transcript'),
        (obs.get('auth'), 'work authentication unavailable'),
        (obs.get('writers') == 1, 'ambiguous or absent Claude writer'),
        (obs.get('binding'), 'native process/cwd/pane binding mismatch'),
        (not obs.get('children'), 'active child processes; wait for tools to quiesce'),
        (obs.get('status') == 'idle', 'native session is not idle'),
        (obs.get('composer'), 'composer not located'),
        (not obs.get('menu'), 'owner choice is open'),
        (not obs.get('draft'), 'unsent draft retained; not safe to reset'),
        (obs.get('auto'), 'genuine auto mode not confirmed'),
        (not obs.get('background'), 'background shells still running'),
        (obs.get('launch_matches'), 'launch constraints differ from registry'),
    ]
    return next((reason for ok, reason in checks if not ok), None)


import fcntl
import hashlib
import re
import shutil
import subprocess
import sys
import time
import uuid
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed

MUTATION_LOCK = threading.Lock()


def serialized(fn):
    def wrapped(*args, **kwargs):
        with MUTATION_LOCK:
            return fn(*args, **kwargs)
    return wrapped


def batch(entries, work, workers=3, metrics=None):
    metrics = metrics if metrics is not None else {}
    metrics.update(active=0, peak_active=0)
    guard = threading.Lock()
    def measured(entry):
        with guard:
            metrics['active'] += 1
            metrics['peak_active'] = max(metrics['peak_active'], metrics['active'])
        try:
            return work(entry)
        finally:
            with guard:
                metrics['active'] -= 1
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(measured, entry) for entry in entries]
        for future in as_completed(futures):
            yield future.result()

HERE = Path(os.path.abspath(__file__)).parent
for _path in (HERE, Path(__file__).resolve().parent, Path(__file__).resolve().parent.parent):
    if str(_path) not in sys.path:
        sys.path.append(str(_path))


def run(*args, **kwargs):
    return subprocess.run(list(args), capture_output=True, text=True, timeout=kwargs.pop('timeout', 20), **kwargs)


def claude_writer_pids(output, want):
    pids = set()
    for line in output.splitlines():
        parts = line.strip().split(None, 2)
        if len(parts) < 3:
            continue
        pid, executable, args = parts
        if (executable == 'claude' or executable.endswith('/claude') or '/claude/versions/' in executable) and re.search(r'--(?:resume|session-id)(?:=|\s+)' + re.escape(want) + r'(?:\s|$)', args):
            pids.add(int(pid))
    return pids


def native_sessions(home):
    result = []
    snapshot = run('ps', '-axo', 'pid=,lstart=', env={**os.environ, 'TZ': 'UTC'}).stdout
    starts = dict(line.strip().split(None, 1) for line in snapshot.splitlines() if line.strip())
    for path in (home / 'sessions').glob('*.json'):
        record = json.loads(path.read_text())
        pid = str(record.get('pid', 0))
        start = starts.get(pid, '').strip()
        if start and start == record.get('procStart'):
            result.append(record)
    return result


def records_since(path, offset=0):
    with open(path, 'rb') as f:
        f.seek(offset)
        lines = f.read().splitlines()
    result = []
    for line in lines:
        try:
            result.append(json.loads(line))
        except (ValueError, UnicodeDecodeError):
            continue
    return result


def receipt(records, session_id, marker, cwd):
    landed = False
    tools = set()
    pwd_ok = False
    for record in records:
        if record.get('sessionId') != session_id:
            continue
        content = record.get('message', {}).get('content', [])
        if record.get('type') == 'user' and marker in str(content):
            landed = True
        if not landed or not isinstance(content, list):
            continue
        for block in content:
            if block.get('type') == 'tool_use' and block.get('name') == 'Bash' and block.get('input', {}).get('command', '').strip() == 'pwd':
                tools.add(block.get('id'))
            if block.get('type') == 'tool_result' and block.get('tool_use_id') in tools and not block.get('is_error'):
                pwd_ok = cwd in str(block.get('content', ''))
            if record.get('type') == 'assistant' and block.get('type') == 'text' and marker in block.get('text', '') and pwd_ok:
                return True
    return False


def shutdown_only(records, prior_backups=None):
    exit_pattern = r'<command-name>/exit</command-name>\s*<command-message>exit</command-message>\s*<command-args></command-args>'
    exit_ids = {r['uuid'] for r in records if r.get('uuid') and r.get('type') == 'user'
                and re.fullmatch(exit_pattern, str(r.get('message', {}).get('content', '')))}
    for record in records:
        kind = record.get('type')
        if kind in ('last-prompt', 'cost-state', 'bridge-session'):
            continue
        if kind == 'permission-mode' and record.get('permissionMode') == 'auto':
            continue
        if kind == 'file-history-snapshot' and (record.get('messageId') in exit_ids or record.get('snapshot', {}).get('trackedFileBackups') in ({}, prior_backups)):
            continue
        content = record.get('message', {}).get('content')
        if kind == 'user' and isinstance(content, str):
            if record.get('isMeta') and content.startswith('<local-command-caveat>') and content.endswith('</local-command-caveat>'):
                continue
            if content == '<local-command-stdout>Bye!</local-command-stdout>':
                continue
            if record.get('parentUuid') in exit_ids and re.fullmatch(r'<local-command-stdout>[^<]*</local-command-stdout>', content):
                continue
            if re.fullmatch(exit_pattern, content):
                continue
        return False
    return True


class Runtime:
    def __init__(self, runtime, evidence, auth=None):
        self.runtime, self.evidence = runtime, evidence
        self.home = Path.home() / '.claude-work'
        self.binary = Path.home() / '.local/bin/claude'
        self.auth = auth
        self.inventory: Optional[list] = None

    def preflight(self, entry, fresh=False):
        import fleet_harness
        import fleet_input
        reason = scope_reason(entry)
        if reason:
            return {'reason': reason}
        want = entry.get('uuid', '')
        if not re.fullmatch(r'[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}', want):
            return {'reason': 'missing or malformed native UUID'}
        spec = fleet_harness.resolve(entry)
        if spec['env'].get('CLAUDE_CONFIG_DIR') != str(self.home):
            return {'reason': 'not the authorized work config directory'}
        paths = list((self.home / 'projects').glob('*/' + want + '.jsonl'))
        if len(paths) != 1:
            return {'reason': 'missing or ambiguous native transcript'}
        path = paths[0]
        inventory = native_sessions(self.home) if fresh or self.inventory is None else self.inventory
        sessions = [r for r in inventory if r['sessionId'] == want]
        if len(sessions) != 1:
            return {'reason': 'ambiguous or absent Claude writer', 'writers': len(sessions)}
        native = sessions[0]
        pid = native['pid']
        writers = claude_writer_pids(run('ps', '-ww', '-axo', 'pid=,args=').stdout, want) | {r['pid'] for r in sessions}
        if writers != {pid}:
            return {'reason': 'ambiguous Claude processes outside native registry', 'writers': sorted(writers)}
        cwd = str(Path(entry['cwd']).expanduser().resolve())
        pane = run('tmux', 'list-panes', '-t', '=' + entry['name'], '-F', '#{pane_pid} #{pane_id}').stdout.strip().splitlines()
        binding = len(pane) == 1 and pane[0].split()[0] == str(pid) and native.get('tmux', '').split(':')[0] == entry['name'] and native['cwd'] == cwd and Path(cwd).is_dir()
        command = run('ps', '-ww', '-p', str(pid), '-o', 'command=').stdout.strip()
        actual_cwd = run('lsof', '-a', '-p', str(pid), '-d', 'cwd', '-Fn').stdout.splitlines()
        binding = binding and ('n' + cwd) in actual_cwd
        children = run('pgrep', '-P', str(pid)).stdout.split()
        composer = fleet_input.read_input(entry['name'])
        screen = run('tmux', 'capture-pane', '-p', '-t', '=' + entry['name'] + ':').stdout
        # Runtime auto-mode changes do not rewrite argv. Compare all other flags.
        expected = spec['args'] + (entry.get('extra_args') or [])
        def constraints(text):
            text = re.sub(r'--permission-mode\s+\S+', '', text)
            text = re.sub(r'--(?:resume|session-id)\s+\S+', '', text)
            return ' '.join(text.split())
        launch_matches = constraints(' '.join(expected)) == constraints(command.partition(' ')[2])
        authenticated = self.auth
        if authenticated is None:
            response = run(str(self.binary), 'auth', 'status', env={**os.environ, 'CLAUDE_CONFIG_DIR': str(self.home)})
            status = json.loads(response.stdout) if response.returncode == 0 else {}
            authenticated = status.get('loggedIn') is True and status.get('configDirectory') == str(self.home)
        native_records = records_since(path)
        valid = any(r.get('sessionId') == want and r.get('type') in ('assistant', 'user') for r in native_records)
        obs = dict(transcript=valid, auth=authenticated, writers=len(sessions), binding=binding,
                   children=children, status=native.get('status'), composer=composer.get('box_found'),
                   menu=composer.get('menu'), draft=not composer.get('empty') and not composer.get('ghost'),
                   auto='auto mode on' in screen, background=bool(re.search(r'\b[1-9]\d* shells?\b', screen)), launch_matches=launch_matches)
        return dict(obs, reason=gate(obs), pid=pid, native=native, input=composer,
                    transcript=str(path), offset=path.stat().st_size, cwd=cwd, spec=spec,
                    command=command, executable=str(self.binary.resolve()),
                    version=run(str(self.binary), '--version').stdout.strip())

    def stop(self, entry, before):
        fresh = self.preflight(entry, fresh=True)
        if fresh['reason'] or fresh.get('pid') != before['pid']:
            raise RuntimeError('pre-stop recheck: ' + str(fresh.get('reason') or 'writer changed'))
        if self.binary.resolve() != Path(before['executable']):
            raise RuntimeError('CLI changed during preflight')
        checkpoint = self.evidence / (entry['name'] + '.transcript.jsonl')
        shutil.copy2(before['transcript'], checkpoint)
        before['checkpoint'] = str(checkpoint)
        before['checkpoint_sha256'] = hashlib.sha256(checkpoint.read_bytes()).hexdigest()
        target = '=' + entry['name'] + ':'
        before['remain_on_exit'] = run('tmux', 'show-options', '-wv', '-t', target, 'remain-on-exit').stdout.strip()
        run('tmux', 'set-option', '-w', '-t', target, 'remain-on-exit', 'on', check=True)
        save(self.evidence / (entry['name'] + '.launch.json'), before)
        run('tmux', 'send-keys', '-t', target, '-l', '/exit', check=True)
        run('tmux', 'send-keys', '-t', target, 'Enter', check=True)
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if not run('ps', '-p', str(before['pid']), '-o', 'pid=').stdout.strip():
                break
            time.sleep(.25)
        else:
            raise RuntimeError('graceful exit not completed; no force kill or relaunch')
        if any(r['sessionId'] == entry['uuid'] for r in native_sessions(self.home)):
            raise RuntimeError('another native writer remains after exit')

    @serialized
    def launch(self, entry, before):
        import fleet_harness
        path = Path(before['transcript'])
        snapshot = Path(before['checkpoint']).read_bytes()
        if not path.read_bytes().startswith(snapshot):
            raise RuntimeError('transcript prefix changed; preserve stopped session for recovery')
        suffix = records_since(path, len(snapshot))
        prior = [r['snapshot']['trackedFileBackups'] for r in records_since(before['checkpoint'])
                 if r.get('type') == 'file-history-snapshot' and 'trackedFileBackups' in r.get('snapshot', {})]
        if not shutdown_only(suffix, prior[-1] if prior else None):
            raise RuntimeError('conversation changed during exit; inspect recovery checkpoint')
        if any(r['sessionId'] == entry['uuid'] for r in native_sessions(self.home)):
            raise RuntimeError('duplicate writer before launch')
        if claude_writer_pids(run('ps', '-ww', '-axo', 'pid=,args=').stdout, entry['uuid']):
            raise RuntimeError('unregistered duplicate writer before launch')
        before['verify_offset'] = path.stat().st_size
        old = os.environ.get('FLEET_CLAUDE_BIN')
        os.environ['FLEET_CLAUDE_BIN'] = before['executable']
        try:
            command = fleet_harness.shell_line(entry, resume=entry['uuid'], extra_args=entry.get('extra_args'))
        finally:
            if old is None:
                os.environ.pop('FLEET_CLAUDE_BIN', None)
            else:
                os.environ['FLEET_CLAUDE_BIN'] = old
        # No -k: tmux refuses if the pane still has a live process.
        before['launch_attempted'] = True
        save(self.evidence / (entry['name'] + '.launch-attempt.json'), before)
        run('tmux', 'respawn-pane', '-t', '=' + entry['name'] + ':', '-c', before['cwd'], 'exec env ' + command, check=True)

    def verify(self, entry, before):
        import fleet_input
        target = '=' + entry['name'] + ':'
        deadline = time.monotonic() + 120
        native = None
        while time.monotonic() < deadline:
            matches = [r for r in native_sessions(self.home) if r['sessionId'] == entry['uuid']]
            info = fleet_input.read_input(entry['name'])
            if len(matches) > 1:
                raise RuntimeError('multiple writers after launch')
            if info.get('menu'):
                raise RuntimeError('startup choice requires owner; no answer sent')
            screen = run('tmux', 'capture-pane', '-p', '-t', target).stdout
            if len(matches) == 1 and info.get('box_found') and (info.get('empty') or info.get('ghost')) and 'auto mode on' in screen:
                native = matches[0]
                break
            time.sleep(.5)
        if not native:
            raise RuntimeError('native resume/auto/composer readiness timed out')
        executable = run('ps', '-ww', '-p', str(native['pid']), '-o', 'comm=').stdout.strip()
        if executable != before['executable'] or native['pid'] == before['pid'] or native['cwd'] != before['cwd']:
            raise RuntimeError('resumed process executable/cwd/PID mismatch')
        marker = 'RESET-' + uuid.uuid4().hex
        prompt = self.evidence / (entry['name'] + '.verify.txt')
        prompt.write_text('Maintenance-only restart verification. Do not resume project work, alter files, make approvals, or resolve pending owner decisions. Your original conversation and restrictions remain authoritative. Run exactly `pwd` using Bash, then reply with ' + marker + ' and one sentence identifying the prior task and any still-held owner gate. Stop after this read-only check.\n')
        result = run('bash', str(self.runtime / 'fleet_dispatch.sh'), entry['name'], str(prompt), timeout=90)
        (self.evidence / (entry['name'] + '.dispatch.txt')).write_text(result.stdout + result.stderr)
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            records = records_since(before['transcript'], before['verify_offset'])
            if receipt(records, entry['uuid'], marker, before['cwd']):
                break
            time.sleep(.5)
        else:
            raise RuntimeError('no fresh linked native pwd result and final reply; dispatch not repeated')
        matches = [r for r in native_sessions(self.home) if r['sessionId'] == entry['uuid']]
        if len(matches) != 1 or matches[0]['pid'] != native['pid']:
            raise RuntimeError('writer changed during verification')
        if before['native'].get('bridgeSessionId') and not matches[0].get('bridgeSessionId'):
            raise RuntimeError('native response verified but remote-control bridge not restored')
        clear_cache_file(self.runtime, entry['name'])
        run('tmux', 'set-option', '-w', '-t', target, 'remain-on-exit', before['remain_on_exit'] or 'off', check=True)
        return {'native': matches[0], 'executable': executable, 'marker': marker,
                'native_response': True, 'pwd': before['cwd'], 'offset': before['verify_offset']}


def clear_binding(state, name):
    for key in ('_liveness', '_stall'):
        state.get(key, {}).pop(name, None)


@serialized
def clear_cache_file(runtime, name):
    path = runtime / 'fleet_watch_state.json'
    if path.exists():
        original = path.read_bytes()
        state = json.loads(original)
        clear_binding(state, name)
        if path.read_bytes() != original:
            raise RuntimeError('watch state changed; cache invalidation needs retry')
        save(path, state)
    import fleet_transcript
    for key in list(fleet_transcript._CACHE):
        if key[-1] == name:
            fleet_transcript._CACHE.pop(key)


def supervision(runtime):
    files = ['fleet_registry.json', 'overwatch/armed.json', 'fleet_watch_requests.json',
             'fleet_watch_acks.json', 'fleet_watch_pending.json']
    data = {name: json.loads((runtime / name).read_text()) for name in files if (runtime / name).exists()}
    jobs = Path.home() / '.hermes/cron/jobs.json'
    if jobs.exists():
        raw = json.loads(jobs.read_text())
        rows = raw if isinstance(raw, list) else raw.get('jobs', [])
        if isinstance(rows, dict):
            rows = list(rows.values())
        # Scheduler counters may advance naturally; its configuration must not.
        keys = ('id', 'name', 'prompt', 'schedule', 'delivery', 'deliver', 'enabled', 'state', 'monitor', 'context_from')
        data['jobs'] = [{k: row.get(k) for k in keys} for row in rows]
    return data


def main(argv=None):
    import argparse
    from collections import Counter
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply', action='store_true', help='gracefully reset eligible live sessions; default is preflight only')
    parser.add_argument('--only', action='append', default=[], help='exact registered name; repeatable')
    parser.add_argument('--workers', type=int, choices=range(1, 4), default=3)
    parser.add_argument('--recover', type=Path, action='append', help='resume stopped, not-yet-launched session JSON; repeatable')
    parser.add_argument('--evidence', required=True, type=Path, help='new private recovery directory')
    args = parser.parse_args(argv)
    os.umask(0o077)
    args.evidence.mkdir(parents=True, exist_ok=False)
    runtime = HERE
    if not (runtime / 'fleet_registry.json').exists():
        parser.error('invoke through the installed runtime path')
    ops = Runtime(runtime, args.evidence)
    before = supervision(runtime)
    save(args.evidence / 'supervision-before.json', before)
    registry = before['fleet_registry.json']['sessions']
    uuid_counts = Counter(e.get('uuid') for e in registry)
    known = {e['uuid'] for e in registry if e.get('uuid')}
    ops.inventory = native_sessions(ops.home)
    unknown = [r for r in ops.inventory if r['sessionId'] not in known]
    response = run(str(ops.binary), 'auth', 'status', env={**os.environ, 'CLAUDE_CONFIG_DIR': str(ops.home)})
    auth = json.loads(response.stdout) if response.returncode == 0 else {}
    ops.auth = auth.get('loggedIn') is True and auth.get('configDirectory') == str(ops.home)
    for row in unknown:
        row['reason'] = 'unregistered native session; no managed pane/launch policy; not touched'
        row['transcripts'] = [str(p) for p in (ops.home / 'projects').glob('*/' + row['sessionId'] + '.jsonl')]
    report: dict = {'unregistered': unknown, 'sessions': [], 'workers': args.workers, 'concurrency': {}}
    started = time.monotonic()
    save(args.evidence / 'report.json', report)
    # The same lock as boot reconcile prevents automatic recovery during the gap.
    with open(runtime / 'fleet_boot.lock', 'a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if args.recover:
            if not args.apply:
                parser.error('--recover requires --apply')
            saved = [json.loads(path.read_text()) for path in args.recover]
            if any(row['entry'] not in registry for row in saved) or not ops.auth:
                parser.error('recovery registry/auth mismatch')
            if len({row['entry']['uuid'] for row in saved}) != len(saved):
                parser.error('duplicate recovery UUID')
            for row in batch(saved, lambda row: recover_one(row, args.evidence, ops), workers=args.workers, metrics=report['concurrency']):
                report['sessions'].append(row)
                print(row['name'], row['status'], row.get('reason', ''), flush=True)
            report['counts'] = dict(Counter(row['status'] for row in report['sessions']))
            report['elapsed_s'] = round(time.monotonic() - started, 3)
            after = supervision(runtime)
            save(args.evidence / 'supervision-after.json', after)
            report['supervision_unchanged'] = before == after
            save(args.evidence / 'report.json', report)
            print(json.dumps({'counts': report['counts'], 'elapsed_s': report['elapsed_s'], 'supervision_unchanged': before == after}))
            return int(any(row['status'] != 'verified' for row in report['sessions']))
        def work(entry):
            tick = time.monotonic()
            reason = scope_reason(entry)
            if not reason and uuid_counts[entry.get('uuid')] != 1:
                return {'name': entry['name'], 'status': 'held', 'reason': 'duplicate registry UUID; no concurrent writer allowed'}
            if args.only and entry['name'] not in args.only:
                reason = 'not selected'
            if reason:
                row = {'name': entry['name'], 'status': 'excluded', 'reason': reason}
            elif args.apply:
                row = reset_one(entry, args.evidence, ops)
            else:
                try:
                    observation = ops.preflight(entry)
                    row = {'name': entry['name'], 'status': 'held' if observation['reason'] else 'ready', 'before': observation, 'reason': observation['reason']}
                except Exception as exc:
                    row = {'name': entry['name'], 'status': 'held', 'reason': str(exc)}
            row.setdefault('elapsed_s', {'total': round(time.monotonic() - tick, 3)})
            return row
        for row in batch(registry, work, workers=args.workers, metrics=report['concurrency']):
            report['sessions'].append(row)
            report['counts'] = dict(Counter(r['status'] for r in report['sessions']))
            save(args.evidence / 'report.json', report)
            print(row['name'], row['status'], row.get('reason', ''), flush=True)
    after = supervision(runtime)
    save(args.evidence / 'supervision-after.json', after)
    report['registry_unchanged'] = before['fleet_registry.json'] == after['fleet_registry.json']
    report['supervision_unchanged'] = before == after
    report['live_after'] = native_sessions(ops.home)
    report['elapsed_s'] = round(time.monotonic() - started, 3)
    save(args.evidence / 'report.json', report)
    print(json.dumps({'counts': report['counts'], 'unregistered': len(unknown), 'registry_unchanged': report['registry_unchanged'], 'supervision_unchanged': report['supervision_unchanged']}))
    return int(any(r['status'] in ('held', 'recovery-required') for r in report['sessions']))


if __name__ == '__main__':
    raise SystemExit(main())
