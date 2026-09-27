#!/usr/bin/env python3
"""Locate a Claude Code session's transcript, robustly.

The naive way - rebuild the project-directory name from the session's cwd - is
fragile: the directory name is derived by replacing every non-alphanumeric
character in the cwd with a dash, and that rule silently broke once already
(dots were not replaced, so a checkout of terraatauri.com resolved to a path
that never exists). Anything that reconstructs a path from a string it does not
own will drift again.

So this module never derives a path from the cwd. Three sources, best first:

  1. UUID GLOB - the uuid alone is unique:
     <config_dir>/projects/*/<uuid>.jsonl
     Immune to the naming scheme, because it does not care what the directory
     is called.

  2. LIVE PROCESS - the pane's claude process carries `--resume <uuid>` (or
     `--session-id <uuid>`) in its command line. That is the conversation the
     session is actually in RIGHT NOW, so it outranks a registry entry that may
     predate a restart or a resume.

  3. NEWEST FILE - only when no uuid is known at all: the most recently
     modified transcript in the project directory.

Ambiguity is reported, never hidden: if the glob matches more than one file the
newest wins and the caller is told, because "I picked one" is a different claim
from "there is one".
"""
import glob
import os
import re
import subprocess
import time

UUID_RE = re.compile(r"--(?:resume|session-id)[=\s]+([0-9a-fA-F-]{36})")
_CACHE = {}


def pane_uuid(tmux, timeout=10):
    """The uuid the session's live process is running, or None.

    Reads the pane's own process first, then its children (claude may be exec'd
    under a shell), and searches only command lines - never the pane contents,
    which scroll.
    """
    try:
        pid = subprocess.run(
            ["tmux", "list-panes", "-t", "=" + tmux, "-F", "#{pane_pid}"],
            capture_output=True, text=True, timeout=timeout,
        ).stdout.strip()
    except Exception:
        return None
    if not pid:
        return None
    pids = [pid]
    try:
        pids += subprocess.run(["pgrep", "-P", pid], capture_output=True, text=True,
                               timeout=timeout).stdout.split()
    except Exception:
        pass
    for p in pids:
        try:
            cmd = subprocess.run(["ps", "-o", "command=", "-p", p],
                                 capture_output=True, text=True, timeout=timeout).stdout
        except Exception:
            continue
        m = UUID_RE.search(cmd or "")
        if m:
            return m.group(1)
    return None


def by_uuid(config_dir, uuid):
    """Every transcript that matches the uuid, newest first."""
    if not config_dir or not uuid:
        return []
    pat = os.path.join(os.path.expanduser(config_dir), "projects", "*", uuid + ".jsonl")
    return sorted(glob.glob(pat), key=os.path.getmtime, reverse=True)


def newest_in_project(config_dir, cwd):
    """Fallback with no uuid: newest transcript in the cwd's project dir.

    Uses a glob over the project dirs rather than a reconstructed name, for the
    same reason as everywhere else here.
    """
    if not config_dir:
        return None
    root = os.path.join(os.path.expanduser(config_dir), "projects")
    hits = [p for p in glob.glob(os.path.join(root, "*", "*.jsonl"))
            if os.path.getmtime(p) > 0]
    if not hits:
        return None
    if cwd:
        # Prefer any directory whose name matches the cwd loosely.
        loose = re.sub(r"[^A-Za-z0-9]", "-", os.path.expanduser(cwd))
        scoped = [p for p in hits if loose in os.path.dirname(p)]
        if scoped:
            hits = scoped
    return max(hits, key=os.path.getmtime)


def resolve(entry, tmux=None, use_cache=True):
    """Resolve a registry entry to (path, source).

    source is one of: 'uuid', 'process', 'newest', 'none'. The path is None when
    nothing was found - callers must treat that as "unknown", not as "not
    delivered".
    """
    cfg = entry.get("config_dir") or ""
    uuid = entry.get("uuid") or ""
    cwd = entry.get("cwd") or ""
    if tmux is None:
        tmux = entry.get("name")
    key = (cfg, uuid, tmux)
    if use_cache:
        hit = _CACHE.get(key)
        if hit and hit[0] and os.path.exists(hit[0]):
            return hit

    # 1. the uuid alone
    hits = by_uuid(cfg, uuid)
    if hits:
        out = (hits[0], "uuid")
        if use_cache:
            _CACHE[key] = out
        return out

    # 2. the live process, which may know a uuid the registry does not
    if tmux:
        live = pane_uuid(tmux)
        if live and live != uuid:
            hits = by_uuid(cfg, live)
            if hits:
                out = (hits[0], "process")
                if use_cache:
                    _CACHE[key] = out
                return out

    # 3. newest in the project dir, only as a last resort
    fallback = newest_in_project(cfg, cwd)
    out = (fallback, "newest") if fallback else (None, "none")
    if use_cache:
        _CACHE[key] = out
    return out


def drift(entry, tmux=None):
    """True when the live process is in a different conversation than the registry claims."""
    if tmux is None:
        tmux = entry.get("name")
    live = pane_uuid(tmux) if tmux else None
    reg = entry.get("uuid")
    return bool(live and reg and live != reg)
