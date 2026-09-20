#!/usr/bin/env python3
"""
fleet_harness.py - how a managed session is LAUNCHED, expressed as config.

WHY THIS EXISTS
The launchers used to hardcode two things: a `personal`/`work` name -> config_dir
map, and the command itself (`CLAUDE_CONFIG_DIR=<cfg> claude --remote-control`).
Both were wrong to fix in code. A fleet can run any harness, and any harness can
take arbitrary flags and arbitrary environment variables. So nothing about the
launch is a name the code knows: it is data.

A profile (a "launch spec") may declare:
    command:      the harness executable                    (default: claude)
    args:         arbitrary extra flags, always passed      (default: [])
    env:          arbitrary environment variables           (default: {})
    config_dir:   shortcut for env.CLAUDE_CONFIG_DIR
    resume_flag:  flag used to resume a session by id       (default: --resume)

A session entry may declare any of the same keys, and they WIN over the
profile's. `config_dir` on either level is honoured as a shortcut, so registries
that predate this module keep working untouched.

Values may reference the environment: `~`, `$VAR` and `${VAR}` are expanded, so
a spec can say `CLAUDE_CONFIG_DIR: ${MY_PROFILE_DIR}` and stay portable.

Config is read from the file named by the FLEET_CONFIG env var, else
./fleet_config.json, else ./config.yaml next to this module; JSON and YAML are
both accepted.
"""
from __future__ import annotations

import json
import os
import re

HERE = os.path.dirname(os.path.abspath(__file__))
_ENV_VAR = re.compile(r'\$\{([A-Za-z_][A-Za-z0-9_]*)\}|\$([A-Za-z_][A-Za-z0-9_]*)')
_NOT_SET = object()


def _candidate_paths() -> list[str]:
    """Config lookup order. FLEET_CONFIG wins; otherwise look beside this module and one
    level up, so the same file is found whether the tool is invoked by its repo path or
    through a symlink from a runtime directory."""
    out = []
    env = os.environ.get('FLEET_CONFIG')
    if env:
        out.append(os.path.expanduser(env))
    for base in (HERE, os.path.dirname(HERE)):
        out += [os.path.join(base, 'fleet_config.json'),
                os.path.join(base, 'config.yaml'),
                os.path.join(base, 'config.yml'),
                os.path.join(base, 'config.json')]
    return out


def candidate_paths() -> list[str]:
    """Where a config file is looked for, in order. Public: other tools share it so
    they cannot drift from this module's discovery."""
    return _candidate_paths()


def load(path: str | None = None) -> dict:
    """Load the fleet config. JSON or YAML; first readable candidate wins."""
    paths = [os.path.expanduser(path)] if path else _candidate_paths()
    for p in paths:
        if not os.path.isfile(p):
            continue
        try:
            text = open(p, errors='replace').read()
        except OSError:
            continue
        if p.endswith(('.yaml', '.yml')):
            try:
                import yaml
                return yaml.safe_load(text) or {}
            except Exception:
                continue
        try:
            return json.loads(text)
        except Exception:
            continue
    return {}


def expand(value: str) -> str:
    """Expand ~ and $VAR / ${VAR} in a config string."""
    if not isinstance(value, str):
        return value

    def sub(m):
        return os.environ.get(m.group(1) or m.group(2), '')

    return os.path.expanduser(_ENV_VAR.sub(sub, value))


def _as_list(v) -> list:
    if v is None:
        return []
    if isinstance(v, str):
        return [v]
    return list(v)


def spec(cfg: dict, profile: str | None) -> dict:
    """The launch spec for a profile name. Accepts either schema shape:
       profiles: {name: {...}}   or   profiles: [{name: ..., ...}, ...]"""
    profiles = cfg.get('profiles') or {}
    if isinstance(profiles, dict):
        return dict(profiles.get(profile or '', {}) or {})
    for entry in profiles:
        if isinstance(entry, dict) and entry.get('name') == profile:
            return dict(entry)
    return {}


def resolve(entry: dict, cfg: dict | None = None, profile: str | None = None) -> dict:
    """Merge a session entry over its profile spec into one launch description.

    Returns {'command': str, 'args': [str], 'env': {k: v}, 'resume_flag': [str],
             'config_dir': str|None}. Session values always win.
    """
    cfg = cfg if cfg is not None else load()
    prof = spec(cfg, profile or entry.get('profile'))

    command = entry.get('command') or prof.get('command') or 'claude'

    args = []
    for src in (prof.get('args'), entry.get('args')):
        args += [expand(a) for a in _as_list(src)]

    env = {}
    for src in (prof.get('env'), entry.get('env')):
        if isinstance(src, dict):
            env.update({str(k): expand(str(v)) for k, v in src.items()})

    # config_dir is a shortcut for the harness's config-dir env var. It never
    # overrides an explicit env entry of the same name.
    config_dir = entry.get('config_dir') or prof.get('config_dir') or None
    if config_dir:
        config_dir = expand(config_dir)
        env.setdefault('CLAUDE_CONFIG_DIR', config_dir)

    resume_flag = entry.get('resume_flag') or prof.get('resume_flag') or '--resume'
    return {
        'command': command,
        'args': args,
        'env': env,
        'resume_flag': _as_list(resume_flag),
        'config_dir': config_dir,
    }


def shell_line(entry: dict, resume: str | None = None, extra_args=None,
               cfg: dict | None = None) -> str:
    """A single shell command string that launches (or resumes) the session.

    Built from the resolved spec, so any harness, any flags, any env vars.
    Values are single-quoted; a literal single quote is escaped.
    """
    r = resolve(entry, cfg=cfg)

    def q(s: str) -> str:
        return "'" + str(s).replace("'", "'\\''") + "'"

    parts = []
    for k, v in r['env'].items():
        parts.append(f'{k}={q(v)}')
    parts.append(r['command'])
    parts += [q(a) for a in r['args']]
    parts += [q(a) for a in _as_list(extra_args)]
    if resume:
        parts += [q(f) for f in r['resume_flag']] + [q(resume)]
    return ' '.join(parts)


def config_dirs(cfg: dict | None = None) -> dict:
    """profile name -> resolved config_dir, for callers that only need the dir.

    Handles both schema shapes: the name is the KEY in `profiles: {name: {...}}`,
    and a FIELD in `profiles: [{name: ..., ...}, ...]`."""
    cfg = cfg if cfg is not None else load()
    out = {}
    profiles = cfg.get('profiles') or {}
    if isinstance(profiles, dict):
        items = [(k, v) for k, v in profiles.items() if isinstance(v, dict)]
    else:
        items = [(v.get('name'), v) for v in profiles if isinstance(v, dict)]
    for name, entry in items:
        cd = entry.get('config_dir')
        if name and cd:
            out[name] = expand(cd)
    return out


if __name__ == '__main__':
    import sys
    if len(sys.argv) > 1:
        entry = json.loads(sys.argv[1])
        print(shell_line(entry, resume=(sys.argv[2] if len(sys.argv) > 2 else None)))
    else:
        cfg = load()
        print(json.dumps({'profiles': config_dirs(cfg)}, indent=2))
