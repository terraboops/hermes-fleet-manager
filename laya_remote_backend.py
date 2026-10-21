"""RemoteBackend: a LayaBackend that scores through a remote `laya serve` over HTTP.

Contract: docs/laya-remote.md, shared code: laya_remote_schema.py. laya_backend_manager builds this
class for a remote rung, and only when the remote config is enabled.

Rules this client enforces:
  * Outbound data is built by an explicit allowlist (see EGRESS_*): protocol, request_id, the model
    pin, and per item only id, state and the question object. Any other key on an event is dropped.
  * Every request has a connect timeout and an absolute total deadline. A watchdog closes the socket
    at the deadline, so a server that trickles bytes cannot hold the caller.
  * Failures are typed (RemoteUnreachable, RemoteAuthError, RemoteTimeout, RemoteBadResponse, plus
    RemoteRevisionMismatch and RemoteProtocolUnsupported). Each carries a fixed, short `reason`
    for /health. Reasons never contain the token, the event text, or any server-supplied text.
  * The token comes from a file (mode 0600 enforced), is read at call time and is never stored in
    an attribute that is printed. No redirects are followed. A non-loopback URL must be https.
  * A 200 is a score only if it validates against the request. Anything else is RemoteBadResponse.
"""
import http.client
import json
import os
import socket
import ssl
import stat
import threading
import time
import urllib.parse

import laya_backends as lb
import laya_remote_schema as rs

BACKEND_NAME = "remote"
MAX_RESPONSE_BYTES = 1 << 20
RETRY_AFTER_MAX_S = 0.5
LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "::1")

# The complete outbound surface of POST /v1/score. Tests assert the wire payload equals these.
EGRESS_REQUEST_KEYS = frozenset({"protocol", "request_id", "expect", "items"})
EGRESS_EXPECT_KEYS = frozenset({"model_revision", "weights_sha256"})
EGRESS_ITEM_KEYS = frozenset({"id", "state", "question"})
EGRESS_QUESTION_KEYS = frozenset({"type", "instructions", "criteria"})


# ---------------------------------------------------------------- errors

class RemoteError(lb.BackendRuntimeError):
    """Base. `reason` is fixed text safe for /health. `latch` means: stop sending event data until
    the owner reconfigures (auth, pin, protocol)."""
    reason = "remote failure"
    latch = False
    retryable = False
    _retry_ok = False       # one retry allowed by the contract (reset, 500, quick 503)
    _retry_wait = 0.0

    def __init__(self, reason=None):
        self.reason = reason or type(self).reason
        super().__init__(self.reason)

    def __str__(self):
        return self.reason


class RemoteUnreachable(RemoteError):
    reason = "remote unreachable"
    retryable = True


class RemoteAuthError(RemoteError):
    reason = "remote rejected the token"
    latch = True
    status = None            # HTTP status when the server refused (401 or 403)


class RemoteTimeout(RemoteError):
    reason = "remote timed out"
    retryable = True


class RemoteBadResponse(RemoteError):
    reason = "remote sent an invalid response"


class RemoteRevisionMismatch(RemoteError):
    reason = "remote model does not match the pinned model"
    latch = True


class RemoteProtocolUnsupported(RemoteError):
    reason = "remote does not serve this protocol version"
    latch = True


# ---------------------------------------------------------------- client

class _Transport:
    """One HTTP exchange with a hard total deadline. Returns (status, headers, raw_body)."""

    def __init__(self, url, connect_timeout, ssl_context=None):
        parts = urllib.parse.urlsplit(url)
        self.scheme = parts.scheme
        self.host = parts.hostname
        self.port = parts.port or (443 if parts.scheme == "https" else 80)
        self.prefix = parts.path.rstrip("/")
        self.connect_timeout = connect_timeout
        self.ssl_context = ssl_context

    def exchange(self, method, path, body, headers, deadline):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise RemoteTimeout()
        timeout = min(self.connect_timeout, remaining)
        if self.scheme == "https":
            ctx = self.ssl_context or ssl.create_default_context()
            conn = http.client.HTTPSConnection(self.host, self.port, timeout=timeout, context=ctx)
        else:
            conn = http.client.HTTPConnection(self.host, self.port, timeout=timeout)
        fired = threading.Event()

        def watchdog():
            fired.set()
            sock = conn.sock
            if sock is not None:
                try:
                    sock.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                try:
                    sock.close()
                except OSError:
                    pass

        timer = None
        try:
            try:
                conn.connect()
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise socket.timeout()
                conn.sock.settimeout(remaining)
                timer = threading.Timer(remaining, watchdog)
                timer.daemon = True
                timer.start()
                conn.request(method, self.prefix + path, body=body, headers=headers)
                resp = conn.getresponse()
                raw = resp.read(MAX_RESPONSE_BYTES + 1)
                if fired.is_set():
                    raise RemoteTimeout()
                declared = resp.getheader("Content-Length")
                if declared is not None and declared.isdigit() and \
                        len(raw) < min(int(declared), MAX_RESPONSE_BYTES + 1):
                    raise RemoteUnreachable("remote closed the connection early")
                return resp.status, resp.headers, raw
            except (socket.timeout, TimeoutError):
                raise RemoteTimeout()
            except ssl.SSLError:
                raise RemoteUnreachable("remote TLS handshake failed")
            except ConnectionRefusedError:
                raise RemoteUnreachable("connection refused")
            except ConnectionResetError:
                if fired.is_set():
                    raise RemoteTimeout()
                e = RemoteUnreachable("remote reset the connection")
                e._retry_ok = True
                raise e
            except (ConnectionError, socket.gaierror, OSError, http.client.HTTPException):
                if fired.is_set():
                    raise RemoteTimeout()
                raise RemoteUnreachable()
        finally:
            if timer is not None:
                timer.cancel()
            try:
                conn.close()
            except Exception:
                pass


