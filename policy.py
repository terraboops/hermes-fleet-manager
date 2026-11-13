"""Escalation policy: the rules and thresholds that decide ask, answer or inform.

Standing policy (skill good-human-decision-making, "Standing escalation policy"):
  ASK     hard-floor asks and auto-mode classifier blocks page the operator.
  DECIDE  a reversible continue/choice ask inside a goal is answered and logged to the decisions
          log, not forwarded.
  INFORM  crashes, dead sessions, usage limits and stalls are news: phrased as news, never a question.

Every pattern and threshold lives in DEFAULTS below and is overridden from a YAML file, so a
classifier can be tuned without a code change. The file is re-read when its mtime changes, so the
daemon picks a change up on the next tick. Lookup order: $FLEET_POLICY, then
<funnel dir>/policy.yaml. A missing or broken file means the defaults, never a crash.
"""
import datetime
import os
import re
import sys
import time

DEFAULTS = {
    # Text that must never be answered by the manager: destructive, outward-facing, or widening access.
    "irreversible": (
        r"\b(?:delete|drop|wipe|purge|destroy|force[- ]push|reset --hard|revoke|rotate|disable (?:the )?(?:auth|check|gate|"
        r"review|ci)|skip (?:the )?(?:review|tests?|ci|gate)|bypass|merge (?:to|into) (?:main|prod)|deploy to prod|"
        r"production deploy|ship to prod|email(?:ing)? (?:the )?client|message (?:the )?client|post to (?:slack|the client)|"
        r"push(?:ing)?\b[^.?]{0,60}\b(?:main|prod|origin)|post(?:ing)? (?:a |the |that )?(?:drafted )?comment|publish(?:ing)?|"
        r"approve (?:one |a |the )?(?:full )?(?:model )?run)\b"
    ),
    # A choice: only strong signals. Prose lists and numbered blockers are not choices.
    "choice": (
        r"\boption\s+[a-d1-4]\b|\(\s*[a-d]\s*\)\s+\S.{3,}?\(\s*[a-d]\s*\)\s+\S|"
        r"\bwhich (?:of (?:these|the)|one|way|option|approach)\b|\bshould (?:i|we)\b[^?.]{3,80}\bor\b|"
        r"\b(?:choose|pick|decide) between\b|\bwhich\b[^?.]{2,60}\b\w+ or \w+\s*\?|"
        r"\bshould\b[^?]{3,120}?,\s*or (?:should )?(?:i|we|it|they)\b[^?]{3,100}\?"
    ),
    # A "may I continue" ask: permission to carry on with work already in scope.
    "continue_ask": (
        r"\b(?:may|can|shall|should) (?:i|we) (?:continue|proceed|go ahead|carry on|keep going|move on|start on|begin|resume)\b|"
        r"\b(?:is it )?ok(?:ay)? (?:if i|to) (?:continue|proceed|go ahead|carry on|keep going|move on|start|begin|resume)\b|"
        r"\b(?:do you )?want me to (?:continue|proceed|go ahead|carry on|keep going|move on|start on|begin|resume)\b|"
        r"\bconfirm (?:that )?(?:i|we) (?:can|should) (?:continue|proceed|go ahead)\b|"
        r"\bawait(?:ing)? (?:your )?(?:go-?ahead|confirmation) to (?:continue|proceed)\b"
    ),
    # An idle or no-change status line. Paired with "no question mark" and "no credential or money ask",
    # it is a status loop, not an ask: ledgered silently (lane 2), never paged.
    "idle_status": (
        r"\b(?:i(?:'m| am) (?:still )?(?:idle|parked|holding)|still (?:idle|parked|blocked|holding)|"
        r"nothing (?:has )?(?:changed|happened|new)|no (?:change|new work|new brief)|unchanged|"
        r"need(?:s)? the next (?:\w+ )?brief|(?:a )?go-ahead for|waiting for (?:the )?next|awaiting (?:the )?next|"
        r"nothing (?:new )?to report|holding (?:the )?(?:push|deploy|merge)|not doing more work)\b"
    ),
    # A status that says the work waits on the operator is an owner hold, not noise: it keeps paging
    # (once per ask, via repeat suppression) instead of being silenced.
    "owner_held": (
        r"\b(?:waits?|waiting|depends?|needs?|requires?|is|are|remain(?:s)?) (?:on |for )?(?:her|the operator|the owner@NAMES@)\b|"
        r"\b(?:her@NAMES@)(?:'s)? (?:call|decision|word|approval|ok)\b|owner[- ]held|"
        r"\b(?:waiting|waits?|depends?|needs?) (?:on|for) you\b|\byour (?:review|answer|decisions?|call|yes|ok|approval|word)\b|"
        r"\b(?:from|for) you\b"
    ),
    # Any sign the session wants something from the operator. Text with none of these, no question
    # mark, and no hold or credential words is progress narration that was mis-raised as an ask.
    "ask_signal": (
        r"\b(?:need|needs|needed|waiting|waits|blocked|approve|approval|decide|decision|confirm|confirmation|"
        r"permission|go-ahead|please|should (?:i|we)|can you|could you|let me know|your (?:call|answer|ok|yes|word)|"
        r"your decisions?|you (?:name|give|choose|pick|tell)|tell me|which|whether)\b"
    ),
    # What the status filter may never silence: a request to be given a credential, money or access.
    "hard_supply": (
        r"\b(?:oauth|api[- ]?key|key|token|secret|credentials?|password|login|billing|invoice|payment|budget|"
        r"spend|purchase|subscription|account|permission|authori[sz]ation|approval)\b"
    ),
    # A real request to be given, or to touch, a credential: a verb of asking or handling next to the
    # secret noun. The noun alone is prose ("checks about agent credentials") and is never a request.
    # A negation just before the verb ("no credentials were touched") cancels the match (see clarify).
    "credential_request": (
        r"\b(?:need(?:s|ed)?|require[sd]?|provide|supply|give|send|paste|share|hand over|grant|issue|enter|type|reveal|"
        r"print|expose|rotate|create|generate|obtain|fetch|retrieve)\b[^.?!\n]{0,40}?"
        r"\b(?:secrets?|credentials?|api[ _-]?keys?|private keys?|access tokens?|passwords?)\b|"
        r"\b(?:secrets?|credentials?|api[ _-]?keys?|private keys?|access tokens?|passwords?)\b[^.?!\n]{0,30}?"
        r"\b(?:is|are) (?:needed|required|missing|expired|invalid)\b"
    ),
    # A request to resume or continue reversible work already in scope (a hold lifted, a go-ahead).
    "resume_ask": r"\b(?:resume|continue|proceed|go-?ahead|carry on|keep going)\b",
    # One news page per mass event: after one of these reasons pages, the same reason from OTHER sessions
    # inside this window is ledgered as batched, not paged. 0 turns it off.
    "mass_event_window_s": 900,
    "mass_event_reasons": ["no_safe_nudge", "nudge_refused"],
    # One page per session per window for delivery-receipt reviews (ACK-MISSED), whatever the dispatch id.
    # 0 turns it off (every distinct dispatch id pages).
    "receipt_review_window_s": 86400,
    # Auto-mode classifier block tags: always a page.
    "classifier_blocks": r"\[(Production Deploy|Security Weaken|Self-Approval|Git Destructive)\]",
    # System events that are news (INFORM), matched by event prefix.
    "hard_prefixes": ["SESSION-DEAD-", "USAGE-LIMIT-", "NO-TRANSCRIPT-"],
    # A "still blocked, nothing changed" status restating an ask already paged waits this long (7 days)
    # before it may page again; other repeats keep the 24 h window.
    "status_repeat_window_s": 604800,
    # A stall shorter than this is not news.
    "stall_inform_after_s": 600,
    # Where reversible answers are logged.
    "decisions_log": "~/.hermes/fleet-decisions.md",
    # Extra names the operator goes by in session text (list). Empty here; set it in the private
    # policy file or the operator_names file next to it. Feeds owner_held and the owner-wait rules.
    "owner_names": [],
    # launchd label of the gate LaunchAgent. $FLEET_GATE_LABEL wins over this.
    "gate_label": "io.hermes.laya-gate",
    # What the manager says back for a "may I continue" ask.
    "continue_reply": (
        "Yes, continue. Stay on the registered goal and keep every step reversible. If a step would delete, "
        "deploy, spend, touch credentials, widen access or message someone outside the house, stop and emit "
        "NEEDS-INPUT naming that step."
    ),
    # INFORM phrasing, one template per reason. Fields: {session} {detail} {mins}.
    "inform": {
        "session_dead": "{session} is dead and not running. {detail}Parked and logged; nothing to decide unless you want it revived.",
        "usage_limit": "{session} hit a usage or credit limit and is waiting for it to reset. {detail}Nothing to decide.",
        "crash": "{session} crashed in its own output. {detail}Logged with the session; nothing to decide unless you want it looked at.",
        "no_safe_nudge": "{session} has been stopped for {mins} min. {detail}Prompts are not landing; it stays parked and is listed in the weekly review.",
        "nudge_refused": "{session} has been stopped for {mins} min after every prompt was tried. {detail}It stays parked and is listed in the weekly review.",
        "no_transcript": "{session} is registered but has no transcript. {detail}Logged; nothing to decide.",
    },
    # Weekly report: a page counts as acted on if the operator sent the session a message within this window.
    "acted_window_hours": 24,
}

