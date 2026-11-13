import torch, time
from laya_port import load
m = load()
ids = torch.randint(0, 50000, (1, 150)); mk = torch.tensor([[20, 30]])
t=time.time()
torch.onnx.export(m, (ids, mk), "laya_fp32.onnx", input_names=["input_ids","marker_pos"],
    output_names=["logits","prob"], dynamic_axes={"input_ids":{1:"L"}}, opset_version=18, dynamo=False)
print("export s", round(time.time()-t,1))
