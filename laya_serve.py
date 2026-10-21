"""`laya serve`: an HTTP server that hosts one local Laya backend behind the remote wire contract.

    laya.py serve --backend mlx --token-env LAYA_SERVE_TOKEN [--host 127.0.0.1] [--port 8765]

Contract: docs/laya-remote.md, code: laya_remote_schema.py (shared with the client).

Rules this module enforces:
  * Binds to loopback by default. It refuses to start without a token unless --insecure-no-auth is
    given, and that flag is refused on a non-loopback address.
  * Tokens come from an environment variable only (never argv). Only SHA-256 hashes are kept, and the
    compare is constant time. Several comma-separated tokens may be valid at once (rotation).
  * Logs are metadata only: request id, route, status, latency. Never `state`, `question`,
    probabilities, tokens, or an exception message (those can echo event text).
  * The server returns probabilities and nothing else. A backend answer that does not match the
    question's criteria is a server fault (500), never a score.
"""
import argparse
import hashlib
import hmac
import json
import logging
import os
import re
import ssl
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import laya_backend_manager as lbm
import laya_backends as lb
import laya_remote_schema as rs

log = logging.getLogger("laya.serve")

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765
DEFAULT_TOKEN_ENV = "LAYA_SERVE_TOKEN"
UNKNOWN = "unknown"
LOOPBACK = ("127.0.0.1", "localhost", "::1")
PROB_RENORM_TOLERANCE = 1e-3          # rounding in a backend's output is fixed up; more is a fault
_SAFE_ID = re.compile(r"^[A-Za-z0-9-]{8,64}$")


def server_version():
    try:
        import yaml
        root = os.path.dirname(os.path.realpath(__file__))
        with open(os.path.join(root, "manifest.yaml"), encoding="utf-8") as f:
            return str((yaml.safe_load(f) or {}).get("version") or UNKNOWN)
    except Exception:
        return UNKNOWN


def hash_token(token):
    return hashlib.sha256(token.encode("utf-8")).digest()


def load_tokens(raw):
    """Token string (comma separated) -> list of SHA-256 digests. Raises ValueError on a bad form."""
    tokens = [t.strip() for t in (raw or "").split(",") if t.strip()]
    for t in tokens:
        if not rs.TOKEN_PATTERN.match(t):
            raise ValueError("token does not match the form laya_<kid>_<secret>")
    return [hash_token(t) for t in tokens]


class _ScrubScoreFaults(logging.Filter):
    """The backend manager logs 'score failed: <Type>: <message>' and a backend message can echo the
    event. Keep the exception type, drop the message, for as long as this process serves."""
    _pat = re.compile(r"(score failed: \w+):[^\n]*")

    def filter(self, record):
        try:
            text = record.getMessage()
        except Exception:
            return False
        if "score failed" in text:
            record.msg, record.args = self._pat.sub(r"\1", text), None
        return True


_scrub = _ScrubScoreFaults()


