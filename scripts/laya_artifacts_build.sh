#!/usr/bin/env bash
# Rebuild every Laya GGUF artifact from the pinned upstream weights.
# Output: ${LAYA_CACHE}/blobs/<sha256>/<name>.gguf and ${LAYA_CACHE}/models/<name>.gguf (symlinks).
# Then run: python3 scripts/laya_artifacts.py verify
# Recipe and pins: docs/laya-artifacts.md. Run from the repo root.
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
case "$(uname -s)" in
  Darwin) LAYA_CACHE="${LAYA_CACHE:-$HOME/Library/Caches/thos/laya}" ;;
  *)      LAYA_CACHE="${LAYA_CACHE:-${XDG_CACHE_HOME:-$HOME/.cache}/thos/laya}" ;;
esac
UPSTREAM_REV=7b928d828b7b0e022f929d9bd2e44165aa270148   # weights are byte-identical at c5d78730
LLAMACPP_COMMIT=6868a52
BUILD="$LAYA_CACHE/build"
mkdir -p "$BUILD" "$LAYA_CACHE/blobs" "$LAYA_CACHE/models"
cd "$BUILD"

# 0. tools
[ -d .venv ] || uv venv -p 3.12 .venv
uv pip install -p .venv/bin/python safetensors torch numpy transformers tokenizers sentencepiece cmake ninja huggingface_hub
[ -d llama.cpp ] || git clone https://github.com/ggml-org/llama.cpp
git -C llama.cpp checkout "$LLAMACPP_COMMIT"

# 1. fetch the upstream checkpoint (no tensor rename needed)
.venv/bin/python -c "
from huggingface_hub import snapshot_download
snapshot_download('convaiinnovations/laya', revision='$UPSTREAM_REV', local_dir='upstream_laya')"

# 2. convert (F16; the source weights are already F16)
PYTHONPATH=llama.cpp/gguf-py .venv/bin/python llama.cpp/convert_hf_to_gguf.py upstream_laya \
  --outtype f16 --outfile laya-f16.gguf

# 3. build the tools and quantize
PATH="$PWD/.venv/bin:$PATH" cmake -S llama.cpp -B llama.cpp/build -G Ninja \
  -DGGML_METAL=OFF -DGGML_BLAS=OFF -DLLAMA_CURL=OFF -DLLAMA_OPENSSL=OFF -DCMAKE_BUILD_TYPE=Release
PATH="$PWD/.venv/bin:$PATH" cmake --build llama.cpp/build --target llama-server llama-quantize -j 8
llama.cpp/build/bin/llama-quantize laya-f16.gguf laya-q8_0.gguf Q8_0
llama.cpp/build/bin/llama-quantize laya-f16.gguf laya-q6_k.gguf Q6_K
llama.cpp/build/bin/llama-quantize laya-f16.gguf laya-q4_k_m.gguf Q4_K_M

# 4. parity against the MLX fp32 reference, one artifact at a time
cp "$REPO"/docs/laya-research/02-runtime-eval-scripts/{trial_gguf.py,ref_mlx.json} .
for f in f16 q8_0 q6_k q4_k_m; do .venv/bin/python trial_gguf.py "laya-$f.gguf" | tee "parity-$f.txt"; done

# 5. install into the content-addressed cache with licence files beside each blob
for f in laya-f16 laya-q8_0 laya-q6_k laya-q4_k_m; do
  sha=$(shasum -a 256 "$f.gguf" | cut -d' ' -f1)
  mkdir -p "$LAYA_CACHE/blobs/$sha"
  cp "$f.gguf" "$LAYA_CACHE/blobs/$sha/$f.gguf"
  cp "$REPO/docs/laya-artifacts-NOTICE.txt" "$LAYA_CACHE/blobs/$sha/NOTICE"
  ln -sfn "$LAYA_CACHE/blobs/$sha/$f.gguf" "$LAYA_CACHE/models/$f.gguf"
done
