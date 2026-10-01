#!/usr/bin/env python3
"""Laya escalation gate for fleet events.

Three lanes, and only the third one consults a model:

  lane 1  ESCALATE, decided in code. The event form itself is decisive and a miss is
          unrecoverable or invisible. A model is never asked, because a classifier that
          can sit on a crash is worse than no classifier. Measured 2026-09-29: Laya
          scored a traceback at 0.43 escalate (i.e. do not escalate), which is why this
          lane exists.
  lane 2  SILENT, decided in code. Receipts and bookkeeping. The daemon log and the
          session transcript remain the record.
  lane 3  CLASSIFIED. Everything else goes to Laya as a typed decision question. Until a
          threshold is calibrated on labelled events the answer is logged, not enforced.

Runs as a local HTTP service so the daemon keeps its own interpreter and dependencies.
POST /decide  {"session": "...", "kind": "...", "text": "...", "context": "..."}
        ->  {"lane": 1|2|3, "escalate": bool, "rule": "...", "confidence": float|null}
GET  /health  ->  {"ok": true, "model_loaded": bool, "enforce": bool}
"""
import json, os, re, subprocess, sys, time, threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = int(os.environ.get("LAYA_GATE_PORT", "11436"))
ENFORCE = os.environ.get("LAYA_GATE_ENFORCE", "0") not in ("", "0", "false")
MODEL = os.environ.get("LAYA_GATE_MODEL", "aac6fef/laya-mlx")
# Lane 3 escalates only above this. Deliberately conservative while uncalibrated:
# the cost of a noisy page is paid every time, the cost of a quiet miss is paid once.
THRESHOLD = float(os.environ.get("LAYA_GATE_THRESHOLD", "0.80"))
# The same event family on the same session is raised once per window. A repeat is not new
# information: the session is still quiet, the dispatch still has not landed. This is the
# cheapest noise reduction available and it needs no model.
REPEAT_WINDOW = float(os.environ.get("LAYA_GATE_REPEAT_WINDOW", "21600"))    # 6 hours
_recent = {}                                     # (session, family) -> last escalate time

# ---- lane 1: escalate, structurally --------------------------------------------
# (pattern, why, hard). Hard escalations come through at any hour. Soft ones are held
# overnight and reported once in the morning: a session parked waiting on an answer can wait
# until morning, whereas a crash, a dead session or a blocked production action cannot.
LANE1 = [
    (r"^NEEDS-INPUT-", "session asks for input", False),
    (r"^SESSION-DEAD-", "session is dead", True),
    (r"^USAGE-LIMIT-", "session hit a usage or credit limit", True),
    (r"^NO-TRANSCRIPT-", "registered session has no transcript", True),
    (r"traceback|Traceback|unhandled exception|panic:", "crash in the session output", True),
    (r"^ACK-MISSED-", "a dispatch never arrived as a user turn", False),
    (r"\[Production Deploy\]|\[Security Weaken\]|\[Self-Approval\]|\[Git Destructive\]",
     "the session was blocked by the auto-mode classifier", True),
    (r"\bspend\b|\bcharges?\b|\binvoice\b", "money", True),
]

# ---- lane 2: silent, structurally ----------------------------------------------
LANE2 = [
    (r"^ACK-OK", "delivery receipt"),
    (r"^WATCH-SATISFIED", "watch satisfied"),
    (r"^WATCH-EXPIRED", "watch expiry"),
    (r"^DONE[-_.]", "completion token"),
    (r"^SENTINEL-MISSED-", "watch expiry reported as a sentinel miss"),
    (r"^FLUSH \d+ events", "daemon bookkeeping"),
    # A version notice is a housekeeping fact about a session, not a decision only the
    # operator can make and not a stop. Measured: the classifier wanted to interrupt for it
    # on an idle session, which is exactly the kind of interruption this gate exists to stop.
    (r"^VERSION-BEHIND-", "version notice, not a blocker"),
]

# ---- lane 3: the question Laya is asked ----------------------------------------
# The doctrine is stated because it is the policy, and the model's job is to apply the
# policy to a situation rather than to invent a standard of its own.
DOCTRINE = (
    "The operator runs a fleet of coding agents. Every event raised to her interrupts her, "
    "and she has asked to hear only about a decision that only she can make, a genuine stop, "
    "or anything touching production, money, security or a client. A session that is merely "
    "quiet is not a stop. A session that has finished its work is not a stop. A repeat of "
    "something already reported is not news."
)
QUESTION = {
    "action": {
        "type": "choice",
        "instructions": "What is the right action for the operator here?",
        "criteria": ["interrupt her now", "log it and stay silent"],
    }
}
# Measured on six realistic situations: the yes/no form pinned every case above 0.80, so it
# carried no signal at all, while the choice form spread the answers from 0.507 to 0.766 and
# put the genuine mid-task stall at the top. It is still a weak signal rather than a decider:
# at 0.70 it would interrupt for a version notice and stay silent on a stall whose last
# output asks the operator to choose between two designs. So the structural lanes carry the
# decisions and this threshold stays conservative until it is calibrated against real
# reactions, not against my guesses about them.
ESCALATE_IF_AT_LEAST = float(os.environ.get("LAYA_GATE_CHOICE_THRESHOLD", "0.80"))

