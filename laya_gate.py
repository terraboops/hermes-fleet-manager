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
]

# ---- lane 3: the question Laya is asked ----------------------------------------
QUESTION = {
    "escalate": {
        "type": "noul",
        "instructions": (
            "The operator runs a fleet of coding agents and is interrupted by every event "
            "raised to her. Should this event interrupt her now?"
        ),
    }
}

_model = None
_model_lock = threading.Lock()


def load_model():
    global _model
    with _model_lock:
        if _model is None:
            import laya_mlx as laya
            _model = laya.load(MODEL)
    return _model


def classify(session, kind, text, context=""):
    """Lane 1 and 2 in code, lane 3 through Laya. Never raises."""
    probe = text or kind or ""
    for pat, why in LANE1:
        if re.search(pat, probe):
            return {"lane": 1, "escalate": True, "rule": why, "confidence": None,
                    "enforced": True}
    for pat, why in LANE2:
        if re.search(pat, probe):
            return {"lane": 2, "escalate": False, "rule": why, "confidence": None,
                    "enforced": True}

    state = (f"Fleet event on session {session}. Event: {kind}. {probe}."
             + (f" Recent session context: {context}" if context else ""))
    try:
        t0 = time.time()
        out = load_model().predict(state, QUESTION)
        ans = out.get("answers", out).get("escalate", {})
        conf = float(ans.get("confidence") or ans.get("noul") or 0.0)
        ms = int((time.time() - t0) * 1000)
        return {"lane": 3, "escalate": bool(conf >= THRESHOLD), "rule": "laya",
                "confidence": round(conf, 4), "ms": ms, "enforced": ENFORCE,
                "state_chars": len(state)}
    except Exception as e:                      # model down, import error, anything
        # Fail OPEN toward the operator: an unclassified event is shown, not swallowed.
        return {"lane": 3, "escalate": True, "rule": f"classifier unavailable ({type(e).__name__})",
                "confidence": None, "enforced": False}


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
                           req.get("text", ""), req.get("context", ""))
        verdict["session"] = req.get("session")
        self._send(200, verdict)

    def log_message(self, format, *args):        # matches the stdlib signature
        pass                                     # the caller logs; this stays quiet


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        for c in [("cc-x", "MATCH", "DONE-thing-123"),
                  ("cc-x", "ACK-OK", "ACK-OK cc-x | DISPATCH-1"),
                  ("cc-x", "MATCH", "NEEDS-INPUT-PICK-A-REPO"),
                  ("cc-x", "MATCH", "traceback: ValueError in tools.py"),
                  ("cc-x", "MATCH", "DONE-wolfgang-j7-live"),
                  ("cc-x", "WATCH-EXPIRED", "WATCH-EXPIRED cc-x DONE-y deadline=1")]:
            print(c[2][:52].ljust(54), json.dumps(classify(*c)))
        sys.exit(0)
    print(f"laya gate on 127.0.0.1:{PORT} enforce={ENFORCE} threshold={THRESHOLD}", flush=True)
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
