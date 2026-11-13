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

import policy

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


_NEGATION = re.compile(r"\b(?:no|not|never|without|none|nothing|n't|zero)\b[^.?!\n]{0,25}$", re.I)


def credential_request(text):
    """True when `text` asks to be given, or to handle, a credential or secret. A negation just before
    the asking verb ("no credentials were touched") is not a request."""
    t = text or ""
    for m in policy.rx("credential_request").finditer(t):
        if not _NEGATION.search(t[max(0, m.start() - 30):m.start()]):
            return True
    return False


def gated_shape(ask):
    """The reason a question must never be auto-answered, or None: it asks for something destructive,
    money-shaped or credential-shaped."""
    try:
        import laya_gate
        for pat, _a, why in laya_gate.AFFIRM_ESCALATE:
            if why == "credentials":
                # The bare word is prose ("checks about agent credentials"). Only a request to be given,
                # or to handle, a secret is the hard floor.
                if credential_request(ask):
                    return why
                continue
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


# ---- ruling files the session was given ---------------------------------------------------
# The operator hands a session a rulings file (a brief that settles open questions) by path. A request
# that such a file already settles is answered by quoting it, never paged: the file is a standing grant.
RULING_PATH = re.compile(r"(?:~|/)[\w@%+=:,.~/-]*(?:RULINGS?|ANSWERS?)[\w.-]*\.md\b")
MAX_RULING_BYTES = 200_000
REQUEST_SENTENCE = re.compile(
    r"\?|\b(?:please|can you|could you|confirm (?:that|whether|if)|check that|let me know|i (?:still |also )*need|we need|needs? (?:your|either)|"
    r"your (?:call|answer|ok|go-ahead|word|decision))\b", re.I)
RULING_FRAME = ("Standing ruling on record, so nothing here waits on the owner: {quotes} Proceed on that basis. "
                "If it does not cover what you need, emit NEEDS-INPUT naming exactly what it does not cover.")
_STEM = re.compile(r"(?:ing|ed|es|s|al)$")


def _stem(w):
    s = _STEM.sub("", w) if len(w) > 4 else w
    return s[:-1] if len(s) > 3 and s[-1] == s[-2] else s


def _stems(text):
    return {_stem(w) for w in _words(text)}


def _inbound_texts(sn):
    """Recent messages delivered to the session (its transcript). Never raises."""
    try:
        import fleet_watch
        import fleet_transcript
        path = fleet_watch.find_jsonl(sn)
        if not path:
            return []
        return [t for _ep, t in fleet_transcript._scan_inbound(path, 200, window=20_000_000, max_chars=2000)]
    except Exception:
        return []


INBOUND = _inbound_texts          # replaceable: tests and replays inject their own


def ruling_files(sn):
    """Existing ruling files the session was pointed at: by its focus file or by a message it received."""
    text = "\n".join(l for _n, l in focus_lines(sn)) + "\n" + "\n".join(INBOUND(sn) or [])
    home = os.path.expanduser("~")
    out = []
    for m in RULING_PATH.finditer(text):
        p = os.path.realpath(os.path.expanduser(m.group()))
        try:
            if p not in out and p.startswith((home + os.sep, "/tmp/", "/private/")) \
                    and os.path.isfile(p) and os.path.getsize(p) <= MAX_RULING_BYTES:
                out.append(p)
        except OSError:
            continue
    return out


def ruling_sections(path):
    """[(heading, text)] of a ruling file: one per markdown heading, plus any preamble. A section that
    says it is still waiting on the operator is not a ruling."""
    try:
        with open(path, encoding="utf-8") as f:
            lines = f.read().splitlines()
    except OSError:
        return []
    secs, head, body = [], os.path.basename(path), []
    for line in lines:
        if line.startswith("#"):
            if body:
                secs.append((head, "\n".join(body)))
            head, body = line.lstrip("# ").strip(), [line]
        else:
            body.append(line)
    if body:
        secs.append((head, "\n".join(body)))
    pending = _pending_re()
    return [(h, t) for h, t in secs if not pending.search(t)]