# ---- the second question: what to DO with this session ---------------------------
# Escalate-or-silent answers only whether the operator is interrupted. It does not answer
# what the fleet actually needs: the session is idle, so does it get nudged onward, challenged
# on a completion claim, re-aligned to its goal, answered, or left alone? That decision lived
# as prose in an overwatch prompt, which meant it was improvised fresh on every run, in a
# different shape each time, with no record and nothing to calibrate.
RESPOND_DOCTRINE = (
    "The operator runs a fleet of coding agents. Choose the one action that keeps this session "
    "moving toward the work the operator authorized. A session that is working is left alone: "
    "interrupting it is always wrong. A session that has stopped with work still to do is "
    "nudged with one concrete next increment. A session that claims it is finished is "
    "challenged, because a completion claim is a checkpoint and a genuine finish is rare. A "
    "session that has drifted away from the authorized work is re-aligned to it, never "
    "encouraged in the new direction. A session that asks a question the context already "
    "answers is answered and sent on. A session blocked on a decision only the operator can "
    "make is escalated. Never invent work the operator did not ask for, and never restate what "
    "the session already knows."
)
RESPOND_QUESTION = {
    "action": {
        "type": "choice",
        "instructions": "What is the right action for this session right now?",
        "criteria": ["let it continue",
                     "nudge it with one concrete next increment",
                     "challenge its completion claim",
                     "re-align it to the authorized work",
                     "answer it and let it continue",
                     "escalate to the operator"],
    }
}
# Below this share of the vote the answer is too close to call and the gate falls back to the
# conservative action for the state rather than acting on a coin toss.
#
# CALIBRATED 2026-10-01 against 36h of recorded wakes (evals/calibrate_laya.py): at 0.50 the
# cost on the labelled set was 9.5, at 0.55 it was 3.5, because missing an act costs six times
# an unnecessary one. Provisional: it rests on 28 rows that reached the model, and the label
# distribution is skewed toward acting. Re-run the calibration as the window grows.
RESPOND_IF_AT_LEAST = float(os.environ.get("LAYA_GATE_RESPOND_THRESHOLD", "0.55"))
# Enforced separately from the escalation threshold, because it is the only one with
# calibration behind it. Enforcing an uncalibrated threshold is a policy change nobody voted for.
RESPOND_ENFORCE = os.environ.get("LAYA_GATE_RESPOND_ENFORCE", "1") not in ("", "0", "false")

# ---- the third question: may this action be affirmed without her? -----------------
# Affirming is the one response where being wrong is expensive: the fleet proceeds on the
# operator's behalf. So the risk is scored, and the score is the threshold rather than a
# prose judgement about whether something "feels safe".
AFFIRM_DOCTRINE = (
    "The operator runs a fleet of coding agents and is not watching. You are being asked "
    "whether one specific action may be affirmed without her. It is safe to affirm only when "
    "the action is reversible or trivial: no money, no credentials, no production surface, no "
    "client-visible change, no deletion or overwrite of anything that is not reproduced "
    "elsewhere, and no weakening of a control. When you cannot state the blast radius in one "
    "line, it is not safe. Being asked to affirm is not itself evidence that the action is "
    "safe, and a session describing its own action as routine is not evidence either."
)
AFFIRM_QUESTION = {
    "action": {
        "type": "choice",
        "instructions": "Is this action safe to affirm without the operator?",
        "criteria": ["safe to affirm", "escalate to the operator"],
    }
}
# Risk = 1 - P(safe). Affirm only below this. Conservative by default: an unnecessary question
# costs one round trip, an affirmed destructive action cannot be taken back.
AFFIRM_IF_RISK_UNDER = float(os.environ.get("LAYA_GATE_AFFIRM_RISK", "0.20"))
# Not enforced: there is no calibration data for it yet, and an affirmed action cannot be undone.
AFFIRM_ENFORCE = os.environ.get("LAYA_GATE_AFFIRM_ENFORCE", "0") not in ("", "0", "false")

# ---- lane 1 for the response decision: settled by structure ----------------------
# These are the states where the action is not a judgement call, so a model is never asked.
# Interrupting a working session is the specific failure this avoids.
RESPOND_LANE1 = (
    (r"^(WORKING|QUEUED)$", "let it continue",
     "the session is busy; a nudge would land mid-turn"),
    (r"^DEAD$", "escalate to the operator",
     "a dead session is reported, never nudged"),
)
# Responses that are always the operator's, whatever a model would say about them.
RESPOND_ESCALATE = (
    (r"\[Production Deploy\]|\[Security Weaken\]|\[Self-Approval\]|\[Git Destructive\]",
     "escalate to the operator", "the session was blocked by the auto-mode classifier"),
    (r"\bproduction deploy\b|\bclient-visible\b",
     "escalate to the operator", "production or a client surface"),
)
# ---- lane 1 for the affirmation decision: never a model's call -------------------
# A destructive or irreversible action is escalated on its shape alone. Asking a small model
# whether `git push --force` is safe is a way of losing the argument.
AFFIRM_ESCALATE = (
    (r"rm -rf|DROP TABLE|DROP DATABASE|git push --force|push origin --delete|force-with-lease|"
     r"terraform destroy|kubectl delete|truncate table|git reset --hard",
     "escalate to the operator", "destructive and irreversible"),
    (r"\[Production Deploy\]|\[Security Weaken\]|\[Self-Approval\]|\[Git Destructive\]",
     "escalate to the operator", "the session was blocked by the auto-mode classifier"),
    (r"\bspend\b|\bcharge[ds]?\b|\binvoice\b|\bpayment\b|\bpurchase\b|\bsubscribe\b",
     "escalate to the operator", "money"),
    (r"secret|credential|api[_ -]?key|private key|access token",
     "escalate to the operator", "credentials"),
)

# ---- quiet hours -----------------------------------------------------------------
# During the day an extra notification costs little. Overnight it costs sleep, and sleep is
# the input to every decision the next day. So the gate tightens at night instead of turning
# off: hard escalations still come through, soft ones and anything the classifier would have
# raised are held and reported once in the morning. Nothing is dropped, it is deferred.
QUIET_HOURS = os.environ.get("LAYA_GATE_QUIET_HOURS", "23:00-05:00")
HELD_FILE = os.path.expanduser(os.environ.get("LAYA_GATE_HELD",
                                              "~/.hermes/logs/laya-gate-held.jsonl"))


def in_quiet_hours(now=None):
    """True inside the quiet window, which may cross midnight. Unparseable config = never quiet."""
    try:
        start_s, end_s = QUIET_HOURS.split("-")
        sh, sm = (int(x) for x in start_s.strip().split(":"))
        eh, em = (int(x) for x in end_s.strip().split(":"))
    except Exception:
        return False
    now = now or time.localtime()
    cur, start, end = now.tm_hour * 60 + now.tm_min, sh * 60 + sm, eh * 60 + em
    return (start <= cur < end) if start <= end else (cur >= start or cur < end)


