"""Plain-PyTorch port of the Laya decision model (ModernBERT-large + 2-layer head + scorer).
Loads the laya-mlx safetensors directly. Batch of one, no padding, so no key-padding mask is needed."""
import glob, json, os, torch, torch.nn as nn, torch.nn.functional as F
from safetensors.torch import load_file

SNAP = glob.glob(os.path.expanduser("~/.cache/huggingface/hub/models--aac6fef--laya-mlx/snapshots/*"))[0]
D, H, HD, NL, FF, EPS, WIN = 1024, 16, 64, 28, 2624, 1e-5, 128
TEMP_CHOICE2 = 1.9063563346862793

def rope(x, base):  # x: [B,H,L,HD], non-traditional (rotate_half) layout
    L = x.shape[2]
    inv = 1.0 / (base ** (torch.arange(0, HD, 2, dtype=torch.float32) / HD))
    ang = torch.arange(L, dtype=torch.float32)[:, None] * inv[None, :]
    cos, sin = torch.cat([ang, ang], -1).cos(), torch.cat([ang, ang], -1).sin()
    x1, x2 = x[..., : HD // 2], x[..., HD // 2 :]
    return x * cos + torch.cat([-x2, x1], -1) * sin

class Layer(nn.Module):
    def __init__(s, i):
        super().__init__()
        s.glob = i % 3 == 0
        s.base = 160000.0 if s.glob else 10000.0
        s.attn_norm = nn.Identity() if i == 0 else nn.LayerNorm(D, eps=EPS, bias=False)
        s.Wqkv = nn.Linear(D, 3 * D, bias=False); s.Wo_a = nn.Linear(D, D, bias=False)
        s.mlp_norm = nn.LayerNorm(D, eps=EPS, bias=False)
        s.Wi = nn.Linear(D, 2 * FF, bias=False); s.Wo_m = nn.Linear(FF, D, bias=False)
    def forward(s, x, local_mask):
        B, L, _ = x.shape
        q, k, v = s.Wqkv(s.attn_norm(x)).reshape(B, L, 3, H, HD).permute(2, 0, 3, 1, 4)
        q, k = rope(q, s.base), rope(k, s.base)
        m = None if s.glob else local_mask
        a = F.scaled_dot_product_attention(q, k, v, attn_mask=m)
        x = x + s.Wo_a(a.transpose(1, 2).reshape(B, L, D))
        val, gate = s.Wi(s.mlp_norm(x)).chunk(2, -1)
        return x + s.Wo_m(F.gelu(val) * gate)

class HeadLayer(nn.Module):
    def __init__(s):
        super().__init__()
        s.in_proj = nn.Linear(D, 3 * D); s.out_proj = nn.Linear(D, D)
        s.norm1 = nn.LayerNorm(D); s.norm2 = nn.LayerNorm(D)
        s.linear1 = nn.Linear(D, 4 * D); s.linear2 = nn.Linear(4 * D, D)
    def forward(s, x):
        B, L, _ = x.shape
        q, k, v = s.in_proj(s.norm1(x)).reshape(B, L, 3, H, HD).permute(2, 0, 3, 1, 4)
        a = F.scaled_dot_product_attention(q, k, v)
        x = x + s.out_proj(a.transpose(1, 2).reshape(B, L, D))
        return x + s.linear2(F.relu(s.linear1(s.norm2(x))))

class Laya(nn.Module):
    def __init__(s):
        super().__init__()
        s.tok = nn.Embedding(50368, D); s.emb_norm = nn.LayerNorm(D, eps=EPS, bias=False)
        s.layers = nn.ModuleList(Layer(i) for i in range(NL))
        s.final_norm = nn.LayerNorm(D, eps=EPS, bias=False)
        s.type_emb = nn.Embedding(3, D)
        s.head = nn.ModuleList(HeadLayer() for _ in range(2))
        s.sc_norm = nn.LayerNorm(D); s.sc1 = nn.Linear(D, D); s.sc2 = nn.Linear(D, 1)
    def forward(s, input_ids, marker_pos):
        """input_ids [1,L] int64, marker_pos [1,2] int64 -> (raw logits [1,2], calibrated P [1,2])"""
        L = input_ids.shape[1]
        pos = torch.arange(L)
        local = ((pos[:, None] - pos[None, :]).abs() <= WIN // 2)[None, None]
        x = s.emb_norm(s.tok(input_ids))
        for l in s.layers: x = l(x, local)
        x = s.final_norm(x) + s.type_emb.weight[0]
        for l in s.head: x = l(x)
        m = x[0, marker_pos[0]]                                  # [2,D]
        logits = s.sc2(F.gelu(s.sc1(s.sc_norm(m)))).reshape(1, 2)
        return logits, torch.softmax(logits / TEMP_CHOICE2, -1)

def load(dtype=torch.float32):
    sd = load_file(os.path.join(SNAP, "model.safetensors")); new = {}
    for k, v in sd.items():
        v = v.to(torch.float32)
        k = k.replace("encoder.embeddings.tok_embeddings", "tok").replace("encoder.embeddings.norm", "emb_norm")
        k = k.replace("encoder.final_norm", "final_norm").replace("encoder.layers.", "layers.")
        k = k.replace(".attn.Wqkv", ".Wqkv").replace(".attn.Wo", ".Wo_a").replace(".mlp.Wi", ".Wi").replace(".mlp.Wo", ".Wo_m")
        k = k.replace("head.layers.", "head.").replace(".self_attn.", ".")
        k = k.replace("scorer.layers.0", "sc_norm").replace("scorer.layers.1", "sc1").replace("scorer.layers.3", "sc2")
        if k.startswith(("act_head", "temperature")): continue
        new[k] = v
    m = Laya(); r = m.load_state_dict(new, strict=True); m.eval()
    return m.to(dtype)
