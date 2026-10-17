"""Does an armed session owe work, or is it parked at a finished task?

An idle session is not a stalled one. A session that reported DONE and has had no instruction since
is finished, and nudging it is noise. A session with an instruction newer than its last DONE, or one
that never reported at all, still owes something. This reads the transcript only.

The operator's instructions are the user turns and queued pastes that are not the fleet's own
ladder text; a nudge must not count as new work, or every nudge would reopen the obligation it was
sent about.
"""
import datetime
import json
import os
import re
import sys

_SCRIPTS = os.path.join(os.path.dirname(os.path.realpath(__file__)), "scripts")
if _SCRIPTS not in sys.path:
    sys.path.insert(0, _SCRIPTS)

DONE_TOKEN = re.compile(r"\bDONE-[A-Za-z0-9_.:-]+")
LADDER_PREFIX = re.compile(
    r"^\S+: (you have been quiet \d+ min|still no progress|you reported the work as done|you answered but changed nothing)")


def _ts(obj):
    t = obj.get("timestamp")
    try:
        return datetime.datetime.fromisoformat(str(t).replace("Z", "+00:00")).timestamp() if t else None
    except ValueError:
        return None


def _attachment_text(obj):
    """A message pasted into a busy session is recorded as a queued_command attachment. Only one
    that a person (or a dispatch on a person's behalf) originated is an instruction."""
    at = obj.get("attachment") or {}
    if at.get("type") != "queued_command" or (at.get("origin") or {}).get("kind") != "human":
        return None
    prompt = at.get("prompt")
    if isinstance(prompt, list):
        prompt = " ".join(b.get("text", "") for b in prompt if isinstance(b, dict))
    return " ".join(str(prompt or "").split()).strip() or None


def is_ladder_text(text):
    return bool(LADDER_PREFIX.match((text or "").strip()))


def state(path, max_bytes=3_000_000):
    """-> {"state": "open"|"complete", "last_inbound": ts|None, "last_done": ts|None}"""
    try:
        import fleet_transcript as ft
        inbound_text = ft._inbound_text
    except Exception:
        inbound_text = None
    last_in = last_done = None
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as f:
            if size > max_bytes:
                f.seek(size - max_bytes)
                f.readline()
            raw = f.read().decode("utf-8", errors="replace")
    except OSError:
        return {"state": "open", "last_inbound": None, "last_done": None}
    for line in raw.splitlines():
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        ts = _ts(obj)
        if ts is None:
            continue
        msg = obj.get("message") or {}
        if msg.get("role") == "assistant":
            content = msg.get("content")
            texts = [content] if isinstance(content, str) else [
                b.get("text", "") for b in (content or []) if isinstance(b, dict) and b.get("type") == "text"]
            if any(DONE_TOKEN.search(t or "") for t in texts):
                last_done = ts
            continue
        text = _attachment_text(obj) if obj.get("type") == "attachment" else None
        if text is None and inbound_text:
            text = inbound_text(obj)
        if text and not is_ladder_text(text):
            last_in = ts
    complete = last_done is not None and (last_in is None or last_done > last_in)
    return {"state": "complete" if complete else "open", "last_inbound": last_in, "last_done": last_done}