def hold(session, family, text, rule, why):
    """Record an event that was not delivered, so the morning digest can report it."""
    try:
        with open(HELD_FILE, "a") as f:
            f.write(json.dumps({"at": int(time.time()), "session": session, "family": family,
                                "text": (text or "")[:300], "rule": rule, "why": why}) + "\n")
    except Exception:
        pass


def morning_digest():
    """One message describing what was held overnight, and nothing at all if the night was quiet."""
    import collections
    try:
        rows = [json.loads(l) for l in open(HELD_FILE) if l.strip()]
    except Exception:
        return ""
    if not rows:
        return ""
    by_session = collections.Counter(r.get("session", "?") for r in rows)
    by_family = collections.Counter(r.get("family", "?") for r in rows)
    hard = [r for r in rows if "crash" in (r.get("rule") or "") or "asks for input" in (r.get("rule") or "")]
    parts = [f"Overnight: {len(rows)} events held while you were quiet."]
    if hard:
        parts.append(f"Of those, {len(hard)} would have been hard escalations and are listed first.")
    parts.append("By session: " + ", ".join(f"{s} {n}" for s, n in by_session.most_common(6)) + ".")
    parts.append("By kind: " + ", ".join(f"{k} {n}" for k, n in by_family.most_common(6)) + ".")
    if not hard:
        parts.append("Nothing needed a decision from you.")
    else:
        for r in hard[:5]:
            parts.append(f"- {r.get('session')}: {r.get('rule')} ({r.get('text','')[:80]})")
    parts.append(f"Full list: {HELD_FILE}")
    try:
        os.replace(HELD_FILE, HELD_FILE + ".sent")     # the digest owns what it reported
    except Exception:
        pass
    return "\n".join(parts)
_history = {}                                    # (session, family) -> [timestamps]

# ---- the record a review reads ------------------------------------------------
LABELS_FILE = os.path.expanduser(os.environ.get("LAYA_GATE_LABELS",
                                                "~/.hermes/logs/laya-gate-labels.jsonl"))
DECISIONS_FILE = os.path.expanduser(os.environ.get("LAYA_GATE_DECISIONS",
                                                   "~/.hermes/logs/laya-gate-decisions.jsonl"))


def _read_jsonl(paths):
    out = []
    for p in paths:
        try:
            for line in open(p, errors="replace"):
                line = line.strip()
                if line:
                    try:
                        out.append(json.loads(line))
                    except Exception:
                        pass
        except FileNotFoundError:
            pass
    return out


def _decision_id(at, session, text):
    import hashlib
    return hashlib.sha1(f"{at}|{session}|{text[:200]}".encode()).hexdigest()[:8]


def all_decisions():
    """Every decision, oldest first, with an id backfilled where the entry predates ids.

    The id is a hash of (timestamp, session, text), so it can be derived for history as
    well as recorded going forward. That keeps the first weeks of decisions addressable
    instead of leaving them unlabellable.
    """
    rows = _read_jsonl([DECISIONS_FILE, DECISIONS_FILE + ".1"])
    for r in rows:
        if not r.get("id"):
            r["id"] = _decision_id(r.get("at", 0), r.get("session", ""), r.get("text", ""))
    return rows


def labels():
    return {r["id"]: r for r in _read_jsonl([LABELS_FILE]) if r.get("id")}


def label(decision_id, verdict, note=""):
    """Record what a decision was actually worth. This is the only ground truth there is:
    the gate can report its own confidence, but only the operator can say whether the
    interruption earned its cost."""
    rows = [r for r in all_decisions() if r.get("id") == decision_id]
    if not rows:
        return None
    r, v = rows[-1], (rows[-1].get("verdict") or {})
    rec = {"id": decision_id, "label": verdict, "note": note, "at": int(time.time()),
           "was": {"session": r.get("session"), "lane": v.get("lane"),
                   "escalate": v.get("escalate"), "rule": v.get("rule"),
                   "confidence": v.get("confidence"),
                   "text": (r.get("text") or "")[:160]}}
    with open(LABELS_FILE, "a") as f:
        f.write(json.dumps(rec) + "\n")
    return rec


REGISTRY_FILE = os.path.expanduser(os.environ.get("LAYA_GATE_REGISTRY",
                                                  "~/.hermes/scripts/cc-watch/fleet_registry.json"))
PROJECT_ROOTS = ("~/.claude-work/projects", "~/.claude-personal/projects")
# If a session carried on by itself this soon after a stall was escalated, the interruption
# probably was not needed. If it stayed silent this long, the event was probably real.
RESUME_WINDOW = float(os.environ.get("LAYA_GATE_RESUME_WINDOW", "600"))
QUIET_AFTER = float(os.environ.get("LAYA_GATE_QUIET_AFTER", "7200"))


def _transcript_for(session):
    """Resolve a session's transcript by uuid. Never rebuild the project directory name: it
    encodes the cwd with separators replaced, and guessing it wrong silently reads nothing."""
    import glob
    try:
        reg = json.load(open(REGISTRY_FILE))
        entry = next((e for e in reg.get("sessions", []) if e.get("name") == session), None)
    except Exception:
        return None
    if not entry or not entry.get("uuid"):
        return None
    for root in PROJECT_ROOTS:
        hits = glob.glob(os.path.expanduser(f"{root}/*/{entry['uuid']}.jsonl"))
        if hits:
            return hits[0]
    return None


def _tail_timestamps(path, max_bytes=1_500_000):
    """Timestamps from the tail of a transcript. The tail keeps this cheap on a session whose
    transcript has grown to hundreds of megabytes."""
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as f:
            if size > max_bytes:
                f.seek(size - max_bytes)
                f.readline()                      # discard the partial first line
            blob = f.read().decode("utf-8", "replace")
    except Exception:
        return []
    return re.findall(r'"timestamp":"([0-9T:\-\.Z]+)"', blob)


def _epoch(iso):
    import datetime
    try:
        return datetime.datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()
    except Exception:
        return None


