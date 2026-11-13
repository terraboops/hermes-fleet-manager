import json, sys, time, numpy as np, onnxruntime as ort
from onnxruntime.quantization import quantize_dynamic, QuantType
ref = [r for r in json.load(open("ref_mlx.json")) if r["dtype"]=="float32"]
def run(path, label):
    so = ort.SessionOptions(); s = ort.InferenceSession(path, so, providers=["CPUExecutionProvider"])
    ds=[]; t=[]
    for r in ref:
        t0=time.time()
        lg,p = s.run(None, {"input_ids":np.array([r["ids"]],dtype=np.int64),"marker_pos":np.array([r["markers"]],dtype=np.int64)})
        t.append(time.time()-t0)
        ds.append((r["score"], round(float(p[0,0]),4), float(np.abs(lg[0]-np.array(r["logits"])).max())))
    print(label, "ms/infer", round(1000*np.mean(t[1:]),0))
    for a,b,c in ds: print("  mlx32",a,"ort",b,"dscore",round(abs(a-b),4),"dlogit",round(c,5),"dec@0.59",(a>=0.59)==(b>=0.59))
run("laya_fp32.onnx","fp32")
if len(sys.argv)>1:
    t=time.time(); quantize_dynamic("laya_fp32.onnx","laya_int8.onnx",weight_type=QuantType.QInt8); print("quant s",round(time.time()-t,1))
    run("laya_int8.onnx","int8-dynamic")
