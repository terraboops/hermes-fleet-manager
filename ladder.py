"""The nudge ladder: what to say on each rung, and what the funnel would do about the result.

Rung 1 asks the session to continue. Rung 2 challenges it: a completion claim needs evidence, and an
answer that changed nothing needs a concrete step. Rung 3 and beyond re-align it to the registered
goal. The words come from templates filled from the session's own focus file, never from a model, so
a nudge can never carry an authorization or an instruction to run something gated.
"""
import os
import re

import policy

FOCUS_DIR = os.path.expanduser(os.environ.get(
    "LAYA_GATE_FOCUS_DIR", "~/.hermes/scripts/cc-watch/overwatch"))

RUNG_NAMES = {1: "nudge", 2: "challenge"}


def rung_name(rung):
    return RUNG_NAMES.get(rung, "realign")


def _focus(sn):
    try:
        with open(os.path.join(FOCUS_DIR, "%s_focus.md" % sn), encoding="utf-8") as f:
            return f.read()
    except OSError:
        return ""


def goal_line(sn):
    """The first line of the session's focus file: the registered statement of what it is for."""
    lines = [l.strip() for l in _focus(sn).splitlines() if l.strip()]
    for line in lines:                       # an explicit standing goal outranks the role line
        m = re.match(r"GOAL\b[^:]*:\s*(.+)$", line)
        if m:
            return re.sub(r"\s+", " ", m.group(1))[:240]
    return re.sub(r"\s+", " ", lines[0])[:240] if lines else ""


def next_increment(sn):
    """An explicit 'NEXT:' line in the focus file, or None. Deliberately not inferred."""
    m = re.search(r"^\s*NEXT(?: INCREMENT)?\s*:\s*(.+)$", _focus(sn), re.I | re.M)
    return re.sub(r"\s+", " ", m.group(1)).strip()[:240] if m else None


def _owner_wait():
    """Owner-wait shapes. The operator's own names come from policy, never from code."""
    who = "you|the owner" + policy.names_alt()
    poss = "your|the owner'?s" + "".join("|" + n + "'?s" for n in map(re.escape, policy.operator_names()))
    return re.compile(
        r"(still waiting on (%s)|waiting on (%s)|waiting for (%s)"
        r"|needs? (%s) (decision|go-ahead|approval|word|hands|keys?)"
        r"|these \w+ (items )?(are )?still waiting on you|nothing has changed since|still blocked)"
        % (who, who, who, poss), re.I)


def waiting_on_owner(last_words):
    """True when a session's latest words say it is parked on the owner. A wait on the owner is
    never a reason to stop: other work in the same goal is still open."""
    return bool(last_words and _owner_wait().search(last_words))


def rung_text(sn, rung, quiet_min, last_outcome=None, last_words=None):
    """(text, grounded). `grounded` is True when the text names a registered next step."""
    nxt = next_increment(sn)
    if waiting_on_owner(last_words):
        goal = goal_line(sn)
        t = (f"{sn}: that wait is recorded once and is not a reason to stop. Checking the owner-held item "
             f"again changes nothing, so do not poll it. Take the next unblocked item for your goal now"
             f"{': ' + goal if goal else ''}. "
             + (f"The next step on record: {nxt}. " if nxt else "")
             + "Ship it, verify it live, and report the commit. Emit NEEDS-INPUT only for a NEW blocker.")
        return t, bool(nxt or goal)
    if rung <= 1:
        t = (f"{sn}: you have been quiet {quiet_min} min. Continue the work and say what you are "
             f"doing, or emit NEEDS-INPUT naming what you need. Do not restate done.")
        if nxt:
            t += f" The next step on record: {nxt}"
        return t, bool(nxt)
    if rung == 2:
        if last_outcome == "restate_done":
            t = (f"{sn}: you reported the work as done and then went quiet. A completion claim needs "
                 f"evidence: name the check that shows it, run it, and show the output. If something "
                 f"remains, continue it.")
        else:
            t = (f"{sn}: you answered but changed nothing. State the single next step, then take it, "
                 f"or emit NEEDS-INPUT naming exactly what blocks it.")
        return t, False
    goal = goal_line(sn)
    t = (f"{sn}: still no progress. Re-read what you are here to do: {goal or 'the goal in your brief'}. "
         + (f"The next step on record: {nxt}. " if nxt else "")
         + "Do the next open item for that goal now, or emit NEEDS-INPUT naming exactly what blocks it.")
    return t, bool(nxt or goal)


def would_page(state, stopped, nudges, wedge_after=3600, max_rungs=5):
    """What the funnel would page for, or None. Pure, so shadow mode can log it beside what the
    legacy gate decided and a review can compare the two.

      NO_SAFE_NUDGE   busy and silent past `wedge_after` (a nudge cannot land mid-turn), or two
                      dispatches in a row that never landed
      NUDGE_REFUSED   every rung used and the latest outcome is not progress
    """
    s = (state or "").upper()
    n = nudges or {}
    if s in ("WORKING", "QUEUED"):
        return "no_safe_nudge" if stopped >= wedge_after else None
    if n.get("failed_dispatches", 0) >= 2:
        return "no_safe_nudge"
    if (n.get("nudge_count") or 0) >= (n.get("nudge_max") or max_rungs) \
            and n.get("last_outcome") not in (None, "progress"):
        return "nudge_refused"
    return None
