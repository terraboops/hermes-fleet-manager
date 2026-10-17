"""The router for enforce mode: which of an armed session's events may reach the operator.

Every event gets one of three outcomes:

    legacy   a hard system event (dead session, usage limit, crash, classifier block). The existing
             gate pages for these; they are the exceptions to the three-case policy.
    page     one of the three cases (or a hard fault), as an escalation with a reason
    handled  nothing is told to the operator. The ledger records what happened and why

Nothing else exists. A keyword rule or a model confidence can no longer turn an event into a page.
"""
import json
import os
import re
import time
from dataclasses import dataclass
from typing import Optional

import clarify
import escalation
import ladder
import obligations

CLASSIFIER = re.compile(r"\[(Production Deploy|Security Weaken|Self-Approval|Git Destructive)\]")
HARD_PREFIXES = ("SESSION-DEAD-", "USAGE-LIMIT-", "NO-TRANSCRIPT-")
REQUESTS = "page-requests.jsonl"
_PAGED = {}          # (session, reason) -> stop start time already paged for


@dataclass
class Action:
    kind: str                                  # legacy | page | handled
    esc: Optional[escalation.Escalation] = None
    note: str = ""


mode = escalation.mode


def is_hard(match):
    return match.startswith(HARD_PREFIXES) or "traceback" in match.lower() or bool(CLASSIFIER.search(match))


def _recent(sn, episode, n=6):
    rows = [r for r in escalation.read(escalation.ATTEMPTS)
            if r.get("session") == sn and r.get("episode") == episode and r.get("kind") != "heartbeat"]
    return rows[-n:]


def card(sn, reason, episode="", evidence=""):
    """What the operator reads: the situation, what was tried, a recommendation and a default."""
    tried = [r for r in _recent(sn, episode) if r.get("rung") in ("nudge", "challenge", "realign")]
    outs = [r for r in _recent(sn, episode) if r.get("kind") == "outcome"]
    steps = ", ".join(r["rung"] for r in tried) or "nothing"
    last = (outs[-1].get("outcome") if outs else None) or "no answer"
    if reason == "nudge_refused":
        return (f"{sn} has stopped and is not responding to prompts. Tried: {steps}. Last answer: {last}"
                f"{' (' + evidence[:160] + ')' if evidence else ''}. "
                f"Recommendation: read its last message and decide if the work is finished. "
                f"Default: leave it parked.")
    if reason == "no_safe_nudge":
        return (f"{sn} has stopped and no safe prompt can be sent. {evidence} "
                f"Recommendation: look at it directly. Default: leave it parked.")
    return f"{sn}: {evidence}"


def _page_once(sn, reason, began, esc):
    key = (sn, reason)
    prev = _PAGED.get(key)
    if prev is not None and abs(prev - began) <= 600:
        return Action("handled", note="already paged for this stop")
    _PAGED[key] = began
    return Action("page", esc)


def route(sn, match, *, ask="", state=None, nudges=None, stopped=0, dispatch=None, path=None,
          manager_owns=False, now=None):
    """Decide one event. Pure apart from `dispatch` (delivering a grounded answer) and the ledger."""
    now = time.time() if now is None else now
    if is_hard(match):
        return Action("legacy", note="hard system event")

    if match.startswith("NEEDS-INPUT-"):
        try:
            kind, reason, text, detail = clarify.resolve(sn, ask, dispatch, now)
        except Exception as e:
            return Action("page", escalation.Escalation(
                sn, escalation.Reason.FUNNEL_FAULT, "%s: could not decide what to do with a question (%r)" % (sn, e),
                rule="clarify raised"))
        if kind == "answered":
            escalation.record_attempt(sn, "", "NEEDS-INPUT", "answer", text, grounded=True,
                                      cite=detail.get("cite"), delivered="LANDED")
            return Action("handled", note="answered from %s" % detail.get("cite"))
        shown = ask.strip()[:500] or "(the session gave no words with its request)"
        body = ("%s is waiting on a decision the registered goals do not cover: %s Recommendation: reply in "
                "one line; the answer goes to the session. Default: it stays parked." % (sn, shown)
                if reason == "clarification_ungrounded"
                else "%s: %s. Question: %s" % (sn, text, shown))
        return Action("page", escalation.Escalation(sn, escalation.Reason(reason), body, rule=text,
                                                    evidence=[detail.get("cite")] if detail.get("cite") else []))

    if match.startswith("ACK-MISSED-"):
        return Action("page", escalation.Escalation(
            sn, escalation.Reason.NO_SAFE_NUDGE,
            "%s: a message to it never arrived as a turn. %s Recommendation: look at it directly." % (sn, match[:200]),
            rule="dispatch undelivered"))

    if match.startswith("STALL"):
        if manager_owns:
            return Action("handled", note="a manager owns this session's ladder")
        ob = obligations.state(path) if path else {"state": "open"}
        if ob["state"] == "complete":
            return Action("handled", note="parked at its finished task")
        would = ladder.would_page(state, stopped, nudges)
        if not would:
            return Action("handled", note="ladder in progress")
        ep = (nudges or {}).get("episode_id", "")
        ev = ("It is busy and silent for %d min; a prompt cannot land mid-turn." % (stopped // 60)
              if would == "no_safe_nudge" and (state or "").upper() in ("WORKING", "QUEUED")
              else ("Prompts did not land." if would == "no_safe_nudge" else ""))
        return _page_once(sn, would, now - stopped, escalation.Escalation(
            sn, escalation.Reason(would), card(sn, would, ep, ev), rule="ladder", evidence=[ep]))

    return Action("handled", note="not one of the three cases")


# ---- pages requested from outside the daemon (fleet_resolve.py escalate) -----------------

def request_page(session, situation, recommendation="", default=""):
    os.makedirs(clarify.FUNNEL_DIR, exist_ok=True)
    with open(os.path.join(clarify.FUNNEL_DIR, REQUESTS), "a", encoding="utf-8") as f:
        f.write(json.dumps({"at": int(time.time()), "session": session, "situation": situation[:600],
                            "recommendation": recommendation[:300], "default": default[:200]}) + "\n")


def drain_requests(pending):
    """Turn queued requests into pages through the door. Returns how many were paged."""
    path = os.path.join(clarify.FUNNEL_DIR, REQUESTS)
    try:
        with open(path, encoding="utf-8") as f:
            rows = [json.loads(l) for l in f if l.strip()]
        os.replace(path, path + ".done")
    except (OSError, ValueError):
        return 0
    for r in rows:
        text = "%s: %s%s%s" % (r["session"], r["situation"],
                               (" Recommendation: " + r["recommendation"]) if r.get("recommendation") else "",
                               (" Default: " + r["default"]) if r.get("default") else "")
        escalation.page(pending, escalation.Escalation(
            r["session"], escalation.Reason.CLARIFICATION_UNGROUNDED, text, rule="requested by overwatch"))
    return len(rows)
