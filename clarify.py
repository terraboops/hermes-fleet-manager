"""Answering a session's question from the goals registered for it, or saying plainly that it cannot.

A NEEDS-INPUT is answered here only when a line the operator wrote into the session's focus file
already decides it. The answer IS that line, quoted, inside a fixed frame: nothing is composed, so
there is nothing to validate beyond "the quoted line exists in the file". Everything else, which is
most questions, becomes a page the operator can answer in one line.

A line only counts as a decision when it says so (SETTLED, RULING IN FORCE, do not re-raise, ...)
and does not also say it is waiting on the operator (awaiting, RAISED, her call ...). A session that
asks the same thing again after being answered has not accepted the answer, which is a refusal.
"""
import hashlib
import json
import os
import re
import time

FUNNEL_DIR = os.path.expanduser(os.environ.get("FLEET_FUNNEL_DIR", "~/.hermes/cache/fleet-funnel"))
FOCUS_DIR = os.path.expanduser(os.environ.get(
    "LAYA_GATE_FOCUS_DIR", "~/.hermes/scripts/cc-watch/overwatch"))

DECISION = re.compile(
    r"SETTLED|RULING IN FORCE|do not re-?raise|not worth a report|ALREADY DECIDED|DECIDED:|"
    r"do not nudge the session on|ROUTED AROUND", re.I)
def _operator_names():
    """Names the operator goes by in focus files, from FLEET_OPERATOR_NAMES (comma separated) or a
    one-per-line file in the funnel directory. Kept out of the code on purpose."""
    raw = os.environ.get("FLEET_OPERATOR_NAMES", "")
    if not raw:
        try:
            with open(os.path.join(FUNNEL_DIR, "operator_names"), encoding="utf-8") as f:
                raw = ",".join(f.read().split())
        except OSError:
            raw = ""
    return [re.escape(n.strip()) for n in raw.split(",") if n.strip()]


def _pending_re():
    who = "|".join(["her", "his", "their", "the operator", "the owner"] + _operator_names())
    return re.compile(
        r"awaiting|waiting on|\bRAISED\b|(?:her|his|their) (?:call|decision|one-line|word)|"
        r"needs? (?:%s)|pending (?:her|his|their)|one-line fix for (?:%s)|open loop" % (who, who), re.I)
STOP = set("""this that with from have what which when will your you the and for are not but can
would should could into about there their they them then than been being also just does done
need needs session work make made using used use want only more some such each other over under
please still next first after before while where whether because via per""".split())
REPEAT_WINDOW = 86400
MIN_SHARED = 3
MIN_SHARE = 0.25

FRAME = ("Standing decision on record for this work: {line} Proceed on that basis. If it does not "
         "cover what you need, emit NEEDS-INPUT naming exactly what it does not cover.")


def _words(text):
    return {w for w in re.findall(r"[a-z0-9_]{4,}", (text or "").lower()) if w not in STOP}


def focus_lines(sn):
    """[(line number, text)] of the session's focus file."""
    try:
        with open(os.path.join(FOCUS_DIR, "%s_focus.md" % sn), encoding="utf-8") as f:
            return [(i + 1, l.strip()) for i, l in enumerate(f) if l.strip()]
    except OSError:
        return []


def ground(sn, ask):
    """(line text, 'focus:L<n>') of the one registered decision that answers `ask`, or None."""
    aw = _words(ask)
    if len(aw) < MIN_SHARED:
        return None
    pending = _pending_re()
    best = None
    for n, line in focus_lines(sn):
        if not DECISION.search(line) or pending.search(line):
            continue
        shared = len(aw & _words(line))
        if shared >= MIN_SHARED and shared / len(aw) >= MIN_SHARE and (best is None or shared > best[0]):
            best = (shared, line, "focus:L%d" % n)
    return (best[1], best[2]) if best else None


def cite_ok(sn, cite, text):
    """True when `cite` names a line of the session's focus file and `text` is that line."""
    m = re.fullmatch(r"focus:L(\d+)", cite or "")
    if not m:
        return False
    want = " ".join((text or "").split())
    return any(n == int(m.group(1)) and want and want in " ".join(line.split())
               for n, line in focus_lines(sn))


def gated_shape(ask):
    """The reason a question must never be auto-answered, or None: it asks for something destructive,
    money-shaped or credential-shaped."""
    try:
        import laya_gate
        for pat, _a, why in laya_gate.AFFIRM_ESCALATE:
            if re.search(pat, ask or "", re.I):
                return why
    except Exception:
        return "could not check the question against the gated shapes"
    return None


def _ask_hash(ask):
    return hashlib.sha1(" ".join((ask or "").lower().split())[:300].encode()).hexdigest()[:12]


def _answered():
    try:
        with open(os.path.join(FUNNEL_DIR, "answered.json"), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _remember(sn, ask, now):
    d = {k: v for k, v in _answered().items() if now - v < REPEAT_WINDOW}
    d["%s|%s" % (sn, _ask_hash(ask))] = now
    os.makedirs(FUNNEL_DIR, exist_ok=True)
    tmp = os.path.join(FUNNEL_DIR, "answered.json.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(d, f)
    os.replace(tmp, os.path.join(FUNNEL_DIR, "answered.json"))


def resolve(sn, ask, dispatch, now=None):
    """-> (kind, reason, text, detail). kind is 'answered' or 'page'. `dispatch(sn, text)` -> bool."""
    now = time.time() if now is None else now
    gated = gated_shape(ask)
    if gated:
        return "page", "gated_action", "asks for something gated (%s)" % gated, {}
    if now - _answered().get("%s|%s" % (sn, _ask_hash(ask)), 0) < REPEAT_WINDOW:
        return "page", "nudge_refused", "asked the same thing again after it was answered", {}
    hit = ground(sn, ask)
    if not hit:
        return "page", "clarification_ungrounded", "no registered decision covers this question", {}
    line, cite = hit
    text = FRAME.format(line=line)
    if not dispatch(sn, text):
        return "page", "no_safe_nudge", "the answer could not be delivered", {"cite": cite}
    _remember(sn, ask, now)
    return "answered", None, text, {"cite": cite}
