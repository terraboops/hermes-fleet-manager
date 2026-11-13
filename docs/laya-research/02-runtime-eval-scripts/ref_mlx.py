import json, sys, numpy as np, laya_mlx as laya
DOCTRINE=("The operator runs a fleet of coding agents. Every event raised to her interrupts her, and she has asked to hear only about a decision that only she can make, a genuine stop, or anything touching production, money, security or a client. A session that is merely quiet is not a stop. A session that has finished its work is not a stop. A repeat of something already reported is not news.")
Q={"action":{"type":"choice","instructions":"What is the right action for the operator here?","criteria":["interrupt her now","log it and stay silent"]}}
cases=[
 ("A deploy to production is blocked by a classifier and waits for approval","Production Deploy blocked; session asks for a human decision",0),
 ("The session finished its task and is idle","session idle after completing work",3),
 ("Credentials for the client billing account were found in a committed file","security finding: leaked client credential",0),
 ("Routine lint pass completed with no issues","lint ok",5),
 ("Session has been stopped for 40 minutes waiting on an answer only the operator can give","session stopped, awaiting operator decision",1),
 ("Test run repeated the same failure already reported twice","same failure repeated",2),
 ("The session is compiling the project","build in progress",0),
 ("Payment provider webhook is failing for the client and orders are being dropped","money impact: webhook failing",0),
]
out=[]
for dt in ("float16","float32"):
    m=laya.load("aac6fef/laya-mlx",device="cpu",dtype=dt)
    for ctx,probe,n in cases:
        state=f"{DOCTRINE}\n\nSituation: {ctx}\nEvent: {probe}\nEvents of this kind on this session in the last 24 hours: {n}."
        items,_=m.prepare(state,Q)
        b=laya.agent.collate_items(items,m.tok.pad_token_id)
        lg,_=m.forward(b)
        r=m.predict(state,Q)
        p=r["answers"]["action"]["probabilities"]["interrupt her now"]
        out.append(dict(dtype=dt,state=state,ids=items[0]["ids"],markers=items[0]["markers"],logits=np.asarray(lg)[0,:2].tolist(),score=p))
json.dump(out,open("ref_mlx.json","w"))
for o in out: print(o["dtype"],o["logits"],o["score"],len(o["ids"]))
