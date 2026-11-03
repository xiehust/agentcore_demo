"""Start one Step Functions patch batch and follow it until the report is written.

usage: run_batch.py [label] [--patch-timeout 600] [--agent-timeout 2400] [--max-agents 10] [--min-free-pct 15] [--min-free-mb 500]
                    [--wave-hard-timeout 0 (=3x patch-timeout)] [--dispatch-grace 180] [--batch-timeout 14400]
"""
import argparse
import json
import pathlib
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
from boto3.dynamodb.conditions import Key  # noqa: E402
from common.config import BATCH_TABLE, load_state, save_state, session  # noqa: E402

ap = argparse.ArgumentParser()
ap.add_argument("label", nargs="?", default="b")
ap.add_argument("--patch-timeout", type=int, default=600)
ap.add_argument("--agent-timeout", type=int, default=2400)
ap.add_argument("--remediation-limit", type=int, default=6)
ap.add_argument("--max-agents", type=int, default=10, help="max concurrent agent sessions; extra failed hosts queue")
ap.add_argument("--min-free-pct", type=int, default=15, help="disk-headroom precheck: min free %% on /")
ap.add_argument("--min-free-mb", type=int, default=500, help="disk-headroom precheck: min free MB on /")
ap.add_argument("--wave-hard-timeout", type=int, default=0, help="cancel hosts still queued (never InProgress) after N s; 0 = 3x patch-timeout")
ap.add_argument("--dispatch-grace", type=int, default=180, help="re-queue a dispatched host the runtime has not claimed after N s")
ap.add_argument("--batch-timeout", type=int, default=14400, help="abort the batch (all open hosts -> NEEDS_HUMAN) after N s")
a = ap.parse_args()

st = load_state()
sfn = session().client("stepfunctions")
table = session().resource("dynamodb").Table(BATCH_TABLE)
batch_id = f"{a.label}-{time.strftime('%Y%m%d-%H%M%S')}"
ex = sfn.start_execution(stateMachineArn=st["sfn_arn"], name=batch_id, input=json.dumps({
    "batchId": batch_id, "patchTimeout": a.patch_timeout, "agentTimeout": a.agent_timeout, "remediationLimit": a.remediation_limit,
    "maxAgents": a.max_agents, "minFreePct": a.min_free_pct, "minFreeMB": a.min_free_mb,
    "waveHardTimeout": a.wave_hard_timeout, "dispatchGrace": a.dispatch_grace, "batchTimeout": a.batch_timeout}))
save_state(last_batch=batch_id)
print("batch", batch_id, ex["executionArn"], flush=True)
t0, last = time.time(), None
while True:
    d = sfn.describe_execution(executionArn=ex["executionArn"])
    rows = table.query(KeyConditionExpression=Key("pk").eq(batch_id) & Key("sk").begins_with("INST#"))["Items"]
    snap = " | ".join(f"{r.get('name')}:{r['status']}" for r in sorted(rows, key=lambda r: r.get("name", "")))
    if snap != last:
        print(f"[{int(time.time() - t0):5d}s] {snap}", flush=True)
        last = snap
    if d["status"] != "RUNNING":
        break
    time.sleep(20)
print("execution", d["status"], d.get("error", ""), d.get("cause", "")[:300])
# the REPORT item is written on success, on a tripped canary breaker and on abort alike
it = table.get_item(Key={"pk": batch_id, "sk": "REPORT"}).get("Item")
if it:
    print(json.dumps(json.loads(it["report"]), indent=1, ensure_ascii=False, default=str))
