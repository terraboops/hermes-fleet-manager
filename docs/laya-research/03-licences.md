# Laya weights and candidate runtimes: licence findings

Task: t_f0cfdbc2. Checked 2026-10-14 against live sources (URLs per row).
This is not legal advice. Where a clause is ambiguous it is written as a question for the owner.

Scope: can `aac6fef/laya-mlx` (used by `hermes-fleet-manager/laya_gate.py`, `MODEL = aac6fef/laya-mlx`) be converted to GGUF / ONNX / PyTorch and shipped inside the thos/bb tooling, together with a runtime.

## 1. Summary table

"Convert" = make a derivative format of the weights or code. "Redistribute" = ship the artifact (or a quantized or converted form) to third parties. "Bundle" = include the runtime in our package.

| Artifact | Licence | Convert? | Redistribute? | Obligations |
|---|---|---|---|---|
| aac6fef/laya-mlx weights (FP16 MLX port) | Apache-2.0 (HF card metadata `license:apache-2.0`, README, `LICENSE` file, `NOTICE`) | Yes (Apache-2.0 s.2 grants right to "prepare Derivative Works") | Yes, source or object form | Ship licence copy; mark changes; carry `NOTICE` text (s.4) |
| convaiinnovations/laya weights (upstream; source of the port) | Apache-2.0 (HF card metadata only; no LICENSE file on the HF repo; GitHub repo has `LICENSE`, SPDX Apache-2.0) | Yes | Yes | Same. No NOTICE file found upstream (GitHub root has none) |
| ModernBERT-large (base of the encoder) | Apache-2.0 (HF card `license: apache-2.0`) | Yes | Yes | Same; attribute Answer.AI/LightOn lineage |
| Laya code (NandhaKishorM/laya) | Apache-2.0 (GitHub API SPDX) | Yes | Yes | Same |
| laya-mlx package (PyPI 0.4.0, mizorewww/laya-mlx) | Apache-2.0 (PyPI `license_expression`; GitHub API SPDX) | Yes, but MLX is Apple-only, so it is not a shippable runtime for GGUF/ONNX targets | Yes | Same; its `NOTICE` credits Laya |
| llama.cpp | MIT (LICENSE: "Copyright (c) 2023-2026 The ggml authors") | Yes | Yes | Keep copyright + permission notice |
| llama-cpp-python | MIT (LICENSE.md: "Copyright (c) 2023 Andrei Betlen"; PyPI says `MIT`) | n/a | Yes | Keep notice; wheel bundles llama.cpp, so keep both notices |
| ONNX Runtime (CPU) | MIT (LICENSE; PyPI classifier) | n/a | Yes | Keep MIT notice; wheel carries `ThirdPartyNotices.txt`, ship it |
| ONNX Runtime GPU wheels (`onnxruntime-gpu`, `-directml`) | MIT (PyPI) for ORT itself | n/a | Yes for ORT | As above. CUDA/cuDNN come from NVIDIA, see CUDA row. Unverified: exact contents of each GPU wheel |
| PyTorch | BSD-3-Clause-style (repo `LICENSE`, GitHub reports "Other"); PyPI SPDX list for torch 2.14.1 is a composite: Apache-2.0, Apache-2.0 WITH LLVM-exception, BSD-2-Clause, BSD-3-Clause, BSL-1.0, MIT | n/a | Yes | Reproduce copyright + conditions + disclaimer in binary form; no endorsement by the named holders |
| CUDA runtime redistributables | NVIDIA proprietary EULA (PyPI `LicenseRef-NVIDIA-Proprietary`) | n/a | Only the files listed in EULA Attachment A | See section 4. cuDNN is not in Attachment A: BLOCKING for bundling |
| ROCm | Per component. Most MIT; some Apache-2.0, NCSA, custom, GPL (ROCgdb GPLv3, kernel driver GPL-2.0) | n/a | Per component | Pick components; avoid GPL pieces. Repo-level `ROCm/ROCm` is MIT but it is only the manifest |
| Vulkan loader | Apache-2.0 plus some more permissive files (Vulkan-Loader `LICENSE.txt`; `LICENSES/` holds Apache-2.0, CC-BY-4.0, HPND-Kevlin-Henney, MIT-Khronos-old, MIT) | n/a | Yes | Keep Apache-2.0 text and any NOTICE; mark changes if modified |

## 2. Weights: chain of title and exact text

Chain: `ModernBERT-large` (Apache-2.0) -> `convaiinnovations/laya` (Apache-2.0, "Backbone: ModernBERT-large (395M ...) + a decision head trained from scratch") -> `aac6fef/laya-mlx` (Apache-2.0, FP16 MLX port).