_cache: dict = {"path": None, "mtime": None, "data": None}


def _path():
    p = os.environ.get("FLEET_POLICY")
    if p:
        return os.path.expanduser(p)
    d = os.path.expanduser(os.environ.get("FLEET_FUNNEL_DIR", "~/.hermes/cache/fleet-funnel"))
    return os.path.join(d, "policy.yaml")


def _load():
    p = _path()
    try:
        m = os.stat(p).st_mtime
    except OSError:
        m = None
    if _cache["data"] is not None and _cache["path"] == p and _cache["mtime"] == m:
        return _cache["data"]
    data = dict(DEFAULTS)
    if m is not None:
        try:
            import yaml
            with open(p, encoding="utf-8") as f:
                over = yaml.safe_load(f) or {}
            if isinstance(over, dict):
                for k, v in over.items():
                    if k in DEFAULTS:
                        if isinstance(DEFAULTS[k], dict) and isinstance(v, dict):
                            data[k] = {**DEFAULTS[k], **v}
                        else:
                            data[k] = v
        except Exception:
            data = dict(DEFAULTS)              # a broken file is the defaults, never a crash
    _cache.update(path=p, mtime=m, data=data)
    return data


def operator_names():
    """Names the operator goes by in session text: the `owner_names` policy key, else
    $FLEET_OPERATOR_NAMES (comma separated), else the one-per-line `operator_names` file in the
    funnel directory. Never defaulted in code."""
    names = _load().get("owner_names") or []
    if not names:
        raw = os.environ.get("FLEET_OPERATOR_NAMES", "")
        if not raw:
            d = os.path.expanduser(os.environ.get("FLEET_FUNNEL_DIR", "~/.hermes/cache/fleet-funnel"))
            try:
                with open(os.path.join(d, "operator_names"), encoding="utf-8") as f:
                    raw = ",".join(f.read().split())
            except OSError:
                raw = ""
        names = raw.split(",")
    return [n.strip() for n in names if n and n.strip()]


