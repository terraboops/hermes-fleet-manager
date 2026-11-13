"""Rename laya-mlx tensor names back to upstream Laya names (MLX stores scorer.layers.N / head in_proj.weight)."""
import sys, os, shutil, glob, re
from safetensors.torch import load_file, save_file
src, dst = sys.argv[1], sys.argv[2]
os.makedirs(dst, exist_ok=True)
sd = load_file(os.path.join(src, "model.safetensors"))
out = {}
for k, v in sd.items():
    k = re.sub(r"^(scorer|act_head)\.layers\.", r"\1.", k)
    out[k] = v.contiguous()
save_file(out, os.path.join(dst, "model.safetensors"), metadata={"format": "pt"})
for f in ("rl_agent_config.json",):
    shutil.copy(os.path.join(src, f), dst)
for d in ("encoder", "tokenizer"):
    shutil.copytree(os.path.join(src, d), os.path.join(dst, d), dirs_exist_ok=True)
print(len(out), "tensors written to", dst)
