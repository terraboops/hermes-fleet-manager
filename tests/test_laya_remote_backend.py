"""RemoteBackend: success, typed errors, enforced timeouts, egress allowlist, no secret in errors."""
import json
import os
import socket
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

import laya_backends as lb  # noqa: E402
import laya_remote_backend as rb  # noqa: E402
import laya_remote_schema as rs  # noqa: E402

TOKEN = "laya_kid01_" + "a" * 40
SECRET_TEXT = "ZEBRA-SECRET-EVENT-TEXT-12345"
CRIT = ["interrupt her now", "log it and stay silent"]
REV, SHA = "rev1", "sha1"
QUESTION = {"action": {"type": "choice", "instructions": "Pick one.", "criteria": CRIT}}
EVENT = {"state": "Event: " + SECRET_TEXT, "question": QUESTION}


class MockServer:
    """Programmable server. `script` is a list of handlers; the last one repeats."""

    def __init__(self):
        self.requests = []
        self.script = []
        outer = self

        class H(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def _go(self):
                n = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(n) if n else b""
                outer.requests.append({"method": self.command, "path": self.path,
                                       "headers": dict(self.headers), "body": body})
                idx = min(len(outer.requests) - 1, len(outer.script) - 1)
                outer.script[idx](self, body)

            do_GET = do_POST = _go

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.httpd.daemon_threads = True
        self.port = self.httpd.server_address[1]
        self.url = f"http://127.0.0.1:{self.port}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


def reply(h, status, obj=None, raw=None, headers=None):
    data = raw if raw is not None else json.dumps(obj).encode()
    h.send_response(status)
    h.send_header("Content-Type", "application/json")
    h.send_header("Content-Length", str(len(data)))
    for k, v in (headers or {}).items():
        h.send_header(k, v)
    h.end_headers()
    h.wfile.write(data)


def info_obj(**kw):
    d = {"protocol": 1, "model": "laya", "model_revision": REV, "weights_sha256": SHA,
         "runtime": "mlx", "rung": "mlx", "dtype": "f16", "temperature_digest": "t",
         "golden_digest": "g", "limits": {"max_items": 16, "max_body_bytes": 65536, "max_tokens": 512}}
    d.update(kw)
    return d


def score_handler(probs=None, **over):
    def h(handler, body):
        req = json.loads(body)
        resp = {"protocol": 1, "request_id": req["request_id"], "model_revision": REV,
                "weights_sha256": SHA, "runtime": "mlx", "server_ms": 5, "queue_ms": 0,
                "items": [{"id": i["id"], "probabilities": probs or {CRIT[0]: 0.7, CRIT[1]: 0.3},
                           "tokens": 10, "truncated": False, "ms": 4} for i in req["items"]]}
        resp.update(over)
        reply(handler, 200, resp)
    return h


def err_handler(status, code, headers=None):
    return lambda h, b: reply(h, status, rs.error_body(code), headers=headers)


class Base(unittest.TestCase):
    def setUp(self):
        self.srv = MockServer()
        self.addCleanup(self.srv.close)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.token_path = os.path.join(self.tmp.name, "tok")
        with open(self.token_path, "w") as f:
            f.write(TOKEN + "\n")
        os.chmod(self.token_path, 0o600)

    def backend(self, **kw):
        kw.setdefault("model_revision", REV)
        kw.setdefault("weights_sha256", SHA)
        kw.setdefault("connect_timeout_s", 0.5)
        kw.setdefault("total_timeout_s", 1.0)
        return rb.RemoteBackend(self.srv.url, self.token_path, **kw)


class ScoreTests(Base):
    def test_success_returns_local_shape(self):
        self.srv.script = [score_handler()]
        out = self.backend().score([EVENT])
        self.assertEqual(out, [{"answers": {"action": {
            "probabilities": {CRIT[0]: 0.7, CRIT[1]: 0.3}, "confidence": 0.7}}}])
        r = self.srv.requests[0]
        self.assertEqual((r["method"], r["path"]), ("POST", "/v1/score"))
        self.assertEqual(r["headers"]["Authorization"], "Bearer " + TOKEN)

    def test_payload_keys_equal_allowlist(self):
        self.srv.script = [score_handler()]
        ev = dict(EVENT, kind="stall", session="mysession", expecting="x", lane=3, token=TOKEN)
        self.backend().score([ev])
        body = json.loads(self.srv.requests[0]["body"])
        self.assertEqual(set(body), rb.EGRESS_REQUEST_KEYS)
        self.assertEqual(set(body["expect"]), rb.EGRESS_EXPECT_KEYS)
        self.assertEqual(len(body["items"]), 1)
        self.assertEqual(set(body["items"][0]), rb.EGRESS_ITEM_KEYS)
        self.assertEqual(set(body["items"][0]["question"]), rb.EGRESS_QUESTION_KEYS)
        wire = self.srv.requests[0]["body"].decode()
        for leak in ("mysession", "stall", "expecting", TOKEN):
            self.assertNotIn(leak, wire)
        # The server-side schema must accept exactly what the client sent.
        rs.ScoreRequest.from_dict(body)

    def test_allowlist_matches_schema_dataclass(self):
        req = rs.ScoreRequest((rs.ScoreItem("0", "s", rs.Question.from_gate(QUESTION)),),
                              rs.ExpectPin(REV, SHA)).to_dict()
        self.assertEqual(set(req), rb.EGRESS_REQUEST_KEYS)
        self.assertEqual(set(req["items"][0]), rb.EGRESS_ITEM_KEYS)

    def test_request_id_random_and_not_from_event(self):
        self.srv.script = [score_handler()]
        b = self.backend()
        b.score([EVENT])
        b.score([EVENT])
        ids = [json.loads(r["body"])["request_id"] for r in self.srv.requests]
        self.assertNotEqual(ids[0], ids[1])
        self.assertNotIn(SECRET_TEXT, ids[0])

    def test_pin_required_before_score(self):
        with self.assertRaises(rb.RemoteBadResponse):
            self.backend(model_revision=None, weights_sha256=None).score([EVENT])
        self.assertEqual(self.srv.requests, [])

    def test_bad_event_never_sent(self):
        with self.assertRaises(rb.RemoteBadResponse):
            self.backend().score([{"state": "x", "question": {"action": {"type": "nope"}}}])
        self.assertEqual(self.srv.requests, [])


class ErrorMappingTests(Base):
    def test_connection_refused(self):
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()
        b = rb.RemoteBackend(f"http://127.0.0.1:{port}", self.token_path, model_revision=REV,
                             weights_sha256=SHA)
        with self.assertRaises(rb.RemoteUnreachable):
            b.score([EVENT])

    def test_auth_401_and_403(self):
        for status, code in ((401, "unauthorized"), (403, "forbidden")):
            self.srv.requests.clear()
            self.srv.script = [err_handler(status, code)]
            with self.assertRaises(rb.RemoteAuthError) as cm:
                self.backend().score([EVENT])
            self.assertTrue(cm.exception.latch)
            self.assertEqual(len(self.srv.requests), 1)       # never retried

    def test_slow_response_times_out(self):
        self.srv.script = [lambda h, b: (time.sleep(1.5), score_handler()(h, b))]
        t0 = time.monotonic()
        with self.assertRaises(rb.RemoteTimeout):
            self.backend(total_timeout_s=0.3).score([EVENT])
        self.assertLess(time.monotonic() - t0, 1.0)

    def test_trickling_response_hits_total_deadline(self):
        def trickle(h, b):
            h.send_response(200)
            h.send_header("Content-Length", "100000")
            h.end_headers()
            try:
                for _ in range(100):
                    h.wfile.write(b"x")
                    h.wfile.flush()
                    time.sleep(0.1)
            except OSError:
                pass
        self.srv.script = [trickle]
        t0 = time.monotonic()
        with self.assertRaises(rb.RemoteTimeout):
            self.backend(total_timeout_s=0.5).score([EVENT])
        self.assertLess(time.monotonic() - t0, 1.5)

    def test_connect_timeout_enforced(self):
        # A listening socket with a full backlog is not portable; assert the value is applied.
        b = self.backend(connect_timeout_s=0.2)
        self.assertEqual(b._transport.connect_timeout, 0.2)

    def test_malformed_json(self):
        self.srv.script = [lambda h, b: reply(h, 200, raw=b"{not json")]
        with self.assertRaises(rb.RemoteBadResponse):
            self.backend().score([EVENT])

    def test_valid_json_wrong_shape(self):
        self.srv.script = [lambda h, b: reply(h, 200, {"items": []})]
        with self.assertRaises(rb.RemoteBadResponse):
            self.backend().score([EVENT])

    def test_probabilities_missing_key_is_not_a_score(self):
        self.srv.script = [score_handler(probs={CRIT[0]: 1.0})]
        with self.assertRaises(rb.RemoteBadResponse):
            self.backend().score([EVENT])

    def test_probabilities_not_summing_to_one(self):
        self.srv.script = [score_handler(probs={CRIT[0]: 0.2, CRIT[1]: 0.2})]
        with self.assertRaises(rb.RemoteBadResponse):
            self.backend().score([EVENT])

    def test_wrong_request_id_and_wrong_model(self):
        self.srv.script = [score_handler(request_id="someone-else")]
        with self.assertRaises(rb.RemoteBadResponse):
            self.backend().score([EVENT])
        self.srv.requests.clear()
        self.srv.script = [score_handler(weights_sha256="other")]
        with self.assertRaises(rb.RemoteBadResponse):
            self.backend().score([EVENT])

    def test_409_and_426_latch(self):
        self.srv.script = [err_handler(409, "revision_mismatch")]
        with self.assertRaises(rb.RemoteRevisionMismatch) as cm:
            self.backend().score([EVENT])
        self.assertTrue(cm.exception.latch)
        self.srv.script = [err_handler(426, "protocol_unsupported")]
        with self.assertRaises(rb.RemoteProtocolUnsupported):
            self.backend().score([EVENT])

    def test_4xx_not_retried(self):
        self.srv.script = [err_handler(422, "invalid_question")]
        with self.assertRaises(rb.RemoteBadResponse):
            self.backend().score([EVENT])
        self.assertEqual(len(self.srv.requests), 1)

    def test_500_retried_once(self):
        self.srv.script = [err_handler(500, "internal")]
        with self.assertRaises(rb.RemoteUnreachable):
            self.backend().score([EVENT])
        self.assertEqual(len(self.srv.requests), 2)

    def test_500_then_success(self):
        self.srv.script = [err_handler(500, "internal"), score_handler()]
        self.assertEqual(len(self.backend().score([EVENT])), 1)
        ids = {json.loads(r["body"])["request_id"] for r in self.srv.requests}
        self.assertEqual(len(ids), 1)                           # a retry reuses the request id

    def test_503_retry_only_when_retry_after_short(self):
        self.srv.script = [err_handler(503, "overloaded", {"Retry-After": "0.1"})]
        with self.assertRaises(rb.RemoteUnreachable):
            self.backend().score([EVENT])
        self.assertEqual(len(self.srv.requests), 2)
        self.srv.requests.clear()
        self.srv.script = [err_handler(503, "overloaded", {"Retry-After": "30"})]
        with self.assertRaises(rb.RemoteUnreachable):
            self.backend().score([EVENT])
        self.assertEqual(len(self.srv.requests), 1)

    def test_429_not_retried(self):
        self.srv.script = [err_handler(429, "rate_limited", {"Retry-After": "1"})]
        with self.assertRaises(rb.RemoteUnreachable):
            self.backend().score([EVENT])
        self.assertEqual(len(self.srv.requests), 1)

    def test_redirect_not_followed(self):
        self.srv.script = [lambda h, b: reply(h, 302, {}, headers={"Location": "http://example.invalid/"})]
        with self.assertRaises(rb.RemoteBadResponse):
            self.backend().score([EVENT])
        self.assertEqual(len(self.srv.requests), 1)

    def test_oversized_response_rejected(self):
        self.srv.script = [lambda h, b: reply(h, 200, raw=b"[" + b"1," * rb.MAX_RESPONSE_BYTES + b"1]")]
        with self.assertRaises(rb.RemoteBadResponse):
            self.backend(total_timeout_s=5.0).score([EVENT])

    def test_errors_are_runtime_errors_with_short_reason(self):
        self.srv.script = [err_handler(401, "unauthorized")]
        with self.assertRaises(lb.BackendRuntimeError) as cm:
            self.backend().score([EVENT])
        self.assertLess(len(cm.exception.reason), 80)

    def test_no_secret_in_any_error(self):
        cases = [err_handler(401, "unauthorized"), err_handler(403, "forbidden"),
                 err_handler(409, "revision_mismatch"), err_handler(500, "internal"),
                 err_handler(503, "backend_unavailable"), err_handler(422, "invalid_question"),
                 lambda h, b: reply(h, 200, raw=b"{bad"),
                 # a hostile server that echoes what it was sent
                 lambda h, b: reply(h, 400, {"error": {"code": "bad_request", "message": b.decode(),
                                                        "retryable": False}}),
                 lambda h, b: reply(h, 200, {"echo": b.decode(), "items": []})]
        for script in cases:
            self.srv.requests.clear()
            self.srv.script = [script]
            with self.assertRaises(rb.RemoteError) as cm:
                self.backend().score([EVENT])
            blob = f"{cm.exception!s}|{cm.exception.reason}|{cm.exception!r}|{cm.exception.args}"
            self.assertNotIn(TOKEN, blob)
            self.assertNotIn(SECRET_TEXT, blob)
            self.assertNotIn("laya_kid01", blob)

    def test_503_is_an_error_not_a_score(self):
        self.srv.script = [err_handler(503, "model_loading")]
        with self.assertRaises(rb.RemoteUnreachable):
            self.backend(total_timeout_s=1.0).score([EVENT])


class TokenTests(Base):
    def test_missing_token_file(self):
        b = rb.RemoteBackend(self.srv.url, os.path.join(self.tmp.name, "nope"),
                             model_revision=REV, weights_sha256=SHA)
        with self.assertRaises(rb.RemoteAuthError):
            b.score([EVENT])
        self.assertEqual(self.srv.requests, [])                 # nothing sent without a token

    def test_group_readable_token_file_refused(self):
        os.chmod(self.token_path, 0o640)
        with self.assertRaises(rb.RemoteAuthError):
            self.backend().score([EVENT])
        self.assertEqual(self.srv.requests, [])

    def test_malformed_token_refused(self):
        with open(self.token_path, "w") as f:
            f.write("hunter2")
        with self.assertRaises(rb.RemoteAuthError) as cm:
            self.backend().score([EVENT])
        self.assertNotIn("hunter2", cm.exception.reason)

    def test_token_reread_each_call(self):
        self.srv.script = [score_handler()]
        b = self.backend()
        b.score([EVENT])
        new = "laya_kid02_" + "b" * 40
        with open(self.token_path, "w") as f:
            f.write(new)
        b.score([EVENT])
        self.assertEqual(self.srv.requests[1]["headers"]["Authorization"], "Bearer " + new)

    def test_token_not_in_repr(self):
        self.assertNotIn(TOKEN, repr(vars(self.backend())))


class ConfigTests(unittest.TestCase):
    def test_rejects_plain_http_non_loopback(self):
        with self.assertRaises(ValueError):
            rb.RemoteBackend("http://example.com")

    def test_rejects_credentials_and_odd_schemes(self):
        for u in ("https://user:pw@example.com", "ftp://example.com", "example.com", "file:///x"):
            with self.assertRaises(ValueError):
                rb.RemoteBackend(u)

    def test_accepts_https_and_loopback(self):
        rb.RemoteBackend("https://laya.example.com")
        rb.RemoteBackend("http://127.0.0.1:8765")
        rb.RemoteBackend("http://localhost:8765")

    def test_timeouts_validated_and_defaults_from_contract(self):
        for bad in (0, -1, None, True):
            with self.assertRaises(ValueError):
                rb.RemoteBackend("https://x.example", connect_timeout_s=bad)
            with self.assertRaises(ValueError):
                rb.RemoteBackend("https://x.example", total_timeout_s=bad)
        b = rb.RemoteBackend("https://x.example")
        self.assertEqual(b.connect_timeout_s, rs.DEFAULT_CONNECT_TIMEOUT_S)
        self.assertEqual(b.total_timeout_s, rs.DEFAULT_TOTAL_TIMEOUT_S["decide"])
        self.assertEqual(b.token_file, os.path.expanduser(rs.DEFAULT_TOKEN_FILE))

    def test_from_config(self):
        cfg = rs.RemoteConfig.resolve(env={
            "LAYA_GATE_BACKEND": "remote", "LAYA_GATE_REMOTE_URL": "https://x.example",
            "LAYA_GATE_REMOTE_ACK": "abc", "LAYA_GATE_REMOTE_CONNECT_TIMEOUT": "0.25"})
        b = rb.RemoteBackend.from_config(cfg, "respond")
        self.assertEqual((b.connect_timeout_s, b.total_timeout_s), (0.25, 2.0))
        with self.assertRaises(ValueError):
            rb.RemoteBackend.from_config(rs.RemoteConfig())


class ProbeLoadHealthTests(Base):
    def test_probe_ok_uses_unauthenticated_healthz(self):
        self.srv.script = [lambda h, b: reply(h, 200, {"ok": True, "model_loaded": True, "protocol": 1})]
        p = self.backend().probe()
        self.assertTrue(p.available)
        self.assertEqual(self.srv.requests[0]["path"], "/healthz")
        self.assertNotIn("Authorization", self.srv.requests[0]["headers"])

    def test_probe_failures_carry_reason(self):
        self.srv.script = [lambda h, b: reply(h, 200, {"ok": True, "model_loaded": False, "protocol": 1})]
        p = self.backend().probe()
        self.assertFalse(p.available)
        self.assertTrue(p.reason)
        self.srv.script = [lambda h, b: reply(h, 200, raw=b"nope")]
        self.assertFalse(self.backend().probe().available)
        self.srv.script = [lambda h, b: reply(h, 200, {"ok": True})]
        self.assertFalse(self.backend().probe().available)

    def test_probe_timeout_and_refused(self):
        self.srv.script = [lambda h, b: time.sleep(1.5)]
        p = self.backend(total_timeout_s=0.3).probe()
        self.assertEqual((p.available, p.reason), (False, rb.RemoteTimeout.reason))

    def test_load_adopts_server_pin_when_unpinned(self):
        self.srv.script = [lambda h, b: reply(h, 200, info_obj())]
        b = self.backend(model_revision=None, weights_sha256=None)
        res = b.load()
        self.assertIsInstance(res, lb.LoadResult)
        self.assertEqual((b.model_revision, b.weights_sha256), (REV, SHA))

    def test_load_with_wrong_pin_is_unavailable(self):
        self.srv.script = [lambda h, b: reply(h, 200, info_obj(model_revision="other"))]
        with self.assertRaises(lb.BackendUnavailable) as cm:
            self.backend().load()
        self.assertNotIn(TOKEN, str(cm.exception))

    def test_load_failure_maps_to_backend_unavailable(self):
        self.srv.script = [err_handler(401, "unauthorized")]
        with self.assertRaises(lb.BackendUnavailable):
            self.backend().load()

    def test_health_parsed(self):
        h = {"status": "ok", "backend": "mlx", "version": "1", "protocol": 1,
             "model_loaded": True, "fallback_chain": ["mlx"]}
        self.srv.script = [lambda hd, b: reply(hd, 200, h)]
        got = self.backend().health()
        self.assertEqual((got.status, got.backend), ("ok", "mlx"))
        self.assertEqual(self.srv.requests[0]["path"], "/health")

    def test_health_bad_body(self):
        self.srv.script = [lambda hd, b: reply(hd, 200, {"status": "ok"})]
        with self.assertRaises(rb.RemoteBadResponse):
            self.backend().health()

    def test_is_a_layabackend(self):
        self.assertTrue(issubclass(rb.RemoteBackend, lb.LayaBackend))
        self.assertEqual(rb.RemoteBackend.name, "remote")


if __name__ == "__main__":
    unittest.main()
