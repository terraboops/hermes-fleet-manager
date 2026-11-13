#!/usr/bin/env python3
"""Read-only native Claude activity; idle is not a tool timeout."""
import datetime
import json
import re
import time
from pathlib import Path

IDLE_AFTER = 300
STALE_TOOL_AFTER = 1800  # diagnostic only; never cancels a tool


def epoch(value):
    if isinstance(value, (int, float)):
        return value
    try:
        return datetime.datetime.fromisoformat(value.replace('Z', '+00:00')).timestamp()
    except (ValueError, TypeError, AttributeError):
        return None


def classify(records, *, now, alive, supervised=False, approval=False,
             child_pids=(), truncated=False, idle_after=IDLE_AFTER,
             stale_tool_after=STALE_TOOL_AFTER, process_started_at=None):
    pending, background = {}, set()
    background_started = {}
    completed = last = None
    last_kind = 'none'
    for record in records:
        kind = record.get('type')
        content = record.get('message', {}).get('content', [])
        stamp = epoch(record.get('timestamp'))
        meaningful = False
        if kind == 'assistant':
            meaningful = True
            for block in content if isinstance(content, list) else []:
                if block.get('type') == 'tool_use':
                    pending[block['id']] = block.get('name', '')
            completed = stamp if record.get('message', {}).get('stop_reason') in ('end_turn', 'stop_sequence') else None
        elif kind == 'user':
            if isinstance(content, list):
                for block in content:
                    if block.get('type') == 'tool_result':
                        meaningful = True
                        pending.pop(block.get('tool_use_id'), None)
                        task = record.get('toolUseResult', {})
                        if isinstance(task, dict) and task.get('backgroundTaskId'):
                            background.add(task['backgroundTaskId'])
                            background_started[task['backgroundTaskId']] = stamp
                    elif block.get('type') == 'text':
                        meaningful = True
            elif isinstance(content, str) and '<task-notification>' in content:
                task = re.search(r'<task-id>([^<]+)</task-id>', content)
                status = re.search(r'<status>(completed|failed|killed)</status>', content)
                if task and status:
                    background.discard(task[1])
                    meaningful = True
            elif isinstance(content, str) and not record.get('isMeta') and not content.startswith(('<command-', '<local-command-')):
                meaningful = True
            if meaningful:
                completed = None
        if kind in ('queue-operation', 'attachment'):
            note = record.get('content', '') if kind == 'queue-operation' else record.get('attachment', {}).get('prompt', '')
            if isinstance(note, str) and '<task-notification>' in note:
                task = re.search(r'<task-id>([^<]+)</task-id>', note)
                status = re.search(r'<status>(completed|failed|killed)</status>', note)
                if task and status:
                    background.discard(task[1])
                    meaningful = True
                    completed = None
            elif kind == 'queue-operation' and record.get('operation') == 'enqueue':
                meaningful = True
                completed = None
        if meaningful:
            last, last_kind = stamp, kind
    if not alive:
        state, reason = 'DEAD', 'native process absent'
    elif approval or 'AskUserQuestion' in pending.values():
        state, reason = 'NEEDS_INPUT', 'owner approval or answer held'
    elif truncated:
        state, reason = 'UNKNOWN', 'incomplete or malformed native stream'
    elif background and not child_pids and process_started_at and all(background_started.get(task) is not None and background_started[task] < process_started_at for task in background):
        state, reason = 'UNKNOWN', 'unresolved background metadata predates current process; no live child evidence'
    elif pending or background or child_pids:
        state, reason = 'WAITING_TOOL', 'unmatched tool call, background task, or live child process'
    elif completed is not None:
        state, reason = 'IDLE', 'native assistant turn completed; no outstanding tools'
    else:
        state, reason = 'BUSY', 'native turn has no verified completion'
    idle_seconds = max(0, now - completed) if state == 'IDLE' else None
    return {'state': state, 'reason': reason, 'last_meaningful_at': last,
            'last_meaningful_type': last_kind, 'idle_since': completed if state == 'IDLE' else None,
            'idle_seconds': idle_seconds, 'idle_after_seconds': idle_after,
            'idle_eligible': bool(supervised and idle_seconds is not None and idle_seconds >= idle_after),
            'pending_tools': sorted(pending), 'background_tasks': sorted(background),
            'child_pids': list(child_pids),
            'stale_observation': bool(state in ('BUSY', 'WAITING_TOOL') and last is not None and now - last >= stale_tool_after),
            'stale_tool_after_seconds': stale_tool_after}


def inspect_entry(entry, path, runtime):
    import fleet_input
    import fleet_reset
    home = Path(entry.get('config_dir', '')).expanduser()
    if entry.get('profile') != 'work' or home != Path.home() / '.claude-work':
        return {'state': 'UNKNOWN', 'reason': 'outside authorized work profile', 'idle_eligible': False}
    native = [r for r in fleet_reset.native_sessions(home) if r['sessionId'] == entry.get('uuid')]
    children = []
    if len(native) == 1:
        children = fleet_reset.run('pgrep', '-P', str(native[0]['pid'])).stdout.split()
    info = fleet_input.read_input(entry['name']) if len(native) == 1 else {}
    armed_path = runtime / 'overwatch/armed.json'
    armed = json.loads(armed_path.read_text()) if armed_path.exists() else {}
    supervised = entry.get('desired') == 'up' and entry['name'] in armed
    if supervised:
        store = json.loads((Path.home() / '.hermes/cron/jobs.json').read_text())
        jobs = store if isinstance(store, list) else store.get('jobs', [])
        jobs = list(jobs.values()) if isinstance(jobs, dict) else jobs
        supervised = any(j.get('name') == armed[entry['name']].get('name') and j.get('enabled') for j in jobs)
    records, truncated = [], False
    try:
        with open(path, 'rb') as source:
            for line in source:
                try:
                    record = json.loads(line)
                    if not line.endswith(b'\n'):
                        truncated = True
                    if record.get('sessionId') in (None, entry.get('uuid')) and not record.get('isSidechain'):
                        records.append(record)
                except (ValueError, UnicodeDecodeError):
                    truncated = True
    except OSError:
        truncated = True
    result = classify(records, now=time.time(), alive=len(native) == 1,
                      supervised=supervised, approval=bool(info.get('menu')),
                      child_pids=children, truncated=truncated,
                      process_started_at=native[0].get('startedAt', 0) / 1000 if len(native) == 1 else None)
    result.update(session=entry['name'], supervised=supervised)
    return result


def main():
    import argparse
    import os
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('session')
    args = parser.parse_args()
    runtime = Path(os.path.abspath(__file__)).parent
    entries = json.loads((runtime / 'fleet_registry.json').read_text())['sessions']
    entry = next(e for e in entries if args.session in (e['name'], e.get('short')))
    paths = list(Path(entry['config_dir']).expanduser().glob('projects/*/' + entry['uuid'] + '.jsonl'))
    if len(paths) != 1:
        parser.error('missing or ambiguous transcript')
    print(json.dumps(inspect_entry(entry, paths[0], runtime), indent=2))


if __name__ == '__main__':
    main()