def outcome_for(decision):
    """What the session did next. This is the only hindsight available for a decision, and it
    is what turns 'what did the gate decide' into 'was the gate right'."""
    session, ts = decision.get("session"), decision.get("at", 0)
    path = _transcript_for(session)
    if not path:
        return {"verdict": "unknown", "why": "no transcript for this session"}
    stamps = [s for s in (_epoch(x) for x in _tail_timestamps(path)) if s]
    if not stamps:
        return {"verdict": "unknown", "why": "no timestamps readable in the tail"}
    after = [s for s in stamps if s > ts]
    if not after:
        # Not yet judgeable: a decision that is only minutes old has not had the chance to be
        # followed by anything, so calling it "stayed quiet" would be a false alarm.
        if time.time() - ts < QUIET_AFTER:
            return {"verdict": "too_recent", "why": f"only {int((time.time() - ts) / 60)} min old"}
        return {"verdict": "stayed_quiet", "why": "no output since the decision"}
    delay = min(after) - ts
    if delay <= RESUME_WINDOW:
        return {"verdict": "resumed", "delay_s": int(delay),
                "why": f"carried on by itself {int(delay)}s later"}
    if delay >= QUIET_AFTER:
        return {"verdict": "stayed_quiet", "delay_s": int(delay),
                "why": f"quiet for {delay / 3600:.1f}h afterwards"}
    return {"verdict": "unclear", "delay_s": int(delay),
            "why": f"next output {int(delay / 60)} min later"}


def _suspects(rows, lab):
    """The two ways a decision can be wrong, judged against what happened next.

    Delivered and the session carried on by itself: an interruption that was probably not
    needed. Held and the session then went quiet for hours: something that may have deserved
    the operator and did not get her. Receipts are excluded from the second list, because a
    completion token going quiet afterwards means the work finished, not that a signal was
    missed.
    """
    wrong, missed = [], []
    for r in rows:
        v = r.get("verdict") or {}
        fam = _family(r.get("text") or "", r.get("session") or "")
        if v.get("escalate") and fam == "STALL":
            o = outcome_for(r)
            if o["verdict"] == "resumed":
                wrong.append((r, o))
        elif not v.get("escalate") and (v.get("lane") == 3 or "held until morning" in str(v.get("rule"))):
            o = outcome_for(r)
            if o["verdict"] == "stayed_quiet":
                missed.append((r, o))
    return wrong, missed


def _other_summary(rows):
    """The response and affirmation decisions. They have no hindsight to judge them by the way
    an escalation does, so what is reported is the split and the confidence: enough to see
    whether the threshold is doing anything at all, or answering the same way every time."""
    import collections, statistics
    if not rows:
        return ""
    by_kind = collections.Counter(r.get("kind") for r in rows)
    L = ["", f"  OTHER DECISIONS  ({len(rows)})",
         "    " + ", ".join(f"{k} {n}" for k, n in by_kind.most_common())]
    for kind in ("respond", "affirm"):
        sel = [r for r in rows if r.get("kind") == kind]
        if not sel:
            continue
        lanes = collections.Counter((r.get("verdict") or {}).get("lane") for r in sel)
        confs = [float((r.get("verdict") or {})["confidence"]) for r in sel
                 if (r.get("verdict") or {}).get("confidence") is not None]
        L.append(f"    {kind}: lane split "
                 + ", ".join(f"lane{k} {n}" for k, n in sorted(lanes.items(), key=lambda x: (x[0] is None, x[0])))
                 + (f"; confidence median {statistics.median(confs):.3f} of {len(confs)}" if confs else ""))
        acts = collections.Counter()
        for r in sel:
            v = r.get("verdict") or {}
            if kind == "respond":
                acts[v.get("response") or "?"] += 1
            else:
                acts["affirmed" if v.get("affirm") else f"escalated (risk {v.get('risk')})"] += 1
        for a, n in acts.most_common(5):
            L.append(f"      {n:4}  {a}")
    return "\n".join(L)


