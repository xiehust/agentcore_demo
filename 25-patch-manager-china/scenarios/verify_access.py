"""Q1 checks: inbound only via VPCE, spoke entry only via STS VPCE, agent credentials pinned to one instance.

outside (this laptop, admin credentials, public endpoints) -> must be DENIED
inside  (orchestrator Lambda + Runtime, ops VPC)            -> must be ALLOWED, with IAM negative cases denied
"""
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
from botocore.exceptions import ClientError  # noqa: E402
from common.config import EXTERNAL_ID, LAMBDA_NAME, SPOKE_ROLE, load_state, role_arn, session  # noqa: E402

st = load_state()
T = st["targets"]
res = {}


def outside(name, fn):
    try:
        fn()
        res[name] = "UNEXPECTED ALLOW"
    except ClientError as e:
        res[name] = f"denied ({e.response['Error']['Code']}): {e.response['Error'].get('Message', '')[:110]}"


ops = session()
outside("outside: InvokeAgentRuntime from internet (admin role)", lambda: ops.client("bedrock-agentcore").invoke_agent_runtime(
    agentRuntimeArn=st["runtime_arn"], runtimeSessionId="outside-probe-".ljust(40, "o"), payload=b'{"mode":"selftest"}'))
outside("outside: AssumeRole spoke from internet (correct ExternalId)", lambda: ops.client("sts").assume_role(
    RoleArn=role_arn(SPOKE_ROLE, T["pd-rpm-lock"]["account"]), RoleSessionName="outside", ExternalId=EXTERNAL_ID,
    Tags=[{"Key": "TargetInstance", "Value": T["pd-rpm-lock"]["instance_id"]}]))

targets = {"account": T["pd-rpm-lock"]["account"], "pinned": T["pd-rpm-lock"]["instance_id"], "other": T["pd-repo-broken"]["instance_id"],
           "ok_account": T["pd-healthy"]["account"], "ok_instance": T["pd-healthy"]["instance_id"]}
r = ops.client("lambda").invoke(FunctionName=LAMBDA_NAME, Payload=json.dumps({"action": "probe", "targets": targets, "models": sys.argv[1:]}).encode())
inside = json.loads(r["Payload"].read())
for k, v in res.items():
    print(f"{k:62s} {v}")
print(json.dumps({"inside_vpc": inside}, indent=2, ensure_ascii=False))