class Service:
    """The hosted backend plus its identity. Loading runs in a thread so /healthz answers at once."""

    def __init__(self, manager, tokens=(), model_revision=UNKNOWN, weights_sha256=UNKNOWN,
                 model_name=None, version=None):
        self.manager = manager
        self.token_hashes = None if tokens is None else list(tokens)
        self.model_revision = model_revision
        self.weights_sha256 = weights_sha256
        self.model_name = model_name
        self.version = version or server_version()
        self.loaded = threading.Event()
        for name in ("laya.backend", "laya.serve"):
            logging.getLogger(name).addFilter(_scrub)
        self._score_lock = threading.Lock()

    def start_loading(self, wait=False):
        def run():
            try:
                self.manager.start()
            except Exception as e:                       # a manager bug must not leave us 'loading'
                log.error("laya serve: backend start failed (%s)", type(e).__name__)
            finally:
                self.loaded.set()
        if wait:
            run()
        else:
            threading.Thread(target=run, name="laya-serve-load", daemon=True).start()

    # ---- state ------------------------------------------------------------------------
    def status(self):
        return self.manager.status()

    def backend_name(self):
        st = self.status()
        return None if st.model_less else st.backend

    def health_state(self):
        if not self.loaded.is_set():
            return rs.STATUS_LOADING
        st = self.status()
        if st.model_less:
            return rs.STATUS_DOWN
        if any(e.get("status") in ("failed", "demoted") for e in st.fallback_chain):
            return rs.STATUS_DEGRADED
        return rs.STATUS_OK

    def healthz(self):
        return rs.Healthz(self.loaded.is_set() and not self.status().model_less,
                          self.backend_name() is not None)

    def health(self):
        st = self.status()
        chain = tuple(f"{e.get('rung')}:{e.get('status')}" for e in st.fallback_chain)
        return rs.Health(self.health_state(), self.backend_name(), self.version,
                         model_loaded=self.backend_name() is not None, fallback_chain=chain)

    def info(self):
        name = self.backend_name() or UNKNOWN
        active = getattr(self.manager, "_active", None)
        model = self.model_name or getattr(active, "model", None) or name
        dtype = getattr(getattr(active, "options", None), "quant_level", UNKNOWN)
        return rs.Info(str(model), self.model_revision, self.weights_sha256, name, name, str(dtype),
                       UNKNOWN, UNKNOWN)

    # ---- scoring ----------------------------------------------------------------------
    def score(self, req):
        """ScoreRequest -> ScoreResponse. Raises ApiError for every defined failure."""
        if (req.expect.model_revision, req.expect.weights_sha256) != (self.model_revision,
                                                                      self.weights_sha256):
            raise ApiError("revision_mismatch")
        if not self.loaded.is_set():
            raise ApiError("model_loading")
        if self.backend_name() is None:
            raise ApiError("backend_unavailable")
        events = [{"state": i.state, "question": {"action": i.question.to_dict()}} for i in req.items]
        t_queue = time.monotonic()
        with self._score_lock:
            queue_ms = (time.monotonic() - t_queue) * 1000.0
            t0 = time.monotonic()
            try:
                raws = self.manager.score(events)
            except lb.BackendRuntimeError:
                # The manager has already demoted down the ladder. No rung left means unavailable.
                raise ApiError("backend_unavailable" if self.backend_name() is None else "internal")
            except Exception as e:
                log.error("laya serve: scoring fault (%s)", type(e).__name__)
                raise ApiError("internal")
            total_ms = (time.monotonic() - t0) * 1000.0
        if not isinstance(raws, (list, tuple)) or len(raws) != len(req.items):
            log.error("laya serve: backend returned the wrong number of results")
            raise ApiError("internal")
        per_item = total_ms / len(req.items)
        items = tuple(self._result_item(sent, raw, per_item) for sent, raw in zip(req.items, raws))
        return rs.ScoreResponse(req.request_id, self.model_revision, self.weights_sha256,
                                self.backend_name() or UNKNOWN, round(total_ms + queue_ms, 3),
                                round(queue_ms, 3), items)

    @staticmethod
    def _result_item(sent, raw, ms):
        try:
            out = raw.get("answers", raw) if isinstance(raw, dict) else {}
            ans = out.get("action", {}) if isinstance(out, dict) else {}
            probs = ans.get("probabilities") if isinstance(ans, dict) else None
            if not isinstance(probs, dict) or set(probs) != set(sent.question.criteria):
                raise ValueError("criteria")
            clean = {k: float(v) for k, v in probs.items() if not isinstance(v, bool)}
            if len(clean) != len(probs) or any(not 0.0 <= v <= 1.0 for v in clean.values()):
                raise ValueError("range")
            total = sum(clean.values())
            if abs(total - 1.0) > PROB_RENORM_TOLERANCE:
                raise ValueError("sum")
            clean = {k: v / total for k, v in clean.items()}
            # Backends do not report token counts yet; 0 / False mean 'not reported'.
            tokens = ans.get("tokens", 0)
            tokens = tokens if isinstance(tokens, int) and not isinstance(tokens, bool) else 0
            truncated = ans.get("truncated", False) is True
        except (ValueError, TypeError):
            log.error("laya serve: backend output did not match the question")
            raise ApiError("internal")
        return rs.ScoreResultItem(sent.id, clean, tokens, truncated, round(ms, 3))