def names_alt():
    """Regex alternation tail for the operator's names: '|name1|name2', or '' when there are none."""
    return "".join("|" + re.escape(n) for n in operator_names())


def _expand(v):
    return v.replace("@NAMES@", names_alt()) if isinstance(v, str) else v


def get(key):
    return _expand(_load()[key])


def rx(key, flags=re.I):
    """Compiled regex for a policy key. A pattern that does not compile falls back to the default."""
    try:
        return re.compile(get(key), flags)
    except re.error:
        return re.compile(_expand(DEFAULTS[key]), flags)


class Rx:
    """A regex that always reflects the current policy; a drop-in for a compiled pattern's search()."""

    def __init__(self, key):
        self.key = key

    def search(self, text, *a, **k):
        return rx(self.key).search(text, *a, **k)

    def match(self, text, *a, **k):
        return rx(self.key).match(text, *a, **k)


# ---- the decisions log --------------------------------------------------------------------

def decisions_path():
    p = os.environ.get("FLEET_DECISIONS_LOG")
    if p:
        return os.path.expanduser(p)
    if "unittest" in sys.modules or "pytest" in sys.modules:
        return os.path.expanduser("~/.hermes/cache/scratch/decisions-under-test.md")
    return os.path.expanduser(get("decisions_log"))


