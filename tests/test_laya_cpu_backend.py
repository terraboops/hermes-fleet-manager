"""CPU rung: option -> artifact mapping, checksum gate, typed errors, probe, last-local-rung order.
Uses a fake llama-server script, so no model is needed. The real-model parity run is
tests/test_laya_cpu_parity_live.py (skipped unless the artifacts are cached)."""
import hashlib
import json
import os
import stat
import sys
import tempfile
import textwrap
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.realpath(__file__))))
import laya_backends as lb  # noqa: E402

FAKE_SERVER = textwrap.dedent("""\
    #!%(py)s
    import http.server, json, sys
    a = sys.argv
    port = int(a[a.index("--port") + 1])
    mode = open(%(mode)r).read().strip()
    print("n_threads = %%s" %% (a[a.index("-t") + 1] if "-t" in a else "auto"), flush=True)
    print("args " + " ".join(a), flush=True)
    if mode == "oom":
        print("ggml_backend_cpu_buffer_type_alloc_buffer: failed to allocate buffer", flush=True)
        sys.exit(1)
    if mode == "crash":
        print("error: bad gguf", flush=True)
        sys.exit(2)
    class H(http.server.BaseHTTPRequestHandler):
        def log_message(self, *x): pass
        def do_GET(self):
            self.send_response(200); self.end_headers(); self.wfile.write(b"{}")
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            crit = body["questions"]["action"]["criteria"]
            assert isinstance(crit, dict), crit
            labels = list(crit)
            out = {"answers": {"action": {"probabilities": {labels[0]: 0.7, labels[1]: 0.3}}}}
            raw = json.dumps(out).encode()
            self.send_response(200); self.send_header("Content-Length", str(len(raw))); self.end_headers()
            self.wfile.write(raw)
    http.server.HTTPServer(("127.0.0.1", port), H).serve_forever()
""")
QUESTION = {"action": {"type": "choice", "instructions": "x", "criteria": ["yes", "no"]}}


class CpuFixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = self.tmp.name
        self.mode = os.path.join(root, "mode")
        open(self.mode, "w").write("ok")
        self.bin = os.path.join(root, "llama-server")
        with open(self.bin, "w") as f:
            f.write(FAKE_SERVER % {"py": sys.executable, "mode": self.mode})
        os.chmod(self.bin, os.stat(self.bin).st_mode | stat.S_IXUSR)
        arts = []
        for name in ("laya-f16", "laya-q8_0", "laya-q4_k_m"):
            data = name.encode() * 100
            sha = hashlib.sha256(data).hexdigest()
            d = os.path.join(root, "cache", "blobs", sha)
            os.makedirs(d)
            with open(os.path.join(d, name + ".gguf"), "wb") as f:
                f.write(data)
            arts.append({"name": name, "file": name + ".gguf", "size_bytes": len(data), "sha256": sha,
                         "cache_path": f"blobs/{sha}/{name}.gguf"})
        self.arts = arts
        self.manifest = os.path.join(root, "m.json")
        json.dump({"artifacts": arts}, open(self.manifest, "w"))
        self.cache = os.path.join(root, "cache")

    def backend(self, **opts):
        b = lb.CpuBackend(lb.BackendOptions(**opts), manifest_path=self.manifest, cache_root=self.cache,
                          server_bin=self.bin)
        self.addCleanup(b.close)
        return b


class ArtifactSelectionTest(CpuFixture):
    def test_quant_level_picks_the_file(self):
        for quant, name in (("auto", "laya-q8_0"), ("8bit", "laya-q8_0"), ("none", "laya-f16"),
                            ("4bit", "laya-q4_k_m")):
            b = self.backend(quant_level=quant)
            r = b.load()
            self.assertEqual(b.artifact["name"], name)
            self.assertIn(name, r.detail)
            b.close()

    def test_threads_and_ctx_reach_the_server(self):
        b = self.backend(num_threads=3, max_seq_len=256)
        r = b.load()
        self.assertEqual(b.threads_effective, 3)
        self.assertIn("threads=3", r.detail)
        log = b._log_text()
        self.assertIn("-t 3 -tb 3", log)
        self.assertIn("-c 256", log)
        self.assertIn("-ub 256", log)
        b.close()
        b = self.backend(num_threads=1)
        b.load()
        self.assertEqual(b.threads_effective, 1)

    def test_load_reports_time(self):
        r = self.backend().load()
        self.assertGreater(r.load_seconds, 0)


