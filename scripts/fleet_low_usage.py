#!/usr/bin/env python3
"""Same-UUID Sonnet cutover using fleet_reset's fail-closed lifecycle."""
import copy
import fcntl
import json
import os
from pathlib import Path
import sys

HERE = Path(__file__).resolve().parent
sys.path[:0] = [str(Path.home() / '.hermes/scripts/cc-watch'), str(HERE), str(HERE.parent)]
import fleet_reset
import fleet_registry_store


def target_entry(entry):
    target = copy.deepcopy(entry)
    args = target['args']
    args[args.index('--model') + 1] = 'sonnet'
    pos = args.index('--tools') + 1
    tools = args[pos].split(',')
    if 'Agent' not in tools:
        tools.append('Agent')
    args[pos] = ','.join(tools)
    pos = args.index('--allowedTools') + 1
    if 'Agent(wolfgang-evidence)' not in args:
        args.insert(pos, 'Agent(wolfgang-evidence)')
    return target


def main():
    import argparse
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--apply', action='store_true')
    p.add_argument('--evidence', type=Path, required=True)
    a = p.parse_args()
    os.umask(0o077)
    a.evidence.mkdir(parents=True, exist_ok=False)
    runtime = Path.home() / '.hermes/scripts/cc-watch'
    reg = runtime / 'fleet_registry.json'
    with open(runtime / 'fleet_boot.lock', 'a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        data = fleet_registry_store.load(reg)
        entry = next(e for e in data['sessions'] if e['name'] == 'cc-w-wolfgang-1c2b')
        if entry['uuid'] != '98eb94ff-fa4c-49fc-8028-01f9fadc461e':
            raise ValueError('Wolfgang identity changed')
        ops = fleet_reset.Runtime(runtime, a.evidence)
        before = ops.preflight(entry)
        target = target_entry(entry)
        # Safe mode intentionally skips discovery. Explicit CLI definitions retain
        # that safety boundary while loading only the authorized markdown agent.
        import yaml
        agent_path = Path(entry['cwd']) / '.claude/agents/wolfgang-evidence.md'
        _, frontmatter, body = agent_path.read_text().split('---', 2)
        definition = yaml.safe_load(frontmatter)
        name = definition.pop('name')
        if name != 'wolfgang-evidence' or definition.get('model') != 'haiku':
            raise ValueError('unexpected child definition')
        for key in ('tools', 'disallowedTools'):
            if isinstance(definition.get(key), str):
                definition[key] = [s.strip() for s in definition[key].split(',')]
        definition['prompt'] = body.strip()
        target['args'] += ['--agents', json.dumps({name: definition}, separators=(',', ':'))]
        receipt = {'entry': entry, 'target': target, 'before': before, 'status': 'preflight'}
        dest = a.evidence / 'receipt.json'
        fleet_reset.save(dest, receipt)
        if before['reason']:
            raise RuntimeError(before['reason'])
        if not a.apply:
            print('ready'); return
        ops.stop(entry, before)
        receipt['status'] = 'stopped'
        fleet_reset.save(dest, receipt)
        ops.launch(target, before)
        receipt['status'] = 'launched'
        fleet_reset.save(dest, receipt)
        # Publish only this authorized launch change; CAS preserves concurrent edits.
        data['sessions'][data['sessions'].index(entry)] = target
        fleet_registry_store.save(reg, data)
        receipt['after'] = ops.verify(target, before)
        receipt['status'] = 'verified'
        fleet_reset.save(dest, receipt)
        assert next(e for e in fleet_registry_store.load(reg)['sessions'] if e['name'] == entry['name']) == target
        print(json.dumps({'status': 'verified', 'uuid': target['uuid'], 'args': target['args']}))

if __name__ == '__main__': main()