class ApiError(Exception):
    def __init__(self, code):
        super().__init__(code)
        self.code = code


def _schema_error_code(e):
    """400 for a malformed body, 413 for too many items, 422 for unknown fields or a bad question."""
    msg = str(e)
    if "too many" in msg:
        return "too_large"
    if "unknown field" in msg or "question" in msg or "criteria" in msg or ".type: unknown" in msg:
        return "invalid_question"
    return "bad_request"


class Handler(BaseHTTPRequestHandler):
    server_version = "laya-serve"
    sys_version = ""
    protocol_version = "HTTP/1.1"
    service = None                      # set by make_server

    def log_message(self, format, *args):   # silence the default per-request line (it carries paths)
        pass

    # ---- plumbing ---------------------------------------------------------------------
    def _send(self, status, body, retry_after=None):
        data = json.dumps(body, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        if retry_after is not None:
            self.send_header("Retry-After", str(retry_after))
        self.end_headers()
        self.wfile.write(data)

    def _error(self, code):
        spec = rs.ERRORS[code]
        self._send(spec.http, rs.error_body(code), 1 if spec.http in (429, 503) else None)
        return spec.http

    def _authorized(self):
        svc = self.service
        if svc.token_hashes is None:                       # --insecure-no-auth
            return True
        token = rs.parse_bearer(self.headers.get(rs.AUTH_HEADER))
        if token is None:
            return False
        digest = hash_token(token)
        ok = False
        for known in svc.token_hashes:                     # no early exit: same work for every token
            ok |= hmac.compare_digest(digest, known)
        return ok

    def _read_body(self):
        length = self.headers.get("Content-Length")
        if length is None or not length.isdigit():
            raise ApiError("bad_request")
        n = int(length)
        if n > rs.MAX_BODY_BYTES:
            raise ApiError("too_large")
        return self.rfile.read(n)

    def _finish(self, rid, route, status, t0):
        log.info("request id=%s route=%s status=%d ms=%d", rid, route, status,
                 int((time.monotonic() - t0) * 1000))

    # ---- routes -----------------------------------------------------------------------
    def do_GET(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    def _dispatch(self, method):
        t0 = time.monotonic()
        path = self.path.split("?", 1)[0]
        rid = "-"
        status = 500
        try:
            if (method, path) == ("GET", rs.PATH_HEALTHZ):
                status = 200
                self._send(200, self.service.healthz().to_dict())
            elif (method, path) in (("GET", rs.PATH_HEALTH), ("GET", rs.PATH_INFO), ("POST", rs.PATH_SCORE)):
                if not self._authorized():
                    # Unread body on a rejected request: close, so keep-alive cannot desync.
                    self.close_connection = True
                    status = self._error("unauthorized")
                elif path == rs.PATH_HEALTH:
                    status = 200
                    self._send(200, self.service.health().to_dict())
                elif path == rs.PATH_INFO:
                    status = 200
                    self._send(200, self.service.info().to_dict())
                else:
                    status, rid = self._score()
            else:
                self.close_connection = True
                status = self._error("not_found")
        except ApiError as e:
            self.close_connection = True
            status = self._error(e.code)
        except (BrokenPipeError, ConnectionResetError):
            return
        except Exception as e:
            log.error("laya serve: unhandled fault (%s)", type(e).__name__)
            self.close_connection = True
            try:
                status = self._error("internal")
            except Exception:
                pass
        self._finish(rid, f"{method} {path if path in _KNOWN_PATHS else '?'}", status, t0)

    def _score(self):
        ctype = (self.headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
        if ctype != "application/json":
            raise ApiError("unsupported_media_type")
        raw = self._read_body()
        try:
            body = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            raise ApiError("bad_request")
        rid = "-"
        if isinstance(body, dict) and isinstance(body.get("request_id"), str) \
                and _SAFE_ID.match(body["request_id"]):
            rid = body["request_id"]
        if isinstance(body, dict) and isinstance(body.get("protocol"), int) \
                and not isinstance(body["protocol"], bool) and body["protocol"] != rs.PROTOCOL:
            self._error_logged(rid, "protocol_unsupported")
            return 426, rid
        try:
            req = rs.ScoreRequest.from_dict(body)
        except rs.SchemaError as e:
            code = _schema_error_code(e)
            self._error_logged(rid, code)
            return rs.ERRORS[code].http, rid
        try:
            resp = self.service.score(req)
        except ApiError as e:
            self._error_logged(rid, e.code)
            return rs.ERRORS[e.code].http, rid
        self._send(200, resp.to_dict())
        return 200, rid

    def _error_logged(self, rid, code):
        self._error(code)


_KNOWN_PATHS = (rs.PATH_HEALTHZ, rs.PATH_HEALTH, rs.PATH_INFO, rs.PATH_SCORE)


def make_server(service, host=DEFAULT_HOST, port=DEFAULT_PORT, tls_cert=None, tls_key=None):
    handler = type("BoundHandler", (Handler,), {"service": service})
    server = ThreadingHTTPServer((host, port), handler)
    server.daemon_threads = True
    if tls_cert:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        ctx.load_cert_chain(tls_cert, tls_key)
        server.socket = ctx.wrap_socket(server.socket, server_side=True)
    return server


def _parse(argv):
    p = argparse.ArgumentParser(prog="laya serve", description="Host a local Laya backend over HTTP.")
    p.add_argument("--host", default=DEFAULT_HOST, help="bind address (default loopback)")
    p.add_argument("--port", type=int, default=DEFAULT_PORT)
    p.add_argument("--backend", default="auto",
                   choices=[b for b in lbm.VALID_OVERRIDES if b != "remote"],
                   help="local backend to host (a remote backend cannot be hosted)")
    p.add_argument("--token-env", default=DEFAULT_TOKEN_ENV,
                   help="env var holding the bearer token(s), laya_<kid>_<secret>, comma separated")
    p.add_argument("--insecure-no-auth", action="store_true",
                   help="DEV ONLY: serve without authentication (loopback only)")
    p.add_argument("--model-revision", default=UNKNOWN, help="revision clients must pin")
    p.add_argument("--weights-sha256", default=UNKNOWN, help="weights hash clients must pin")
    p.add_argument("--tls-cert")
    p.add_argument("--tls-key")
    return p.parse_args(argv)


def main(argv=None, env=None):
    args = _parse(list(sys.argv[1:] if argv is None else argv))
    env = os.environ if env is None else env
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    loopback = args.host in LOOPBACK
    if args.insecure_no_auth:
        if not loopback:
            log.error("--insecure-no-auth is only allowed on a loopback address")
            return 2
        tokens = None
        log.warning("laya serve: AUTHENTICATION IS OFF (--insecure-no-auth). Development only.")
    else:
        try:
            tokens = load_tokens(env.get(args.token_env, ""))
        except ValueError as e:
            log.error("laya serve: %s (from $%s)", e, args.token_env)
            return 2
        if not tokens:
            log.error("laya serve: no token in $%s; set it or pass --insecure-no-auth for development",
                      args.token_env)
            return 2
    if args.tls_cert and not args.tls_key:
        log.error("--tls-cert needs --tls-key")
        return 2
    if not loopback and not args.tls_cert:
        log.warning("laya serve: non-loopback address without TLS; terminate TLS in front of it")
    mgr_env = dict(env)
    mgr_env[lbm.ENV_VAR] = args.backend
    manager = lbm.BackendManager(env=mgr_env)
    service = Service(manager, tokens, args.model_revision, args.weights_sha256)
    try:
        server = make_server(service, args.host, args.port, args.tls_cert, args.tls_key)
    except OSError as e:
        log.error("laya serve: cannot bind %s:%s (%s)", args.host, args.port, type(e).__name__)
        return 1
    service.start_loading()
    log.info("laya serve: listening on %s:%d, backend=%s (loading), auth=%s",
             args.host, server.server_address[1], args.backend, "off" if tokens is None else "bearer")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