def review(days=1):
    """A periodic read on whether the gate is doing its job, and what is still unjudged."""
    import collections, datetime, statistics
    cut = time.time() - days * 86400
    everything = [r for r in all_decisions() if r.get("at", 0) >= cut]
    # Escalation decisions carry no `kind` (they predate it); the response and affirmation
    # decisions do. Keep them apart or the escalation statistics stop meaning anything.
    rows = [r for r in everything if (r.get("kind") or "escalate") == "escalate"]
    others = [r for r in everything if r.get("kind") in ("respond", "affirm")]
    if not rows:
        return (f"Laya gate review: no escalation decisions in the last {days} days."
                + _other_summary(others))
    lab = labels()
    esc = [r for r in rows if (r.get("verdict") or {}).get("escalate")]
    by_lane = collections.Counter((r.get("verdict") or {}).get("lane") for r in rows)
    by_rule = collections.Counter((r.get("verdict") or {}).get("rule") or "?" for r in rows)
    confs = []
    for r in rows:
        v = r.get("verdict") or {}
        if v.get("lane") == 3 and v.get("confidence") is not None:
            try:
                confs.append(float(v["confidence"]))
            except (TypeError, ValueError):
                pass
    unjudged = [r for r in esc if r.get("id") not in lab]
    judged = [lab[r["id"]] for r in esc if r.get("id") in lab]
    good = sum(1 for j in judged if j.get("label") == "good")
    bad = sum(1 for j in judged if j.get("label") == "bad")

    L = [f"Laya gate review, last {days} days",
         f"  decisions {len(rows)}   delivered {len(esc)}   held silent {len(rows) - len(esc)}",
         "  by lane: " + ", ".join(f"lane{k} {n}" for k, n in sorted(by_lane.items(), key=lambda x: (x[0] is None, x[0]))),
         "  by rule:"]
    for rule, n in by_rule.most_common(8):
        L.append(f"    {n:4}  {rule}")
    if confs:
        near = sum(1 for c in confs if abs(c - ESCALATE_IF_AT_LEAST) <= 0.05)
        L.append(f"  lane 3 confidence: min {min(confs):.3f}  median {statistics.median(confs):.3f}  "
                 f"max {max(confs):.3f}   threshold {ESCALATE_IF_AT_LEAST}")
        L.append(f"    within 0.05 of the threshold: {near} (only these make the threshold choice matter)")
    wrong, missed = _suspects(rows, lab)
    L.append("")
    L.append(f"  DECISIONS THAT MAY HAVE BEEN WRONG  ({len(wrong)})")
    if wrong:
        L.append("    delivered, and the session carried on by itself anyway:")
        for r, o in wrong[:8]:
            v = r.get("verdict") or {}
            t = datetime.datetime.fromtimestamp(r["at"]).strftime("%m-%d %H:%M")
            L.append(f"    {r.get('id')}  {t}  {r.get('session')}  conf={v.get('confidence')}  {o['why']}")
    else:
        L.append("    none: every escalated stall was followed by real silence")
    L.append(f"  HELD, AND THE SESSION THEN WENT QUIET  ({len(missed)})")
    if missed:
        L.append("    possibly deserved you and did not get you:")
        for r, o in missed[:8]:
            v = r.get("verdict") or {}
            t = datetime.datetime.fromtimestamp(r["at"]).strftime("%m-%d %H:%M")
            L.append(f"    {r.get('id')}  {t}  {r.get('session')}  {v.get('rule')}  {o['why']}")
    else:
        L.append("    none: everything held was followed by the session moving on")
    L.append("")
    if judged:
        L.append(f"  judged by you: {good} worth it, {bad} not  ({good / (good + bad) * 100:.0f}% precision on judged)")
    else:
        L.append("  judged by you: nothing yet, so precision is unknown")
    if unjudged:
        L.append(f"  awaiting your call ({len(unjudged)}):")
        for r in unjudged[:12]:
            v = r.get("verdict") or {}
            t = datetime.datetime.fromtimestamp(r["at"]).strftime("%m-%d %H:%M")
            L.append(f"    {r.get('id')}  {t}  lane{v.get('lane')} conf={v.get('confidence')}  "
                     f"{r.get('session')}  {(r.get('text') or '')[:64]}")
        if len(unjudged) > 12:
            L.append(f"    ... and {len(unjudged) - 12} more")
        L.append("  label with: laya_gate.py --label <id> good|bad [note]")
    other = _other_summary(others)
    if other:
        L.append(other)
    return "\n".join(L)


_model = None
_model_lock = threading.Lock()


def load_model():
    global _model
    with _model_lock:
        if _model is None:
            import laya_mlx as laya
            _model = laya.load(MODEL)
    return _model


def _family(text, session):
    """The event family, with the session name stripped: 'STALL-CC-P-FORMA-FC3: ...' -> 'STALL'."""
    m = re.match(r"([A-Z][A-Z0-9_.-]*)", text or "")
    fam = m.group(1) if m else (text or "")[:20]
    up = (session or "").upper()
    if up:
        fam = fam.replace("-" + up, "").replace(up, "")
    return fam.strip("-_.") or "event"


def classify(session, kind, text, context="", expecting=False):
    """Lane 1 and 2 in code, lane 3 through Laya. Never raises.

    `expecting` says whether this session has work in flight (an armed watch or a pending
    dispatch). It is the difference between a session that has gone quiet because it
    finished, and one that has gone quiet mid-task. Without it, every idle session in a
    fleet of sixteen raises a stall every few minutes, which is what happened: 49 of 53
    escalations in one night were stalls on sessions doing nothing.
    """
    probe = text or kind or ""
    quiet = in_quiet_hours()
    fam = _family(probe, session)
    for pat, why, hard in LANE1:
        if re.search(pat, probe):
            if quiet and not hard:
                hold(session, fam, probe, why, "quiet hours: soft escalation")
                return {"lane": 1, "escalate": False, "rule": f"held until morning: {why}",
                        "confidence": None, "enforced": True, "family": fam, "held": True}
            return {"lane": 1, "escalate": True, "rule": why, "confidence": None,
                    "enforced": True, "family": fam}
    for pat, why in LANE2:
        if re.search(pat, probe):
            return {"lane": 2, "escalate": False, "rule": why, "confidence": None,
                    "enforced": True}

    if fam in ("STALL", "SENTINEL") and not expecting:
        return {"lane": 2, "escalate": False, "rule": "quiet session with nothing in flight",
                "confidence": None, "enforced": True, "family": fam}

    key = (session, fam)
    last = _recent.get(key, 0)
    if time.time() - last < REPEAT_WINDOW:
        return {"lane": 2, "escalate": False, "rule": f"repeat of a {fam} already raised",
                "confidence": None, "enforced": True, "family": fam,
                "minutes_since": int((time.time() - last) / 60)}

    if quiet:
        hold(session, fam, probe, "classified", "quiet hours: classified event")
        return {"lane": 3, "escalate": False, "rule": "held until morning (quiet hours)",
                "confidence": None, "enforced": True, "family": fam, "held": True}

    hist = _history.setdefault((session, fam), [])
    cutoff = time.time() - 86400
    hist[:] = [t for t in hist if t >= cutoff]
    state = (f"{DOCTRINE}\n\n"
             f"Situation: {context or 'no further detail available'}\n"
             f"Event: {probe}\n"
             f"Events of this kind on this session in the last 24 hours: {len(hist)}.")
    try:
        t0 = time.time()
        out = load_model().predict(state, QUESTION)
        ans = out.get("answers", out).get("action", {})
        probs = ans.get("probabilities") or {}
        conf = float(probs.get("interrupt her now") or ans.get("confidence") or 0.0)
        ms = int((time.time() - t0) * 1000)
        escalate = bool(conf >= ESCALATE_IF_AT_LEAST)
        hist.append(time.time())
        if escalate:
            _recent[key] = time.time()
        return {"lane": 3, "escalate": escalate, "rule": "laya",
                "confidence": round(conf, 4), "ms": ms, "enforced": ENFORCE,
                "state_chars": len(state), "family": fam, "prior_24h": len(hist) - 1}
    except Exception as e:                      # model down, import error, anything
        # Fail OPEN toward the operator: an unclassified event is shown, not swallowed.
        return {"lane": 3, "escalate": True, "rule": f"classifier unavailable ({type(e).__name__})",
                "confidence": None, "enforced": False, "family": fam}


