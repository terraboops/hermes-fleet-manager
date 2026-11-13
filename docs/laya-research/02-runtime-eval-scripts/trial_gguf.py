import json, sys, subprocess, time, urllib.request, os, signal
gguf = sys.argv[1]; port = 18089
ref = [r for r in json.load(open("ref_mlx.json")) if r["dtype"]=="float32"]
Q={"action":{"type":"choice","instructions":"What is the right action for the operator here?","criteria":{"interrupt her now":None,"log it and stay silent":None}}}
p = subprocess.Popen(["llama.cpp/build/bin/llama-server","-m",gguf,"--port",str(port),"-ngl","0","-c","1024","--no-webui"],stdout=open("server.log","w"),stderr=subprocess.STDOUT)
try:
    for _ in range(120):
        try: urllib.request.urlopen(f"http://127.0.0.1:{port}/health").read(); break
        except Exception: time.sleep(1)
    ts=[]
    for r in ref:
        body=json.dumps({"state":r["state"],"questions":Q}).encode()
        t=time.time()
        res=json.load(urllib.request.urlopen(urllib.request.Request(f"http://127.0.0.1:{port}/v1/systemone",body,{"Content-Type":"application/json"})))
        ts.append(time.time()-t)
        s=res["answers"]["action"]["probabilities"]["interrupt her now"]
        print(os.path.basename(gguf),"mlx32",r["score"],"gguf",round(s,4),"d",round(abs(s-r["score"]),4),"tok",res["usage"]["input_tokens"],len(r["ids"]),"dec",(s>=0.59)==(r["score"]>=0.59))
    print("ms/req (excl first)", round(1000*sum(ts[1:])/len(ts[1:])))
finally:
    p.send_signal(signal.SIGTERM); p.wait()
