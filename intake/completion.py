"""Completion check: a worker may not close a card with 'not tested' output."""
from __future__ import annotations

import re
from typing import Any, Optional

# Phrases that mean the work was not verified. Matched on summary, result and metadata values.
_PATTERNS = [
    r"\bnot[\s_-]+tested\b", r"\buntested\b", r"\bnot[\s_-]+verified\b",
    r"\bno[\s_-]+tests?[\s_-]+(?:were[\s_-]+)?(?:run|ran|executed)\b",
    r"\btests?[\s_-]+(?:were[\s_-]+)?not[\s_-]+run\b", r"\bnot[\s_-]+run\b",
]
_RX = re.compile("|".join(_PATTERNS), re.I)


class CompletionRejected(ValueError):
    pass


def _strings(obj: Any):
    if isinstance(obj, str):
        yield obj
    elif isinstance(obj, dict):
        for v in obj.values():
            yield from _strings(v)
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            yield from _strings(v)


def check_completion(summary: Optional[str] = None, result: Optional[str] = None,
                     metadata: Optional[dict] = None) -> Optional[str]:
    """Return the offending text when the completion claims untested work, else None."""
    for s in _strings([summary, result, metadata]):
        m = _RX.search(s)
        if m:
            return m.group(0)
    return None


def guarded_complete(kb, conn, task_id: str, *, summary: Optional[str] = None,
                     result: Optional[str] = None, metadata: Optional[dict] = None, **kw) -> bool:
    """kb.complete_task behind the check. A rejected completion leaves the card unchanged
    (still running, lease intact) and writes a comment so the refusal is auditable."""
    hit = check_completion(summary, result, metadata)
    if hit:
        kb.add_comment(conn, task_id, "intake", f"completion rejected: output says '{hit}'. "
                       "Run the check, then complete with the observed result.")
        raise CompletionRejected(f"completion rejected: '{hit}'")
    return kb.complete_task(conn, task_id, summary=summary, result=result, metadata=metadata, **kw)