STATE_CLI = os.path.expanduser(os.environ.get(
    "LAYA_GATE_STATE_CLI", "~/.hermes/scripts/cc-watch/fleet_state.py"))
LAST_CLI = os.path.expanduser(os.environ.get(
    "LAYA_GATE_LAST_CLI", "~/.hermes/scripts/cc-watch/fleet_last.py"))
FOCUS_DIR = os.path.expanduser(os.environ.get(
    "LAYA_GATE_FOCUS_DIR", "~/.hermes/scripts/cc-watch/overwatch"))
SITUATION_CHARS = int(os.environ.get("LAYA_GATE_SITUATION_CHARS", "4000"))
# How much of the situation goes into the decision LOG. The log is the training data for the
# next improvement, so it keeps the input the decision was made on, not just its size.
LAYA_LOG_SITUATION_CHARS = int(os.environ.get("LAYA_GATE_LOG_SITUATION_CHARS", "2500"))
_recent_response = {}                            # (session, response) -> last time given


def _run(cmd, timeout=12):
    """A helper CLI's output, or an empty string. A decision is never worth an exception."""
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return ((p.stdout or "") + (p.stderr or "")).strip()
    except Exception:
        return ""


def _registry_entry(session):
    try:
        reg = json.load(open(REGISTRY_FILE))
        return next((e for e in reg.get("sessions", []) if e.get("name") == session), None)
    except Exception:
        return None


def situation(session, probe="", context="", state=None):
    """The context a decision needs, assembled rather than assumed.

    A decision made on one line of event text is a guess. The gate already learned this once
    (an event without its situation produced confident nonsense), and the response decision
    needs more than the event: WHO the session is, WHAT the operator actually asked it to do,
    what its standing ruling is, what it last said, whether work is in flight, and how often
    this has come up before. Every part is optional and a missing one is NAMED, because a
    silently absent field reads to the model as a situation without that fact in it.
    """
    parts = []
    entry = _registry_entry(session) or {}
    who = f"Session: {session}"
    if entry.get("profile"):
        who += f" (profile {entry['profile']}"
        who += f", cwd {entry.get('cwd')})" if entry.get("cwd") else ")"
    parts.append(who + ".")

    focus = os.path.join(FOCUS_DIR, f"{session}_focus.md")
    try:
        with open(focus, errors="replace") as f:
            txt = f.read().strip()
        if txt:
            parts.append("The operator's standing ruling for this session:\n" + txt[:1200])
    except Exception:
        parts.append("No standing ruling is armed for this session.")

    if state:
        parts.append(f"Computed state: {state}.")
    else:
        line = _run([sys.executable, STATE_CLI, session]).splitlines()
        parts.append(f"Computed state: {line[0] if line else 'unavailable'}.")

    last = _run([sys.executable, LAST_CLI, session])
    if last:
        tail = last.split("last 3 message(s), oldest first:")[-1].strip()
        parts.append("What the session last said:\n" + (tail[-1200:] or "nothing readable"))

    inbound = _run([sys.executable, LAST_CLI, session, "--inbound", "2"])
    if inbound and "last 0 inbound" not in inbound:
        parts.append("The most recent instruction that reached it:\n" + inbound[-800:])

    inflight = []
    for f in ("fleet_watch_acks.json", "fleet_watch_pending.json"):
        try:
            rows = json.load(open(os.path.expanduser(f"~/.hermes/scripts/cc-watch/{f}")))
            inflight += [r for r in rows if isinstance(r, dict) and r.get("session") == session]
        except Exception:
            pass
    parts.append(f"Work in flight: {len(inflight)} armed watch(es) or pending dispatch(es)."
                 if inflight else "Work in flight: none recorded.")

    try:
        cutoff = time.time() - 86400
        prior = [r for r in all_decisions()
                 if r.get("session") == session and r.get("at", 0) >= cutoff]
        parts.append(f"Decisions already made about this session in the last 24h: {len(prior)}.")
    except Exception:
        parts.append("Prior decisions on this session: unavailable.")

    if context:
        parts.append(f"Extra context from the caller:\n{context[:800]}")
    if probe:
        parts.append(f"Event under decision: {probe[:600]}")
    return "\n".join(parts)[:SITUATION_CHARS]


def _ask(question, doctrine, state):
    """One Laya call. Returns (probabilities, milliseconds). Raises on anything."""
    t0 = time.time()
    out = load_model().predict(f"{doctrine}\n\n{state}", question)
    ans = out.get("answers", out).get("action", {})
    return (ans.get("probabilities") or {}), int((time.time() - t0) * 1000)


def _log_decision(kind, session, text, verdict, situation_text=""):
    """Record a decision the gate made on a caller's behalf, so it can be reviewed and
    calibrated later. The escalation decisions are logged by the daemon that delivers them;
    these have no single caller, so the gate writes them itself.

    The SITUATION and the full probability map are recorded, not just the verdict: without them
    a wrong answer says nothing about why it was wrong, and the next attempt at improving this
    has to guess at the input it is being judged on. Truncated to keep the log readable.
    """
    rec = {"at": int(time.time()), "kind": kind, "session": session,
           "text": (text or "")[:300], "situation_chars": len(situation_text or ""),
           "situation": (situation_text or "")[:LAYA_LOG_SITUATION_CHARS],
           "verdict": verdict}
    rec["id"] = _decision_id(rec["at"], session, rec["text"])
    try:
        with open(DECISIONS_FILE, "a") as f:
            f.write(json.dumps(rec) + "\n")
    except Exception:
        pass
    return rec


def _default_response(state):
    """What the policy did before there was a model: the fallback whenever the model is unsure,
    below threshold, or unavailable. An uncalibrated model must never become a silent policy
    change, so being unsure reproduces the standing behaviour exactly."""
    s = (state or "").upper()
    if s in ("WORKING", "QUEUED"):
        return "let it continue"
    if s in ("DEAD", "NEEDS-INPUT"):
        return "escalate to the operator"
    return "nudge it with one concrete next increment"      # IDLE, and anything unrecognised


