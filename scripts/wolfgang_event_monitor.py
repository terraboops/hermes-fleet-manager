#!/usr/bin/env python3
"""Wolfgang-only event fingerprint: no time heartbeat or repeated gate inference."""
import json
from pathlib import Path
import subprocess
import sys

SESSION = 'cc-w-wolfgang-1c2b'
RUNTIME = Path.home() / '.hermes/scripts/cc-watch'


def stable_state(value):
    return '|'.join(part for part in value.strip().split('|') if not part.startswith(('hb=', 'idle_due=')))


def main(jev=False):
    run = subprocess.run([sys.executable, str(RUNTIME / 'fleet_state.py'), SESSION], capture_output=True, text=True, timeout=30)
    if run.returncode or not run.stdout.strip():
        raise RuntimeError('native state unavailable')
    result = {'session': SESSION, 'state': stable_state(run.stdout)}
    if not jev:
        try:
            pr = subprocess.run(['gh', 'pr', 'view', '102', '--repo', 'bitcomplete/operate', '--json', 'state,headRefOid,mergedAt'], capture_output=True, text=True, timeout=20)
            result['pr102'] = json.loads(pr.stdout) if pr.returncode == 0 else {'lookup': 'unavailable'}
        except (subprocess.TimeoutExpired, ValueError):
            result['pr102'] = {'lookup': 'unavailable'}
    print(json.dumps(result, sort_keys=True))

if __name__ == '__main__':
    main('--jev' in sys.argv)
