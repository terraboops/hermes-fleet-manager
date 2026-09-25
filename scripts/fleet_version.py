#!/usr/bin/env python3
"""Know which CLI version each fleet session is running, and which version is newest.

Why this exists
---------------
Claude Code installs as versioned binaries under `<install>/versions/<ver>` with a
`claude` symlink pointing at one of them. The symlink is NOT a reliable statement of
what a session runs:

  * a running process keeps the binary it was started with, so sessions legitimately
    differ while they are up;
  * the symlink gets rewritten underneath them (an update, or a channel move) with no
    effect on those processes;
  * the CLI's own status bar only renders the version when the pane is wide enough,
    and a transcript's `version` field only updates when that session writes a turn.

So the running version is read from the process itself, and the newest version is
resolved from several sources with the highest winning.

Channel hazard (learned the hard way, 2026-09-25)
-------------------------------------------------
The published dist-tags were `stable: 2.1.274` and `latest: 2.1.282`. Anything that
resolves "newest" through the `stable` tag silently moves the install BACKWARDS.
This module never consults `stable`; `latest` is the newest published, and it is
cross-checked against versions already present locally.

Sources for "newest", in order of trust (highest version wins across all that answer):
  1. `npm view @anthropic-ai/claude-code dist-tags.latest`  - what the vendor publishes
  2. the highest version already present in the local install store
  3. a cached last-known value, so a network failure cannot silently downgrade us
The winning source is always reported, so a surprising answer can be traced.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

VERSION_RE = re.compile(r"(\d+)\.(\d+)\.(\d+)")


def _ver_tuple(text: str) -> tuple[int, int, int] | None:
    m = VERSION_RE.search(text or "")
    return (int(m.group(1)), int(m.group(2)), int(m.group(3))) if m else None


def _ver_key(text: str) -> tuple[int, int, int]:
    """Sort key for versions; unparseable sorts lowest instead of blowing up."""
    return _ver_tuple(text) or (0, 0, 0)


def install_roots() -> list[Path]:
    """Every place a native install keeps its versioned binaries."""
    home = Path.home()
    return [
        home / ".local/share/claude/versions",
        home / ".claude/local/versions",
        Path("/opt/homebrew/lib/node_modules/@anthropic-ai/claude-code"),
    ]


def local_versions() -> list[str]:
    """Versions already present on this machine (cached, no network)."""
    found: set[str] = set()
    for root in install_roots():
        if not root.is_dir():
            continue
        for child in root.iterdir():
            t = _ver_tuple(child.name)
            if t:
                found.add(".".join(str(x) for x in t))
    return sorted(found, key=_ver_key)


def local_bin(version: str) -> Path | None:
    """Absolute path to a specific installed version, or None."""
    for root in install_roots():
        cand = root / version
        if cand.exists():
            return cand
    return None


def newest_local() -> str | None:
    vs = local_versions()
    return vs[-1] if vs else None


def _cache_path() -> Path:
    return Path(os.path.expanduser("~/.hermes/cache/fleet-cli-latest.json"))


def published_latest(timeout: int = 20) -> tuple[str | None, str]:
    """Ask the registry what `latest` is. Returns (version, source)."""
    try:
        out = subprocess.run(
            ["npm", "view", "@anthropic-ai/claude-code", "dist-tags", "--json"],
            capture_output=True, text=True, timeout=timeout,
        )
        if out.returncode == 0 and out.stdout.strip():
            tags = json.loads(out.stdout)
            v = (tags.get("latest") or "").strip()
            t = _ver_tuple(v)
            if t:
                # NOTE: deliberately not 'stable'. See the channel hazard above.
                return ".".join(str(x) for x in t), "npm:latest"
    except Exception:
        pass
    return None, "npm:unavailable"


def _read_cache() -> str | None:
    try:
        return json.loads(_cache_path().read_text()).get("version")
    except Exception:
        return None


def newest_bin() -> Path | None:
    """Absolute path to the newest installed CLI, for launching new sessions.

    A caller that launches through this path cannot be dragged backwards by the
    `claude` symlink being repointed (an update, a channel move), which is the
    failure that silently downgraded the whole fleet on 2026-09-25.
    Respects FLEET_CLAUDE_BIN as an explicit pin.
    """
    pin = os.environ.get("FLEET_CLAUDE_BIN")
    if pin:
        if Path(pin).exists():
            return Path(pin)
        # An explicit pin that does not exist is an operator error. Say so loudly rather
        # than silently substituting something else, which would make the pin a no-op the
        # operator believes is in force.
        print(f"fleet_version: FLEET_CLAUDE_BIN={pin} does not exist; "
              f"falling back to the newest local version", file=sys.stderr)
    nl = newest_local()
    if not nl:
        return None
    b = local_bin(nl)
    return Path(b) if b else None


# Higher wins when several sources report the same version, so the reported source
# is the most authoritative one rather than whichever happened to be appended last.
_SOURCE_RANK = {"npm:latest": 3, "local-store": 2, "cache": 1}


def _write_cache(version: str, source: str) -> None:
    p = _cache_path()
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({"version": version, "source": source}))
    except Exception:
        pass


def latest_version(use_network: bool = True) -> tuple[str | None, str]:
    """The newest version we can establish, with the source that won.

    Every source is consulted and the highest wins, so one stale or failed source
    cannot fake a downgrade. A network answer is cached so the next run survives
    being offline.
    """
    candidates: list[tuple[tuple[int, int, int], str, str]] = []

    if use_network:
        v, src = published_latest()
        if v:
            t = _ver_tuple(v)
            if t:
                candidates.append((t, v, src))

    nl = newest_local()
    if nl:
        t = _ver_tuple(nl)
        if t:
            candidates.append((t, nl, "local-store"))

    cached = _read_cache()
    if cached:
        t = _ver_tuple(cached)
        if t:
            candidates.append((t, cached, "cache"))

    if not candidates:
        return None, "no-source"
    # Highest version wins. Among equal versions the most authoritative source wins,
    # so the reported source is trustworthy rather than an accident of append order.
    candidates.sort(key=lambda c: (c[0], _SOURCE_RANK.get(c[2], 0)))
    top = candidates[-1]
    _write_cache(top[1], top[2])
    return top[1], top[2]


def _pid_of(session: str) -> int | None:
    """The claude PID inside a tmux session, if it can be determined."""
    try:
        out = subprocess.run(
            ["tmux", "list-panes", "-t", f"={session}", "-F", "#{pane_pid}"],
            capture_output=True, text=True, timeout=10,
        )
        pane_pid = out.stdout.strip().splitlines()[0] if out.stdout.strip() else None
    except Exception:
        pane_pid = None

    # Prefer a matched claude process amongst the pane and its descendants.
    try:
        out = subprocess.run(["pgrep", "-x", "claude"], capture_output=True, text=True, timeout=10)
        pids = [int(x) for x in out.stdout.split()]
    except Exception:
        return None
    if not pids:
        return None

    if pane_pid:
        try:
            ps = subprocess.run(
                ["ps", "-eo", "pid,ppid"], capture_output=True, text=True, timeout=10,
            ).stdout
            parent: dict[int, int] = {}
            for line in ps.splitlines()[1:]:
                parts = line.split()
                if len(parts) >= 2:
                    parent[int(parts[0])] = int(parts[1])
            for pid in pids:
                walk, guard = pid, 0
                while walk and guard < 40:
                    if walk == int(pane_pid):
                        return pid
                    walk = parent.get(walk, 0)
                    guard += 1
        except Exception:
            pass
    return None


def running_version(session: str) -> tuple[str | None, str]:
    """Version a session's claude process is EXECUTING, with how it was found.

    Primary source is the mapped executable of the live process, which is the only
    thing that is true regardless of symlink churn or pane width.
    """
    pid = _pid_of(session)
    if pid:
        try:
            out = subprocess.run(
                ["lsof", "-p", str(pid), "-a", "-d", "txt"],
                capture_output=True, text=True, timeout=15,
            )
            for line in out.stdout.splitlines()[1:]:
                t = _ver_tuple(os.path.basename(line.split()[-1]) if line.split() else "")
                if t and "versions" in line:
                    return ".".join(str(x) for x in t), "proc-exec"
        except Exception:
            pass

    # Fallback: the newest version a transcript recorded (stale until it writes again).
    return None, "unknown"


def registry_path(explicit: str | None = None) -> str:
    """Find the registry wherever we were invoked from.

    The CLIs normally run inside the fleet scripts dir (cwd-relative works), but this
    module is also imported from the repo root, so a bare relative name silently finds
    nothing and reports an empty fleet. Try the obvious places in order.
    """
    if explicit:
        return explicit
    import os as _os
    here = _os.path.dirname(_os.path.abspath(__file__))
    for cand in (
        "fleet_registry.json",
        _os.path.join(here, "fleet_registry.json"),
        _os.path.expanduser("~/.hermes/scripts/cc-watch/fleet_registry.json"),
    ):
        if _os.path.exists(cand):
            return cand
    return "fleet_registry.json"


def report(path: str | None = None) -> dict:
    latest, latest_src = latest_version()
    try:
        reg = json.load(open(registry_path(path)))
    except Exception:
        reg = {"sessions": []}
    rows = []
    for e in reg.get("sessions", []):
        ver, src = running_version(e["name"])
        rows.append({
            "name": e["name"],
            "profile": e.get("profile"),
            "version": ver,
            "source": src,
            "latest": latest,
            "up_to_date": (ver == latest) if (ver and latest) else None,
        })
    return {"latest": latest, "latest_source": latest_src, "sessions": rows}


def main() -> int:
    cmd = sys.argv[1] if len(sys.argv) > 1 else "report"

    if cmd == "latest":
        v, src = latest_version()
        print(f"{v} (source: {src})")
        return 0 if v else 1

    if cmd == "of":
        if len(sys.argv) < 3:
            print("usage: fleet_version.py of <session>", file=sys.stderr)
            return 2
        v, src = running_version(sys.argv[2])
        print(f"{v or 'unknown'} (source: {src})")
        return 0 if v else 1

    if cmd == "report":
        d = report()
        print(f"latest: {d['latest']} (source: {d['latest_source']})")
        print()
        behind = [r for r in d["sessions"] if r["up_to_date"] is False]
        for r in sorted(d["sessions"], key=lambda r: (r["version"] or "", r["name"])):
            mark = "" if r["up_to_date"] is not False else "   <-- behind"
            print(f"  {r['name']:<24} {r['profile'] or '':<9} {r['version'] or 'unknown':<10}{mark}")
        print()
        print(f"behind: {len(behind)} of {len(d['sessions'])}")
        return 0

    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main())
