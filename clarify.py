"""Answering a session's question from the goals registered for it, or saying plainly that it cannot.

A NEEDS-INPUT is answered here only when a line the operator wrote into the session's focus file
already decides it. The answer IS that line, quoted, inside a fixed frame: nothing is composed, so
there is nothing to validate beyond "the quoted line exists in the file". Everything else, which is
most questions, becomes a page the operator can answer in one line.

When no registered decision covers the question, the session's own recommendation is adopted (see
`recommendation` and `adopt`). A stalled session costs more than a reversible wrong choice, so the order
of outcomes is: right answer, then a reversible wrong one, then silence, then an unsafe one. Only the
last is guarded hard: gated shapes, injection-shaped text and irreversible recommendations page instead.

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


# ---- adopting the session's own recommendation ------------------------------------------
# The ask is text a session wrote, which may itself quote untrusted content it read. It is data. Text
# that tries to instruct the manager, or the model behind it, is never adopted.
INJECTION = re.compile(
    r"ignore (?:all |any |the )?(?:previous|prior|above|earlier)|disregard (?:the |your |all )?(?:previous|prior|above|"
    r"instructions|rules)|you are now|new instructions?:|system prompt|as the (?:fleet|manager|operator)[, ]+(?:i|you)|"
    r"tell the (?:fleet|manager) to|override (?:the )?(?:gate|classifier|safeguard)|"
    r"(?:reveal|print|send|post|exfiltrate|upload) (?:the |your |any )?(?:token|key|secret|credential|password|env)|"
    r"curl [^\n]*\|\s*(?:ba)?sh|base64 -d", re.I)

IRREVERSIBLE = re.compile(
    r"\b(?:delete|drop|wipe|purge|destroy|force[- ]push|reset --hard|revoke|rotate|disable (?:the )?(?:auth|check|gate|"
    r"review|ci)|skip (?:the )?(?:review|tests?|ci|gate)|bypass|merge (?:to|into) (?:main|prod)|deploy to prod|"
    r"production deploy|ship to prod|email(?:ing)? (?:the )?client|message (?:the )?client|post to (?:slack|the client))\b", re.I)

# "Recommendation: X", "I recommend X", "recommended: X", "my pick is X", "default: X"
RECO = re.compile(
    r"(?:\brecommendation|\brecommended|\bI(?:'d| would| will)? recommend|\bmy (?:pick|choice|recommendation)(?: is)?|"
    r"\bI(?:'d| would) (?:pick|choose|go with)|\bgo(?:ing)? with)\s*(?:is|:|-)?\s*(.{12,400}?)(?:\n\n|\n(?=[A-Z0-9*#-])|$)",
    re.I | re.S)
DEFAULT = re.compile(r"\bdefault\s*(?:is|:|-)\s*(.{8,300}?)(?:\n\n|\n(?=[A-Z0-9*#-])|$)", re.I | re.S)

ADOPT_FRAME = ("Proceed with your own recommendation: {reco} Keep it reversible and on the registered goal. "
               "If doing it would delete, deploy, spend, touch credentials or widen access, stop and emit "
               "NEEDS-INPUT naming exactly that step instead.")
PICK_FRAME = ("No registered decision covers this and you named no recommendation. Take the option that is "
              "reversible, smallest, and closest to the registered goal, say which one you took and why in one "
              "line, and continue. If every option is irreversible or gated, emit NEEDS-INPUT naming that.")


def recommendation(ask):
    """The session's stated recommendation, or its stated default, or None. Quoted, never composed."""
    for rx in (RECO, DEFAULT):
        m = rx.search(ask or "")
        if m:
            return " ".join(m.group(1).split()).rstrip(" .;,") + "."
    return None