def log_decision(what, why, undo, now=None):
    """Append one line to the decisions log: date, what, why, how to undo. True if written."""
    day = datetime.date.fromtimestamp(time.time() if now is None else now).isoformat()
    line = "- %s %s. Why: %s. Undo: %s.\n" % (day, " ".join(what.split()).rstrip("."),
                                              " ".join(why.split()).rstrip("."),
                                              " ".join(undo.split()).rstrip("."))
    try:
        p = decisions_path()
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "a", encoding="utf-8") as f:
            f.write(line)
        return True
    except OSError:
        return False


# ---- INFORM phrasing ----------------------------------------------------------------------

def is_inform(reason):
    return (getattr(reason, "value", reason) or "") in get("inform")


def inform_text(reason, session, detail="", stopped_s=0):
    """News, not a question: what happened, what was done, and that nothing needs deciding."""
    key = getattr(reason, "value", reason)
    tpl = get("inform").get(key)
    if not tpl:
        return None
    detail = " ".join((detail or "").split())
    if detail and not detail.endswith((".", "!", "?")):
        detail += "."
    return tpl.format(session=session, detail=(detail + " ") if detail else "", mins=int((stopped_s or 0) // 60))


# ---- weekly counts ------------------------------------------------------------------------

def weekly_counts(pages, attempts, inbound, now=None, days=7):
    """Pages sent in the last `days` and the share the operator acted on.

    pages     escalation-ledger rows (page rows carry `reason`; `kind` marks handled/suppressed rows)
    attempts  attempt-ledger rows; an `answer` rung is a question the fleet answered itself
    inbound   inbound(session, since, until) -> [text] of messages that reached the session in that
              window. A message the fleet itself sent (it is in the attempt ledger) is not the operator.

    "Acted on" is a heuristic and is reported as one: the operator sent the session a message within
    `acted_window_hours` after the page. A page with no such message counts as not acted on.
    """
    now = time.time() if now is None else now
    since = now - days * 86400
    win = get("acted_window_hours") * 3600
    sent = [r for r in pages if r.get("reason") and not r.get("kind") and r.get("at", 0) >= since]
    answered = [r for r in attempts if r.get("rung") == "answer" and r.get("at", 0) >= since]
    suppressed = sum(1 for r in pages if r.get("kind") == "suppressed_repeat" and r.get("at", 0) >= since)
    fleet_texts = {}
    for r in attempts:
        if r.get("text"):
            fleet_texts.setdefault(r.get("session"), []).append(r["text"][:60])
    out = {"days": days, "pages_sent": len(sent), "by_class": {}, "by_reason": {},
           "answered_by_fleet": len(answered), "suppressed_repeats": suppressed,
           "acted_on": 0, "unknown": 0}
    for r in sent:
        cls = r.get("class") or ("inform" if is_inform(r["reason"]) else "ask")
        out["by_class"][cls] = out["by_class"].get(cls, 0) + 1
        out["by_reason"][r["reason"]] = out["by_reason"].get(r["reason"], 0) + 1
    asks = [r for r in sent if (r.get("class") or ("inform" if is_inform(r["reason"]) else "ask")) == "ask"]
    for r in asks:
        if r["at"] + win > now:
            out["unknown"] += 1                       # the window has not closed yet
            continue
        try:
            msgs = inbound(r["session"], r["at"], r["at"] + win)
        except Exception:
            out["unknown"] += 1
            continue
        mine = fleet_texts.get(r["session"], [])
        if any(not any(f and f in m for f in mine) for m in msgs):
            out["acted_on"] += 1
    decided = len(asks) - out["unknown"]
    out["asks"] = len(asks)
    out["acted_share"] = round(out["acted_on"] / decided, 2) if decided > 0 else None
    return out


def format_weekly(w):
    share = "n/a" if w["acted_share"] is None else "%d%%" % round(w["acted_share"] * 100)
    return ("Last %(days)d days: %(pages_sent)d pages sent (%(asks)d asks, %(inf)d news). "
            "Acted on: %(acted_on)d of %(decided)d asks that have closed their window (%(share)s)%(unk)s. "
            "Answered by the fleet instead of paging: %(answered_by_fleet)d. Repeats suppressed: %(suppressed_repeats)d.") % {
        **w, "inf": w["by_class"].get("inform", 0), "decided": w["asks"] - w["unknown"], "share": share,
        "unk": ("; %d still inside the window" % w["unknown"]) if w["unknown"] else ""}
