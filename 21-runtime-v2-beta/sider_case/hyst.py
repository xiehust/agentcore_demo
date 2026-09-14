"""对照实验: 区分"自然衰减"和"guest 释放后的滞后"。
A(对照): 什么都不做, 采样 360s -> 测自然衰减速率
B(处理): t=60 占 4GB, t=120 释放, 采样到 360s -> 看释放后是否按同样速率衰减
若 B 释放后被钉住而 A 在衰减, 说明滞后是 guest free 特有的。
"""
import argparse
import asyncio, json, ssl, time, uuid
from pathlib import Path
import certifi, websockets
from bedrock_agentcore.runtime import AgentCoreRuntimeClient
REGION="us-west-2"
ARN="arn:aws:bedrock-agentcore:us-west-2:687912291502:runtime/session_snapshot_test-C5bkUXCPHn"
SSL=ssl.create_default_context(cafile=certifi.where())

async def run(arm, c, samples, record, args, persist, sample_file):
    sid=record["sid"]
    url,hdr=c.generate_ws_connection(runtime_arn=args.arn,session_id=sid,endpoint_name=args.endpoint)
    ev=record["events"]
    async with websockets.connect(url,additional_headers=hdr,ssl=SSL,open_timeout=180,max_size=2**20) as ws:
        async def send(o):
            await ws.send(json.dumps(o))
            r=json.loads(await asyncio.wait_for(ws.recv(), timeout=60))
            record.setdefault("responses", []).append(r); persist()
            return r
        async def watch(sec,ph):
            await ws.send(json.dumps({"action":"memwatch","interval_s":1.0,"duration_s":sec}))
            while True:
                m=json.loads(await asyncio.wait_for(ws.recv(), timeout=30))
                if m.get("action")=="memwatch_done": return
                if m.get("action")!="memsample" or m.get("session_id")!=sid:
                    raise ValueError(f"Unexpected sample: {m}")
                m["_phase"]=ph; m["_session_id"]=sid; m["_arm"]=arm; samples.append(m)
                sample_file.write(json.dumps(m,ensure_ascii=False)+"\n"); sample_file.flush()
        p=await send({"action":"ping"})
        ev.append(("connect",time.time(),p["memstat"]["uptime_s"]))
        if p.get("session_id")!=sid:
            raise ValueError("Ping session mismatch")
        if arm=="B":
            m=p["memstat"]
            if m.get("vm_mem_available_mb", 0)<4608:
                raise ValueError("Insufficient available guest memory for safe 4GiB test")
            limit=m.get("cgroup_mem_max")
            if str(limit).isdigit() and int(limit)-m.get("cgroup_mem_current_bytes", 0)<4608*1048576:
                raise ValueError("Insufficient cgroup headroom for safe 4GiB test")
        persist()
        await watch(60,"pre")
        if arm=="B":
            r=await send({"action":"alloc","mb":4096})
            if r.get("action")!="alloc_result" or r.get("held_mb")!=4096:
                raise ValueError(f"Allocation not confirmed: {r}")
            ev.append(("alloc",time.time(),r["held_mb"])); persist()
            await watch(60,"held")
            r=await send({"action":"free"})
            if r.get("action")!="free_result" or r.get("held_mb")!=0:
                raise ValueError(f"Release not confirmed: {r}")
            ev.append(("free",time.time(),r["held_mb"])); persist()
        else:
            await watch(60,"held_noop")
        await watch(240,"tail")
    return sid,ev

async def main(args):
    c=AgentCoreRuntimeClient(region=args.region); samples=[]
    d=args.out; d.mkdir(parents=True,exist_ok=True)
    records=[{"arm":a,"sid":f"hyst{a}-{uuid.uuid4().hex}-{uuid.uuid4().hex}"[:64],
              "events":[]} for a in "AB"]
    def persist():
        tmp=d/"events.tmp"
        tmp.write_text(json.dumps(records,ensure_ascii=False,indent=2))
        tmp.replace(d/"events.json")
    # Save session IDs before connection so the supervisor can stop failed arms.
    persist()
    with (d/"memory_samples.jsonl").open("x") as f:
        tasks=[asyncio.create_task(run(r["arm"],c,samples,r,args,persist,f)) for r in records]
        try:
            res=await asyncio.wait_for(asyncio.gather(*tasks), timeout=540)
        finally:
            for task in tasks:
                if not task.done(): task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            persist()
    for (sid,ev),arm in zip(res,"AB"):
        print(arm,sid)
        for k,t,v in ev: print('  ',k,round(t,1),v)
    print("目录:",d)

if __name__=="__main__":
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--region", default=REGION)
    parser.add_argument("--arn", default=ARN)
    parser.add_argument("--endpoint", default="test_endpoint")
    parser.add_argument("--out", type=Path, default=Path("results")/f"hysteresis_{time.strftime('%Y%m%d_%H%M%S')}")
    asyncio.run(main(parser.parse_args()))
