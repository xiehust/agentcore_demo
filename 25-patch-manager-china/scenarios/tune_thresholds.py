"""Offline calibration of the LTM cosine gates (no OpenSearch needed): embeds the KB / lessons and the real recall
queries of earlier batches (patch status + SSM output, exactly what the agent embeds) with Titan V2 and prints the
similarity matrix. usage: tune_thresholds.py <batchId> [<batchId> ...]"""
import json
import math
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "agent"))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "agentmem"))
from boto3.dynamodb.conditions import Key  # noqa: E402
from common.config import BATCH_TABLE, EMBED_DIM, EMBED_MODEL_ID, GLOBAL_PROFILE, MODEL_REGION, SPOKE_PROFILES, session  # noqa: E402

br = session(GLOBAL_PROFILE, MODEL_REGION).client("bedrock-runtime")


def embed(t):
    r = br.invoke_model(modelId=EMBED_MODEL_ID, body=json.dumps({"inputText": t[:20000], "dimensions": EMBED_DIM, "normalize": True}))
    return json.loads(r["body"].read())["embedding"]


def cos(a, b):
    return sum(x * y for x, y in zip(a, b)) / (math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b)))


from agentmem import records as R  # noqa: E402  (pure helpers only: query_digest, filter_keywords, kb_text)
from memory_profile import PATCH_PROFILE  # noqa: E402

kb = json.loads((pathlib.Path(__file__).resolve().parent.parent / "seed/knowledge.json").read_text())
learned = [it for it in session().resource("dynamodb").Table("patchdemo-memory").query(
    KeyConditionExpression=Key("pk").eq("KB"))["Items"] if not it.get("verified")]
for it in learned:
    it["signature"] = it["sk"]
    it["keywords"] = R.filter_keywords(it.get("keywords", []), PATCH_PROFILE)[0]
entries = kb + learned
ev = {e["signature"]: embed(R.kb_text(e)) for e in entries}

bt = session().resource("dynamodb").Table(BATCH_TABLE)
rows = []
for b in sys.argv[1:]:
    for it in bt.query(KeyConditionExpression=Key("pk").eq(b) & Key("sk").begins_with("INST#"))["Items"]:
        if it["status"] == "PATCH_OK":
            continue
        out = ""
        if it.get("patch_command_id"):
            ssm = session(SPOKE_PROFILES[it["account"]]).client("ssm")
            inv = ssm.list_command_invocations(CommandId=it["patch_command_id"], InstanceId=it["instance_id"], Details=True)["CommandInvocations"]
            out = " ".join(p.get("Output", "") for i in inv for p in i.get("CommandPlugins", []))
        q = f"{it.get('patch_status', '')}\n{out}"
        rows.append((f"{b[:2]}:{it['name']}", it.get("signature"), embed(R.query_digest(q, PATCH_PROFILE)), R.query_digest(q, PATCH_PROFILE)[:160]))

print("query".ljust(22), "truth".ljust(32), " ".join(s[:14].rjust(14) for s in ev))
for name, truth, v, dig in rows:
    print(name.ljust(22), str(truth)[:32].ljust(32), " ".join(f"{cos(v, ev[s]):14.3f}" for s in ev))
refl = session().resource("dynamodb").Table("patchdemo-memory").query(KeyConditionExpression=Key("pk").eq("REFLECTION"))["Items"]
rv = [(r["sk"].split("#")[0] + "#" + r["sk"].split("#")[1][:2], embed(f"{r['signature']}: {r['lesson']}")) for r in refl]
print("\nquery vs lessons (top 3):")
for name, truth, v, dig in rows:
    top = sorted(((cos(v, e), n) for n, e in rv), reverse=True)[:3]
    print(" ", name.ljust(22), "  ".join(f"{n}={c:.3f}" for c, n in top))
print("\nlesson duplicates (>0.85):")
for i, (a, va) in enumerate(rv):
    for b, vb in rv[i + 1:]:
        if cos(va, vb) > 0.85:
            print(f"  {a} ~ {b}: {cos(va, vb):.3f}")
print("\nsignature-to-signature (merge gate):")
sigs = list(ev)
for i, a in enumerate(sigs):
    for b in sigs[i + 1:]:
        print(f"  {a} ~ {b}: {cos(ev[a], ev[b]):.3f}")
