"""Remote Laya end to end: a real `laya serve` instance on an ephemeral port, the RemoteBackend client,
the backend ladder and the gate's /health, with the negative cases.

No external network: every server listens on 127.0.0.1. No fixed sleeps: waits are on events and
sockets, and the timeout case is bounded by the client's own deadline."""
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.request
from unittest import mock
from http.server import ThreadingHTTPServer

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

import laya_backend_manager as bm  # noqa: E402
import laya_backends as lb  # noqa: E402
import laya_gate  # noqa: E402
import laya_remote_backend as rb  # noqa: E402
import laya_serve  # noqa: E402
from tests.test_laya_backend_manager import apple, make  # noqa: E402

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
TOKEN = "laya_kid01_" + "a" * 40
WRONG = "laya_kid99_" + "z" * 40
REV, SHA = "rev-e2e", "sha-e2e"
SENTINEL = "SENTINEL-ZEBRA-7731-DO-NOT-LEAK"
CRIT = ["interrupt her now", "log it and stay silent"]
QUESTION = {"action": {"type": "choice", "instructions": "Pick one.", "criteria": CRIT}}
LANE3_TEXT = "the build log was rotated " + SENTINEL     # unremarkable: lanes 1 and 2 pass it to the model
EVENT = {"state": "Event: STALL " + SENTINEL, "question": QUESTION}
REMOTE_PROBS = {CRIT[0]: 0.8, CRIT[1]: 0.2}


def served_scores(events):
    return [{"answers": {"action": {"probabilities": dict(REMOTE_PROBS), "confidence": 0.8}}}
            for _ in events]


def closed_port():
    """A port nothing listens on: bind, read the number, close. Connecting is refused at once."""
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class Instance:
    """`laya serve` in this process: the real Service and Handler, on a port the OS picked.

    Records, at the socket and handler level, what reaches it: accepted connections, raw score
    request bodies, and the status of each answer."""

    def __init__(self, token=TOKEN, score_fn=served_scores, backend_name="fakegpu"):
        self.connections = 0
        self.bodies = []
        self.statuses = []
        self.release = threading.Event()        # tests that block scoring wait on this
        cls = lb.make_fake_backend(backend_name, score_fn=lambda ev: score_fn(ev))
        mgr = bm.BackendManager(detect=lambda: None, env={}, rungs=[cls])
        self.service = laya_serve.Service(mgr, [laya_serve.hash_token(token)], REV, SHA, version="e2e")
        self.service.start_loading(wait=True)
        self.server = laya_serve.make_server(self.service, "127.0.0.1", 0)
        outer = self

        class Recording(self.server.RequestHandlerClass):
            def _read_body(self):
                body = super()._read_body()
                outer.bodies.append(body)
                return body

            def _send(self, status, *a, **kw):
                outer.statuses.append(status)
                return super()._send(status, *a, **kw)

        self.server.RequestHandlerClass = Recording
        real_get_request = self.server.get_request

        def counting_get_request():
            conn = real_get_request()
            outer.connections += 1
            return conn
        self.server.get_request = counting_get_request
        self.port = self.server.server_address[1]
        self.url = f"http://127.0.0.1:{self.port}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def close(self):
        self.release.set()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(5)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.token_path = self.write_token(TOKEN)
        laya_gate._manager = None
        laya_gate._model = None
        laya_gate._model_error = None
        laya_gate._recent.clear()
        self.addCleanup(self.reset_gate)
        for target, value in (("_log_decision", lambda *a, **k: None), ("in_quiet_hours", lambda *a, **k: False)):
            p = mock.patch.object(laya_gate, target, value)
            p.start()
            self.addCleanup(p.stop)

    @staticmethod
    def reset_gate():
        laya_gate._manager = None
        laya_gate._model = None
        laya_gate._model_error = None

    def write_token(self, token, name="tok"):
        path = os.path.join(self.tmp.name, name)
        with open(path, "w") as f:
            f.write(token + "\n")
        os.chmod(path, 0o600)
        return path

    def instance(self, **kw):
        inst = Instance(**kw)
        self.addCleanup(inst.close)
        return inst

    def remote_cfg(self, url, **kw):
        cfg = {"enabled": True, "url": url, "token_file": self.token_path, "ack": "stamp",
               "connect_timeout": 0.5, "total_timeout": 1.0}
        cfg.update(kw)
        return cfg

    def main_instance(self, remote, ladder=("remote", "cpu")):
        """The main instance: a ladder of remote then a local fake cpu that answers differently."""
        cpu = make("cpu", tag="local-cpu")
        by_name = {"cpu": cpu, "remote": lb.RemoteBackend}
        m = bm.BackendManager(hw=apple(), env={}, config_value=None, ladder=list(ladder),
                              remote_config=remote, rungs=[cpu, lb.RemoteBackend],
                              backends_by_name=by_name)
        laya_gate._manager = m
        laya_gate._model = m.adapter
        return m

    def gate_health(self):
        """GET /health from the gate's own HTTP handler, on an ephemeral port."""
        srv = ThreadingHTTPServer(("127.0.0.1", 0), laya_gate.Handler)
        srv.daemon_threads = True
        t = threading.Thread(target=srv.serve_forever, daemon=True)
        t.start()
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{srv.server_address[1]}/health", timeout=5) as r:
                return json.loads(r.read())
        finally:
            srv.shutdown()
            srv.server_close()
            t.join(5)

    def predict(self, m):
        return m.adapter.predict(EVENT["state"], QUESTION)