# A choice: only strong signals. Prose lists, "needs X, Y or Z" and numbered blockers are NOT choices.
CHOICE = re.compile(
    r"\boption\s+[a-d1-4]\b|\(\s*[a-d]\s*\)\s+\S.{3,}?\(\s*[a-d]\s*\)\s+\S|"
    r"\bwhich (?:of (?:these|the)|one|way|option|approach)\b|\bshould (?:i|we)\b[^?.]{3,80}\bor\b|"
    r"\b(?:choose|pick|decide) between\b|\bwhich\b[^?.]{2,60}\b\w+ or \w+\s*\?", re.I)
# The session is asking the operator to supply something, not to pick: never adopt.
NEEDS_SUPPLY = re.compile(
    r"\bI need (?:a|an|the|your|her|his)\b|\bneeds? (?:a|an|the) (?:\w+ ){0,3}(?:client|key|token|secret|credential|"
    r"password|login|account|approval|access)\b|\b(?:blocked|waiting) on\b|\bcan(?:'|no)t proceed\b|"
    r"\b(?:please )?(?:provide|supply|give me|grant)\b", re.I)
# Idle / blocked / status text that asks for new work, not a decision.
IDLE = re.compile(
    r"\b(?:i(?:'m| am) idle|still idle|nothing (?:has )?(?:changed|happened)|no change|still blocked|"
    r"need the next (?:\w+ )?brief|go-ahead for|waiting for (?:the )?next|awaiting (?:the )?next)\b", re.I)


def is_choice(ask):
    """True when the ask presents options to pick between. Status and 'give me work' text is not one."""
    a = ask or ""
    if NEEDS_SUPPLY.search(a) and not RECO.search(a):
        return False
    return bool(CHOICE.search(a)) and not (IDLE.search(a) and not RECO.search(a))


def adoptable(ask, reco=None):
    """(ok, why-not). The screen a recommendation passes before the manager repeats it back."""
    if INJECTION.search(ask or ""):
        return False, "the question contains text that tries to instruct the manager"
    if reco and IRREVERSIBLE.search(reco):
        return False, "the recommendation is irreversible or outward-facing"
    return True, ""


def held_for_operator(sn, ask):
    """The focus-file line that says this very question is waiting on the operator, or None. A hold the
    operator wrote outranks the session's recommendation and the safest-option rule: it pages."""
    aw = _words(ask)
    if len(aw) < MIN_SHARED:
        return None
    pending = _pending_re()
    for n, line in focus_lines(sn):
        if not pending.search(line):
            continue
        shared = len(aw & _words(line))
        if shared >= MIN_SHARED and shared / len(aw) >= MIN_SHARE:
            return "focus:L%d" % n
    return None


def adopt(sn, ask, dispatch, now):
    """-> (kind, reason, text, detail) when the session's own recommendation (or the safest-option rule) can
    stand in for a registered decision, else None so the caller pages."""
    held = held_for_operator(sn, ask)
    if held:
        return "page", "clarification_ungrounded", "a line on record holds this for the operator (%s)" % held, {"cite": held}
    reco = recommendation(ask)
    ok, why = adoptable(ask, reco)
    if not ok:
        return "page", "clarification_ungrounded", why, {}
    if not (ask or "").strip():
        return None                                  # no words at all: nothing to adopt, a person looks
    if not reco and not is_choice(ask):
        return None                                  # not a choice (idle, blocked, wants new work): a person looks
    text = ADOPT_FRAME.format(reco=reco) if reco else PICK_FRAME
    if not dispatch(sn, text):
        return "page", "no_safe_nudge", "the answer could not be delivered", {"cite": "session:recommendation"}
    _remember(sn, ask, now)
    return "answered", None, text, {"cite": "session:recommendation" if reco else "rule:safest-option"}


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
        got = adopt(sn, ask, dispatch, now)
        if got:
            return got
        return "page", "clarification_ungrounded", "no registered decision covers this question", {}
    line, cite = hit
    text = FRAME.format(line=line)
    if not dispatch(sn, text):
        return "page", "no_safe_nudge", "the answer could not be delivered", {"cite": cite}
    _remember(sn, ask, now)
    return "answered", None, text, {"cite": cite}
