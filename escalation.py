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
import sys
import time
import uuid
from dataclasses import dataclass, field, asdict
from enum import Enum

import policy

# None means "decide at write time": FLEET_LEDGER_DIR if set, a scratch directory when a test
# runner is loaded (a test suite once filled the real ledger with its own sessions), else the logs.
LOG_DIR = None
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
    CRASH = "crash"
    LEGACY = "legacy"


HARD = {Reason.CRASH, Reason.SESSION_DEAD, Reason.USAGE_LIMIT, Reason.CLASSIFIER_BLOCK, Reason.GATED_ACTION,
        Reason.FUNNEL_FAULT}


def mode():
    """enforce | shadow | off. FLEET_FUNNEL overrides; otherwise the file the CLI writes, so a
    launchd daemon needs no environment to change mode."""
    env = (os.environ.get("FLEET_FUNNEL") or "").strip().lower()
    if not env:
        try:
            d = os.path.expanduser(os.environ.get("FLEET_FUNNEL_DIR", "~/.hermes/cache/fleet-funnel"))
            with open(os.path.join(d, "mode"), encoding="utf-8") as f:
                env = f.read().strip().lower()
        except OSError:
            env = ""
    return env if env in ("off", "shadow", "enforce") else "shadow"


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


def _dir():
    if LOG_DIR is not None:
        return LOG_DIR
    d = os.environ.get("FLEET_LEDGER_DIR")
    if d:
        return os.path.expanduser(d)
    if "unittest" in sys.modules or "pytest" in sys.modules:
        return os.path.expanduser("~/.hermes/cache/scratch/ledger-under-test")
    return os.path.expanduser("~/.hermes/logs")


def _append(name, row):
    """Never raises: a ledger that cannot write must not take a page down with it."""
    try:
        os.makedirs(_dir(), exist_ok=True)
        with open(os.path.join(_dir(), name), "a", encoding="utf-8") as f:
            f.write(json.dumps(row, default=str) + "\n")
        return True
    except Exception:
        return False


def read(name, since=0.0):
    out = []
    try:
        with open(os.path.join(_dir(), name), encoding="utf-8") as f:
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
    if "crash in the session output" in r or m.lower().startswith("traceback"):
        return Reason.CRASH
    if "wedge" in r and "mid-turn" in r:
        return Reason.NO_SAFE_NUDGE
    if "no revival" in r or "nudged" in r:
        return Reason.NUDGE_REFUSED
    if "gate" in r and ("unreachable" in r or "unavailable" in r):
        return Reason.FUNNEL_FAULT
    return Reason.LEGACY


# ---- repeat suppression ------------------------------------------------------------------
# A page must mean something is broken that only the operator can fix. The same ask, from the same
# session, reported again is not new information: it is "still blocked on the same thing". It is told
# once, then ledgered as a suppressed repeat until the ask changes or the window passes. State lives in
# a file so a daemon restart does not reset it. Real breakage always pages.
REPEAT_WINDOW = int(os.environ.get("FLEET_REPEAT_WINDOW", "86400"))
NEVER_SUPPRESS = {Reason.CRASH, Reason.SESSION_DEAD, Reason.USAGE_LIMIT, Reason.CLASSIFIER_BLOCK, Reason.FUNNEL_FAULT}
REPEAT_FILE = "page-repeats.json"


# Words that only say "nothing is new". Sessions rephrase these every tick ("still idle", "no change",
# "same as before"), so they carry no identity. What identifies an ask is the thing it is blocked on.
FILLER = set("""still no change changed unchanged nothing new same since last check again as before
still idle blocked waiting remain remaining remains yet already just now is are was were has have had has
the a an and or of to on in for with from that this these those it its i im my me you your our we they them
there theres cant can will would should could do does did not nor but if then than so any all both each
one two three four five six seven eight nine ten asks something gated question session needs need needed
cc w p""".split())
SIMILAR = float(os.environ.get("FLEET_REPEAT_SIMILARITY", "0.6"))


def content_words(text):
    t = (text or "").lower()
    t = re.sub(r"[0-9a-f]{7,40}|\d+[:.]\d+|\d+", " ", t)
    return {w for w in re.findall(r"[a-z]{3,}", t) if w not in FILLER}


def fingerprint(text):
    """Stable display form of an ask: its content words, sorted. Exact equality is not used for
    matching (see `similar`), this is for the ledger."""
    return " ".join(sorted(content_words(text)))[:160]


def similar(a, b):
    """True when two asks name mostly the same things. Jaccard over content words."""
    wa, wb = content_words(a), content_words(b)
    if not wa or not wb:
        return False
    return len(wa & wb) / len(wa | wb) >= SIMILAR


