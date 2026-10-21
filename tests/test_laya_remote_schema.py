"""Wire contract for the opt-in remote Laya server: round trips, strict parsing, auth, opt-in."""
import json
import unittest

import laya_remote_schema as s

Q = {"action": {"type": "choice", "instructions": "What is the right action?",
                "criteria": ["interrupt her now", "log it and stay silent"]}}
PIN = s.ExpectPin("rev1", "sha1")


def req(n=1):
    items = tuple(s.ScoreItem(str(i), f"state {i}", s.Question.from_gate(Q)) for i in range(n))
    return s.ScoreRequest(items, PIN)


def resp_for(r, probs=None):
    probs = probs or {"interrupt her now": 0.6, "log it and stay silent": 0.4}
    return s.ScoreResponse(r.request_id, "rev1", "sha1", "rt", 4.0, 1.0,
                           tuple(s.ScoreResultItem(i.id, dict(probs), 10, False, 3.0) for i in r.items))


def wire(o):
    return json.loads(json.dumps(o.to_dict()))


class RoundTrips(unittest.TestCase):
    def test_request(self):
        r = req(3)
        self.assertEqual(s.ScoreRequest.from_dict(wire(r)), r)

    def test_response(self):
        r = resp_for(req(2))
        self.assertEqual(s.ScoreResponse.from_dict(wire(r)), r)

    def test_health_info_healthz(self):
        h = s.Health("ok", "mlx", "1.2.3", model_loaded=True, fallback_chain=("mlx", "cpu"))
        self.assertEqual(s.Health.from_dict(wire(h)), h)
        self.assertEqual(s.Health.from_dict(wire(s.Health("unavailable", None, "1"))).backend, None)
        i = s.Info("m", "rev", "sha", "rt", "rung", "f16", "t", "g")
        self.assertEqual(s.Info.from_dict(wire(i)), i)
        z = s.Healthz(True, False)
        self.assertEqual(s.Healthz.from_dict(wire(z)), z)

    def test_request_id_is_random_and_fresh(self):
        self.assertNotEqual(req().request_id, req().request_id)


class StrictRequest(unittest.TestCase):
    def test_unknown_fields_rejected_everywhere(self):
        for path in (lambda d: d, lambda d: d["items"][0], lambda d: d["items"][0]["question"],
                     lambda d: d["expect"]):
            d = wire(req())
            path(d)["session"] = "x"
            with self.assertRaises(s.SchemaError):
                s.ScoreRequest.from_dict(d)

    def test_bad_shapes(self):
        base = wire(req())
        cases = []
        for mut in (lambda d: d.pop("expect"), lambda d: d.update(items=[]),
                    lambda d: d.update(items=base["items"] * 17),
                    lambda d: d["items"][0]["question"].update(criteria=[]),
                    lambda d: d["items"][0]["question"].update(criteria=["a", "a"]),
                    lambda d: d["items"][0]["question"].update(type="free"),
                    lambda d: d.update(protocol="1"), lambda d: d.update(request_id=5)):
            d = json.loads(json.dumps(base))
            mut(d)
            cases.append(d)
        dup = json.loads(json.dumps(base))
        dup["items"] = dup["items"] * 2
        cases.append(dup)
        for d in cases:
            with self.assertRaises(s.SchemaError):
                s.ScoreRequest.from_dict(d)

    def test_error_never_echoes_value(self):
        d = wire(req())
        d["items"][0]["state"] = 7
        d["items"][0]["extra"] = "SECRET-EVENT-TEXT"
        with self.assertRaises(s.SchemaError) as c:
            s.ScoreRequest.from_dict(d)
        self.assertNotIn("SECRET", str(c.exception))


class StrictResponse(unittest.TestCase):
    def check(self, mutate):
        r = req()
        d = wire(resp_for(r))
        mutate(d)
        with self.assertRaises(s.SchemaError):
            s.ScoreResponse.from_dict(d).validate_against(r)

    def test_valid(self):
        r = req()
        self.assertEqual(s.ScoreResponse.from_dict(wire(resp_for(r))).validate_against(r).items[0].id, "0")

    def test_unknown_response_fields_ignored(self):
        r = req()
        d = wire(resp_for(r))
        d["new_field"] = 1
        d["items"][0]["extra"] = 2
        s.ScoreResponse.from_dict(d).validate_against(r)

    def test_failures(self):
        self.check(lambda d: d["items"][0]["probabilities"].pop("interrupt her now"))  # missing key
        self.check(lambda d: d["items"][0]["probabilities"].update({"interrupt her now": 0.9}))  # sum
        self.check(lambda d: d.update(request_id="other"))
        self.check(lambda d: d.update(model_revision="rev2"))
        self.check(lambda d: d.update(weights_sha256="zzz"))
        self.check(lambda d: d["items"][0].update(id="9"))
        self.check(lambda d: d["items"].clear())
        self.check(lambda d: d["items"][0]["probabilities"].update({"interrupt her now": 1.5}))
        self.check(lambda d: d["items"][0].pop("truncated"))
        self.check(lambda d: d["items"][0]["probabilities"].update({"extra": 0.0}))


