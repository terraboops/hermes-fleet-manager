# Laya rung latency and memory (2026-10-21)

Commit: `2357e4cb349aad80d84f5f467267be74dd7a13d2 (+uncommitted changes)`
Hardware: Apple M4 Pro, 48.0 GiB RAM, 14 physical / 14 logical cores, macOS 26.2
Workload: 307 recorded parity cases (tests/fixtures/laya_parity/v1-mlxref, manifest sha256 `2cde16706ea1`), 10 warmup calls, 307 measured calls per combination, one event per call.
Reproduce: `~/.hermes/venvs/laya-mlx/bin/python scripts/bench_rungs.py`

Peak memory is the highest RSS sampled after each call (load / steady in notes).

| rung | quant | peak memory | p50 ms | p95 ms | notes |
|---|---|---|---|---|---|
| mlx | n/a | 943 MiB | 29.565 | 36.256 | load 932 MiB, steady 943 MiB, load 0.5s; quant not selectable on this rung (fixed checkpoint); RSS excludes Metal buffers |
| cuda | auto | - | - | - | **UNAVAILABLE**: nvidia-smi not found on PATH; supported: NVIDIA compute capability 7.5+ (Turing and newer) |
| cuda | none | - | - | - | **UNAVAILABLE**: nvidia-smi not found on PATH; supported: NVIDIA compute capability 7.5+ (Turing and newer) |
| cuda | 8bit | - | - | - | **UNAVAILABLE**: nvidia-smi not found on PATH; supported: NVIDIA compute capability 7.5+ (Turing and newer) |
| cuda | 4bit | - | - | - | **UNAVAILABLE**: nvidia-smi not found on PATH; supported: NVIDIA compute capability 7.5+ (Turing and newer) |
| rocm | auto | - | - | - | **UNAVAILABLE**: rocminfo not found on PATH; supported: AMD gfx908, gfx90a, gfx942, gfx950 and gfx1010+ (RDNA 1 and newer); GCN/Vega use Vulkan |
| rocm | none | - | - | - | **UNAVAILABLE**: rocminfo not found on PATH; supported: AMD gfx908, gfx90a, gfx942, gfx950 and gfx1010+ (RDNA 1 and newer); GCN/Vega use Vulkan |
| rocm | 8bit | - | - | - | **UNAVAILABLE**: rocminfo not found on PATH; supported: AMD gfx908, gfx90a, gfx942, gfx950 and gfx1010+ (RDNA 1 and newer); GCN/Vega use Vulkan |
| rocm | 4bit | - | - | - | **UNAVAILABLE**: rocminfo not found on PATH; supported: AMD gfx908, gfx90a, gfx942, gfx950 and gfx1010+ (RDNA 1 and newer); GCN/Vega use Vulkan |
| legacy-gpu | auto | - | - | - | **UNAVAILABLE**: nvidia-smi not found on PATH; supported: NVIDIA compute capability 5.0+ (Maxwell and newer); AMD GCN/Vega has no prebuilt runtime, use the Vulkan rung |
| legacy-gpu | none | - | - | - | **UNAVAILABLE**: nvidia-smi not found on PATH; supported: NVIDIA compute capability 5.0+ (Maxwell and newer); AMD GCN/Vega has no prebuilt runtime, use the Vulkan rung |
| legacy-gpu | 8bit | - | - | - | **UNAVAILABLE**: nvidia-smi not found on PATH; supported: NVIDIA compute capability 5.0+ (Maxwell and newer); AMD GCN/Vega has no prebuilt runtime, use the Vulkan rung |
| legacy-gpu | 4bit | - | - | - | **UNAVAILABLE**: nvidia-smi not found on PATH; supported: NVIDIA compute capability 5.0+ (Maxwell and newer); AMD GCN/Vega has no prebuilt runtime, use the Vulkan rung |
| vulkan | auto | - | - | - | **UNAVAILABLE**: no Vulkan llama-server for macOS (the CPU rung covers it); supported: a hardware Vulkan device on Linux or Windows (not llvmpipe, lavapipe or SwiftShader) |
| vulkan | none | - | - | - | **UNAVAILABLE**: no Vulkan llama-server for macOS (the CPU rung covers it); supported: a hardware Vulkan device on Linux or Windows (not llvmpipe, lavapipe or SwiftShader) |
| vulkan | 8bit | - | - | - | **UNAVAILABLE**: no Vulkan llama-server for macOS (the CPU rung covers it); supported: a hardware Vulkan device on Linux or Windows (not llvmpipe, lavapipe or SwiftShader) |
| vulkan | 4bit | - | - | - | **UNAVAILABLE**: no Vulkan llama-server for macOS (the CPU rung covers it); supported: a hardware Vulkan device on Linux or Windows (not llvmpipe, lavapipe or SwiftShader) |
| cpu | auto | 850 MiB | 195.206 | 315.411 | load 839 MiB, steady 850 MiB, load 0.5s; artifact laya-q8_0, threads=10 |
| cpu | none | 857 MiB | 597.419 | 828.804 | load 845 MiB, steady 857 MiB, load 0.7s; artifact laya-f16, threads=10 |
| cpu | 8bit | 850 MiB | 218.452 | 371.571 | load 839 MiB, steady 850 MiB, load 0.7s; artifact laya-q8_0, threads=10 |
| cpu | 4bit | 527 MiB | 342.067 | 528.16 | load 516 MiB, steady 527 MiB, load 0.5s; artifact laya-q4_k_m, threads=10 |
| remote | n/a | 892 MiB | 211.95 | 340.745 | load 891 MiB, steady 884 MiB, load 0.7s; loopback `laya serve --backend cpu` (default quant); no real network hop; RSS = serve process tree |
| rule-gate | n/a | 31 MiB | 0.006 | 0.009 | load 31 MiB, steady 31 MiB, load 0.0s; no model: lane 1/2 regex tables only (laya_gate._family, LANE1, LANE2); in-process RSS |

Reading the numbers:

- Unavailable rows were probed on this machine and cannot load here (no NVIDIA, ROCm or Vulkan runtime on macOS). They are unmeasured, not zero, and the GPU rungs stay unverified on hardware.
- RSS is process memory only. mlx weights and Metal buffers can live outside it.
- cpu auto and cpu 8bit load the same Q8_0 file; the spread between them is run-to-run noise on a shared host.
- F16 and Q8_0 show near-equal RSS: the llama-server compute and context buffers dominate at this model size, so quantization moves latency more than memory (only Q4_K_M cuts memory).
- The host was shared with other work during the run (1 minute load average per combination is in the JSON: load_avg_1m). Each combination waited for the CPU to settle first (quiet_start in the JSON), but p95 still carries scheduler noise. Re-run on an idle host for release numbers.
- remote is a loopback `laya serve`, so it shows server and client overhead, not network time.
- rule-gate is the lane 1/2 regex tables only, not the full classify() path (which writes state).
