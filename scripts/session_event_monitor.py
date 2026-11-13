#!/usr/bin/env python3
"""Event-only fingerprint of one session, for a cron monitor.

Prints one JSON line: the session's native state with its clock fields removed. A monitor that
runs this wakes its job only when the line changes, so a heartbeat tick or an idle-timer edge
is never an event and no model inference runs for it.

  session_event_monitor.py <session>
"""
import json
import os
import subprocess
import sys

RUNTIME = os.path.expanduser(os.environ.get('FLEET_RUNTIME', '~/.hermes/scripts/cc-watch'))
CLOCK_FIELDS = ('hb=', 'idle_due=')


def stable_state(value):
    """A state string without its clock fields."""
    return '|'.join(p for p in value.strip().split('|') if not p.startswith(CLOCK_FIELDS))


def fingerprint(session):
    run = subprocess.run([sys.executable, os.path.join(RUNTIME, 'fleet_state.py'), session],
                         capture_output=True, text=True, timeout=30)
    if run.returncode or not run.stdout.strip():
        raise RuntimeError('native state unavailable')
    return {'session': session, 'state': stable_state(run.stdout)}


def main(session=None, extra=None):
    """Print the fingerprint. `extra` is an optional callable returning a dict to merge in."""
    session = session or (sys.argv[1] if len(sys.argv) > 1 else '')
    if not session:
        raise SystemExit('usage: session_event_monitor.py <session>')
    result = fingerprint(session)
    if extra:
        result.update(extra())
    print(json.dumps(result, sort_keys=True))


if __name__ == '__main__':
    main()
