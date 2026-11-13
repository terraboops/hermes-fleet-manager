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
import datetime
import glob
import hashlib
import json
import os
import re
import subprocess
import sys
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
        # The session project directory usually follows this encoding. Prefer exact
        # matches before the permissive prefix fallback, which collides for names
        # such as `hiringloop` and `hiringloop-pr533`.
        loose = re.sub(r"[^A-Za-z0-9]", "-", os.path.expanduser(cwd))
        exact = [p for p in hits if os.path.basename(os.path.dirname(p)) == loose]
        if exact:
            hits = exact
        else:
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
    provider = entry.get("provider") or _provider(entry)
    if provider == "codex":
        return resolve_codex(entry)
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


def _provider(entry):
    """Prefer the registry's explicit provider; profile config may lag a canary."""
    explicit = entry.get("provider")
    if explicit:
        return explicit
    try:
        import fleet_harness
        return fleet_harness.resolve(entry).get("provider", "claude")
    except Exception:
        return "claude"


def resolve_codex(entry):
    """Resolve Codex's native rollout JSONL by authoritative thread id.

    We only accept a unique match. A partial trailing record is harmless because readers
    ignore malformed JSON lines; when no ID is known, cwd-based discovery must be unique.
    """
    resolved_home = None
    try:
        import fleet_harness
        resolved_home = fleet_harness.resolve(entry).get("env", {}).get("CODEX_HOME")
    except Exception:
        pass
    home = os.path.expanduser(entry.get("codex_home") or resolved_home or
                              entry.get("config_dir") or os.environ.get("CODEX_HOME", "~/.codex"))
    root = os.path.join(home, "sessions")
    paths = glob.glob(os.path.join(root, "**", "*.jsonl"), recursive=True)
    sid = entry.get("uuid") or entry.get("session_id")
    matches = []
    for path in paths:
        meta = codex_metadata(path)
        if not meta:
            continue
        # Subagent records inherit session_id from their parent; id is the
        # native thread identity when present. Older rollouts may lack id.
        if sid and (meta.get("id") or meta.get("session_id")) == sid:
            matches.append(path)
        elif not sid and entry.get("cwd") and os.path.realpath(meta.get("cwd", "")) == os.path.realpath(os.path.expanduser(entry["cwd"])):
            matches.append(path)
    if len(matches) == 1:
        return matches[0], "session_id" if sid else "unique_cwd"
    if len(matches) > 1:
        return None, "ambiguous"
    return None, "none"


def codex_metadata(path):
    """Read the first valid session_meta record from a Codex rollout."""
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                try:
                    obj = json.loads(line)
                except ValueError:
                    continue
                if obj.get("type") == "session_meta":
                    return obj.get("payload") or {}
    except OSError:
        pass
    return None


def record_role(obj):
    """Provider-neutral speaker name for Claude and Codex JSONL records."""
    msg = obj.get("message") or {}
    if msg.get("role"):
        return msg["role"]
    if obj.get("type") in ("user", "assistant"):
        return obj["type"]
    payload = obj.get("payload") or {}
    if obj.get("type") == "response_item" and payload.get("type") == "message":
        return payload.get("role")
    return None


def record_text(obj):
    msg = obj.get("message") or obj.get("payload") or {}
    content = msg.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(b.get("text", "") for b in content
                           if isinstance(b, dict) and b.get("type") in ("text", "output_text", "input_text"))
    return ""


