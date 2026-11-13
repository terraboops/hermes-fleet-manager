#!/usr/bin/env python3
"""Launch-policy autonomy for registered Claude sessions.

A session's launch args decide what its auto-mode classifier can see. `--restricted`
ignores user, project and local settings files, so standing autoMode grants never reach
the classifier; `--safe-mode` also drops CLAUDE.md, skills and plugins. A session that
holds an owner grant therefore must not launch with either flag, and must carry the grant
in `--settings` so it survives any settings-file change.

  fleet_autonomy.py show  <name>
  fleet_autonomy.py apply <name> --grant <name> [--apply]

`apply` is a dry run unless --apply is given; it backs the registry up first and never
touches uuid, cwd, profile, desired state or watches. Restart through fleet_reset.py.
"""
import argparse
import json
import os
import shutil
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
RT = os.path.expanduser('~/.hermes/scripts/cc-watch')
for _p in (HERE, os.path.dirname(HERE), RT):
    if _p not in sys.path:
        sys.path.insert(0, _p)

RESTRICTIVE = ('--safe-mode', '--restricted')

GRANTS_FILE = os.path.expanduser(os.environ.get(
    'HFM_GRANTS', '~/.config/hermes-fleet-manager/grants.json'))


def load_grants(path=None):
    """Named owner grants: {name: [statement, ...]}. They name real projects and clusters, so they
    live in a private JSON file outside the repository (see grants.example.json), never in code."""
    try:
        with open(path or GRANTS_FILE, encoding='utf-8') as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {}
    return {k: [str(x) for x in v] for k, v in data.items() if isinstance(v, list)}


GRANTS = load_grants()


def is_restricted(args):
    return any(a in RESTRICTIVE for a in (args or []))


def strip_restrictions(args):
    return [a for a in (args or []) if a not in RESTRICTIVE]


def _settings_pairs(args):
    """Indexes of `--settings <value>` pairs."""
    return [i for i, a in enumerate(args) if a == '--settings' and i + 1 < len(args)]


def auto_mode_allow(args):
    """Every autoMode.allow entry (minus $defaults) already carried in --settings JSON."""
    out = []
    for i in _settings_pairs(args or []):
        try:
            block = json.loads(args[i + 1]).get('autoMode', {})
        except (ValueError, AttributeError):
            continue
        out += [x for x in block.get('allow', []) if x != '$defaults' and x not in out]
    return out


def with_auto_mode(args, allow):
    """args plus one --settings autoMode block: $defaults first, then the unique allow entries.

    Idempotent. Replaces any earlier autoMode --settings pair; other --settings pairs stay.
    Only `allow` is ever written: hard_deny, soft_deny and environment are left to defaults.
    """
    args = list(args or [])
    keep = without_auto_mode(args)
    merged = []
    for x in auto_mode_allow(args) + list(allow or []):
        if x not in merged:
            merged.append(x)
    if not merged:
        return keep
    block = {'autoMode': {'allow': ['$defaults'] + merged}}
    return keep + ['--settings', json.dumps(block, separators=(',', ':'))]


def without_auto_mode(args):
    """args minus every autoMode-only --settings pair (what with_auto_mode adds)."""
    args = list(args or [])
    skip = set()
    for i in _settings_pairs(args):
        try:
            data = json.loads(args[i + 1])
        except ValueError:
            continue
        if isinstance(data, dict) and set(data) == {'autoMode'}:
            skip |= {i, i + 1}
    return [a for i, a in enumerate(args) if i not in skip]


def grant_args(args, grant):
    if grant not in GRANTS:
        raise ValueError('unknown grant: %s (define it in %s)' % (grant, GRANTS_FILE))
    return with_auto_mode(strip_restrictions(args), GRANTS[grant])


def check_unowned_restrictions(source, args, allow_restricted=False):
    """Refuse restriction flags the source session never had (a move must not add them)."""
    if is_restricted(args) and not is_restricted(source.get('args')) and not allow_restricted:
        raise ValueError('args add --safe-mode/--restricted the source never had; they hide '
                         'settings-file autoMode grants from the classifier. Remove them, or '
                         'pass --allow-restricted for a deliberate restriction')


def carry_over(source, target_args):
    """Target args for a provider move: the source's autoMode grants come along; restriction
    flags are kept only when the source itself had them (never added by the move)."""
    out = list(target_args or [])
    if not is_restricted(source.get('args')):
        out = strip_restrictions(out)
    allow = auto_mode_allow(source.get('args')) + list(source.get('auto_mode_allow') or [])
    return with_auto_mode(out, allow) if allow else out


def apply_grant(name, grant, write=False, evidence=None):
    import fleet_reg
    d = fleet_reg.load()
    rows = [e for e in d['sessions'] if e.get('name') == name]
    if len(rows) != 1:
        raise SystemExit('name must match exactly one registry row')
    row = rows[0]
    if row.get('provider', 'claude') != 'claude' or row.get('profile') != 'work':
        raise SystemExit('work-profile Claude sessions only')
    before = list(row.get('args') or [])
    after = grant_args(before, grant)
    report = {'name': name, 'uuid': row.get('uuid'), 'before': before, 'after': after,
              'changed': before != after}
    if write and before != after:
        backup = fleet_reg.REG + '.bak-autonomy-' + time.strftime('%Y%m%d-%H%M%S')
        shutil.copy2(fleet_reg.REG, backup)
        row['args'] = after
        row['auto_mode_allow'] = GRANTS[grant]
        fleet_reg.save(d)
        back = next(e for e in fleet_reg.load()['sessions'] if e['name'] == name)
        if back.get('args') != after or back.get('uuid') != row.get('uuid'):
            raise SystemExit('readback mismatch; restore ' + backup)
        report['backup'] = backup
    return report


def main(argv=None):
    ap = argparse.ArgumentParser(description=(__doc__ or '').split('\n\n')[0])
    sub = ap.add_subparsers(dest='cmd', required=True)
    s = sub.add_parser('show')
    s.add_argument('name')
    a = sub.add_parser('apply')
    a.add_argument('name')
    a.add_argument('--grant', required=True, choices=sorted(GRANTS))
    a.add_argument('--apply', action='store_true')
    ns = ap.parse_args(argv)
    if ns.cmd == 'show':
        import fleet_reg
        row = next(e for e in fleet_reg.load()['sessions'] if e.get('name') == ns.name)
        print(json.dumps({'restricted': is_restricted(row.get('args')),
                          'auto_mode_allow': auto_mode_allow(row.get('args')),
                          'args': row.get('args')}, indent=2))
    else:
        print(json.dumps(apply_grant(ns.name, ns.grant, write=ns.apply), indent=2))


if __name__ == '__main__':
    main()
