"""Human take-over of a NEEDS_HUMAN instance: same runtimeSessionId, conversation restored from self-built STM.

Path = ops portal backend (here: the orchestrator Lambda's portal_chat action, inside the VPC) acting as the
on-call engineer's IAM Identity Center role -> InvokeAgentRuntime with runtimeUserId=<engineer>.
usage: takeover.py <instance-name> "<message>" [operator]
"""
import json
import os
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
from common.config import LAMBDA_NAME, load_state, session  # noqa: E402

st = load_state()
name, msg = sys.argv[1], sys.argv[2]
operator = sys.argv[3] if len(sys.argv) > 3 else "zhang.san"
ev = {"action": "portal_chat", "batchId": os.environ.get("BATCH", st["last_batch"]), "instanceId": st["targets"][name]["instance_id"],
      "message": msg, "operator": operator}
r = session().client("lambda").invoke(FunctionName=LAMBDA_NAME, Payload=json.dumps(ev).encode())
print(json.dumps(json.loads(r["Payload"].read()), indent=1, ensure_ascii=False))