def prior_assistant_context(path, token, window=2_000_000, event_key=None):
    """Assistant text since the prior inbound turn, before this emitted token.

    None means the marker or turn boundary was not found in the bounded window.
    Tool results and user text are never treated as assistant evidence.
    """
    try:
        with open(path, 'rb') as f:
            f.seek(0, os.SEEK_END)
            start = max(0, f.tell() - window)
            f.seek(start)
            data = f.read().decode('utf-8', 'replace')
    except (OSError, TypeError):
        return None
    rows = []
    seen = False
    for line in reversed(data.splitlines()):
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        role = record_role(obj)
        text = record_text(obj)
        if not seen:
            uid = obj.get('uuid') or (obj.get('message') or {}).get('id')
            key = uid or hashlib.sha256(line.encode('utf-8')).hexdigest()
            if (role == 'assistant' and (event_key is None or key == event_key) and any(
                    s.strip().strip('`* ').strip() == token for s in text.splitlines())):
                seen = True
            continue
        inbound = text if role == 'user' else obj.get('content') if obj.get('type') == 'queue-operation' else None
        if inbound:
            import obligations
            if not obligations.is_ladder_text(inbound):
                return '\n'.join(reversed(rows))
            continue
        if role == 'assistant' and text:
            rows.append(text)
    return None


def drift(entry, tmux=None):
    """True when the live process is in a different conversation than the registry claims."""
    if tmux is None:
        tmux = entry.get("name")
    live = pane_uuid(tmux) if tmux else None
    reg = entry.get("uuid")
    return bool(live and reg and live != reg)


def _registry_entry(tmux):
    reg = os.path.expanduser("~/.hermes/scripts/cc-watch/fleet_registry.json")
    try:
        with open(reg) as f:
            data = json.load(f)
    except Exception:
        return None
    entries = data.get("sessions", data) if isinstance(data, dict) else data
    if isinstance(entries, dict):
        entries = list(entries.values())
    for e in entries or []:
        if e.get("name") == tmux:
            return e
    return None


# ---------------------------------------------------------------------------
# RECENT INBOUND INSTRUCTIONS (2026-09-28)
#
# Why this lives here: the daemon's state fingerprint is what WAKES the
# overwatch, and that fingerprint was derived from the pane and a daemon log
# line only - it never carried the operator's own instructions. Real failure it
# exists for: a session's marketplace-plugin plan was parked as
# "not-yet-authorised" 24 minutes after Terra asked for it, because the agent
# watching the session had no visibility of her message. An agent cannot judge
# whether work is authorised without reading what the operator actually asked.
#
# Everything that arrives as a user turn counts, including a dispatch relayed on
# the operator's behalf: a relayed dispatch IS her instruction, and those are
# exactly the messages that were invisible. Only machine FRAMES are skipped:
# system reminders, hook output, command frames, compaction summaries and other
# agents' hand-backs, none of which are instructions from her.
# ---------------------------------------------------------------------------
_FRAME_PREFIXES = (
    "<system-reminder", "<local-command", "<command-name", "<command-message",
    "<task-notification", "<agent-message", "<user-prompt-submit-hook",
    "<post-tool-use", "<session-start-hook",
)
_FRAME_NEEDLES = (
    "[Subagent hand-back]",
    "<agent-message from=",
    "This session is being continued from a previous conversation",
)
# An image a turn carries is annotated "[Image: original WxH, displayed at ...]";
# when the turn holds nothing else, the harness records it as an isMeta turn
# companion (no image data, just the annotation). That is a machine frame about
# an image, never something the operator said: counting it as the newest inbound
# hides their real last instruction, so a watcher reads a phantom as the
# authorisation for whatever the session happens to be doing.
_IMAGE_COMPANION = re.compile(r"^\[Image: original \d+x\d+")
DEFAULT_INBOUND_WINDOW = 2_000_000


_PASTE_OPEN = re.compile(r"^<pasted_content\b[^>]*>\s*")
_PASTE_CLOSE = re.compile(r"\s*</pasted_content>\s*$")


def _clean_paste(raw):
    """Strip the wrapper Claude Code puts around a pasted instruction."""
    text = _PASTE_OPEN.sub("", raw)
    text = _PASTE_CLOSE.sub("", text)
    return " ".join(text.split()).strip()


