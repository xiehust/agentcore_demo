"""Long-term memory admin. The OpenSearch domain is VPC-only, so everything goes laptop -> Lambda (VPC) -> Runtime
(mode=memory_admin), which owns the embedding key and the OpenSearch role.

  seed_memory.py --reset            drop + recreate the index, then import seed/knowledge.json (verified, Kiro/wiki)
  seed_memory.py                    (re)import seed/knowledge.json
  seed_memory.py --migrate          also move agent-learned KB / lessons / instance history from the old DynamoDB LTM
                                    through the new quality gate (LLM consolidation, keyword filter, reflection generation)
  seed_memory.py --list             print the index
  seed_memory.py --search "<text>" [instanceId]   show what recall would return
  seed_memory.py --verify <signature> <reviewer>  human review: mark an agent-learned entry verified
  seed_memory.py --dedupe           merge near-duplicate reflections
"""
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
from boto3.dynamodb.conditions import Key  # noqa: E402
from botocore.config import Config  # noqa: E402
from common.config import LAMBDA_NAME, MEMORY_TABLE, log, session  # noqa: E402

lam = session().client("lambda", config=Config(read_timeout=900, retries={"max_attempts": 1}))


def admin(**ev):
    r = lam.invoke(FunctionName=LAMBDA_NAME, Payload=json.dumps({"action": "memory_admin", **ev}, default=str).encode())
    out = json.loads(r["Payload"].read())
    if isinstance(out, dict) and out.get("errorMessage"):
        raise SystemExit(out)
    return out


def show(x):
    print(json.dumps(x, indent=1, ensure_ascii=False, default=str))


argv = sys.argv[1:]
if "--list" in argv:
    out = admin(op="list")
    print("stats:", out["stats"])
    for e in out["kb"]:
        print(f"KB {e['signature']:<24} verified={e.get('verified')!s:<5} conf={e.get('confirmations', 0)} aliases={e.get('aliases')}\n"
              f"   keywords={e.get('keywords')} suggested={e.get('suggested_keywords', [])} proposals={len(e.get('update_proposals', []))}")
    for r in out["reflection"]:
        print(f"REFLECTION {r['id']} conf={r.get('confidence')} episodes={len(r.get('source_episodes', []))} :: {r['title']}\n"
              f"   use_cases: {r.get('use_cases', '')[:160]}\n   hints: {r.get('hints', '')[:200]}")
    for e in out["episode"]:
        print(f"EPISODE {e['batch_id'][:2]} {e['instance_id']} {e.get('status'):<12} {e.get('signature')} :: {e.get('reflection', '')[:120]}")
    raise SystemExit
if "--search" in argv:
    i = argv.index("--search")
    show(admin(op="search", query=argv[i + 1], instanceId=argv[i + 2] if len(argv) > i + 2 else "-"))
    raise SystemExit
if "--dedupe" in argv:
    i = argv.index("--dedupe")
    show(admin(op="dedupe_reflections", **({"gate": float(argv[i + 1])} if len(argv) > i + 1 else {})))
    show(admin(op="refilter_keywords"))
    raise SystemExit
if "--verify" in argv:
    i = argv.index("--verify")
    show(admin(op="verify", signature=argv[i + 1], reviewer=argv[i + 2]))
    raise SystemExit

if "--reset" in argv:
    show(admin(op="reset", confirm="reset-patch-ltm"))
seed = json.loads((pathlib.Path(__file__).resolve().parent.parent / "seed/knowledge.json").read_text())
show(admin(op="seed", items=seed))

if "--migrate" in argv:
    t = session().resource("dynamodb").Table(MEMORY_TABLE)

    def q(pk):
        return t.query(KeyConditionExpression=Key("pk").eq(pk))["Items"]
    kb = [{"signature": it["sk"], **{k: it[k] for k in ("keywords", "symptom", "root_cause", "fix", "learned_from") if k in it}}
          for it in q("KB") if not it.get("verified")]
    refl = [{"signature": it["signature"], "lesson": it["lesson"], "session_id": it["sk"].split("#", 1)[1]} for it in q("REFLECTION")]
    st = json.loads((pathlib.Path(__file__).resolve().parent.parent / ".state.json").read_text())
    inst = []
    for tgt in st["targets"].values():
        for it in q(f"INST#{tgt['instance_id']}"):
            ts, batch = it["sk"].split("#", 1)
            inst.append({"instance_id": tgt["instance_id"], "batch_id": batch, "ts": int(ts),
                         **{k: it.get(k, "") for k in ("status", "root_cause", "summary", "signature")}})
    log(f"migrating {len(kb)} learned KB, {len(refl)} lessons, {len(inst)} instance summaries")
    show(admin(op="import", kb=kb, reflections=refl, instances=inst))
    import time
    while True:  # import runs LLM consolidation / reflection steps in the runtime -> async
        time.sleep(15)
        job = admin(op="job", name="import")
        if not job.get("running"):
            show(job)
            break
        log("import running ...")