def respond(session, probe="", state=None, context="", choice_ui=False, log=True):
    """What to DO with this session: the fleet's second decision.

    Lanes 1 and 2 in code, lane 3 through Laya. `state` is the fingerprint state
    (WORKING / IDLE / QUEUED / NEEDS-INPUT / DEAD); `choice_ui` says the session is showing
    its own numbered picker, which is the operator's checkpoint by default. Never raises.
    """
    probe = probe or ""
    verdict = {"session": session, "state": state, "response": None, "enforced": True}
    for pat, action, why in RESPOND_LANE1:
        if state and re.match(pat, state.upper()):
            verdict.update(lane=1, response=action, rule=why, confidence=None)
            return verdict if not log else (_log_decision("respond", session, probe, verdict), verdict)[1]
    if choice_ui:
        verdict.update(lane=1, response="escalate to the operator", confidence=None,
                       rule="the session is showing its own choice UI, which is her checkpoint")
        return verdict if not log else (_log_decision("respond", session, probe, verdict), verdict)[1]
    for pat, action, why in RESPOND_ESCALATE:
        if re.search(pat, probe):
            verdict.update(lane=1, response=action, rule=why, confidence=None)
            return verdict if not log else (_log_decision("respond", session, probe, verdict), verdict)[1]

    sit = situation(session, probe, context, state)
    try:
        probs, ms = _ask(RESPOND_QUESTION, RESPOND_DOCTRINE, sit)
        verdict["probabilities"] = {k: round(float(v), 4) for k, v in probs.items()}
        ranked = sorted(((float(v), k) for k, v in probs.items()), reverse=True)
        conf, action = ranked[0] if ranked else (0.0, "let it continue")
        if conf < RESPOND_IF_AT_LEAST:
            # Too close to call. Fall back to the standing policy for this state, so an
            # uncalibrated model cannot quietly change how the fleet behaves.
            verdict.update(lane=3, response=_default_response(state), rule="laya: below threshold, standing policy",
                           confidence=round(conf, 4), ms=ms,
                           runner_up=ranked[1][1] if len(ranked) > 1 else None)
        else:
            key = (session, action)
            if time.time() - _recent_response.get(key, 0) < REPEAT_WINDOW:
                # The same act, on the same session, inside the window. Suppressing the REPEAT
                # must not become inaction: falling back to "let it continue" turned a repeat
                # into silence, which is a policy change nobody chose. Degrade to the standing
                # policy for the state instead.
                verdict.update(lane=2, response=_default_response(state),
                               rule=f"repeat of a {action} already given, standing policy",
                               confidence=round(conf, 4))
            else:
                _recent_response[key] = time.time()
                verdict.update(lane=3, response=action, rule="laya", confidence=round(conf, 4),
                               ms=ms, enforced=RESPOND_ENFORCE)
    except Exception as e:
        # Fail to the standing policy, never to an action nobody chose.
        verdict.update(lane=3, response=_default_response(state),
                       rule=f"classifier unavailable ({type(e).__name__}), standing policy",
                       confidence=None)
    verdict["situation_chars"] = len(sit)
    if log:
        _log_decision("respond", session, probe, verdict, sit)
    return verdict


def affirm(session, action, context="", log=True):
    """May this action be affirmed without her? The fleet's third decision.

    Returns a RISK score, not a feeling: risk = 1 - P(safe to affirm). The action is affirmed
    only under AFFIRM_IF_RISK_UNDER, and anything destructive, irreversible, money-shaped,
    credential-shaped or production-shaped is escalated on its shape alone, without a model.
    Never raises.
    """
    action = action or ""
    verdict = {"session": session, "affirm": False, "risk": 1.0, "enforced": True}
    for pat, _a, why in AFFIRM_ESCALATE:
        if re.search(pat, action, re.I):
            verdict.update(lane=1, rule=why, confidence=None)
            if log:
                _log_decision("affirm", session, action, verdict)
            return verdict
    sit = situation(session, action, context)
    try:
        probs, ms = _ask(AFFIRM_QUESTION, AFFIRM_DOCTRINE, sit)
        verdict["probabilities"] = {k: round(float(v), 4) for k, v in probs.items()}
        p_safe = float(probs.get("safe to affirm") or 0.0)
        risk = round(1.0 - p_safe, 4)
        verdict.update(lane=3, risk=risk, affirm=risk < AFFIRM_IF_RISK_UNDER, rule="laya",
                       confidence=round(p_safe, 4), ms=ms, enforced=AFFIRM_ENFORCE,
                       threshold=AFFIRM_IF_RISK_UNDER)
    except Exception as e:
        # Unscored risk is treated as maximum risk: an unanswered question is not consent.
        verdict.update(lane=3, rule=f"classifier unavailable ({type(e).__name__})", confidence=None)
    verdict["situation_chars"] = len(sit)
    if log:
        _log_decision("affirm", session, action, verdict, sit)
    return verdict


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, payload):
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.startswith("/health"):
            self._send(200, {"ok": True, "model_loaded": _model is not None,
                             "enforce": ENFORCE, "threshold": ESCALATE_IF_AT_LEAST,
                             "respond_threshold": RESPOND_IF_AT_LEAST,
                             "respond_enforce": RESPOND_ENFORCE,
                             "affirm_risk_under": AFFIRM_IF_RISK_UNDER,
                             "affirm_enforce": AFFIRM_ENFORCE,
                             "questions": ["decide", "respond", "affirm"],
                             "quiet_hours": QUIET_HOURS, "quiet_now": in_quiet_hours(),
                             "port": PORT})
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self):
        try:
            n = int(self.headers.get("Content-Length") or 0)
            req = json.loads(self.rfile.read(n) or b"{}")
        except Exception as e:
            self._send(400, {"error": f"bad request: {e}"})
            return
        if self.path.startswith("/respond"):
            verdict = respond(req.get("session", "?"), req.get("text", ""), req.get("state"),
                              req.get("context", ""), bool(req.get("choice_ui", False)))
        elif self.path.startswith("/affirm"):
            verdict = affirm(req.get("session", "?"), req.get("action", ""), req.get("context", ""))
        elif self.path.startswith("/decide"):
            verdict = classify(req.get("session", "?"), req.get("kind", ""),
                               req.get("text", ""), req.get("context", ""),
                               bool(req.get("expecting", False)))
            verdict["session"] = req.get("session")
        else:
            self._send(404, {"error": "not found"})
            return
        self._send(200, verdict)

    def log_message(self, format, *args):        # matches the stdlib signature
        pass                                     # the caller logs; this stays quiet


