"""What a session did after a nudge, read from its own transcript.

A transcript line grows for many reasons, and the fleet's own paste is one of them: a landed nudge is
a user turn, so "the file grew" cannot mean "the session responded", let alone "made progress". This
reads only assistant turns after a timestamp and sorts them:

    progress      a tool call, or a PROGRESS/DONE/NEEDS-INPUT token
    restate_done  text claiming completion, no tool call
    refusal       text declining to continue, no tool call
    ack_only      other text, no tool call (answered, did nothing)
    silent        no assistant turn at all

Everything except `progress` counts against the session when a ladder decides whether to escalate.
"""
import datetime
import json
import os
import re

TOKEN = re.compile(r"\b(?:PROGRESS|DONE|NEEDS-INPUT)-[A-Za-z0-9_.:-]+")
# Completion claims and refusals are phrases a session actually uses; the lists are short on purpose
# and any text matching neither is `ack_only`, which counts the same.
DONE_CLAIM = re.compile(
    r"\b(already|is|are|now|all)\s+(done|complete|completed|finished|shipped|live|merged|deployed)\b|"
    r"\bnothing (left|more|else|remaining) to do\b|\bwork is (done|complete)\b|"
    r"\b(all|every) (tasks?|items?|work|steps?) (is |are )?(done|complete|finished)\b", re.I)
REFUSAL = re.compile(
    r"\b(i|we) (can'?t|cannot|won'?t|will not|am not going to|decline to|refuse to)\b|"
    r"\bnot going to (continue|proceed|do)\b|\bno further action\b|\bstopping here\b", re.I)


def _ts(obj):
    t = obj.get("timestamp")
    if not t:
        return None
    try:
        return datetime.datetime.fromisoformat(str(t).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def assistant_turns_since(path, since, max_bytes=2_000_000):
    """[(ts, text, used_tool)] for assistant turns after `since`, oldest first."""
    out = []
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as f:
            if size > max_bytes:
                f.seek(size - max_bytes)
                f.readline()                      # drop the partial first line
            raw = f.read().decode("utf-8", errors="replace")
    except OSError:
        return out
    for line in raw.splitlines():
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        msg = obj.get("message") or {}
        if msg.get("role") != "assistant":
            continue
        ts = _ts(obj)
        if ts is None or ts <= since:
            continue
        content = msg.get("content")
        text, tool = [], False
        if isinstance(content, str):
            text.append(content)
        else:
            for b in content or []:
                if not isinstance(b, dict):
                    continue
                if b.get("type") == "tool_use":
                    tool = True
                elif b.get("type") == "text":
                    text.append(b.get("text") or "")
        out.append((ts, "\n".join(text).strip(), tool))
    return out


def evaluate(path, since):
    """-> {"outcome": ..., "evidence": short text, "turns": n}"""
    turns = assistant_turns_since(path, since)
    if not turns:
        return {"outcome": "silent", "evidence": "", "turns": 0}
    for ts, text, tool in turns:
        if tool:
            return {"outcome": "progress", "evidence": "tool call", "turns": len(turns)}
        tok = TOKEN.search(text)
        if tok:
            return {"outcome": "progress", "evidence": tok.group(0), "turns": len(turns)}
    text = " ".join(t for _, t, _ in turns)[-600:]
    if DONE_CLAIM.search(text):
        return {"outcome": "restate_done", "evidence": text[-200:], "turns": len(turns)}
    if REFUSAL.search(text):
        return {"outcome": "refusal", "evidence": text[-200:], "turns": len(turns)}
    return {"outcome": "ack_only", "evidence": text[-200:], "turns": len(turns)}
