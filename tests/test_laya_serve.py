"""`laya serve`: auth, schema validation, error codes, health, and log hygiene."""
import http.client
import io
import json
import logging
import os
import sys
import threading
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

import laya_backend_manager as lbm  # noqa: E402
import laya_backends as lb  # noqa: E402
import laya_remote_schema as rs  # noqa: E402
import laya_serve  # noqa: E402

TOKEN = "laya_kid01_" + "a" * 40
OTHER = "laya_kid02_" + "b" * 40
SECRET_TEXT = "ZEBRA-SECRET-EVENT-TEXT-12345"
CRIT = ["interrupt her now", "log it and stay silent"]
REV, SHA = "rev1", "sha1"


def score_fn(events):
    out = []
    for e in events:
        crit = e["question"]["action"]["criteria"]
        out.append({"answers": {"action": {"probabilities": {crit[0]: 0.7, crit[1]: 0.3},
                                           "confidence": 0.7}}})
    return out


def make_service(backend=None, tokens=(laya_serve.hash_token(TOKEN),), load=True):
    cls = backend or lb.make_fake_backend("fakegpu", score_fn=score_fn)
    mgr = lbm.BackendManager(detect=lambda: None, env={}, rungs=[cls])
    svc = laya_serve.Service(mgr, list(tokens) if tokens is not None else None, REV, SHA, version="9.9")
    if load:
        svc.start_loading(wait=True)
    return svc


def body(**over):
    b = {"protocol": 1, "request_id": "11111111-2222-3333-4444-555555555555",
         "expect": {"model_revision": REV, "weights_sha256": SHA},
         "items": [{"id": "0", "state": SECRET_TEXT,
                    "question": {"type": "choice", "instructions": "pick", "criteria": CRIT}}]}
    b.update(over)
    return b


def item(i):
    return {"id": str(i), "state": "s",
            "question": {"type": "choice", "instructions": "i", "criteria": CRIT}}


class Base(unittest.TestCase):
    service_kwargs = {}

    def setUp(self):
        self.svc = make_service(**self.service_kwargs)
        self.server = laya_serve.make_server(self.svc, "127.0.0.1", 0)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.logbuf = io.StringIO()
        self.handler = logging.StreamHandler(self.logbuf)
        self.handler.setLevel(logging.DEBUG)
        root = logging.getLogger()
        root.addHandler(self.handler)
        self._old = root.level
        root.setLevel(logging.DEBUG)

    def tearDown(self):
        root = logging.getLogger()
        root.removeHandler(self.handler)
        root.setLevel(self._old)
        self.server.shutdown()
        self.server.server_close()

    def call(self, method, path, payload=None, token=TOKEN, headers=None, raw=None):
        h = {}
        if token is not None:
            h["Authorization"] = f"Bearer {token}"
        data = raw
        if payload is not None:
            data = json.dumps(payload).encode()
        if data is not None:
            h["Content-Type"] = "application/json"
        h.update(headers or {})
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        c.request(method, path, body=data, headers=h)
        r = c.getresponse()
        txt = r.read().decode()
        c.close()
        return r.status, (json.loads(txt) if txt else None)


class Score(Base):
    def test_valid_token_returns_validated_score(self):
        status, out = self.call("POST", "/v1/score", body())
        self.assertEqual(status, 200)
        resp = rs.ScoreResponse.from_dict(out)
        resp.validate_against(rs.ScoreRequest.from_dict(body()))
        self.assertAlmostEqual(resp.items[0].probabilities[CRIT[0]], 0.7)
        self.assertEqual(resp.runtime, "fakegpu")
        self.assertNotIn("escalate", json.dumps(out))

    def test_missing_wrong_or_malformed_token_is_401(self):
        for tok in (None, "laya_kid01_" + "c" * 40, "nonsense"):
            status, out = self.call("POST", "/v1/score", body(), token=tok)
            self.assertEqual((status, out["error"]["code"]), (401, "unauthorized"), tok)
        status, _ = self.call("POST", "/v1/score", body(), token=None,
                              headers={"Authorization": "Basic abc"})
        self.assertEqual(status, 401)

    def test_unconfigured_token_is_rejected(self):
        self.assertEqual(self.call("POST", "/v1/score", body(), token=OTHER)[0], 401)

    def test_malformed_bodies_are_4xx(self):
        cases = [
            (dict(raw=b"{not json"), 400),
            (dict(payload={"protocol": 1}), 400),
            (dict(payload=body(items=[])), 400),
            (dict(payload=body(extra="x")), 422),
            (dict(payload=body(items=[{"id": "0", "state": "s", "question": {
                "type": "choice", "instructions": "i", "criteria": []}}])), 422),
            (dict(payload=body(items=[item(i) for i in range(17)])), 413),
            (dict(payload=body(protocol=2)), 426),
            (dict(payload=body(expect={"model_revision": "other", "weights_sha256": SHA})), 409),
        ]
        for kw, want in cases:
            status, out = self.call("POST", "/v1/score", **kw)
            self.assertEqual(status, want, kw)
            self.assertIn("code", out["error"])

    def test_wrong_content_type_and_oversize(self):
        status, out = self.call("POST", "/v1/score", raw=b"{}", headers={"Content-Type": "text/plain"})
        self.assertEqual((status, out["error"]["code"]), (415, "unsupported_media_type"))
        status, out = self.call("POST", "/v1/score", raw=b" " * (rs.MAX_BODY_BYTES + 1))
        self.assertEqual((status, out["error"]["code"]), (413, "too_large"))

    def test_unknown_path_is_404(self):
        self.assertEqual(self.call("GET", "/v1/admin")[0], 404)
        self.assertEqual(self.call("GET", "/v1/score")[0], 404)

    def test_logs_carry_no_event_text(self):
        self.call("POST", "/v1/score", body())
        self.call("POST", "/v1/score", body(extra=SECRET_TEXT))
        self.call("POST", "/v1/score", raw=("{" + SECRET_TEXT).encode())
        self.call("POST", "/v1/score", body(), token=TOKEN + "x")
        logs = self.logbuf.getvalue()
        self.assertIn("status=200", logs)
        for needle in (SECRET_TEXT, TOKEN, "0.7", CRIT[0]):
            self.assertNotIn(needle, logs)


