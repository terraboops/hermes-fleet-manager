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
import json, os, re, sys, time, threading
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
# Ordered; first match wins and its id is logged.
LANE1 = [
    (r"^NEEDS-INPUT-", "session asks for input"),
    (r"^SESSION-DEAD-", "session is dead"),
    (r"^USAGE-LIMIT-", "session hit a usage or credit limit"),
    (r"^NO-TRANSCRIPT-", "registered session has no transcript"),
    (r"traceback|Traceback|unhandled exception|panic:", "crash in the session output"),
    (r"^ACK-MISSED-", "a dispatch never arrived as a user turn"),
    (r"\[Production Deploy\]|\[Security Weaken\]|\[Self-Approval\]|\[Git Destructive\]",
     "the session was blocked by the auto-mode classifier"),
    (r"\bspend\b|\bcharges?\b|\binvoice\b", "money"),
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
_history = {}                                    # (session, family) -> [timestamps]


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
    for pat, why in LANE1:
        if re.search(pat, probe):
            return {"lane": 1, "escalate": True, "rule": why, "confidence": None,
                    "enforced": True}
    for pat, why in LANE2:
        if re.search(pat, probe):
            return {"lane": 2, "escalate": False, "rule": why, "confidence": None,
                    "enforced": True}

    fam = _family(probe, session)
    if fam in ("STALL", "SENTINEL") and not expecting:
        return {"lane": 2, "escalate": False, "rule": "quiet session with nothing in flight",
                "confidence": None, "enforced": True, "family": fam}

    key = (session, fam)
    last = _recent.get(key, 0)
    if time.time() - last < REPEAT_WINDOW:
        return {"lane": 2, "escalate": False, "rule": f"repeat of a {fam} already raised",
                "confidence": None, "enforced": True, "family": fam,
                "minutes_since": int((time.time() - last) / 60)}

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
                             "enforce": ENFORCE, "threshold": THRESHOLD, "port": PORT})
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self):
        if not self.path.startswith("/decide"):
            self._send(404, {"error": "not found"})
            return
        try:
            n = int(self.headers.get("Content-Length") or 0)
            req = json.loads(self.rfile.read(n) or b"{}")
        except Exception as e:
            self._send(400, {"error": f"bad request: {e}"})
            return
        verdict = classify(req.get("session", "?"), req.get("kind", ""),
                           req.get("text", ""), req.get("context", ""),
                           bool(req.get("expecting", False)))
        verdict["session"] = req.get("session")
        self._send(200, verdict)

    def log_message(self, format, *args):        # matches the stdlib signature
        pass                                     # the caller logs; this stays quiet


if __name__ == "__main__":
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
        sys.exit(0)
    print(f"laya gate on 127.0.0.1:{PORT} enforce={ENFORCE} threshold={THRESHOLD}", flush=True)
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
