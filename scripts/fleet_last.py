#!/usr/bin/env python3
"""What a session last SAID — the words, not just the state.

Reads the tail of a registered session's own transcript and prints its most recent
assistant messages, plus the newest sentinel token it emitted.

Complements `fleet_state.py`: that emits a one-line deterministic fingerprint (used as
a cron `monitor` to gate an overwatch, so it must stay free of volatile text) and
answers WHICH STATE a session is in. This answers WHAT IT SAID. Reading a session's own
account of its work should not need a bespoke transcript parser written on the spot —
which is the thing this replaces.

It reads only the tail of the file (default 4 MB), so it stays fast on a transcript of
several hundred megabytes, and it drops the first partial line when it seeks mid-file.

Usage:  fleet_last.py <tmux-session-name> [--messages N] [--bytes N]
Exit:   0 ok · 2 unknown session or no transcript found.
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    import fleet_harness as _harness
except Exception:  # keep working as a plain script even without the harness module
    _harness = None

DEFAULT_BYTES = 4_000_000
TOKEN = re.compile(r"(?:DONE|NEEDS-INPUT|STALLED|CHILD-DONE)-[A-Za-z0-9_.:-]+")


def _registry_path() -> str:
    cfg = {}
    if _harness is not None:
        try:
            cfg = _harness.load()
        except Exception:
            cfg = {}
    return os.path.expanduser(
        cfg.get("registry_file", "~/.hermes/scripts/cc-watch/fleet_registry.json"))


def _entry(session: str) -> dict | None:
    d = json.load(open(_registry_path()))
    entries = d.get("sessions", d if isinstance(d, list) else [])
    for e in entries:
        if session in (e.get("name"), e.get("short")):
            return e
    return None


def _transcript(e: dict) -> str | None:
    """<config_dir>/projects/<cwd-slug>/<uuid>.jsonl, then a glob fallback."""
    cfg_dir = os.path.expanduser(str(e.get("config_dir") or ""))
    uuid = e.get("uuid") or ""
    if not uuid:
        return None
    if cfg_dir:
        slug = re.sub(r"[^A-Za-z0-9]", "-", os.path.expanduser(str(e.get("cwd") or "")))
        cand = os.path.join(cfg_dir, "projects", slug, f"{uuid}.jsonl")
        if os.path.exists(cand):
            return cand
        hits = glob.glob(os.path.join(cfg_dir, "projects", "*", f"{uuid}.jsonl"))
        if hits:
            return max(hits, key=os.path.getmtime)
    return None


def _tail(path: str, nbytes: int) -> list[str]:
    size = os.path.getsize(path)
    with open(path, "rb") as f:
        if size > nbytes:
            f.seek(size - nbytes)
            chunk = f.read().decode("utf-8", "replace")
            return chunk.splitlines()[1:]      # first line is a partial record
        return f.read().decode("utf-8", "replace").splitlines()


def main() -> int:
    p = argparse.ArgumentParser(description="Print a session's most recent messages.")
    p.add_argument("session")
    p.add_argument("--messages", type=int, default=3, help="how many to print (default 3)")
    p.add_argument("--bytes", type=int, default=DEFAULT_BYTES, help="tail window in bytes")
    a = p.parse_args()

    e = _entry(a.session)
    if not e:
        print(f"{a.session} is not in the registry ({_registry_path()})", file=sys.stderr)
        return 2
    path = _transcript(e)
    if not path or not os.path.exists(path):
        print(f"no transcript found for {a.session} (uuid={e.get('uuid')!r})", file=sys.stderr)
        return 2

    texts, tools, tokens = [], [], []
    for line in _tail(path, a.bytes):
        try:
            d = json.loads(line)
        except Exception:
            continue
        m = d.get("message") or {}
        if m.get("role") != "assistant":
            # ROLE-FILTER, for the same reason the daemon has one: a dispatched
            # instruction asks the session to reply with a token, so that token is
            # present as a USER turn the moment it is sent. Scanning every line
            # reports a token the session never emitted.
            continue
        c = m.get("content")
        if isinstance(c, list):
            for b in c:
                if b.get("type") == "text" and (b.get("text") or "").strip():
                    t = b["text"].strip()
                    # A transcript can carry the same turn more than once; a repeat sitting
                    # adjacent to its twin is not a second message.
                    if not texts or texts[-1] != t:
                        texts.append(t)
                    tokens += TOKEN.findall(t)
                elif b.get("type") == "tool_use":
                    inp = json.dumps(b.get("input") or {})
                    tools.append(f"{b.get('name')}: {inp[:120]}")

    print(f"{a.session}  ({e.get('profile', '?')})  cwd={e.get('cwd', '')}  "
          f"uuid={str(e.get('uuid'))[:8]}")
    print(f"transcript: {path} ({os.path.getsize(path)/1e6:.1f} MB)")
    if tokens:
        print(f"newest token: {tokens[-1]}")
    if tools:
        print("\nrecent tool calls (oldest first):")
        for t in tools[-6:]:
            print(f"  {t}")
    print(f"\nlast {min(a.messages, len(texts))} message(s), oldest first:")
    for t in texts[-a.messages:]:
        print("  ---")
        for ln in t.splitlines():
            print(f"  {ln}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
