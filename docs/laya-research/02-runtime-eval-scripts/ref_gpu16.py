import json, laya_mlx as laya
ref=[r for r in json.load(open("ref_mlx.json")) if r["dtype"]=="float32"]
Q={"action":{"type":"choice","instructions":"What is the right action for the operator here?","criteria":["interrupt her now","log it and stay silent"]}}
m=laya.load("aac6fef/laya-mlx",device="gpu",dtype="float16")
for r in ref:
    s=m.predict(r["state"],Q)["answers"]["action"]["probabilities"]["interrupt her now"]
    print("mlx32",r["score"],"mlx-gpu-fp16",s,"d",round(abs(s-r["score"]),4))