def _retry_after(headers):
    try:
        return float(headers.get("Retry-After"))
    except (TypeError, ValueError):
        return None


class RemoteBackend(lb.LayaBackend):
    """Scores through `laya serve`. Events in, the same raw outputs a local backend returns."""
    name = BACKEND_NAME

    def __init__(self, url, token_file=rs.DEFAULT_TOKEN_FILE,
                 connect_timeout_s=rs.DEFAULT_CONNECT_TIMEOUT_S,
                 total_timeout_s=rs.DEFAULT_TOTAL_TIMEOUT_S["decide"],
                 model_revision=None, weights_sha256=None, options=None, ssl_context=None):
        super().__init__(options)
        if not isinstance(url, str):
            raise ValueError("url must be a string")
        parts = urllib.parse.urlsplit(url)
        if parts.scheme == "https" and parts.hostname:
            pass
        elif parts.scheme == "http" and parts.hostname in LOOPBACK_HOSTS:
            pass
        else:
            raise ValueError("url must be https unless it is loopback")
        if parts.username or parts.password:
            raise ValueError("url must not carry credentials")
        for v in (connect_timeout_s, total_timeout_s):
            if not isinstance(v, (int, float)) or isinstance(v, bool) or v <= 0:
                raise ValueError("timeouts must be positive numbers")
        self.url = url
        self.host = parts.hostname
        self.token_file = os.path.expanduser(token_file)
        self.connect_timeout_s = float(connect_timeout_s)
        self.total_timeout_s = float(total_timeout_s)
        self.model_revision = model_revision
        self.weights_sha256 = weights_sha256
        self._transport = _Transport(url, self.connect_timeout_s, ssl_context)
        self.last_info = None
        self.last_remote_ms = None

    @classmethod
    def from_config(cls, cfg, question="decide", **kw):
        """Build from a resolved, enabled rs.RemoteConfig."""
        if not cfg.enabled:
            raise ValueError("remote is not enabled")
        return cls(cfg.url, cfg.token_file or rs.DEFAULT_TOKEN_FILE, cfg.connect_timeout_s,
                   cfg.total_timeout_for(question), **kw)

    # -- token

    def _token(self):
        try:
            st = os.stat(self.token_file)
            if not stat.S_ISREG(st.st_mode) or st.st_mode & 0o077:
                raise RemoteAuthError("token file must be a regular file with mode 0600")
            with open(self.token_file, encoding="utf-8") as f:
                token = f.read().strip()
        except RemoteAuthError:
            raise
        except OSError:
            raise RemoteAuthError("token file is missing or unreadable")
        if not rs.TOKEN_PATTERN.match(token):
            raise RemoteAuthError("token file does not hold a valid token")
        return token

    # -- HTTP

    def _call(self, method, path, payload=None, auth=True, budget=None):
        """One request with at most one contract-permitted retry inside the total budget."""
        deadline = time.monotonic() + (budget if budget is not None else self.total_timeout_s)
        body = None
        headers = {"Accept": "application/json"}
        if payload is not None:
            body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            headers["Content-Type"] = "application/json"
        if auth:
            headers.update(rs.auth_header(self._token()))
        retried = False
        while True:
            try:
                status, hdrs, raw = self._transport.exchange(method, path, body, headers, deadline)
                return self._interpret(status, hdrs, raw)
            except RemoteError as e:
                if retried or not e._retry_ok or deadline - time.monotonic() <= 0:
                    raise
                retried = True
                wait = e._retry_wait
                if wait >= deadline - time.monotonic():
                    raise
                time.sleep(wait)

    def _interpret(self, status, headers, raw):
        if len(raw) > MAX_RESPONSE_BYTES:
            raise RemoteBadResponse("remote response is too large")
        if 200 <= status < 300:
            try:
                return json.loads(raw.decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                raise RemoteBadResponse("remote sent malformed JSON")
        try:
            err_body = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            err_body = None
        spec = rs.parse_error(status, err_body)
        if status in (401, 403):
            e = RemoteAuthError("remote rejected the token" if status == 401
                                else "remote refused this token for the request")
            e.status = status
            raise e
        if status == 409:
            raise RemoteRevisionMismatch()
        if status == 426:
            raise RemoteProtocolUnsupported()
        if status == 429:
            raise RemoteUnreachable("remote is rate limiting")
        if status == 503:
            e = RemoteUnreachable("remote has no usable model" if spec.code == "backend_unavailable"
                                  else "remote is loading or overloaded")
            ra = _retry_after(headers)
            if ra is not None and ra <= RETRY_AFTER_MAX_S:
                e._retry_ok, e._retry_wait = True, ra
            raise e
        if status >= 500:
            e = RemoteUnreachable("remote server fault")
            e._retry_ok, e._retry_wait = True, 0.0
            raise e
        raise RemoteBadResponse(f"remote rejected the request (HTTP {status})")

    # -- LayaBackend

    def probe(self):
        """Cheap liveness check against the unauthenticated /healthz. Never sends event data."""
        try:
            hz = rs.Healthz.from_dict(self._call("GET", rs.PATH_HEALTHZ, auth=False,
                                                 budget=self.total_timeout_s))
        except rs.SchemaError:
            return lb.Probe(False, "remote sent an invalid response")
        except RemoteError as e:
            return lb.Probe(False, e.reason)
        if hz.protocol != rs.PROTOCOL:
            return lb.Probe(False, RemoteProtocolUnsupported.reason)
        if not hz.ok or not hz.model_loaded:
            return lb.Probe(False, "remote model is not loaded")
        return lb.Probe(True)

    def health(self):
        """Authenticated /health, parsed. Raises a RemoteError subclass on failure."""
        try:
            return rs.Health.from_dict(self._call("GET", rs.PATH_HEALTH))
        except rs.SchemaError:
            raise RemoteBadResponse()

    def info(self):
        try:
            return rs.Info.from_dict(self._call("GET", rs.PATH_INFO))
        except rs.SchemaError:
            raise RemoteBadResponse()

    def load(self):
        """Resolve the model pin. With no pin given, adopt the one the server reports (first use)."""
        t0 = time.monotonic()
        try:
            info = self.info()
        except RemoteError as e:
            err = lb.BackendUnavailable(e.reason)
            err.remote_error = e         # the manager reads the typed cause for /health
            raise err from None
        if info.protocol != rs.PROTOCOL:
            raise lb.BackendUnavailable(RemoteProtocolUnsupported.reason)
        if self.model_revision is None and self.weights_sha256 is None:
            self.model_revision, self.weights_sha256 = info.model_revision, info.weights_sha256
        elif (info.model_revision, info.weights_sha256) != (self.model_revision, self.weights_sha256):
            raise lb.BackendUnavailable(RemoteRevisionMismatch.reason)
        self.last_info = info
        return lb.LoadResult(round(time.monotonic() - t0, 3), f"remote {self.host}")

    def build_request(self, events):
        """The only place outbound data is assembled. Events contribute `state` and `question`
        and nothing else; ids are positional; the request id is random."""
        if self.model_revision is None or self.weights_sha256 is None:
            raise RemoteBadResponse("model pin is not set; call load() first")
        try:
            items = tuple(
                rs.ScoreItem(str(i), e["state"], rs.Question.from_gate(e["question"]))
                for i, e in enumerate(events))
            return rs.ScoreRequest(items, rs.ExpectPin(self.model_revision, self.weights_sha256))
        except (rs.SchemaError, KeyError, TypeError):
            raise RemoteBadResponse("event does not fit the remote contract") from None

    def score(self, events):
        req = self.build_request(events)
        t0 = time.monotonic()
        raw = self._call("POST", rs.PATH_SCORE, req.to_dict())
        try:
            resp = rs.ScoreResponse.from_dict(raw).validate_against(req)
        except rs.SchemaError:
            raise RemoteBadResponse() from None
        self.last_remote_ms = round((time.monotonic() - t0) * 1000.0, 1)
        return [{"answers": {"action": {"probabilities": dict(r.probabilities),
                                        "confidence": max(r.probabilities.values())}}}
                for r in resp.items]