def _repeat_path():
    d = LOG_DIR if LOG_DIR else os.path.expanduser(os.environ.get("FLEET_FUNNEL_DIR", "~/.hermes/cache/fleet-funnel"))
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, REPEAT_FILE)


def _repeats():
    try:
        with open(_repeat_path(), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


_DISPATCH_RE = re.compile(r"DISPATCH-[A-Za-z0-9_.-]+-\d{10,}")


def _same_ask(a, b):
    """True when two ask texts are the same ask. Dispatch identity is material: different attempts
    are different potential losses, and generic word similarity strips their numeric IDs."""
    ma, mb = _DISPATCH_RE.search(a or ""), _DISPATCH_RE.search(b or "")
    if ma or mb:
        return bool(ma and mb and ma.group() == mb.group())
    return similar(a, b)


def _repeat_key(esc):
    marker = _DISPATCH_RE.search(esc.text)
    return "%s|%s|%s" % (esc.session, esc.reason.value, marker.group() if marker else fingerprint(esc.text))


def repeat_window(text):
    """How long a page suppresses its repeats. A \"still blocked, nothing changed\" status restating a hold
    already told to the operator is not news: it waits the (longer) status window, from policy."""
    if policy.Rx("idle_status").search(text or ""):
        return max(REPEAT_WINDOW, int(policy.get("status_repeat_window_s")))
    return REPEAT_WINDOW


def _record_repeat(key, now, text, confirmed):
    """Write one ask into the ledger. False when it could not be made durable.

    An entry is UNCONFIRMED until the page it stands for has itself been persisted by the daemon
    (see commit_repeats). An unconfirmed entry only suppresses repeats while that page is still in
    the live queue: if the process dies first, the queue is gone, and the ledger must not keep
    silencing a page nobody ever saw."""
    keep = max(REPEAT_WINDOW, int(policy.get("status_repeat_window_s")))
    seen = {k: v for k, v in _repeats().items() if isinstance(v, list) and now - v[0] < keep}
    seen[key] = [now, text[:400], bool(confirmed)]
    try:
        tmp = _repeat_path() + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(seen, f)
        os.replace(tmp, _repeat_path())
    except OSError:
        return False
    return True


def repeated(esc, now, pending=None):
    """True when a similar ask from the same session and reason was already paged inside the window.
    Records the ask (unconfirmed) when it was not. `pending` is the live queue: an unconfirmed
    ledger entry counts only while its page is still queued there."""
    if esc.reason in NEVER_SUPPRESS:
        return False
    prefix = "%s|%s|" % (esc.session, esc.reason.value)
    queued = {p.get("repeat_key") for p in (pending or [])}
    again = False
    for k, v in _repeats().items():
        if not (k.startswith(prefix) and isinstance(v, list) and now - v[0] < repeat_window(esc.text)):
            continue
        confirmed = v[2] if len(v) > 2 else True      # entries from before the flag were confirmed
        if (confirmed or pending is None or k in queued) and _same_ask(esc.text, v[1]):
            again = True
            break
    if not again and not _record_repeat(_repeat_key(esc), now, esc.text, pending is None):
        return False                       # cannot remember: page rather than risk silence
    return again


def commit_repeats(pending):
    """Confirm the ledger entry of every queued page. Call AFTER the queue has been persisted."""
    for item in pending:
        key = item.get("repeat_key")
        if key and not item.get("repeat_done"):
            if _record_repeat(key, item.get("at", int(time.time())), item.get("match", ""), True):
                item["repeat_done"] = True


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
    if policy.is_inform(esc.reason) and esc.situation != "inform":
        # News, not a question: rephrase a raw event token into what happened. Detail is kept.
        detail = re.sub(r"^\S+-\d{6,}\s*(?:--\s*)?|^[A-Z][A-Z-]+-\S+\s*(?:--\s*)?", "", esc.text or "")[:300]
        news = policy.inform_text(esc.reason, esc.session, detail)
        if news:
            esc.text, esc.situation = news, "inform"
    row = {"at": now, "session": esc.session, "reason": esc.reason.value, "rule": esc.rule,
           "class": "inform" if policy.is_inform(esc.reason) else "ask",
           "text": esc.text[:400], "urgent": esc.urgent, "hard": esc.reason in HARD,
           "evidence": esc.evidence, "mode": mode()}
    if mode() == "enforce" and repeated(esc, now, pending=pending):
        _append(ESCALATIONS, {**row, "kind": "suppressed_repeat", "handled": True})
        return row                         # ledgered, not queued: the operator already knows
    _append(ESCALATIONS, row)
    item = {"session": esc.session, "match": esc.text, "at": now, "urgent": esc.urgent,
            "reason": esc.reason.value}
    if mode() == "enforce" and esc.reason not in NEVER_SUPPRESS:
        item["repeat_key"] = _repeat_key(esc)     # ledgered by commit_repeats once persisted
    pending.append(item)
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