def probs(out):
    return out["answers"]["action"]["probabilities"]


class HappyPathTest(Base):
    def test_scores_come_from_the_remote_and_health_names_it(self):
        inst = self.instance()
        m = self.main_instance(self.remote_cfg(inst.url))
        out = self.predict(m)
        self.assertEqual(probs(out), REMOTE_PROBS)
        self.assertEqual(len(inst.bodies), 1)                # one score request reached the server
        h = self.gate_health()
        self.assertEqual(h["backend"], "remote")
        self.assertEqual(h["active_label"], f"remote {inst.url}")
        self.assertEqual(h["skipped"], [])
        self.assertFalse(h["model_less"])
        # the remote's own backend name, as the remote reports it on /health and /v1/info
        req = urllib.request.Request(inst.url + "/health", headers={"Authorization": "Bearer " + TOKEN})
        with urllib.request.urlopen(req, timeout=5) as r:
            remote_health = json.loads(r.read())
        self.assertEqual(remote_health["backend"], "fakegpu")
        self.assertEqual(remote_health["status"], "ok")
        self.assertEqual(m._active.last_info.rung, "fakegpu")
        self.assertNotIn(TOKEN, json.dumps(h))

    def test_gate_lane_three_decision_uses_the_remote(self):
        inst = self.instance()
        self.main_instance(self.remote_cfg(inst.url))
        verdict = laya_gate.classify("cc-t", "", LANE3_TEXT)
        self.assertEqual(verdict["lane"], 3, verdict)
        self.assertEqual(len(inst.bodies), 1, verdict)
        self.assertIn(SENTINEL, json.loads(inst.bodies[0])["items"][0]["state"])

    def test_laya_serve_subprocess_is_a_real_second_instance(self):
        """The CLI entry point, as the docs run it: its own process, token from the environment."""
        port = closed_port()
        env = dict(os.environ, LAYA_SERVE_TOKEN=TOKEN, PYTHONUNBUFFERED="1")
        for k in list(env):
            if k.startswith("LAYA_GATE_") or k == "LAYA_BACKEND":
                del env[k]
        proc = subprocess.Popen(
            [sys.executable, os.path.join(ROOT, "scripts", "laya.py"), "serve", "--backend", "stub",
             "--port", str(port), "--model-revision", REV, "--weights-sha256", SHA],
            env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        self.addCleanup(proc.stdout.close)
        self.addCleanup(proc.wait, 10)
        self.addCleanup(proc.terminate)
        url = f"http://127.0.0.1:{port}"
        deadline = time.monotonic() + 30
        ready = False
        while time.monotonic() < deadline and proc.poll() is None:
            try:
                with urllib.request.urlopen(url + "/healthz", timeout=1) as r:
                    ready = json.loads(r.read()).get("model_loaded") is True
            except OSError:
                ready = False
            if ready:
                break
            try:
                proc.wait(0.05)
            except subprocess.TimeoutExpired:
                pass
        self.assertTrue(ready, f"server did not come up (exit={proc.poll()})")
        req = urllib.request.Request(url + "/health", headers={"Authorization": "Bearer " + TOKEN})
        with urllib.request.urlopen(req, timeout=5) as r:
            self.assertEqual(json.loads(r.read())["backend"], "stub")
        be = rb.RemoteBackend(url, self.token_path, model_revision=REV, weights_sha256=SHA,
                              connect_timeout_s=1.0, total_timeout_s=5.0)
        be.load()
        stub_q = {"action": {"type": "choice", "instructions": "Pick one.",
                             "criteria": ["interrupt her now", "hold"]}}
        out = be.score([{"state": EVENT["state"], "question": stub_q}])
        self.assertEqual(set(probs(out[0])), {"interrupt her now", "hold"})
        self.assertAlmostEqual(sum(probs(out[0]).values()), 1.0, places=5)


class UnreachableTest(Base):
    def test_closed_port_falls_to_the_next_rung_with_a_reason(self):
        url = f"http://127.0.0.1:{closed_port()}"
        m = self.main_instance(self.remote_cfg(url))
        out = self.predict(m)
        self.assertNotEqual(probs(out), REMOTE_PROBS)
        h = self.gate_health()
        self.assertEqual(h["backend"], "cpu")
        self.assertEqual(h["skipped"][0]["rung"], "remote")
        reason = h["skipped"][0]["reason"]
        self.assertIn(url, reason)
        self.assertTrue("connection refused" in reason or "unreachable" in reason, reason)
        self.assertNotIn(TOKEN, json.dumps(h))

    def test_closed_port_and_no_local_rung_is_model_less_not_a_crash(self):
        url = f"http://127.0.0.1:{closed_port()}"
        m = self.main_instance(self.remote_cfg(url), ladder=("remote",))
        st = m.start()
        self.assertTrue(st.model_less)
        h = self.gate_health()
        self.assertIsNone(h["backend"])
        self.assertTrue(h["model_less"])
        self.assertTrue("connection refused" in h["skipped"][0]["reason"]
                        or "unreachable" in h["skipped"][0]["reason"])
        # lane 3 fails open toward showing the event
        verdict = laya_gate.classify("cc-t", "", LANE3_TEXT)
        self.assertTrue(verdict["escalate"], verdict)
        self.assertIn("classifier unavailable", verdict["rule"])


class WrongTokenTest(Base):
    def test_server_refuses_then_gate_falls_back_without_leaking_the_token(self):
        inst = self.instance()
        bad = self.write_token(WRONG, "bad")
        m = self.main_instance(self.remote_cfg(inst.url, token_file=bad))
        self.predict(m)
        self.assertIn(401, inst.statuses)
        self.assertEqual(inst.bodies, [])                    # no event data was sent to a refusing server
        h = self.gate_health()
        self.assertEqual(h["backend"], "cpu")
        self.assertEqual(h["skipped"][0]["reason"], f"remote {inst.url}: auth failed (401)")
        blob = json.dumps(h)
        self.assertNotIn(WRONG, blob)
        self.assertNotIn(TOKEN, blob)
        # an auth failure latches: no retry traffic on the next call
        before = inst.connections
        self.predict(m)
        self.assertEqual(inst.connections, before)
        self.assertIsNone(self.gate_health()["skipped"][0]["retry_in_s"])

    def test_wrong_token_is_not_logged(self):
        inst = self.instance()
        bad = self.write_token(WRONG, "bad")
        m = self.main_instance(self.remote_cfg(inst.url, token_file=bad))
        with self.assertLogs(level="DEBUG") as cm:
            self.predict(m)
        self.assertFalse(any(WRONG in line or TOKEN in line for line in cm.output), cm.output)


class TimeoutTest(Base):
    def test_slow_remote_falls_back_within_the_timeout_bound(self):
        total = 0.4
        holder = {}

        def blocked(events):
            holder["inst"].release.wait(30)          # released by teardown, never a fixed sleep
            return served_scores(events)

        inst = Instance(score_fn=blocked)
        holder["inst"] = inst
        self.addCleanup(inst.close)
        m = self.main_instance(self.remote_cfg(inst.url, total_timeout=total))
        t0 = time.monotonic()
        out = self.predict(m)
        elapsed = time.monotonic() - t0
        self.assertNotEqual(probs(out), REMOTE_PROBS)        # the local rung answered
        self.assertLess(elapsed, total + 1.5, elapsed)       # bounded by the client deadline, with slack
        h = self.gate_health()
        self.assertEqual(h["backend"], "cpu")
        self.assertEqual(h["skipped"][0]["reason"], f"remote {inst.url}: timeout after {total:g}s")
        self.assertIsNotNone(h["skipped"][0]["retry_in_s"])  # a timeout cools down; it does not latch


class NoEgressByDefaultTest(Base):
    def assert_silent(self, inst):
        self.assertEqual(inst.connections, 0)
        self.assertEqual(inst.bodies, [])

    def test_url_present_but_remote_not_enabled(self):
        inst = self.instance()
        m = self.main_instance(self.remote_cfg(inst.url, enabled=False))
        out = self.predict(m)
        self.assertNotEqual(probs(out), REMOTE_PROBS)
        self.assertEqual(self.gate_health()["backend"], "cpu")
        self.assert_silent(inst)

    def test_gate_decision_with_default_config_sends_nothing(self):
        inst = self.instance()
        self.main_instance(self.remote_cfg(inst.url, enabled=False))
        verdict = laya_gate.classify("cc-t", "", LANE3_TEXT)
        self.assertEqual(verdict["lane"], 3, verdict)       # the model path ran, locally
        self.assert_silent(inst)

    def test_enabled_without_consent_stamp_stays_off(self):
        inst = self.instance()
        cfg = self.remote_cfg(inst.url)
        del cfg["ack"]
        m = self.main_instance(cfg)
        self.predict(m)
        self.assertEqual(self.gate_health()["backend"], "cpu")
        self.assert_silent(inst)

    def test_env_local_vetoes_an_enabled_remote(self):
        inst = self.instance()
        cpu = make("cpu")
        m = bm.BackendManager(hw=apple(), env={"LAYA_GATE_BACKEND": "local"}, config_value=None,
                              ladder=["remote", "cpu"], remote_config=self.remote_cfg(inst.url),
                              rungs=[cpu, lb.RemoteBackend],
                              backends_by_name={"cpu": cpu, "remote": lb.RemoteBackend})
        self.predict(m)
        self.assert_silent(inst)

    def test_default_manager_without_any_remote_config_opens_no_socket(self):
        inst = self.instance()
        cpu = make("cpu")
        m = bm.BackendManager(hw=apple(), env={}, config_value=None, rungs=[cpu, lb.RemoteBackend],
                              backends_by_name={"cpu": cpu}, remote_config={})
        self.predict(m)
        self.assert_silent(inst)


class FieldAllowlistTest(Base):
    def test_request_body_keys_match_the_documented_egress_list(self):
        inst = self.instance()
        m = self.main_instance(self.remote_cfg(inst.url))
        ev = dict(EVENT, kind="stall", session="my-session", expecting=True, lane=3,
                  token=TOKEN, cwd="/Users/someone/private")
        m.score([ev])
        self.assertEqual(len(inst.bodies), 1)
        body = json.loads(inst.bodies[0])
        self.assertEqual(set(body), {"protocol", "request_id", "expect", "items"})
        self.assertEqual(set(body["expect"]), {"model_revision", "weights_sha256"})
        self.assertEqual(len(body["items"]), 1)
        self.assertEqual(set(body["items"][0]), {"id", "state", "question"})
        self.assertEqual(set(body["items"][0]["question"]), {"type", "instructions", "criteria"})
        # the code's own constants agree with the test's literal lists
        self.assertEqual(set(body), rb.EGRESS_REQUEST_KEYS)
        wire = inst.bodies[0].decode()
        for leak in ("my-session", "someone", TOKEN, "expecting"):
            self.assertNotIn(leak, wire)

    def test_bearer_travels_only_in_the_header(self):
        inst = self.instance()
        m = self.main_instance(self.remote_cfg(inst.url))
        self.predict(m)
        self.assertTrue(inst.bodies)
        for b in inst.bodies:
            self.assertNotIn(TOKEN.encode(), b)


if __name__ == "__main__":
    unittest.main()
