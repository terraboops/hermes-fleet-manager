"""Standalone hardware detection: Apple Silicon, CUDA, ROCm, Vulkan, CPU, RAM.

Every probe degrades to available=False with a reason string. Nothing raises.
Probes take injectable `which`, `runner`, `plat`, `cpu_count` and `ram_bytes`
so tests never touch the real machine.
"""
from __future__ import annotations

import json
import os
import platform
import shutil
import subprocess
from dataclasses import asdict, dataclass, field
from typing import Callable, Optional, Tuple

PROBE_TIMEOUT_S = 5


@dataclass
class GpuProbe:
    available: bool = False
    reason: str = ""
    path: Optional[str] = None
    gpu_name: Optional[str] = None
    vram_mib: Optional[int] = None


@dataclass
class HardwareInfo:
    apple_silicon: bool = False
    system: str = ""
    machine: str = ""
    cpu_model: Optional[str] = None
    cpu_cores: Optional[int] = None
    ram_bytes: Optional[int] = None
    nvidia: GpuProbe = field(default_factory=GpuProbe)
    rocm: GpuProbe = field(default_factory=GpuProbe)
    vulkan: GpuProbe = field(default_factory=GpuProbe)

    def to_dict(self) -> dict:
        return asdict(self)

    def to_json(self, **kw) -> str:
        return json.dumps(self.to_dict(), **kw)


def default_runner(argv, timeout) -> Tuple[int, str]:
    cp = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
    return cp.returncode, cp.stdout or ""


def _safe(fn, default=None):
    try:
        return fn()
    except Exception:
        return default


def _probe_binary(binary, argv, which, runner, parse=None) -> GpuProbe:
    try:
        path = which(binary)
        if not path:
            return GpuProbe(reason=f"{binary} not found on PATH")
        try:
            rc, out = runner([path] + list(argv), PROBE_TIMEOUT_S)
        except subprocess.TimeoutExpired:
            return GpuProbe(path=path, reason=f"{binary} timed out after {PROBE_TIMEOUT_S}s")
        if rc != 0:
            return GpuProbe(path=path, reason=f"{binary} failed: exit {rc}")
        probe = GpuProbe(available=True, path=path)
        if parse:
            _safe(lambda: parse(probe, out))
        return probe
    except Exception as e:  # probes never raise
        return GpuProbe(reason=f"{binary} probe error: {e}")


def _parse_nvidia(probe: GpuProbe, out: str) -> None:
    line = out.strip().splitlines()[0] if out.strip() else ""
    name, _, mem = line.rpartition(",")
    if not name:
        return
    probe.gpu_name = name.strip()
    probe.vram_mib = int(mem.strip().split()[0])


def _cpu_model(system, which, runner) -> Optional[str]:
    if system == "Darwin":
        path = which("sysctl")
        if path:
            rc, out = runner([path, "-n", "machdep.cpu.brand_string"], PROBE_TIMEOUT_S)
            if rc == 0 and out.strip():
                return out.strip()
    elif system == "Linux":
        with open("/proc/cpuinfo") as f:
            for line in f:
                if line.lower().startswith("model name"):
                    return line.split(":", 1)[1].strip()
    return None


def _ram_bytes() -> Optional[int]:
    return os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES")


def detect_hardware(which: Callable = shutil.which, runner: Callable = default_runner,
                    plat=platform, cpu_count: Callable = os.cpu_count,
                    ram_bytes: Callable = _ram_bytes) -> HardwareInfo:
    system = _safe(plat.system, "") or ""
    machine = _safe(plat.machine, "") or ""
    info = HardwareInfo(apple_silicon=(system == "Darwin" and machine == "arm64"),
                        system=system, machine=machine)
    info.cpu_cores = _safe(cpu_count)
    info.ram_bytes = _safe(ram_bytes)
    info.cpu_model = (_safe(lambda: _cpu_model(system, which, runner))
                      or _safe(plat.processor) or None)
    info.nvidia = _probe_binary(
        "nvidia-smi", ["--query-gpu=name,memory.total", "--format=csv,noheader,nounits"],
        which, runner, _parse_nvidia)
    info.rocm = _probe_binary("rocm-smi", [], which, runner)
    info.vulkan = _probe_binary("vulkaninfo", ["--summary"], which, runner)
    return info


if __name__ == "__main__":
    print(detect_hardware().to_json(indent=2))
