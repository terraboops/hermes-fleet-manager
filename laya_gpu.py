"""GPU device probes and support tables for the Laya GPU rungs.

Pure functions plus thin subprocess wrappers. Nothing here loads weights or imports a GPU library: a probe
runs one short vendor tool (nvidia-smi, rocminfo, vulkaninfo) with a timeout. The runner is injectable so
tests never touch real hardware.

Supported generations (the runtime is the prebuilt llama-server release pinned in laya_artifacts.json,
llama.cpp b11612; every number below is from the release assets and build files, NOT from a run):

    NVIDIA modern   compute capability >= 7.5 (Turing and newer), the CUDA 13.4 build
    NVIDIA legacy   compute capability 5.0 .. 7.4 (Maxwell, Pascal, Volta), the CUDA 12.8 build, which
                    carries PTX for sm_50 and up. Below 5.0 (Kepler and older): unsupported. A card at
                    7.5 or above is also accepted by the legacy rung, as a second attempt with the
                    lower-memory artifact after the modern rung failed.
    AMD ROCm        gfx908, gfx90a, gfx942, gfx950 (CDNA 1 to 4) and gfx1010 and newer (RDNA 1 and newer),
                    the prebuilt ROCm build. GCN and Vega (gfx8xx, gfx900, gfx906) have no prebuilt: they
                    are NOT supported by any ROCm rung and fall to Vulkan. There is no AMD legacy rung.
    Vulkan          any real (non-software) Vulkan device on Linux or Windows. Laya's graph on Mesa
                    drivers (RADV, ANV, NVK) is unverified. macOS is not served (CPU covers it).
"""
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from typing import Callable, List, Optional, Tuple

PROBE_TIMEOUT_S = 5

CUDA_MODERN_MIN_CC = (7, 5)
CUDA_LEGACY_MIN_CC = (5, 0)
ROCM_EXPLICIT_GFX = ("gfx908", "gfx90a", "gfx942", "gfx950")
ROCM_MIN_RDNA_GFX = 1010
SOFTWARE_VULKAN_MARKERS = ("llvmpipe", "lavapipe", "swiftshader", "software rasterizer")

# Messages the runtime prints when a device allocation fails. Lower case; matched as substrings.
GPU_OOM_MARKERS = (
    "out of memory", "cudamalloc failed", "cuda error: out of memory", "cudaerrormemoryallocation",
    "hipmalloc failed", "hip error: out of memory", "hiperrormemoryallocation",
    "erroroutofdevicememory", "erroroutofhostmemory", "vk::outofdevicememoryerror",
    "failed to allocate cuda", "failed to allocate rocm", "failed to allocate vulkan",
    "unable to allocate cuda", "unable to allocate rocm", "unable to allocate vulkan",
    "failed to allocate buffer", "unable to allocate", "cublas_status_alloc_failed",
)


@dataclass
class GpuDevice:
    index: int
    name: str
    compute_cap: Optional[Tuple[int, int]] = None   # NVIDIA
    gfx: Optional[str] = None                       # AMD
    vram_total_mib: Optional[int] = None
    vram_free_mib: Optional[int] = None


@dataclass
class GpuScan:
    devices: List[GpuDevice] = field(default_factory=list)
    reason: str = ""                                # why there are none, when there are none


def _default_runner(argv, timeout):
    cp = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
    return cp.returncode, cp.stdout or ""


def _run(tool, argv, which, runner):
    """(output, reason). Output is None when the tool is absent or fails."""
    path = which(tool)
    if not path:
        return None, f"{tool} not found on PATH"
    try:
        rc, out = runner([path] + list(argv), PROBE_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        return None, f"{tool} timed out after {PROBE_TIMEOUT_S}s"
    except Exception as e:                           # OSError and friends: a probe never raises
        return None, f"{tool} probe error: {type(e).__name__}: {e}"
    if rc != 0:
        return None, f"{tool} failed: exit {rc}"
    return out, ""


def parse_compute_cap(text):
    m = re.fullmatch(r"\s*(\d+)\.(\d+)\s*", str(text))
    return (int(m.group(1)), int(m.group(2))) if m else None


def scan_nvidia(which=shutil.which, runner=_default_runner) -> GpuScan:
    """One nvidia-smi call: index, name, compute capability, VRAM. No driver call beyond that."""
    out, reason = _run("nvidia-smi", ["--query-gpu=index,name,compute_cap,memory.total,memory.free",
                                      "--format=csv,noheader,nounits"], which, runner)
    if out is None:
        return GpuScan(reason=reason)
    devices = []
    for line in out.strip().splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 5:
            continue
        try:
            devices.append(GpuDevice(index=int(parts[0]), name=parts[1], compute_cap=parse_compute_cap(parts[2]),
                                     vram_total_mib=int(parts[3]), vram_free_mib=int(parts[4])))
        except ValueError:
            continue
    if not devices:
        return GpuScan(reason="nvidia-smi listed no usable device")
    return GpuScan(devices)


def scan_rocm(which=shutil.which, runner=_default_runner) -> GpuScan:
    """rocminfo lists one agent per CPU and GPU; GPU agents are the ones named gfxNNNN."""
    out, reason = _run("rocminfo", [], which, runner)
    if out is None:
        return GpuScan(reason=reason)
    devices, marketing = [], None
    for line in out.splitlines():
        key, _, val = line.partition(":")
        key, val = key.strip(), val.strip()
        if key == "Marketing Name":
            marketing = val
        elif key == "Name" and re.fullmatch(r"gfx[0-9a-f]+", val):
            devices.append(GpuDevice(index=len(devices), name=marketing or val, gfx=val))
    if not devices:
        return GpuScan(reason="rocminfo listed no GPU agent")
    return GpuScan(devices)


def scan_vulkan(which=shutil.which, runner=_default_runner, plat_system=None) -> GpuScan:
    """vulkaninfo --summary. Software devices (llvmpipe, lavapipe, SwiftShader, CPU type) are dropped."""
    if plat_system is None:
        import platform
        plat_system = platform.system()
    if plat_system == "Darwin":
        return GpuScan(reason="no Vulkan llama-server for macOS (the CPU rung covers it)")
    out, reason = _run("vulkaninfo", ["--summary"], which, runner)
    if out is None:
        return GpuScan(reason=reason)
    devices, name, dtype = [], None, None

    def flush():
        if name is None:
            return
        soft = (dtype and "CPU" in dtype.upper()) or any(m in name.lower() for m in SOFTWARE_VULKAN_MARKERS)
        if not soft:
            devices.append(GpuDevice(index=len(devices), name=name))

    seen = 0
    for line in out.splitlines():
        key, _, val = line.partition("=")
        key, val = key.strip(), val.strip()
        if key == "deviceName":
            flush()
            name, dtype = val, None
            seen += 1
        elif key == "deviceType":
            dtype = val
    flush()
    if not devices:
        return GpuScan(reason="no hardware Vulkan device" + (" (software renderer only)" if seen else ""))
    return GpuScan(devices)


def rocm_gfx_supported(gfx):
    """True for the gfx ids the prebuilt ROCm llama-server targets. GCN and Vega are not on the list."""
    gfx = str(gfx).lower()
    if gfx in ROCM_EXPLICIT_GFX:
        return True
    m = re.fullmatch(r"gfx(\d{4})[0-9a-f]?", gfx)
    return bool(m) and int(m.group(1)) >= ROCM_MIN_RDNA_GFX


def cc_str(cc):
    return f"{cc[0]}.{cc[1]}" if cc else "unknown"
