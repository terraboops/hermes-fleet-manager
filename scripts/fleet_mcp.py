#!/usr/bin/env python3
"""Ensure the MCP servers a fleet session needs exist in its Claude Code profile.

WHY THIS EXISTS
The launchers (`fleet_layout.py resume`, `restart_fleet_sessions.py`) start sessions as a bare
`CLAUDE_CONFIG_DIR=<cfg> claude --remote-control`. They therefore ASSUME the profile already has
every MCP server the session needs. A missing server is completely silent — the session just can't
reach the tool and no error surfaces. Repro (2026-09-17): the example session was doing the example-mcp
end-to-end test pass with no `example-mcp` MCP registered, discovered it mid-task, and had to ask the
human for the add commands by hand.

USAGE
    fleet_mcp.py status [profile|config_dir]     # what's registered vs expected
    fleet_mcp.py ensure [profile|config_dir]     # add anything missing (idempotent)

Launchers call `ensure_for(cfg)` directly before they launch sessions.

SAFETY
- `ensure` NEVER re-adds or overwrites an existing server, so it cannot churn or rotate a working
  entry (the `claude mcp add` CLI has no --force and would otherwise re-embed env values).
- Tokens are read from FILES at add time (`env_files`); no secret is stored in this repo or in the
  registry below — only the path to read it from.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys

HOME = os.path.expanduser("~")

# ---------------------------------------------------------------------------
# Registry: which servers each profile's sessions require.
# Add a profile entry here (or a server under it) and every launch ensures it.
#   command      : executable for a stdio server
#   args         : argv after the command
#   env          : literal (non-secret) environment values
#   env_files    : env var -> file whose CONTENTS become the value (secrets)
#   scope        : claude mcp add -s <scope>  (default "user")
# ---------------------------------------------------------------------------
PROFILES: dict[str, dict] = {
    "work": {
        "config_dir": f"{HOME}/.claude-example-a",
        "servers": {
            "example-mcp": {
                "command": "node",
                "args": [f"{HOME}/Developer/example-mcp/src/mcp/stdio.ts"],
                "env": {"EXAMPLE_API_URL": "https://example.ts.net"},
                "env_files": {"EXAMPLE_TOKEN": f"{HOME}/.config/example-mcp/token"},
                "scope": "user",
            },
        },
    },
    "personal": {
        "config_dir": f"{HOME}/.claude-example-b",
        "servers": {},
    },
}

# config_dir -> profile name (reverse lookup, so callers can pass a path)
_BY_DIR = {os.path.expanduser(p["config_dir"]): name for name, p in PROFILES.items()}


def resolve(target: str | None) -> tuple[str | None, str]:
    """Return (profile_name, config_dir) for a profile name, a path, or None."""
    if not target:
        return None, f"{HOME}/.claude-example-a"
    if target in PROFILES:
        return target, os.path.expanduser(PROFILES[target]["config_dir"])
    path = os.path.expanduser(target)
    return _BY_DIR.get(path), path


def _config_path(cfg: str) -> str:
    return os.path.join(os.path.expanduser(cfg), ".claude.json")


def registered(cfg: str) -> set[str]:
    """Server names currently registered for this config dir (empty if no file yet)."""
    path = _config_path(cfg)
    if not os.path.exists(path):
        return set()
    try:
        with open(path) as fh:
            return set((json.load(fh).get("mcpServers") or {}).keys())
    except (json.JSONDecodeError, OSError):
        return set()


def _add(cfg: str, name: str, spec: dict) -> tuple[bool, str]:
    """Run `claude mcp add` for one server. Returns (ok, message)."""
    cmd = ["claude", "mcp", "add", "-s", spec.get("scope", "user"), name]
    for key, val in (spec.get("env") or {}).items():
        cmd += ["-e", f"{key}={val}"]
    for key, path in (spec.get("env_files") or {}).items():
        resolved = os.path.expanduser(path)
        if not os.path.exists(resolved):
            return False, f"{name}: env file missing for {key} ({resolved})"
        try:
            value = open(resolved).read().strip()
        except OSError as exc:
            return False, f"{name}: cannot read {resolved}: {exc}"
        if not value:
            return False, f"{name}: empty secret file {resolved}"
        cmd += ["-e", f"{key}={value}"]
    cmd += ["--", spec["command"], *spec.get("args", [])]

    env = dict(os.environ, CLAUDE_CONFIG_DIR=os.path.expanduser(cfg))
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=120, env=env)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, f"{name}: add failed: {exc}"
    if r.returncode != 0:
        return False, f"{name}: add exited {r.returncode}: {(r.stderr or r.stdout).strip()[:200]}"
    return True, f"{name}: added"


def ensure_for(cfg: str, quiet: bool = True) -> list[str]:
    """Ensure every registered server for this config dir exists. Idempotent.

    Returns the list of server names that were actually ADDED (so callers can log a delta).
    Never raises: a provisioning problem must not block launching the fleet.
    """
    cfg = os.path.expanduser(cfg)
    profile, _ = resolve(cfg)
    if not profile:
        return []  # unknown profile: nothing declared, nothing to do
    have = registered(cfg)
    added: list[str] = []
    for name, spec in PROFILES[profile]["servers"].items():
        if name in have:
            continue
        ok, msg = _add(cfg, name, spec)
        if ok:
            added.append(name)
        if not quiet:
            print(f"  {'ok ' if ok else 'ERR'} {msg}")
    return added


def status(target: str | None = None) -> int:
    profile, cfg = resolve(target)
    print(f"config_dir: {cfg}")
    print(f"profile:    {profile or '(not in registry — no servers declared)'}")
    if not profile:
        print(f"registered: {sorted(registered(cfg))}")
        return 0
    have = registered(cfg)
    for name in sorted(PROFILES[profile]["servers"]):
        print(f"  {'PRESENT' if name in have else 'MISSING'}  {name}")
    extra = sorted(have - set(PROFILES[profile]["servers"]))
    if extra:
        print(f"  (also registered, not managed here: {extra})")
    return 0


def main() -> int:
    if len(sys.argv) < 2 or sys.argv[1] not in ("status", "ensure"):
        print(__doc__)
        return 2
    action, target = sys.argv[1], (sys.argv[2] if len(sys.argv) > 2 else None)
    if action == "status":
        return status(target)
    added = ensure_for(resolve(target)[1], quiet=False)
    print(f"ensure complete; added: {added or 'nothing (already present)'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