- Port card: "Apache-2.0. Original Laya models and code are by Convai Innovations and contributors. See `LICENSE`, `NOTICE`, `mlx_config.json` and `manifest.json`." https://huggingface.co/aac6fef/laya-mlx
- Port card on the nature of the change: "Conversion changes parameter names for MLX and preserves FP16 weights. It does not retrain or quantize to fewer bits."
- HF metadata for the port: `"cardData":{"license":"apache-2.0", "base_model":"convaiinnovations/laya"}` https://huggingface.co/api/models/aac6fef/laya-mlx
- Port `NOTICE`: "This product includes software derived from Laya: https://github.com/NandhaKishorM/laya Copyright Convai Innovations and Laya contributors. Licensed under Apache-2.0." https://huggingface.co/aac6fef/laya-mlx/raw/main/NOTICE
- Upstream card front matter: `license: apache-2.0`, tag `commercial-use`. https://huggingface.co/convaiinnovations/laya
- ModernBERT-large front matter: `license: apache-2.0`. https://huggingface.co/answerdotai/ModernBERT-large
- Package metadata: laya-mlx 0.4.0 `license_expression: Apache-2.0`. https://pypi.org/pypi/laya-mlx/json

Apache-2.0 grant (s.2), as embedded in NVIDIA's EULA copy and identical in the weights' `LICENSE`:
"each Contributor hereby grants to You a perpetual, worldwide, non-exclusive, no-charge, royalty-free, irrevocable copyright license to reproduce, prepare Derivative Works of, publicly display, publicly perform, sublicense, and distribute the Work and such Derivative Works in Source or Object form."
Its definition of Object form includes "conversions to other media types", so GGUF, ONNX and PyTorch are covered.

Apache-2.0 s.4 obligations that apply to every converted or quantized artifact:
- "(a) You must give any other recipients of the Work or Derivative Works a copy of this License"
- "(b) You must cause any modified files to carry prominent notices stating that You changed the files"
- "(d) If the Work includes a "NOTICE" text file ... any Derivative Works that You distribute must include a readable copy of the attribution notices"

Answers: convert = yes. Quantize and redistribute = yes. Attribution = ship `LICENSE`, carry the port's `NOTICE` plus Convai/Laya credit, and mark converted files as changed.

Prefer converting from `convaiinnovations/laya` directly rather than from the third-party MLX port. Both are Apache-2.0, but the first removes one link from the chain.

## 3. Runtime excerpts

- llama.cpp: "MIT License / Copyright (c) 2023-2026 The ggml authors". https://raw.githubusercontent.com/ggml-org/llama.cpp/master/LICENSE
- llama-cpp-python: "MIT License / Copyright (c) 2023 Andrei Betlen". https://raw.githubusercontent.com/abetlen/llama-cpp-python/main/LICENSE.md
- ONNX Runtime: "MIT License / Copyright (c) Microsoft Corporation"; `ThirdPartyNotices.txt` begins "This software incorporates material from third parties." https://raw.githubusercontent.com/microsoft/onnxruntime/main/LICENSE and `.../ThirdPartyNotices.txt`
- ORT PyPI: onnxruntime 1.31.0, onnxruntime-gpu 1.31.0, onnxruntime-directml 1.24.4 all classify as MIT. https://pypi.org/project/onnxruntime-gpu/
- PyTorch: BSD-style, "3. Neither the names of Facebook, Deepmind Technologies, NYU, NEC Laboratories America ... may be used to endorse or promote products derived from this software without specific prior written permission." Binary redistribution clause 2 requires reproducing the notice. https://raw.githubusercontent.com/pytorch/pytorch/main/LICENSE and `/NOTICE`
- ROCm: "ROCm is released by Advanced Micro Devices, Inc. (AMD) and is licensed per component separately." Component table lists MIT for most, "Custom" for hipCUB and RCCL, NCSA for Comgr and rocminfo, "GNU General Public License v3.0" for ROCgdb. https://rocm.docs.amd.com/en/latest/about/license.html
- Vulkan loader: "The majority of files in this project use the Apache 2.0 License. There are a few exceptions and their license can be found in the source. Any license deviations from Apache 2.0 are "more permissive" licenses." https://raw.githubusercontent.com/KhronosGroup/Vulkan-Loader/main/LICENSE.txt

All of the above permit conversion-neutral bundling in our package, with notice retention. None restrict the use case (a local classifier).

## 4. CUDA redistributables (the restrictive one)

Source: https://docs.nvidia.com/cuda/eula/index.html (v13.4, last updated January 26, 2026). PyPI marks the `nvidia-cuda-runtime-cu12` and `nvidia-cublas-cu12` wheels `LicenseRef-NVIDIA-Proprietary`.