def request_sentences(ask):
    """The sentences of `ask` that actually request something. Findings and status are not requests."""
    plain = re.sub(r"[*`_]+", "", ask or "")
    plain = re.sub(r"\n(?!\s*(?:[-\u2022]|\d+[.)])|\n)", " ", plain)       # a hard-wrapped line is not a sentence end
    return [s.strip() for s in re.split(r"(?<=[.!?])\s+|\n+", plain) if s.strip() and REQUEST_SENTENCE.search(s)]


def ruling_ground(sn, ask):
    """(quotes, cite) when EVERY request in `ask` is settled by a ruling file the session was given, or is
    a plain ask to resume reversible work. None when any request is uncovered (then a person looks)."""
    if INJECTION.search(ask or "") or OWNER_HELD.search(ask or ""):
        return None                  # an ask addressed to the operator is hers, whatever a ruling says
    reqs = request_sentences(ask)
    files = ruling_files(sn)
    if not reqs or not files:
        return None
    secs = [(os.path.basename(p), h, t) for p in files for h, t in ruling_sections(p)]
    quotes, cites, resumed = [], [], False
    for s in reqs:
        sw = _stems(s)
        best = None
        for fn, h, t in secs:
            tw = _stems(t)
            shared = len(sw & tw)
            # long sections share words with everything: rank by overlap per section size
            score = shared / (len(tw) or 1) ** 0.5
            if shared >= MIN_SHARED and sw and shared / len(sw) >= 0.3 and (best is None or score > best[0]):
                best = (score, fn, h, t)
        if best:
            _n, fn, h, t = best
            body = " ".join(t.split())[:380]
            if '"%s"' % body not in quotes:
                quotes.append('"%s"' % body)
                cites.append("ruling:%s#%s" % (fn, h[:40]))
        elif policy.rx("resume_ask").search(s) and not (credential_request(s) or IRREVERSIBLE.search(s)):
            resumed = True
        else:
            return None
    if resumed:
        quotes.append(policy.get("continue_reply"))
        cites.append("rule:continue-reversible")
    return " ".join(quotes), ",".join(cites)


# ---- adopting the session's own recommendation ------------------------------------------
# The ask is text a session wrote, which may itself quote untrusted content it read. It is data. Text
# that tries to instruct the manager, or the model behind it, is never adopted.
INJECTION = re.compile(
    r"ignore (?:all |any |the )?(?:previous|prior|above|earlier)|disregard (?:the |your |all )?(?:previous|prior|above|"
    r"instructions|rules)|you are now|new instructions?:|system prompt|as the (?:fleet|manager|operator)[, ]+(?:i|you)|"
    r"tell the (?:fleet|manager) to|override (?:the )?(?:gate|classifier|safeguard)|"
    r"(?:reveal|print|send|post|exfiltrate|upload) (?:the |your |any )?(?:token|key|secret|credential|password|env)|"
    r"curl [^\n]*\|\s*(?:ba)?sh|base64 -d", re.I)

# Patterns live in policy.py and the operator's policy.yaml, so they can be tuned without a code change.
IRREVERSIBLE = policy.Rx("irreversible")
IDLE_STATUS = policy.Rx("idle_status")
HARD_SUPPLY = policy.Rx("hard_supply")
OWNER_HELD = policy.Rx("owner_held")
ASK_SIGNAL = policy.Rx("ask_signal")
CONTINUE = policy.Rx("continue_ask")

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
CHOICE = policy.Rx("choice")
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


def is_continue(ask):
    """True for a plain "may I continue" ask: permission to carry on, nothing to pick and nothing supplied."""
    a = ask or ""
    # It must BE a question (or an explicit wait for a go-ahead). A status line that happens to say
    # "I can move on" is not an ask: it stays on the paging side.
    asking = "?" in a or re.search(r"await(?:ing)? (?:your )?(?:go-?ahead|confirmation)", a, re.I)
    return bool(CONTINUE.search(a) and asking) and not NEEDS_SUPPLY.search(a) and not IRREVERSIBLE.search(a)