def _inbound_text(obj):
    """The instruction text of an inbound record, or None if it is not one.

    TWO SHAPES, and both are the operator. A message typed at a FREE prompt is a
    `user` turn. A message pasted into a BUSY session - which is every dispatch
    the fleet sends - is recorded as a `queue-operation` whose `content` carries
    the paste (and whose matching `remove`/`absorbed_mid_turn` record says it was
    absorbed in-turn). Reading only user turns is why a dispatched instruction
    was invisible to the daemon even while the session was acting on it.
    """
    if record_role(obj) == "user":
        text = " ".join(record_text(obj).split()).strip()
    elif obj.get("type") == "queue-operation":
        raw = obj.get("content")
        text = _clean_paste(raw) if isinstance(raw, str) else ""
    else:
        msg = obj.get("message") or {}
        if msg.get("role") != "user":
            return None
        content = msg.get("content")
        parts = []
        if isinstance(content, str):
            parts = [content]
        elif isinstance(content, list):
            parts = [b.get("text", "") for b in content
                     if isinstance(b, dict) and b.get("type") == "text"]
        text = " ".join(" ".join(p.split()) for p in parts).strip()
        if text.startswith("<pasted_content"):
            text = _clean_paste(text)
    if not text:
        return None
    if obj.get("isMeta") and _IMAGE_COMPANION.match(text):
        return None
    if any(text[:200].startswith(p) for p in _FRAME_PREFIXES):
        return None
    if any(n in text[:400] for n in _FRAME_NEEDLES):
        return None
    return text


def _scan_inbound(path, n, window=DEFAULT_INBOUND_WINDOW, max_chars=200):
    """Last `n` inbound instructions in the tail window, oldest first.

    The window is bounded because transcripts reach hundreds of megabytes and
    the fingerprint is computed on every monitor tick.
    """
    out = []
    try:
        with open(path, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            fh.seek(max(0, size - window))
            chunk = fh.read()
    except OSError:
        return out
    for line in reversed(chunk.decode("utf-8", "replace").splitlines()):
        if not line.strip():
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            continue  # a truncated first line of the window
        text = _inbound_text(obj)
        if text is None:
            continue
        # An enqueue and its matching remove/absorbed_mid_turn carry the SAME
        # content; a repeat of the message just collected is not a new message.
        if out and out[-1][1] == text[:max_chars]:
            continue
        ts = obj.get("timestamp")
        epoch = None
        if ts:
            try:
                epoch = datetime.datetime.fromisoformat(
                    ts.replace("Z", "+00:00")).timestamp()
            except ValueError:
                epoch = None
        out.append((epoch, text[:max_chars]))
        if len(out) >= n:
            break
    out.reverse()
    return out


def newest_inbound(path, max_chars=200):
    """(epoch, text) of the newest inbound instruction, or (None, None)."""
    hits = _scan_inbound(path, 1, max_chars=max_chars)
    return hits[-1] if hits else (None, None)


def recent_inbound(path, n=3, max_chars=200):
    """The last n inbound instructions, oldest first. [(epoch, text), ...]"""
    return _scan_inbound(path, n, max_chars=max_chars)


def main(argv=None):
    """CLI so the shell dispatcher never has to re-derive a transcript path.

    resolve <session>  ->  "<path>\\t<source>\\t<drift>"
    An unresolved transcript prints an empty path with source 'none', which the
    caller must treat as UNKNOWN rather than as failure.
    """
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] != "resolve" or len(argv) < 2:
        print("usage: fleet_transcript.py resolve <session>", file=sys.stderr)
        return 2
    tmux = argv[1]
    entry = _registry_entry(tmux)
    if entry is None:
        print("\tnone\tno-registry-entry")
        return 0
    path, source = resolve(entry, tmux=tmux)
    print("%s\t%s\t%s" % (path or "", source, "drift" if drift(entry, tmux=tmux) else "ok"))
    return 0


if __name__ == "__main__":
    import sys
    raise SystemExit(main())
