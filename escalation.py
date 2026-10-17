"""The one door to the operator, and the ledger behind it.

Every event that can interrupt a person goes through `page()`. A page names a REASON from a short
closed list, so "why was I told" always has an answer, and every page and every attempt to avoid
one is written to an append-only ledger a review can read.

The intended policy is three cases, plus a few hard system exceptions:

    NO_SAFE_NUDGE             work is in flight, the session stopped, and no nudge can be built or
                              delivered (includes a mid-turn wedge: a nudge cannot land mid-turn)
    NUDGE_REFUSED             the ladder (nudge, challenge, re-align) ran out with no progress
    CLARIFICATION_UNGROUNDED  the session needs an answer the registered goals cannot supply

    SESSION_DEAD, USAGE_LIMIT, CLASSIFIER_BLOCK, GATED_ACTION, FUNNEL_FAULT   hard exceptions

LEGACY marks a page raised by a rule that predates the funnel. It carries the rule that raised it,
so the replay can show which legacy paths the three cases do not cover.

Modes (FLEET_FUNNEL): `shadow` (default) pages exactly as before and records what the funnel would
have done; `enforce` is reserved for after the shadow review; `off` records nothing extra.
"""
import json
import os
import re
import time
import uuid
from dataclasses import dataclass, field, asdict
from enum import Enum

LOG_DIR = os.path.expanduser(os.environ.get("FLEET_LEDGER_DIR", "~/.hermes/logs"))
ATTEMPTS = "fleet-attempts.jsonl"
ESCALATIONS = "fleet-escalations.jsonl"


class Reason(str, Enum):
    NO_SAFE_NUDGE = "no_safe_nudge"
    NUDGE_REFUSED = "nudge_refused"
    CLARIFICATION_UNGROUNDED = "clarification_ungrounded"
    SESSION_DEAD = "session_dead"
    USAGE_LIMIT = "usage_limit"
    CLASSIFIER_BLOCK = "classifier_block"
    GATED_ACTION = "gated_action"
    FUNNEL_FAULT = "funnel_fault"
    LEGACY = "legacy"


HARD = {Reason.SESSION_DEAD, Reason.USAGE_LIMIT, Reason.CLASSIFIER_BLOCK, Reason.GATED_ACTION,
        Reason.FUNNEL_FAULT}


def mode():
    m = (os.environ.get("FLEET_FUNNEL") or "shadow").strip().lower()
    return m if m in ("off", "shadow", "enforce") else "shadow"


@dataclass
class Escalation:
    session: str
    reason: Reason
    text: str                                   # what is delivered
    rule: str = ""                              # the rule that raised it (legacy) or the ladder state
    situation: str = ""
    recommendation: str = ""
    options: list = field(default_factory=list)
    default: str = ""
    evidence: list = field(default_factory=list)    # attempt ids that preceded this page
    urgent: bool = True


def _append(name, row):
    """Never raises: a ledger that cannot write must not take a page down with it."""
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        with open(os.path.join(LOG_DIR, name), "a", encoding="utf-8") as f:
            f.write(json.dumps(row, default=str) + "\n")
        return True
    except Exception:
        return False


def read(name, since=0.0):
    out = []
    try:
        with open(os.path.join(LOG_DIR, name), encoding="utf-8") as f:
            for line in f:
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                if r.get("at", 0) >= since:
                    out.append(r)
    except OSError:
        pass
    return out


def record_attempt(session, episode, trigger, rung, text="", **extra):
    """One resolver action (a nudge, a challenge, a re-alignment, an answer) or an observation
    about one. Returns the attempt id, so a later page can cite it."""
    aid = uuid.uuid4().hex[:8]
    _append(ATTEMPTS, {"id": aid, "at": int(time.time()), "session": session, "episode": episode,
                       "trigger": trigger, "rung": rung, "text": (text or "")[:400], **extra})
    return aid


def record_outcome(attempt_id, session, episode, outcome, evidence=""):
    _append(ATTEMPTS, {"id": uuid.uuid4().hex[:8], "at": int(time.time()), "session": session,
                       "episode": episode, "kind": "outcome", "of": attempt_id, "outcome": outcome,
                       "evidence": (evidence or "")[:300]})


def legacy_reason(match, rule=""):
    """The best reason for an event a pre-funnel rule paged. Honest about the rest: LEGACY."""
    m = match or ""
    r = (rule or "").lower()
    if m.startswith("SESSION-DEAD"):
        return Reason.SESSION_DEAD
    if m.startswith("USAGE-LIMIT"):
        return Reason.USAGE_LIMIT
    if "classifier" in r or "[Production Deploy]" in m or "[Security Weaken]" in m \
            or "[Self-Approval]" in m or "[Git Destructive]" in m:
        return Reason.CLASSIFIER_BLOCK
    if "wedge" in r and "mid-turn" in r:
        return Reason.NO_SAFE_NUDGE
    if "no revival" in r or "nudged" in r:
        return Reason.NUDGE_REFUSED
    if "gate" in r and ("unreachable" in r or "unavailable" in r):
        return Reason.FUNNEL_FAULT
    return Reason.LEGACY


def page(pending, esc, now=None):
    """Hand one escalation to the delivery queue. `pending` is the daemon's queue list.

    An unknown or missing reason is a bug in a caller, and the answer to a bug here is never to
    swallow the page: it is recorded as a FUNNEL_FAULT and delivered anyway."""
    now = int(now if now is not None else time.time())
    if not isinstance(esc.reason, Reason):
        try:
            esc.reason = Reason(esc.reason)
        except ValueError:
            _append(ESCALATIONS, {"at": now, "session": esc.session, "kind": "bad_reason",
                                  "given": str(esc.reason), "rule": esc.rule})
            esc.rule = ("unknown reason %r; " % (esc.reason,)) + esc.rule
            esc.reason = Reason.FUNNEL_FAULT
    row = {"at": now, "session": esc.session, "reason": esc.reason.value, "rule": esc.rule,
           "text": esc.text[:400], "urgent": esc.urgent, "hard": esc.reason in HARD,
           "evidence": esc.evidence, "mode": mode()}
    _append(ESCALATIONS, row)
    pending.append({"session": esc.session, "match": esc.text, "at": now, "urgent": esc.urgent,
                    "reason": esc.reason.value})
    return row


_HB = [0.0]


def heartbeat(now=None, every=600):
    """A row per ten minutes saying the funnel ran. A resolver that silently stopped is the failure
    mode that pages nobody, so its absence has to be detectable."""
    now = time.time() if now is None else now
    if now - _HB[0] >= every:
        _HB[0] = now
        _append(ATTEMPTS, {"id": "hb", "at": int(now), "kind": "heartbeat", "mode": mode()})
        return True
    return False
