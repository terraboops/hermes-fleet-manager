"""Hardware detection: every probe degrades to 'unavailable' with a reason; none raise."""
import json
import subprocess
import unittest
from unittest import mock

import laya_hardware as lh

GIB = 1024 ** 3


def make_env(binaries=None, outputs=None, system="Linux", machine="x86_64",
             cpu_count=8, timeout_on=(), ram=16 * GIB):
    """Build (which, runner, plat) fakes. outputs maps binary name -> (rc, stdout)."""
    binaries = binaries or {}
    outputs = outputs or {}

    def which(name):
        return binaries.get(name)

    def runner(argv, timeout):
        name = argv[0].split("/")[-1]
        if name in timeout_on:
            raise subprocess.TimeoutExpired(argv, timeout)
        if name not in outputs:
            raise FileNotFoundError(name)
        return outputs[name]

    plat = mock.Mock()
    plat.system.return_value = system
    plat.machine.return_value = machine
    plat.processor.return_value = "x86_64"
    return which, runner, plat, cpu_count, ram


def run(**kw):
    which, runner, plat, cpus, ram = make_env(**kw)
    return lh.detect_hardware(which=which, runner=runner, plat=plat,
                              cpu_count=lambda: cpus, ram_bytes=lambda: ram)


class HardwareTests(unittest.TestCase):
    def test_apple_silicon(self):
        info = run(system="Darwin", machine="arm64", cpu_count=10, ram=48 * GIB,
                   binaries={"sysctl": "/usr/sbin/sysctl"},
                   outputs={"sysctl": (0, "Apple M4 Pro\n")})
        self.assertTrue(info.apple_silicon)
        self.assertEqual(info.cpu_model, "Apple M4 Pro")
        self.assertEqual(info.cpu_cores, 10)
        self.assertEqual(info.ram_bytes, 48 * GIB)
        self.assertFalse(info.nvidia.available)
        self.assertIn("nvidia-smi", info.nvidia.reason)

    def test_intel_mac_is_not_apple_silicon(self):
        self.assertFalse(run(system="Darwin", machine="x86_64").apple_silicon)

    def test_nvidia_box(self):
        info = run(binaries={"nvidia-smi": "/usr/bin/nvidia-smi"},
                   outputs={"nvidia-smi": (0, "NVIDIA GeForce RTX 4090, 24564\n")})
        self.assertFalse(info.apple_silicon)
        self.assertTrue(info.nvidia.available)
        self.assertEqual(info.nvidia.gpu_name, "NVIDIA GeForce RTX 4090")
        self.assertEqual(info.nvidia.vram_mib, 24564)
        self.assertFalse(info.rocm.available)

    def test_nvidia_nonzero_exit(self):
        info = run(binaries={"nvidia-smi": "/usr/bin/nvidia-smi"},
                   outputs={"nvidia-smi": (9, "NVIDIA-SMI has failed")})
        self.assertFalse(info.nvidia.available)
        self.assertIn("exit 9", info.nvidia.reason)

    def test_nvidia_garbled_output(self):
        info = run(binaries={"nvidia-smi": "/usr/bin/nvidia-smi"},
                   outputs={"nvidia-smi": (0, "gibberish")})
        self.assertTrue(info.nvidia.available)
        self.assertIsNone(info.nvidia.vram_mib)

    def test_rocm_box(self):
        info = run(binaries={"rocm-smi": "/opt/rocm/bin/rocm-smi",
                             "vulkaninfo": "/usr/bin/vulkaninfo"},
                   outputs={"rocm-smi": (0, "GPU[0] : ok"),
                            "vulkaninfo": (0, "Vulkan Instance Version: 1.3")})
        self.assertTrue(info.rocm.available)
        self.assertTrue(info.vulkan.available)
        self.assertFalse(info.nvidia.available)

    def test_no_gpu(self):
        info = run()
        for probe in (info.nvidia, info.rocm, info.vulkan):
            self.assertFalse(probe.available)
            self.assertTrue(probe.reason)
        self.assertEqual(info.cpu_cores, 8)

    def test_probe_timeout_records_reason(self):
        info = run(binaries={"nvidia-smi": "/usr/bin/nvidia-smi"},
                   timeout_on=("nvidia-smi",))
        self.assertFalse(info.nvidia.available)
        self.assertIn("timed out", info.nvidia.reason)

    def test_runner_raising_anything_never_escapes(self):
        which, _, plat, cpus, ram = make_env(binaries={"rocm-smi": "/x/rocm-smi"})

        def boom(argv, timeout):
            raise RuntimeError("kaboom")
        info = lh.detect_hardware(which=which, runner=boom, plat=plat,
                                  cpu_count=lambda: cpus, ram_bytes=lambda: ram)
        self.assertFalse(info.rocm.available)
        self.assertIn("kaboom", info.rocm.reason)

    def test_ram_and_cpu_probe_failures_do_not_raise(self):
        which, runner, plat, _, _ = make_env()

        def bad():
            raise OSError("no")
        info = lh.detect_hardware(which=which, runner=runner, plat=plat,
                                  cpu_count=bad, ram_bytes=bad)
        self.assertIsNone(info.cpu_cores)
        self.assertIsNone(info.ram_bytes)

    def test_serialisation(self):
        info = run(binaries={"nvidia-smi": "/usr/bin/nvidia-smi"},
                   outputs={"nvidia-smi": (0, "RTX, 8192\n")})
        d = info.to_dict()
        self.assertEqual(d["nvidia"]["vram_mib"], 8192)
        self.assertEqual(json.loads(info.to_json()), d)

    def test_default_subprocess_runner_maps_to_tuple(self):
        cp = subprocess.CompletedProcess(["x"], 0, stdout="hi", stderr="")
        with mock.patch("laya_hardware.subprocess.run", return_value=cp) as m:
            self.assertEqual(lh.default_runner(["x"], 2), (0, "hi"))
            self.assertEqual(m.call_args.kwargs["timeout"], 2)


if __name__ == "__main__":
    unittest.main()