class BadOutput(Base):
    service_kwargs = {"backend": lb.make_fake_backend(
        "wrong", score_fn=lambda ev: [{"answers": {"action": {"probabilities": {"other": 1.0}}}}] * len(ev))}

    def test_output_not_matching_criteria_is_500_not_a_score(self):
        status, out = self.call("POST", "/v1/score", body())
        self.assertEqual((status, out["error"]["code"]), (500, "internal"))


def _boom(events):
    raise lb.BackendRuntimeError(SECRET_TEXT)


class Boom(Base):
    service_kwargs = {"backend": lb.make_fake_backend("boom", score_fn=_boom)}

    def test_every_rung_failing_is_503_and_message_not_logged_or_returned(self):
        status, out = self.call("POST", "/v1/score", body())
        self.assertEqual((status, out["error"]["code"]), (503, "backend_unavailable"))
        self.assertNotIn(SECRET_TEXT, json.dumps(out))
        self.assertNotIn(SECRET_TEXT, self.logbuf.getvalue())
        self.assertIn("score failed: BackendRuntimeError", self.logbuf.getvalue())
        self.assertEqual(self.call("GET", "/health")[1]["status"], "unavailable")


class ModelLess(Base):
    service_kwargs = {"backend": lb.make_fake_backend("down", available=False)}

    def test_no_backend_is_503_and_health_unavailable(self):
        status, out = self.call("POST", "/v1/score", body())
        self.assertEqual((status, out["error"]["code"]), (503, "backend_unavailable"))
        h = rs.Health.from_dict(self.call("GET", "/health")[1])
        self.assertEqual((h.status, h.backend, h.model_loaded), ("unavailable", None, False))


class Loading(Base):
    service_kwargs = {"load": False}

    def test_loading_state(self):
        status, out = self.call("POST", "/v1/score", body())
        self.assertEqual((status, out["error"]["code"]), (503, "model_loading"))
        self.assertEqual(self.call("GET", "/health")[1]["status"], "loading")
        self.assertEqual(self.call("GET", "/healthz", token=None)[1]["ok"], False)


class Health(Base):
    def test_health_names_backend_version_and_needs_auth(self):
        status, out = self.call("GET", "/health")
        self.assertEqual(status, 200)
        h = rs.Health.from_dict(out)
        self.assertEqual((h.status, h.backend, h.version, h.model_loaded), ("ok", "fakegpu", "9.9", True))
        self.assertEqual(self.call("GET", "/health", token=None)[0], 401)

    def test_healthz_is_open_and_minimal(self):
        status, out = self.call("GET", "/healthz", token=None)
        self.assertEqual(status, 200)
        self.assertEqual(set(out), {"ok", "model_loaded", "protocol"})
        self.assertNotIn("fakegpu", json.dumps(out))

    def test_info(self):
        status, out = self.call("GET", "/v1/info")
        self.assertEqual(status, 200)
        info = rs.Info.from_dict(out)
        self.assertEqual((info.model_revision, info.weights_sha256, info.runtime), (REV, SHA, "fakegpu"))


class NoAuthMode(Base):
    service_kwargs = {"tokens": None}

    def test_no_auth_service_accepts_tokenless(self):
        self.assertEqual(self.call("POST", "/v1/score", body(), token=None)[0], 200)


class Cli(unittest.TestCase):
    def run_main(self, argv, env):
        buf = io.StringIO()
        h = logging.StreamHandler(buf)
        root = logging.getLogger()
        root.addHandler(h)
        try:
            return laya_serve.main(argv, env=env), buf.getvalue()
        finally:
            root.removeHandler(h)

    def test_refuses_without_token(self):
        code, logs = self.run_main(["--port", "0", "--backend", "stub"], {})
        self.assertEqual(code, 2)
        self.assertIn("--insecure-no-auth", logs)

    def test_refuses_malformed_token_without_echoing_it(self):
        code, logs = self.run_main(["--backend", "stub"], {"LAYA_SERVE_TOKEN": "hunter2hunter2"})
        self.assertEqual(code, 2)
        self.assertNotIn("hunter2", logs)

    def test_insecure_refused_off_loopback(self):
        code, _ = self.run_main(["--insecure-no-auth", "--host", "0.0.0.0"], {})
        self.assertEqual(code, 2)

    def test_insecure_warns_and_serves_on_loopback(self):
        orig = laya_serve.make_server
        seen = {}

        def fake_make(service, host, port, *a):
            srv = orig(service, host, 0)
            seen.update(host=host, hashes=service.token_hashes)
            threading.Timer(0.3, srv.shutdown).start()
            return srv
        laya_serve.make_server = fake_make
        try:
            code, logs = self.run_main(["--insecure-no-auth", "--backend", "stub", "--port", "0"], {})
        finally:
            laya_serve.make_server = orig
        self.assertEqual(code, 0)
        self.assertEqual(seen, {"host": "127.0.0.1", "hashes": None})
        self.assertIn("AUTHENTICATION IS OFF", logs)

    def test_remote_backend_cannot_be_hosted(self):
        with self.assertRaises(SystemExit):
            laya_serve.main(["--backend", "remote"], env={})


if __name__ == "__main__":
    unittest.main()
