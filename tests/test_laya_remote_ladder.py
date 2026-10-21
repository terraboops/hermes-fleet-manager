"""Remote rung on the backend ladder: opt-in, any position, fallback, cooldown, /health."""
import json
import os
import socket
import sys
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

import laya_backends as lb  # noqa: E402
import laya_backend_manager as bm  # noqa: E402
import laya_gate  # noqa: E402
import laya_remote_backend as rb  # noqa: E402
from tests.test_laya_backend_manager import apple, make, EV  # noqa: E402
from tests.test_laya_remote_backend import (  # noqa: E402
    Base, TOKEN, info_obj, score_handler, err_handler, reply)


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class LadderBase(Base):
    def remote_cfg(self, url=None, **kw):
        cfg = {"enabled": True, "url": url or self.srv.url, "token_file": self.token_path,
               "ack": "stamp", "connect_timeout": 0.5, "total_timeout": 1.0}
        cfg.update(kw)
        return cfg

    def manager(self, ladder, remote=None, fakes=None, clock=None, **kw):
        fakes = fakes or {}
        classes = {n: fakes.get(n) or make(n) for n in ("mlx", "cpu")}
        by_name = dict(classes, remote=lb.RemoteBackend, stub=lb.StubBackend)
        return bm.BackendManager(hw=apple(), env={}, config_value=None, ladder=ladder,
                                 remote_config=remote if remote is not None else {},
                                 rungs=[classes["mlx"], classes["cpu"], lb.RemoteBackend],
                                 backends_by_name=by_name, clock=clock or time.monotonic, **kw)

    def health(self, m):
        laya_gate._manager = m
        self.addCleanup(setattr, laya_gate, "_manager", None)
        return laya_gate._health_backend_fields()

    def serve(self, *handlers):
        self.srv.script = list(handlers)


def info_ok(h, b):
    reply(h, 200, info_obj())


def route(info=info_ok, score=None):
    score = score or score_handler()

    def h(handler, body):
        (score if handler.path.endswith("/score") else info)(handler, body)
    return h


class OptInTest(LadderBase):
    def test_default_config_never_builds_or_dials_remote(self):
        with mock.patch.object(rb.RemoteBackend, "__init__", side_effect=AssertionError("built")), \
                mock.patch("socket.socket.connect", side_effect=AssertionError("dialled")), \
                mock.patch("socket.create_connection", side_effect=AssertionError("dialled")):
            m = bm.BackendManager(hw=apple(), env={}, config_value=None,
                                  rungs=[make("cpu"), lb.RemoteBackend],
                                  backends_by_name={"cpu": make("cpu")})
            m.start()
            self.assertEqual(m.status().backend, "cpu")
            # cpu down too: the remote rung is reached, is off, and still builds and dials nothing
            m2 = bm.BackendManager(hw=apple(), env={}, config_value=None,
                                   rungs=[make("cpu", load_exc=lb.BackendLoadError("x")), lb.RemoteBackend],
                                   backends_by_name={})
            st = m2.start()
        self.assertTrue(st.model_less)
        self.assertEqual(st.notices, [])
        self.assertIn("not enabled", st.skipped[-1]["reason"])
        self.assertEqual(self.srv.requests, [])

    def test_configured_but_not_enabled_emits_a_notice_and_stays_local(self):
        cfg = self.remote_cfg(enabled=False)
        m = self.manager(["mlx", "remote", "cpu"], remote=cfg, fakes={"mlx": make("mlx", load_exc=lb.BackendLoadError("x"))})
        with self.assertLogs("laya.backend", level="WARNING") as cm:
            st = m.start()
        self.assertEqual(st.backend, "cpu")
        self.assertTrue(any("not enabled" in n for n in st.notices))
        self.assertTrue(any("staying local-only" in line for line in cm.output))
        self.assertEqual(self.srv.requests, [])

    def test_enabled_with_problems_stays_off_and_says_why(self):
        cfg = self.remote_cfg()
        del cfg["ack"]
        m = self.manager(["remote", "cpu"], remote=cfg)
        st = m.start()
        self.assertEqual(st.backend, "cpu")
        self.assertIn("consent stamp", st.skipped[0]["reason"])
        self.assertEqual(self.srv.requests, [])

    def test_env_local_overrides_config_enable(self):
        m = bm.BackendManager(hw=apple(), env={"LAYA_GATE_BACKEND": "local"}, config_value=None,
                              ladder=["remote", "cpu"], remote_config=self.remote_cfg(),
                              rungs=[make("cpu"), lb.RemoteBackend], backends_by_name={"cpu": make("cpu"), "remote": lb.RemoteBackend})
        self.assertEqual(m.start().backend, "cpu")
        self.assertEqual(self.srv.requests, [])


