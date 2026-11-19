"""Delete everything deploy.py created (targets, gateway, interceptor Lambda, IAM roles, API key providers)."""

import json
import time
from pathlib import Path

import boto3

REGION = "us-east-1"
PREFIX = "ic-reroute-poc"
STATE = Path(__file__).parent / "state.json"
state = json.loads(STATE.read_text()) if STATE.exists() else {}

sess = boto3.Session(region_name=REGION)
ctl, lam, iam = sess.client("bedrock-agentcore-control"), sess.client("lambda"), sess.client("iam")

if state.get("harness_id"):  # created by harness_demo.py
    try:
        ctl.delete_harness(harnessId=state["harness_id"])
    except ctl.exceptions.ResourceNotFoundException:
        pass

gid = state.get("gateway_id")
if gid:
    for k in ("websearch_target_id", "exa_target_id", "anysearch_target_id", "bocha_target_id"):
        if state.get(k):
            try:
                ctl.delete_gateway_target(gatewayIdentifier=gid, targetId=state[k])
            except ctl.exceptions.ResourceNotFoundException:
                pass
    for _ in range(30):
        if not ctl.list_gateway_targets(gatewayIdentifier=gid).get("items"):
            break
        time.sleep(5)
    try:
        ctl.delete_gateway(gatewayIdentifier=gid)
    except ctl.exceptions.ResourceNotFoundException:
        pass

for name in ("exa", "anysearch", "bocha"):
    if state.get(f"{name}_provider_arn"):
        try:
            ctl.delete_api_key_credential_provider(name=f"{PREFIX}-{name}")
        except ctl.exceptions.ResourceNotFoundException:
            pass

for fn in (f"{PREFIX}-interceptor",):
    try:
        lam.delete_function(FunctionName=fn)
    except lam.exceptions.ResourceNotFoundException:
        pass

for role in (f"{PREFIX}-gateway-role", f"{PREFIX}-lambda-role", f"{PREFIX}-harness-role"):
    try:
        for p in iam.list_role_policies(RoleName=role)["PolicyNames"]:
            iam.delete_role_policy(RoleName=role, PolicyName=p)
        for p in iam.list_attached_role_policies(RoleName=role)["AttachedPolicies"]:
            iam.detach_role_policy(RoleName=role, PolicyArn=p["PolicyArn"])
        iam.delete_role(RoleName=role)
    except iam.exceptions.NoSuchEntityException:
        pass

STATE.unlink(missing_ok=True)
print("cleaned up")