if __name__ == "__main__":
    # The model lives in the laya-mlx venv. Called with a bare `python3`, `import laya_mlx`
    # fails and every decision quietly degrades to "classifier unavailable" + standing policy
    # — a caller has no way to tell a real answer from the fallback. Hand off to the
    # interpreter that has the package instead of degrading in silence.
    if not os.environ.get("LAYA_GATE_REEXEC"):
        import importlib.util
        try:
            have_model = importlib.util.find_spec("laya_mlx") is not None
        except (ImportError, ValueError):
            have_model = False
        if not have_model:
            venv_py = os.path.expanduser(os.environ.get(
                "LAYA_GATE_PYTHON", "~/.hermes/venvs/laya-mlx/bin/python3"))
            if os.path.exists(venv_py):
                os.environ["LAYA_GATE_REEXEC"] = "1"
                os.execv(venv_py, [venv_py] + sys.argv)
    if "--review" in sys.argv:
        i = sys.argv.index("--review")
        days = int(sys.argv[i + 1]) if len(sys.argv) > i + 1 and sys.argv[i + 1].isdigit() else 7
        print(review(days))
        sys.exit(0)
    if "--label" in sys.argv:
        i = sys.argv.index("--label")
        if len(sys.argv) < i + 3:
            print("usage: laya_gate.py --label <id> good|bad [note]")
            sys.exit(2)
        rec = label(sys.argv[i + 1], sys.argv[i + 2],
                    " ".join(sys.argv[i + 3:]))
        print(json.dumps(rec, indent=1) if rec else f"no decision with id {sys.argv[i + 1]}")
        sys.exit(0 if rec else 1)
    if "--respond" in sys.argv:
        i = sys.argv.index("--respond")
        if len(sys.argv) < i + 2:
            print("usage: laya_gate.py --respond <session> [--state WORKING|IDLE|QUEUED|"
                  "NEEDS-INPUT|DEAD] [--text TEXT] [--context TEXT] [--choice-ui] [--no-log]")
            sys.exit(2)

        def _opt(name, default=None):
            return sys.argv[sys.argv.index(name) + 1] if name in sys.argv else default

        print(json.dumps(respond(sys.argv[i + 1], _opt("--text", ""), _opt("--state"),
                                 _opt("--context", ""), "--choice-ui" in sys.argv,
                                 log="--no-log" not in sys.argv), indent=1))
        sys.exit(0)
    if "--affirm" in sys.argv:
        i = sys.argv.index("--affirm")
        if len(sys.argv) < i + 3:
            print("usage: laya_gate.py --affirm <session> <action> [--context TEXT] [--no-log]")
            sys.exit(2)
        ctx = sys.argv[sys.argv.index("--context") + 1] if "--context" in sys.argv else ""
        print(json.dumps(affirm(sys.argv[i + 1], sys.argv[i + 2], ctx,
                                log="--no-log" not in sys.argv), indent=1))
        sys.exit(0)
    if "--digest" in sys.argv:
        # The morning rollup: prints what was held overnight, or nothing at all, so a cron job
        # can deliver it and stay silent on a quiet night.
        print(morning_digest())
        sys.exit(0)
    if "--selftest" in sys.argv:
        print("=== lane assignment ===")
        for c in [("cc-x", "MATCH", "DONE-thing-123", False),
                  ("cc-x", "ACK-OK", "ACK-OK cc-x | DISPATCH-1", False),
                  ("cc-x", "MATCH", "NEEDS-INPUT-PICK-A-REPO", False),
                  ("cc-x", "MATCH", "traceback: ValueError in tools.py", False),
                  ("cc-x", "WATCH-EXPIRED", "WATCH-EXPIRED cc-x DONE-y deadline=1", False)]:
            print(f"  {c[2][:50]:52} {json.dumps(classify(c[0], c[1], c[2], '', c[3]))}")
        print("\n=== the fix that matters: a stall on an idle session ===")
        stall = "STALL-CC-P-FORMA-FC3: no new transcript output for 360s (alive, not producing)"
        print(f"  idle session     {json.dumps(classify('cc-p-forma-fc3', 'STALL', stall))}")
        print(f"  work in flight   {json.dumps(classify('cc-p-forma-fc3', 'STALL', stall, '', True))}")
        print("\n=== repeat suppression ===")
        a = classify('cc-p-forma-fc3', 'STALL', stall, '', True)
        b = classify('cc-p-forma-fc3', 'STALL', stall, '', True)
        print(f"  first            {json.dumps(a)}")
        print(f"  second           {json.dumps(b)}")
        print("\n=== the response decision: what to DO with the session ===")
        for st in ("WORKING", "QUEUED", "DEAD"):
            print(f"  state {st:8} {json.dumps(respond('cc-x', 'state changed', st, log=False))}")
        print(f"  choice UI  {json.dumps(respond('cc-x', 'picker on screen', 'NEEDS-INPUT', choice_ui=True, log=False))}")
        print(f"  idle       {json.dumps(respond('cc-x', 'state changed', 'IDLE', log=False))}")
        print("\n=== the affirmation decision: is this action safe without her ===")
        for act in ("add a regression test for the parser",
                    "git push --force origin main",
                    "rotate the production API key",
                    "delete the merged feature branch"):
            print(f"  {act[:42]:44} {json.dumps(affirm('cc-x', act, log=False))}")
        sys.exit(0)
    print(f"laya gate on 127.0.0.1:{PORT} enforce={ENFORCE} threshold={THRESHOLD}", flush=True)
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