class FallbackTest(LadderBase):
    def test_first_rung_auth_failure_falls_to_next_and_shows_in_health(self):
        self.serve(err_handler(401, "unauthorized"))
        m = self.manager(["remote", "cpu"], remote=self.remote_cfg())
        st = m.start()
        self.assertEqual(st.backend, "cpu")
        h = self.health(m)
        self.assertEqual(h["backend"], "cpu")
        self.assertEqual(h["skipped"][0]["rung"], "remote")
        self.assertEqual(h["skipped"][0]["reason"], f"remote {self.srv.url}: auth failed (401)")
        self.assertNotIn(TOKEN, json.dumps(h))
        self.assertNotIn(TOKEN, json.dumps(h["backend_status"]))

    def test_first_rung_timeout_reason(self):
        self.serve(lambda h, b: time.sleep(1.0))
        m = self.manager(["remote", "cpu"], remote=self.remote_cfg(total_timeout=0.3))
        m.start()
        h = self.health(m)
        self.assertEqual(h["backend"], "cpu")
        self.assertEqual(h["skipped"][0]["reason"], f"remote {self.srv.url}: timeout after 0.3s")

    def test_first_rung_connection_refused_reason(self):
        url = f"http://127.0.0.1:{free_port()}"
        m = self.manager(["remote", "cpu"], remote=self.remote_cfg(url=url))
        m.start()
        h = self.health(m)
        self.assertEqual(h["backend"], "cpu")
        self.assertEqual(h["skipped"][0]["reason"], f"remote {url}: connection refused")

    def test_all_rungs_failing_is_model_less_with_reasons(self):
        self.serve(err_handler(401, "unauthorized"))
        fakes = {n: make(n, load_exc=lb.BackendLoadError("down")) for n in ("mlx", "cpu")}
        m = self.manager(["mlx", "remote", "cpu"], remote=self.remote_cfg(), fakes=fakes)
        st = m.start()
        self.assertTrue(st.model_less)
        h = self.health(m)
        self.assertIsNone(h["backend"])
        self.assertTrue(h["model_less"])
        self.assertEqual([s["rung"] for s in h["skipped"]], ["mlx", "remote", "cpu"])
        self.assertIn("auth failed (401)", h["skipped"][1]["reason"])
        # the existing gate path: lane 3 gets BackendRuntimeError and fails open; the load error names the chain
        with self.assertRaises(lb.BackendRuntimeError):
            m.score([EV])
        self.assertIn("auth failed (401)", laya_gate._load_failure_text(m.status()))

    def test_remote_in_a_middle_rung_serves(self):
        self.serve(route())
        fakes = {"mlx": make("mlx", load_exc=lb.BackendLoadError("no metal"))}
        m = self.manager(["mlx", "remote", "cpu"], remote=self.remote_cfg(), fakes=fakes)
        st = m.start()
        self.assertEqual(st.backend, "remote")
        self.assertEqual(st.active_label, f"remote {self.srv.url}")
        out = m.score([{"state": "s", "question": {"action": {"type": "choice", "instructions": "i",
                                                              "criteria": ["interrupt her now", "log it and stay silent"]}}}])
        self.assertEqual(len(out), 1)
        h = self.health(m)
        self.assertEqual([s["rung"] for s in h["skipped"]], ["mlx"])   # cpu was never tried, so it is not skipped

    def test_remote_first_serves_and_cpu_is_untouched(self):
        self.serve(route())
        m = self.manager(["remote", "mlx", "cpu"], remote=self.remote_cfg())
        self.assertEqual(m.start().backend, "remote")
        self.assertEqual(m.status().skipped, [])

    def test_second_remote_entry_after_a_dead_first(self):
        self.serve(route())
        dead = self.remote_cfg(url=f"http://127.0.0.1:{free_port()}")
        m = self.manager([{"remote": dead}, {"remote": self.remote_cfg()}, "cpu"])
        st = m.start()
        self.assertEqual(st.backend, "remote")
        self.assertEqual(st.active_label, f"remote {self.srv.url}")
        self.assertIn("connection refused", st.skipped[0]["reason"])


