"""GPU rungs of the Laya ladder: probes, supported generations, OOM mapping and fallback.

No real hardware: device tools are faked through the injectable runner, and llama-server is a tiny script
that records its argv and either serves /health or dies with the message a real runtime prints.
"""
import hashlib
import json
import os
import stat
import sys
import textwrap

import tempfile
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import laya_backend_manager as mgr  # noqa: E402
import laya_backends as lb  # noqa: E402
import laya_gpu  # noqa: E402
from laya_hardware import GpuProbe, HardwareInfo  # noqa: E402

# Hardware report that lets the manager try every GPU rung; the rungs' own probes then decide.
GPU_BOX = HardwareInfo(system="Linux", machine="x86_64", nvidia=GpuProbe(True), rocm=GpuProbe(True),
                       vulkan=GpuProbe(True))

NVIDIA_HEADER = "index, name, compute_capability"


def nvidia_runner(rows, calls=None):
    """rows: (index, name, 'M.m', total_mib, free_mib). Answers nvidia-smi only."""
    def run(argv, timeout):
        if calls is not None:
            calls.append(argv)
        assert os.path.basename(argv[0]) == "nvidia-smi"
        return 0, "\n".join(f"{i}, {n}, {cc}, {t}, {f}" for i, n, cc, t, f in rows) + "\n"
    return run


def which_only(*tools):
    return lambda t: f"/usr/bin/{t}" if t in tools else None


PASCAL = [(0, "NVIDIA GTX 1060", "6.1", 6144, 6000)]
TURING = [(0, "NVIDIA RTX 2080", "7.5", 8192, 8000)]
KEPLER = [(0, "NVIDIA GTX 780", "3.5", 3072, 3000)]
AMPERE = [(0, "NVIDIA RTX 3090", "8.6", 24576, 24000)]


# ---- fixtures: a fake llama-server and a cache of tiny real artifacts ------------------------

FAKE_SERVER = textwrap.dedent('''\
    #!%s
    import http.server, json, os, sys, time
    out = os.environ.get("LAYA_FAKE_OUT")
    argv = sys.argv[1:]
    if out:
        json.dump({"argv": argv, "visible": {k: os.environ.get(k) for k in
                   ("CUDA_VISIBLE_DEVICES", "HIP_VISIBLE_DEVICES", "GGML_VK_VISIBLE_DEVICES")}}, open(out, "w"))
    mode = os.environ.get("LAYA_FAKE_MODE", "ok")
    msgs = {"oom_cuda": "ggml_cuda_init: found 1 CUDA devices\\nCUDA error: out of memory\\n",
            "oom_hip": "hipMalloc failed: out of memory\\n",
            "oom_vk": "vk::Device::allocateMemory: ErrorOutOfDeviceMemory\\n",
            "crash": "segmentation fault in kernel\\n"}
    if mode in msgs:
        sys.stderr.write(msgs[mode]); sys.stderr.flush()
        sys.exit(1)
    port = int(argv[argv.index("--port") + 1])
    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200); self.end_headers(); self.wfile.write(b"{}")
        def log_message(self, *a): pass
    http.server.HTTPServer(("127.0.0.1", port), H).serve_forever()
''' % sys.executable)


def make(cls, cache, rows=None, options=None, tool="nvidia-smi", runner=None, binary=True):
    runner = runner or nvidia_runner(rows or [])
    return cls(options or lb.BackendOptions(), manifest_path=cache["manifest"], cache_root=cache["root"],
               server_bin=cache["server"] if binary else None, which=which_only(tool), runner=runner)