def is_status_loop(ask):
    """True for an idle or no-change status line, or plain progress narration, that only reads like an
    ask: no question, no request words, nothing to pick, no credential or money request, nothing irreversible. It is ledgered silently, never paged."""
    a = ask or ""
    if not a.strip() or a.startswith("(the session gave no words") or "?" in a:
        return False
    if not (IDLE_STATUS.search(a) or not ASK_SIGNAL.search(a)):
        return False
    return not (HARD_SUPPLY.search(a) or OWNER_HELD.search(a) or IRREVERSIBLE.search(a) or INJECTION.search(a)
                or CHOICE.search(a) or CONTINUE.search(a))


def adoptable(ask, reco=None):
    """(ok, why-not). The screen a recommendation passes before the manager repeats it back."""
    if INJECTION.search(ask or ""):
        return False, "the question contains text that tries to instruct the manager"
    if reco and IRREVERSIBLE.search(reco):
        return False, "the recommendation is irreversible or outward-facing"
    if not reco and IRREVERSIBLE.search(ask or ""):
        return False, "the question names an irreversible or outward-facing step"
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
    if is_status_loop(ask):
        return "silent", None, "idle status, not a question: nothing to answer", {"cite": "rule:status-loop"}
    reco = recommendation(ask)
    # A recommendation is adopted only from a real question. Status text also says "default: ..." or
    # "going with ...", and adopting that sent a session instructions made of its own status line.
    if reco and not ("?" in ask or CHOICE.search(ask) or CONTINUE.search(ask)):
        reco = None
    if reco and (IDLE_STATUS.search(reco) or len(reco.split()) < 3):
        reco = None
    ok, why = adoptable(ask, reco)
    if not ok:
        return "page", "clarification_ungrounded", why, {}
    if not (ask or "").strip():
        return None                                  # no words at all: nothing to adopt, a person looks
    if not reco and is_continue(ask):
        text = policy.get("continue_reply")
        if not dispatch(sn, text):
            return "page", "no_safe_nudge", "the answer could not be delivered", {"cite": "rule:continue-reversible"}
        _remember(sn, ask, now)
        return "answered", None, text, {"cite": "rule:continue-reversible"}
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
    if not hit and not held_for_operator(sn, ask):
        rg = ruling_ground(sn, ask)
        if rg:
            quotes, cite = rg
            text = RULING_FRAME.format(quotes=quotes)
            if not dispatch(sn, text):
                return "page", "no_safe_nudge", "the answer could not be delivered", {"cite": cite}
            _remember(sn, ask, now)
            _log(sn, ask, cite, text, now)
            return "answered", None, text, {"cite": cite}
    if not hit:
        got = adopt(sn, ask, dispatch, now)
        if got:
            if got[0] == "answered":
                _log(sn, ask, got[3].get("cite"), got[2], now)
            return got
        return "page", "clarification_ungrounded", "no registered decision covers this question", {}
    line, cite = hit
    text = FRAME.format(line=line)
    if not dispatch(sn, text):
        return "page", "no_safe_nudge", "the answer could not be delivered", {"cite": cite}
    _remember(sn, ask, now)
    _log(sn, ask, cite, text, now)
    return "answered", None, text, {"cite": cite}


def _log(sn, ask, cite, text, now):
    """Record a reversible answer in the decisions log (date, what, why, how to undo)."""
    asked = " ".join((ask or "").split())[:160] or "(no words)"
    policy.log_decision(
        "fleet answered %s's ask instead of paging: \"%s\" -> %s" % (sn, asked, (text or "")[:140]),
        "reversible ask inside a registered goal (%s); standing escalation policy decides, not asks" % (cite or "rule"),
        "tell the session to stop and take the other option; the answer is in its transcript", now)