class CooldownTest(LadderBase):
    def test_score_failure_demotes_for_a_cooldown_then_remote_returns(self):
        calls = {"n": 0}
        good = route()

        def flaky(handler, body):
            if handler.path.endswith("/score"):
                calls["n"] += 1
                if calls["n"] <= 2:
                    return reply(handler, 500, {})
            good(handler, body)
        self.serve(flaky)
        clock = Clock()
        q = {"action": {"type": "choice", "instructions": "i",
                        "criteria": ["interrupt her now", "log it and stay silent"]}}
        ev = {"state": "s", "question": q}
        m = self.manager(["remote", "cpu"], remote=self.remote_cfg(), clock=clock)
        m.start()
        self.assertEqual(m.status().backend, "remote")
        m.score([ev])                                   # 500, its one retry 500, then cpu answers
        self.assertEqual(m.status().backend, "cpu")
        self.assertIn("remote", m.status().skipped[0]["reason"])
        seen = len(self.srv.requests)
        clock.t += 30
        m.score([ev])
        self.assertEqual(len(self.srv.requests), seen)  # inside the cooldown: no network at all
        self.assertEqual(m.status().skipped[0]["retry_in_s"], 30)
        clock.t += 31
        m.score([ev])                                   # cooldown over: remote is tried again and wins
        self.assertEqual(m.status().backend, "remote")
        self.assertEqual(m.status().skipped, [])

    def test_latched_auth_failure_is_not_retried(self):
        self.serve(err_handler(401, "unauthorized"))
        clock = Clock()
        m = self.manager(["remote", "cpu"], remote=self.remote_cfg(), clock=clock)
        m.start()
        seen = len(self.srv.requests)
        clock.t += 10_000
        m.score([EV])
        self.assertEqual(len(self.srv.requests), seen)
        self.assertEqual(m.status().backend, "cpu")
        self.assertIsNone(m.status().skipped[0]["retry_in_s"])


class LadderConfigTest(LadderBase):
    def test_unknown_entries_are_reported_and_dropped(self):
        m = self.manager(["mlx", "warp-drive", "cpu"])
        st = m.start()
        self.assertEqual(st.ladder, ["mlx", "cpu"])
        self.assertIn("warp-drive", st.ladder_error)

    def test_no_valid_entry_uses_the_default_ladder(self):
        m = self.manager(["nonsense"])
        st = m.start()
        self.assertEqual(st.ladder_source, "default")
        self.assertEqual(st.backend, "mlx")

    def test_ladder_read_from_config_file(self):
        import tempfile
        with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as f:
            f.write("laya:\n  ladder: [cpu, remote]\n  remote:\n    enabled: false\n")
        self.addCleanup(os.unlink, f.name)
        self.assertEqual(bm.read_config_laya(f.name)["ladder"], ["cpu", "remote"])
        self.assertEqual(bm.read_config_laya("/nonexistent"), {})


if __name__ == "__main__":
    unittest.main()