- Grant: "Distribute those portions of the SDK that are identified in this Agreement as distributable, as incorporated in object code format into a software application that meets the distribution requirements indicated in this Agreement." (1.1.1)
- Requirement: "Your application must have material additional functionality, beyond the included portions of the SDK." (1.1.2.1)
- Requirement: "The distributable portions of the SDK shall only be accessed by your application." (1.1.2.2)
- Requirement: "The terms under which you distribute your application must be consistent with the terms of this Agreement" (1.1.2.5)
- Requirement: "You agree to notify NVIDIA in writing of any known or suspected distribution or use of the SDK not in compliance" (1.1.2.6)
- Limit: "you may not distribute or sublicense the SDK as a stand-alone product." (1.2.2)
- Limit: "You may not use the SDK in any manner that would cause it to become subject to an open source software license." (1.2.5)
- Scope: "The SDK is licensed for you to develop applications only for use in systems with NVIDIA GPUs." (2.1)
- Attachment A lists distributable files: CUDA Runtime (`libcudart.so`), cuFFT, cuBLAS (`libcublas.so`, `libcublasLt.so`), cuSPARSE, cuSOLVER, cuRAND, NPP, nvJPEG, NVRTC and others.
- Linux files "may be copied and redistributed ... provided that the object code files are not modified in any way (except for unzipping of compressed files)." (2.3)

Reading: bundling `libcudart` and `libcublas` unmodified inside a package that does real work is allowed. Bundling the whole toolkit, or shipping CUDA alone, is not. cuDNN does not appear in Attachment A of this EULA. It has its own NVIDIA licence, which I did not retrieve (PyPI for `nvidia-cudnn-cu12` returns no licence metadata).

Safest route: do not bundle CUDA. Declare it as an external dependency (user installs it, or pulls `nvidia-*` wheels from PyPI themselves). Both ORT-GPU and PyTorch work that way.

## 5. Unresolved items (treat as blocking until closed)

1. BLOCKING for any CUDA bundling: cuDNN licence and redistribution terms not found. Not in EULA Attachment A. Question for owner: do we bundle cuDNN at all, or is "user installs CUDA/cuDNN" acceptable?
2. BLOCKING for any CUDA bundling: the owner must decide whether our package satisfies "material additional functionality" and "only be accessed by your application", and whether a notification process for NVIDIA (1.1.2.6) is acceptable. Ask counsel if we bundle.
3. Training data and teacher: the upstream card mentions a "teacher" (0.735 self-agreement ceiling) and RL training, but I found no statement of the teacher model's identity or the training data's licence. Apache-2.0 on the weights does not by itself answer whether the teacher's output terms bind the weights. Question for owner: is that residual risk acceptable, or do we ask Convai Innovations? Not blocking the Apache grant, but unverified.
4. Upstream HF repo `convaiinnovations/laya` has no `LICENSE` or `NOTICE` file in its file list; the grant is in card metadata and the GitHub repo `LICENSE` only. GitHub repo root has no `NOTICE` (404). Question for owner: is card metadata plus GitHub `LICENSE` enough evidence of the grant for the thos record? I think so, but it is a judgement.
5. Third-party port: the port card says it is an independent port; its `NOTICE` says "Model weights are downloaded separately from Convai Innovations on Hugging Face; they are not included in this repository", yet the HF repo does contain `model.safetensors`. Minor inconsistency in the port's own text. Mitigation: convert from the upstream checkpoint (item above).
6. ROCm: pick the actual components shipped. Each needs its own check; hipCUB and RCCL are "Custom", ROCgdb and the kernel driver are GPL and must not be bundled. Not assessed per component.
7. ONNX Runtime GPU wheels: contents of each wheel (bundled native libraries and their notices) not unpacked. Check `ThirdPartyNotices.txt` inside the exact wheel we pin.
8. PyTorch wheels: the PyPI composite SPDX list means the wheel carries third-party licences (including the `nvidia-*` dependencies if we vendor them). Ship the wheel's own licence files; do not vendor the NVIDIA dependencies without item 1/2 resolved.
9. Vulkan loader: no `NOTICE` retrieved; the `LICENSES/` folder shows non-Apache exceptions. Only an issue if we vendor the loader rather than use the system one. Recommendation: use the system loader.
10. Patents: Apache-2.0 s.3 grants patent licence from contributors, terminating if we sue over the Work. No other runtime here has an express patent grant (MIT, BSD). Question for owner: is that acceptable?
11. Not checked: llama.cpp/ggml bundled third-party sources, and GGUF converter coverage for ModernBERT plus Laya's custom heads (technical feasibility belongs to another task, not licensing).

## 6. Bottom line

Weights: nothing blocks converting or redistributing, provided `LICENSE` + `NOTICE` ship and changes are marked. Runtimes: llama.cpp, llama-cpp-python, ONNX Runtime, PyTorch, Vulkan loader and chosen ROCm parts are permissive and bundleable with notices. The only hard wall is NVIDIA CUDA/cuDNN: do not bundle, treat as an external dependency.