class GpuCase(unittest.TestCase):
    """Builds a cache with tiny real artifacts and a fake llama-server for each test."""

    def setUp(self):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        tmp = td.name
        root = os.path.join(tmp, "cache")
        arts = []
        for name, size in (("laya-f16", 4000), ("laya-q8_0", 2000), ("laya-q4_k_m", 1000)):
            data = (name.encode() * size)[:size]
            sha = hashlib.sha256(data).hexdigest()
            rel = f"blobs/{sha}/{name}.gguf"
            os.makedirs(os.path.dirname(os.path.join(root, rel)))
            with open(os.path.join(root, rel), "wb") as f:
                f.write(data)
            arts.append({"name": name, "file": f"{name}.gguf", "size_bytes": size, "sha256": sha, "cache_path": rel})
        manifest = os.path.join(tmp, "laya_artifacts.json")
        with open(manifest, "w") as f:
            json.dump({"schema": 1, "artifacts": arts}, f)
        server = os.path.join(tmp, "llama-server")
        with open(server, "w") as f:
            f.write(FAKE_SERVER)
        os.chmod(server, 0o755)
        self.tmp = tmp
        self.cache = {"manifest": manifest, "root": root, "server": server, "out": os.path.join(tmp, "argv.json")}
        env = {k: v for k, v in os.environ.items() if not k.startswith("LAYA_")}
        env["LAYA_FAKE_OUT"] = self.cache["out"]
        patcher = mock.patch.dict(os.environ, env, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        for p in (mock.patch.object(lb, "_free_ram_bytes", lambda: 1 << 40),
                  mock.patch.object(lb.LlamaServerBackend, "LABEL_WAIT_SECONDS", 20)):
            p.start()
            self.addCleanup(p.stop)

    def setenv(self, k, v):
        os.environ[k] = v

    def make(self, cls, rows=None, options=None, tool="nvidia-smi", runner=None, binary=True):
        return make(cls, self.cache, rows, options, tool, runner, binary)

    def loaded(self, b):
        try:
            return b.load()
        finally:
            b.close()

    def recorded(self):
        with open(self.cache["out"]) as f:
            return json.load(f)


# ---- registration and order -------------------------------------------------------------------------

class RegistrationTest(unittest.TestCase):
    def test_rungs_registered_in_chain_order(self):
        self.assertEqual([r.name for r in lb.RUNGS], ["mlx", "cuda/rocm", "legacy-gpu", "vulkan", "cpu", "remote"])
        for name in ("cuda", "rocm", "legacy", "vulkan"):
            self.assertFalse(issubclass(lb.backend_for(name), lb._Unimplemented), name)
        self.assertFalse(issubclass(lb.CudaRocmBackend, lb._Unimplemented))


# ---- supported generations --------------------------------------------------------------------------

class GenerationTest(GpuCase):
    def test_cuda_modern_needs_cc_75(self):
        self.assertTrue(self.make(lb.CudaBackend, TURING).probe().available)
        self.assertTrue(self.make(lb.CudaBackend, AMPERE).probe().available)
        p = self.make(lb.CudaBackend, PASCAL).probe()
        self.assertFalse(p.available)
        self.assertIn("6.1", p.reason)
        self.assertIn("7.5", p.reason)

    def test_legacy_covers_maxwell_to_volta_and_rejects_kepler(self):
        for row in (PASCAL, [(0, "GTX 970", "5.2", 4096, 4000)], [(0, "GTX 750", "5.0", 2048, 2000)]):
            self.assertTrue(self.make(lb.LegacyGpuBackend, row).probe().available, row)
        p = self.make(lb.LegacyGpuBackend, KEPLER).probe()
        self.assertFalse(p.available)
        self.assertIn("3.5", p.reason)
        self.assertIn("5.0", p.reason)
        self.assertIn("Vulkan", p.reason)          # the statement points AMD GCN/Vega at Vulkan

    def test_unreadable_compute_cap_is_unavailable_not_a_crash(self):
        p = self.make(lb.CudaBackend, [(0, "NVIDIA OLD", "[N/A]", 4096, 4000)]).probe()
        self.assertFalse(p.available)
        self.assertIn("unreadable", p.reason)

    def test_rocm_gfx_table(self):
        for gfx in ("gfx908", "gfx90a", "gfx942", "gfx950", "gfx1010", "gfx1030", "gfx1101", "gfx1201"):
            self.assertTrue(laya_gpu.rocm_gfx_supported(gfx), gfx)
        for gfx in ("gfx900", "gfx906", "gfx803", "gfx902"):
            self.assertFalse(laya_gpu.rocm_gfx_supported(gfx), gfx)

    def test_rocm_probe_accepts_rdna_and_rejects_vega(self):
        self.assertTrue(self.make(lb.RocmBackend, tool="rocminfo", runner=rocm_runner("gfx1030")).probe().available)
        p = self.make(lb.RocmBackend, tool="rocminfo", runner=rocm_runner("gfx900", "Vega 56")).probe()
        self.assertFalse(p.available)
        self.assertIn("gfx900", p.reason)
        self.assertIn("Vulkan", p.reason)

    def test_vulkan_skips_software_devices(self):
        scan = laya_gpu.scan_vulkan(which_only("vulkaninfo"), vk_runner(VULKANINFO), plat_system="Linux")
        self.assertEqual([d.name for d in scan.devices], ["AMD Radeon RX 580 Series (RADV POLARIS10)"])
        only_soft = VULKANINFO.split("GPU1:")[0]
        with mock.patch("platform.system", lambda: "Linux"):
            p = self.make(lb.VulkanBackend, tool="vulkaninfo", runner=vk_runner(only_soft)).probe()
        self.assertFalse(p.available)
        self.assertIn("software", p.reason)

    def test_vulkan_not_offered_on_macos(self):
        scan = laya_gpu.scan_vulkan(which_only("vulkaninfo"), vk_runner(VULKANINFO), plat_system="Darwin")
        self.assertFalse(scan.devices)
        self.assertIn("macOS", scan.reason)


ROCMINFO = """\
ROCk module is loaded
Agent 1
  Name:                    AMD Ryzen 9 5950X 16-Core Processor
  Marketing Name:          AMD Ryzen 9 5950X 16-Core Processor
Agent 2
  Name:                    {gfx}
  Marketing Name:          {name}
"""

VULKANINFO = """\
Devices:
========
GPU0:
    deviceType         = PHYSICAL_DEVICE_TYPE_CPU
    deviceName         = llvmpipe (LLVM 17.0.6, 256 bits)
GPU1:
    deviceType         = PHYSICAL_DEVICE_TYPE_DISCRETE_GPU
    deviceName         = AMD Radeon RX 580 Series (RADV POLARIS10)
"""


def rocm_runner(gfx, name="AMD Radeon"):
    def run(argv, timeout):
        assert os.path.basename(argv[0]) == "rocminfo"
        return 0, ROCMINFO.format(gfx=gfx, name=name)
    return run


def vk_runner(text):
    return lambda argv, timeout: (0, text)


# ---- probes are cheap: no weights, no server, one tool call ---------------------------------------

class ProbeTest(GpuCase):
    def test_probe_loads_nothing(self):
        calls = []

        def boom(*a, **k):
            raise AssertionError("probe must not hash or start anything")
        b = self.make(lb.CudaBackend, runner=nvidia_runner(TURING, calls))
        with mock.patch.object(lb, "_file_sha256", boom), mock.patch.object(lb.subprocess, "Popen", boom):
            self.assertTrue(b.probe().available)
            self.assertTrue(b.is_available())
        self.assertEqual(len(calls), 2)            # one vendor call per probe
        self.assertTrue(all(c[0].endswith("nvidia-smi") for c in calls))

    def test_probe_reasons_when_pieces_are_missing(self):
        self.assertIn("nvidia-smi not found", self.make(lb.CudaBackend, tool="other").probe().reason)
        p = self.make(lb.CudaBackend, TURING, binary=False).probe()
        self.assertFalse(p.available)
        self.assertIn("llama-server", p.reason)

    def test_path_llama_server_is_not_used_for_gpu(self):
        d = os.path.join(self.tmp, "bin")
        os.makedirs(d)
        exe = os.path.join(d, "llama-server")
        with open(exe, "w") as f:
            f.write("#!/bin/sh\n")
        os.chmod(exe, 0o755)
        with mock.patch.dict(os.environ, {"PATH": d + os.pathsep + os.environ.get("PATH", "")}):
            self.assertFalse(self.make(lb.CudaBackend, TURING, binary=False).probe().available)

    def test_runtime_found_via_env_var(self):
        self.setenv("LAYA_LLAMA_SERVER_CUDA", self.cache["server"])
        self.assertTrue(self.make(lb.CudaBackend, TURING, binary=False).probe().available)


# ---- load: artifact, options, device pinning, timing ----------------------------------------------

class LoadTest(GpuCase):
    def test_modern_loads_fp16_offloads_and_reports_load_time(self):
        res = self.loaded(self.make(lb.CudaBackend, TURING))
        rec = self.recorded()
        self.assertGreater(res.load_seconds, 0)
        self.assertIn("laya-f16", res.detail)
        self.assertIn("RTX 2080", res.detail)
        argv = rec["argv"]
        self.assertEqual(argv[argv.index("-ngl") + 1], "99")
        self.assertIn("laya-f16.gguf", argv[argv.index("-m") + 1])
        self.assertEqual(rec["visible"]["CUDA_VISIBLE_DEVICES"], "0")

    def test_legacy_defaults_to_lower_memory_artifact(self):
        self.assertIn("laya-q8_0", self.loaded(self.make(lb.LegacyGpuBackend, PASCAL)).detail)

    def test_weak_machine_options_reach_the_server(self):
        opts = lb.BackendOptions(quant_level="8bit", batch_size=2, num_threads=3, max_seq_len=256)
        res = self.loaded(self.make(lb.CudaBackend, TURING, options=opts))
        argv = self.recorded()["argv"]
        self.assertIn("laya-q8_0", res.detail)
        self.assertIn("batch=2", res.detail)
        self.assertEqual(argv[argv.index("-c") + 1], "256")
        self.assertEqual(argv[argv.index("-ub") + 1], "256")
        self.assertEqual(argv[argv.index("-t") + 1], "3")

    def test_picks_the_qualifying_gpu_with_most_free_vram(self):
        rows = [(0, "GTX 1060", "6.1", 6144, 6000), (1, "RTX 2080", "7.5", 8192, 4000),
                (2, "RTX 3090", "8.6", 24576, 20000)]
        self.loaded(self.make(lb.CudaBackend, rows))
        self.assertEqual(self.recorded()["visible"]["CUDA_VISIBLE_DEVICES"], "2")

    def test_corrupt_artifact_is_a_load_error_not_oom(self):
        blobs = os.path.join(self.cache["root"], "blobs")
        for d in os.listdir(blobs):
            for g in os.listdir(os.path.join(blobs, d)):
                if g.startswith("laya-f16"):
                    with open(os.path.join(blobs, d, g), "r+b") as fh:
                        fh.write(b"x")
        with self.assertRaises(lb.BackendLoadError) as cm:
            self.make(lb.CudaBackend, TURING).load()
        self.assertNotIsInstance(cm.exception, lb.BackendOOMError)


# ---- OOM maps to the typed error ------------------------------------------------------------------

class OomTest(GpuCase):
    def test_vram_shortfall_is_typed_oom_before_any_process_starts(self):
        b = self.make(lb.CudaBackend, [(0, "RTX 2080", "7.5", 8192, 0)])      # 0 MiB free
        self.assertTrue(b.probe().available)       # a full card is not 'unavailable'; load reports OOM
        with mock.patch.object(lb.subprocess, "Popen", side_effect=AssertionError("must not spawn")):
            with self.assertRaises(lb.BackendOOMError) as cm:
                b.load()
        self.assertIn("VRAM", str(cm.exception))

    def test_runtime_oom_message_maps_to_typed_error(self):
        cases = [("oom_cuda", lb.CudaBackend, TURING, "nvidia-smi", None),
                 ("oom_cuda", lb.LegacyGpuBackend, PASCAL, "nvidia-smi", None),
                 ("oom_hip", lb.RocmBackend, None, "rocminfo", rocm_runner("gfx1030")),
                 ("oom_vk", lb.VulkanBackend, None, "vulkaninfo", vk_runner(VULKANINFO))]
        for mode, cls, rows, tool, runner in cases:
            with self.subTest(mode=mode, cls=cls.__name__), \
                    mock.patch.dict(os.environ, {"LAYA_FAKE_MODE": mode}), \
                    mock.patch("platform.system", lambda: "Linux"):
                b = self.make(cls, rows, tool=tool, runner=runner)
                with self.assertRaises(lb.BackendOOMError):
                    b.load()
                self.assertIsNone(b._proc)         # no orphan server left behind

    def test_other_crash_is_load_error_not_oom(self):
        self.setenv("LAYA_FAKE_MODE", "crash")
        with self.assertRaises(lb.BackendLoadError) as cm:
            self.make(lb.CudaBackend, TURING).load()
        self.assertNotIsInstance(cm.exception, lb.BackendOOMError)

    def test_load_on_a_box_without_a_qualifying_device_is_unavailable(self):
        with self.assertRaises(lb.BackendUnavailable):
            self.make(lb.CudaBackend, KEPLER).load()


# ---- the cuda/rocm rung ---------------------------------------------------------------------------

def both_runner(nv_rows, gfx):
    def run(argv, timeout):
        if os.path.basename(argv[0]) == "nvidia-smi":
            return (9, "") if nv_rows is None else nvidia_runner(nv_rows)(argv, timeout)
        return rocm_runner(gfx)(argv, timeout)
    return run


class CudaRocmTest(GpuCase):
    def rung(self, nv_rows, gfx):
        return lb.CudaRocmBackend(lb.BackendOptions(), manifest_path=self.cache["manifest"],
                                  cache_root=self.cache["root"], server_bin=self.cache["server"],
                                  which=which_only("nvidia-smi", "rocminfo"), runner=both_runner(nv_rows, gfx))

    def test_prefers_cuda_then_rocm_and_reports_both_reasons(self):
        self.assertIn("cuda", self.loaded(self.rung(TURING, "gfx1030")).detail)
        self.assertTrue(self.loaded(self.rung(None, "gfx1030")).detail.startswith("rocm"))
        p = self.rung(PASCAL, "gfx900").probe()
        self.assertFalse(p.available)
        self.assertIn("cuda:", p.reason)
        self.assertIn("rocm:", p.reason)


# ---- fallback through the real manager --------------------------------------------------------------

def scan_of(rows):
    return laya_gpu.GpuScan([laya_gpu.GpuDevice(i, n, compute_cap=laya_gpu.parse_compute_cap(cc),
                                                vram_total_mib=t, vram_free_mib=f) for i, n, cc, t, f in rows])


class Cpu(lb.CpuBackend):
    def load(self):
        return lb.LoadResult(0.01, "fake cpu")

    def probe(self):
        return lb.Probe(True)


class ChainTest(GpuCase):
    def rung(self, cls, scan, server=None):
        """A rung class pinned to a fixed device scan and a server script, built as the manager builds it."""
        cache = self.cache

        class Pinned(cls):
            def __init__(self, options=None):
                super().__init__(options, manifest_path=cache["manifest"], cache_root=cache["root"],
                                 server_bin=server or cache["server"])

            def _scan(self):
                return scan
        Pinned.name = cls.name
        return Pinned

    def modern(self, scan, server=None):
        outer = self.rung(lb.CudaBackend, scan, server)

        class Modern(lb.CudaRocmBackend):
            def __init__(self, options=None):
                super().__init__(options)
                self.DELEGATES = (outer,)
        return Modern

    def run_chain(self, rungs):
        m = mgr.BackendManager(env={"LAYA_BACKEND": "auto"}, hw=GPU_BOX, rungs=rungs, options=lb.BackendOptions())
        st = m.start()
        self.addCleanup(lambda: m._active is not None and getattr(m._active, "close", lambda: None)())
        return st

    def test_pascal_box_skips_modern_and_lands_on_legacy(self):
        scan = scan_of(PASCAL)
        st = self.run_chain([self.modern(scan), self.rung(lb.LegacyGpuBackend, scan), Cpu])
        self.assertEqual(st.backend, "legacy-gpu")
        self.assertIsNotNone(st.load_ms)
        first = st.fallback_chain[0]
        self.assertEqual((first["rung"], first["status"]), ("cuda/rocm", "skipped"))
        self.assertIn("6.1", first["reason"])

    def test_modern_oom_falls_through_to_legacy_then_cpu(self):
        scan = scan_of(AMPERE)
        oom = os.path.join(self.tmp, "oom-server")
        with open(oom, "w") as f:
            f.write(FAKE_SERVER.replace('os.environ.get("LAYA_FAKE_MODE", "ok")', '"oom_cuda"'))
        os.chmod(oom, 0o755)
        modern = self.modern(scan, server=oom)
        st = self.run_chain([modern, self.rung(lb.LegacyGpuBackend, scan), Cpu])
        self.assertEqual([(c["rung"], c["status"]) for c in st.fallback_chain],
                         [("cuda/rocm", "failed"), ("legacy-gpu", "loaded")])
        self.assertTrue(st.fallback_chain[0]["reason"].startswith("out of memory: BackendOOMError"))
        st = self.run_chain([modern, modern, Cpu])
        self.assertEqual(st.backend, "cpu")
        self.assertEqual([c["status"] for c in st.fallback_chain], ["failed", "failed", "loaded"])

    def test_no_gpu_at_all_reaches_cpu_without_loading_gpu_rungs(self):
        empty = laya_gpu.GpuScan(reason="nvidia-smi not found on PATH")
        st = self.run_chain([self.rung(lb.LegacyGpuBackend, empty), self.rung(lb.VulkanBackend, empty), Cpu])
        self.assertEqual(st.backend, "cpu")
        self.assertEqual([c["status"] for c in st.fallback_chain], ["skipped", "skipped", "loaded"])


if __name__ == "__main__":
    unittest.main()
