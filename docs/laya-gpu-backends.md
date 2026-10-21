# Laya GPU backends

Status: implemented, **NOT HARDWARE-VERIFIED** on every rung. The build host had no NVIDIA, AMD or Vulkan device.

Code: `laya_backends.py` (`CudaBackend`, `RocmBackend`, `LegacyGpuBackend`, `VulkanBackend`, `CudaRocmBackend`, all on `GpuLlamaBackend`), `laya_gpu.py` (device probes and support tables), `tests/test_laya_gpu_backends.py`.

## Runtime

The runtime chosen in `laya-runtime-decision.md` is a pinned `llama-server` (llama.cpp b11612, commit 6868a52) on loopback, serving the GGUF artifacts in `laya_artifacts.json`. Every GPU rung starts it with `-ngl 99`. The rungs differ in which prebuilt binary they start and which artifact they load.

The binary is never taken from `PATH`, because a `llama-server` on `PATH` is usually the CPU build and would pass the probe on the wrong rung. Lookup order per rung: the rung's environment variable, then `<cache>/runtimes/<variant>/llama-server`. Fetching those binaries is the shipping work and is not done here.

| Rung | Variant dir | Env var | Prebuilt asset |
|---|---|---|---|
| cuda | `runtimes/cuda` | `LAYA_LLAMA_SERVER_CUDA` | CUDA 13.4 |
| legacy-gpu | `runtimes/cuda-legacy` | `LAYA_LLAMA_SERVER_CUDA_LEGACY` | CUDA 12.8 |
| rocm | `runtimes/rocm` | `LAYA_LLAMA_SERVER_ROCM` | ROCm 10.0 |
| vulkan | `runtimes/vulkan` | `LAYA_LLAMA_SERVER_VULKAN` | Vulkan |

## Minimum supported generations

Taken from the release assets and build files (decision memo section 3, tagged [SRC]). None of it was run.

- NVIDIA modern: compute capability 7.5 and newer (Turing and later). This is the floor of the CUDA 13.4 build.
- NVIDIA legacy: compute capability 5.0 and newer (Maxwell, Pascal, Volta). The CUDA 12.8 build carries PTX for sm_50 and up, so older code is JIT compiled. Fast int8 needs 6.1. Kepler (3.x) and older are unsupported. A 7.5+ card is also accepted by this rung as a second attempt, with the lower-memory artifact, after the modern rung failed.
- AMD ROCm: gfx908, gfx90a, gfx942, gfx950 (CDNA 1 to 4) and gfx1010 and newer (RDNA 1 and newer), the targets of the prebuilt ROCm build. GCN and Vega (gfx8xx, gfx900, gfx906) have no prebuilt and are not supported by any ROCm rung. A source build with `GPU_TARGETS` could cover older ROCm targets; that is out of scope.
- AMD legacy: there is no AMD legacy rung. GCN and Vega cards go to the Vulkan rung.
- Vulkan: any hardware Vulkan device on Linux or Windows. Software renderers (llvmpipe, lavapipe, SwiftShader, device type CPU) do not count. macOS is not served; the CPU rung covers it.

A probe states the failing generation and the supported ones in its reason, and the manager records that reason in `fallback_chain`.

## Vulkan: implemented

The decision memo records that the pinned runtime ships a Vulkan build. The rung is therefore implemented. Whether Laya's graph runs correctly on Mesa drivers (RADV, ANV, NVK) is unknown. The rung stays NOT HARDWARE-VERIFIED until `trial_gguf.py` passes on a real device.

## Artifacts and weak-machine options

| Rung | `auto` | `none` | `8bit` | `4bit` |
|---|---|---|---|---|
| cuda, rocm | F16 | F16 | Q8_0 | Q4_K_M |
| legacy-gpu, vulkan | Q8_0 | F16 | Q8_0 | Q4_K_M |

- Modern rungs use the FP16 artifact. Legacy and Vulkan take the lower-memory Q8_0 by default. No FP32 GGUF is built: the source weights are F16, so FP32 would add memory and no fidelity.
- Q4_K_M fails parity on the 8-state sample (artifacts doc). It is reachable only by `quant_level=4bit`, and the load logs a warning.
- `max_seq_len` sets `-c`, `-b` and `-ub`. `num_threads` sets `-t` and `-tb`. `batch_size` is how many events go to the server per round. All come from `BackendOptions` (env `LAYA_*` or `laya.options`).
- With several qualifying GPUs the one with the most free VRAM is used, pinned through `CUDA_VISIBLE_DEVICES`, `HIP_VISIBLE_DEVICES` or `GGML_VK_VISIBLE_DEVICES` for the child only.

## Probe and load

- `probe()` runs one vendor tool with a 5 s timeout (`nvidia-smi` with `compute_cap`, `rocminfo`, `vulkaninfo --summary`) and stats the binary and artifact. It loads no weights, hashes nothing and starts no server. Measured on a box with none of the tools: under 1 ms.
- `load()` takes a fresh VRAM reading, checks size and sha256 against the manifest, starts the server, waits for `/health`, and returns `LoadResult(load_seconds, detail)`. The manager logs it and exposes it as `load_ms`.

## Error mapping

- Free VRAM below the artifact size (x1.25 plus 64 MiB) raises `BackendOOMError` before any process starts. VRAM is not read for ROCm and Vulkan, so those rely on the next rule.
- A `llama-server` that exits while loading, with `out of memory`, `cudaMalloc failed`, `hipMalloc failed`, `ErrorOutOfDeviceMemory`, `failed to allocate ... buffer` or similar in its log, raises `BackendOOMError`. Any other exit raises `BackendLoadError`.
- No qualifying device, or no runtime binary, raises `BackendUnavailable`. All three mean "try the next rung".
- An out-of-memory error during `score()` is a `BackendRuntimeError`; the manager demotes the rung and falls down the ladder.

## Verification status

| Rung | Unit tests (faked tools and server) | Real hardware |
|---|---|---|
| cuda (modern) | yes | NOT HARDWARE-VERIFIED |
| rocm | yes | NOT HARDWARE-VERIFIED |
| legacy-gpu | yes | NOT HARDWARE-VERIFIED |
| vulkan | yes | NOT HARDWARE-VERIFIED |

To close this out, run `trial_gguf.py` against the matching `llama-server` on: a Pascal or Maxwell card (legacy), a Turing or newer card (cuda), an RDNA card (rocm), and RADV, ANV and NVK (vulkan). Until then the manifest keeps these rungs at `verified: false`.