class ChecksumTest(CpuFixture):
    def test_corrupt_file_is_a_load_error(self):
        path = lb.CpuBackend(manifest_path=self.manifest, cache_root=self.cache)._artifact_path(self.arts[1])
        with open(path, "r+b") as f:
            f.seek(3)
            f.write(b"X")
        with self.assertRaises(lb.BackendLoadError) as cm:
            self.backend().load()
        self.assertIn("sha256", str(cm.exception))
        self.assertNotIsInstance(cm.exception, lb.BackendOOMError)

    def test_wrong_size_is_a_load_error(self):
        path = lb.CpuBackend(manifest_path=self.manifest, cache_root=self.cache)._artifact_path(self.arts[1])
        with open(path, "ab") as f:
            f.write(b"x")
        with self.assertRaises(lb.BackendLoadError):
            self.backend().load()

    def test_missing_artifact(self):
        os.remove(lb.CpuBackend(manifest_path=self.manifest, cache_root=self.cache)._artifact_path(self.arts[1]))
        b = self.backend()
        self.assertFalse(b.is_available())
        with self.assertRaises(lb.BackendLoadError):
            b.load()


class TypedErrorTest(CpuFixture):
    def test_server_alloc_failure_is_oom(self):
        open(self.mode, "w").write("oom")
        with self.assertRaises(lb.BackendOOMError):
            self.backend().load()

    def test_other_server_failure_is_load_error_not_oom(self):
        open(self.mode, "w").write("crash")
        with self.assertRaises(lb.BackendLoadError) as cm:
            self.backend().load()
        self.assertNotIsInstance(cm.exception, lb.BackendOOMError)

    def test_not_enough_ram_is_oom_before_spawning(self):
        with mock.patch.object(lb, "_free_ram_bytes", return_value=1):
            b = self.backend()
            self.assertFalse(b.probe().available)
            self.assertIn("RAM", b.probe().reason)
            with self.assertRaises(lb.BackendOOMError):
                b.load()
        self.assertIsNone(b._proc)

    def test_no_binary_is_unavailable(self):
        b = lb.CpuBackend(manifest_path=self.manifest, cache_root=self.cache, server_bin="/nonexistent")
        with mock.patch.dict(os.environ, {"LAYA_LLAMA_SERVER": ""}), \
                mock.patch.object(lb.shutil, "which", return_value=None):
            self.assertFalse(b.is_available())
            with self.assertRaises(lb.BackendUnavailable):
                b.load()

    def test_probe_does_not_start_anything(self):
        b = self.backend()
        self.assertTrue(b.probe().available)
        self.assertIsNone(b._proc)


class ScoreTest(CpuFixture):
    def test_scores_in_chunks_with_object_criteria(self):
        b = self.backend(batch_size=2)
        b.load()
        out = b.score([{"state": "s%d" % i, "question": QUESTION} for i in range(5)])
        self.assertEqual(len(out), 5)
        self.assertEqual(out[0]["answers"]["action"]["probabilities"], {"yes": 0.7, "no": 0.3})
        self.assertGreater(b.peak_rss, 0)

    def test_score_before_load(self):
        with self.assertRaises(lb.BackendRuntimeError):
            self.backend().score([{"state": "s", "question": QUESTION}])

    def test_dead_server_is_runtime_error(self):
        b = self.backend()
        b.load()
        b._proc.kill()
        b._proc.wait()
        with self.assertRaises(lb.BackendRuntimeError):
            b.score([{"state": "s", "question": QUESTION}])


class OrderTest(unittest.TestCase):
    def test_cpu_is_the_last_local_rung(self):
        names = [r.name for r in lb.RUNGS]
        self.assertEqual(names[names.index("cpu") + 1:], ["remote"])
        self.assertIs(lb.backend_for("cpu"), lb.CpuBackend)


if __name__ == "__main__":
    unittest.main()