class Errors(unittest.TestCase):
    def test_body_roundtrip_and_fixed_text(self):
        for code, spec in s.ERRORS.items():
            b = s.error_body(code)
            self.assertEqual(s.parse_error(spec.http, b), spec)
        self.assertEqual({e.http for e in s.ERRORS.values()},
                         {400, 401, 403, 404, 409, 413, 415, 422, 426, 429, 500, 503})

    def test_unknown_body_never_echoed(self):
        e = s.parse_error(502, {"error": {"code": "x", "message": "LEAK"}})
        self.assertNotIn("LEAK", e.message)
        self.assertEqual(s.parse_error(500, "junk").http, 500)

    def test_auth_status(self):
        self.assertTrue(s.is_auth_failure(401) and s.is_auth_failure(403))
        self.assertFalse(s.is_auth_failure(503))


class Auth(unittest.TestCase):
    def test_bearer(self):
        self.assertEqual(s.parse_bearer(s.auth_header("laya_k1_abc")["Authorization"]), "laya_k1_abc")
        for bad in (None, "", "Bearer", "Bearer  ", "Basic abc", "bearer abc"):
            self.assertIsNone(s.parse_bearer(bad))

    def test_token_pattern(self):
        self.assertTrue(s.TOKEN_PATTERN.match("laya_abcd_" + "A" * 43))
        self.assertFalse(s.TOKEN_PATTERN.match("laya_abcd_short"))


class OptIn(unittest.TestCase):
    GOOD = {"LAYA_GATE_BACKEND": "remote", "LAYA_GATE_REMOTE_URL": "https://laya.example:8443",
            "LAYA_GATE_REMOTE_ACK": "abc"}

    def test_default_is_local(self):
        for env, cfg in (({}, None), ({}, {}), ({"LAYA_GATE_BACKEND": "local"}, None),
                         ({"LAYA_GATE_BACKEND": "garbage"}, {"enabled": True}),
                         ({}, {"enabled": "yes", "url": "https://x"}), ({}, {"url": "https://x"})):
            c = s.RemoteConfig.resolve(env, cfg)
            self.assertFalse(c.enabled)
            self.assertEqual(c.problems, ())

    def test_enabled_defaults(self):
        c = s.RemoteConfig.resolve(self.GOOD)
        self.assertTrue(c.enabled)
        self.assertEqual((c.redact, c.questions, c.fallback), ("R2", ("decide",), "local-then-standing"))
        self.assertEqual(c.connect_timeout_s, 0.5)
        self.assertEqual((c.total_timeout_for("decide"), c.total_timeout_for("respond")), (1.5, 2.0))
        self.assertEqual(c.token_file, s.DEFAULT_TOKEN_FILE)

    def test_enabled_but_incomplete_stays_off_with_reason(self):
        for env in ({"LAYA_GATE_BACKEND": "remote"},
                    {**self.GOOD, "LAYA_GATE_REMOTE_URL": "http://laya.example"},
                    {**self.GOOD, "LAYA_GATE_REMOTE_ACK": ""},
                    {**self.GOOD, "LAYA_GATE_REMOTE_REDACT": "R0"},
                    {**self.GOOD, "LAYA_GATE_REMOTE_QUESTIONS": "decide,nope"},
                    {**self.GOOD, "LAYA_GATE_REMOTE_CONNECT_TIMEOUT": "-1"}):
            c = s.RemoteConfig.resolve(env)
            self.assertFalse(c.enabled)
            self.assertTrue(c.problems)

    def test_loopback_http_ok_and_inline_token_refused(self):
        env = {**self.GOOD, "LAYA_GATE_REMOTE_URL": "http://127.0.0.1:8765"}
        self.assertTrue(s.RemoteConfig.resolve(env).enabled)
        c = s.RemoteConfig.resolve(self.GOOD, {"token": "laya_x_y"})
        self.assertFalse(c.enabled)

    def test_config_block_and_env_precedence(self):
        cfg = {"enabled": True, "url": "https://a.example", "ack": "k", "redact": "r3"}
        c = s.RemoteConfig.resolve({}, cfg)
        self.assertTrue(c.enabled)
        self.assertEqual(c.redact, "R3")
        c = s.RemoteConfig.resolve({"LAYA_GATE_REMOTE_URL": "https://b.example"}, cfg)
        self.assertEqual(c.url, "https://b.example")
        self.assertFalse(s.RemoteConfig.resolve({"LAYA_GATE_BACKEND": "local"}, cfg).enabled)


if __name__ == "__main__":
    unittest.main()
