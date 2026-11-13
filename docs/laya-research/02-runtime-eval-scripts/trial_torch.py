import json, torch, numpy as np, time
from laya_port import load
ref = json.load(open("ref_mlx.json")); m = load()
print("params", sum(p.numel() for p in m.parameters()))
rows=[]
for r in ref:
    if r["dtype"]!="float32": continue
    with torch.no_grad():
        lg,p = m(torch.tensor([r["ids"]]), torch.tensor([r["markers"]]))
    rows.append((r["logits"], lg[0].tolist(), r["score"], round(p[0,0].item(),4)))
for a,b,s,t in rows: print("mlx32", [round(x,4) for x in a], "torch32", [round(x,4) for x in b], "score", s, t, "dlogit", max(abs(x-y) for x,y in zip(a,b)))
