"""Build the agent image, create the Runtime in VPC mode with IAM (SigV4) inbound auth, lock it to the ops VPCE.

Order matters (deny first, then use):
  1. create runtime  2. attach resource policy (Deny unless aws:SourceVpce = ops bedrock-agentcore VPCE)
  3. scenarios/verify_access.py must observe AccessDenied from the internet before batches run.
"""
import base64
import json
import subprocess
import sys
import time

sys.path.insert(0, __import__("os").path.dirname(__file__) + "/..")
from common.config import (EMBED_DIM, EMBED_MODEL_ID, OS_INDEX, API_KEY_PROVIDER, BATCH_TABLE, ECR_REPO, EXTERNAL_ID, EXTRACT_MODEL_ID, MEMORY_TABLE,  # noqa: E402
                           MODEL_ID, MODEL_REGION, OPS_ACCOUNT, REGION, RUNTIME_NAME, RUNTIME_ROLE, SPOKE_ROLE, TAGS,
                           load_state, log, role_arn, save_state, session)

ROOT = __import__("pathlib").Path(__file__).resolve().parent.parent  # build context: agent/ + agentmem/ (see .dockerignore)


def build_and_push() -> str:
    ecr = session().client("ecr")
    try:
        ecr.create_repository(repositoryName=ECR_REPO, imageScanningConfiguration={"scanOnPush": True},
                              tags=TAGS)
    except ecr.exceptions.RepositoryAlreadyExistsException:
        pass
    auth = ecr.get_authorization_token()["authorizationData"][0]
    user, pwd = base64.b64decode(auth["authorizationToken"]).decode().split(":")
    registry = auth["proxyEndpoint"].replace("https://", "")
    subprocess.run(["docker", "login", "-u", user, "--password-stdin", registry], input=pwd.encode(), check=True,
                   capture_output=True)
    tag = time.strftime("v%Y%m%d%H%M%S")
    uri = f"{registry}/{ECR_REPO}:{tag}"
    subprocess.run(["docker", "build", "--platform", "linux/arm64", "-f", str(ROOT / "agent/Dockerfile"), "-t", uri, str(ROOT)], check=True)
    subprocess.run(["docker", "push", uri], check=True, capture_output=True)
    log("pushed", uri)
    return uri


def main():
    st = load_state()
    ac = session().client("bedrock-agentcore-control")
    uri = build_and_push()
    env = {"MODEL_ID": MODEL_ID, "EXTRACT_MODEL_ID": EXTRACT_MODEL_ID, "MODEL_REGION": MODEL_REGION,
           "API_KEY_PROVIDER": API_KEY_PROVIDER, "BATCH_TABLE": BATCH_TABLE, "MEMORY_TABLE": MEMORY_TABLE,
           "SPOKE_ROLE": SPOKE_ROLE, "EXTERNAL_ID": EXTERNAL_ID, "AWS_REGION": REGION, "RERUN_TIMEOUT": "600", "ENABLE_PROBE": "1",
           "OS_ENDPOINT": st["os_endpoint"], "OS_INDEX": OS_INDEX, "EMBED_MODEL_ID": EMBED_MODEL_ID, "EMBED_DIM": str(EMBED_DIM)}
    if st.get("workload_identity"):  # SigV4 calls without runtimeUserId carry no workload token -> agent fetches its own
        env["WORKLOAD_NAME"] = st["workload_identity"].rsplit("/", 1)[-1]
    common = dict(
        agentRuntimeArtifact={"containerConfiguration": {"containerUri": uri}},
        roleArn=role_arn(RUNTIME_ROLE),
        networkConfiguration={"networkMode": "VPC", "networkModeConfig": {
            "subnets": st["private_subnets"], "securityGroups": [st["agent_sg"]]}},
        protocolConfiguration={"serverProtocol": "HTTP"},
        # single-instance diagnosis is minutes; free the microVM soon after, cap far below the 8h maximum
        lifecycleConfiguration={"idleRuntimeSessionTimeout": 600, "maxLifetime": 7200},
        environmentVariables=env,
    )
    if st.get("runtime_id"):
        r = ac.update_agent_runtime(agentRuntimeId=st["runtime_id"], **common)
        log("updated runtime", st["runtime_id"], "version", r.get("agentRuntimeVersion"))
    else:
        r = ac.create_agent_runtime(agentRuntimeName=RUNTIME_NAME, description="Patch failure diagnosis agent", tags={"Project": "patchdemo"}, **common)
        save_state(runtime_id=r["agentRuntimeId"], runtime_arn=r["agentRuntimeArn"])
        log("created runtime", r["agentRuntimeArn"])
    rid = r["agentRuntimeId"]
    for _ in range(90):
        d = ac.get_agent_runtime(agentRuntimeId=rid)
        if d["status"] in ("READY", "CREATE_FAILED", "UPDATE_FAILED"):
            break
        time.sleep(10)
    log("runtime status", d["status"], d.get("failureReason", ""))
    if d["status"] != "READY":
        sys.exit(1)
    save_state(workload_identity=d.get("workloadIdentityDetails", {}).get("workloadIdentityArn"))

    # Resource policy: inbound only through the ops VPC's bedrock-agentcore interface endpoint.
    vpce = st["vpce"]["bedrock-agentcore"]
    rarn = d["agentRuntimeArn"]
    for res in (rarn, f"{rarn}/runtime-endpoint/DEFAULT"):
        policy = {"Version": "2012-10-17", "Statement": [
            {"Sid": "AllowOpsInvokers", "Effect": "Allow",
             "Principal": {"AWS": [role_arn("PatchOrchestratorLambdaRole"), role_arn("PatchOnCallRole")]},
             "Action": ["bedrock-agentcore:InvokeAgentRuntime", "bedrock-agentcore:InvokeAgentRuntimeForUser"], "Resource": res},
            # Deny EVERY action on this runtime from outside the VPCE (invoke, command, stop session, ...), not only invoke.
            {"Sid": "DenyOutsideOpsVpce", "Effect": "Deny", "Principal": "*",
             "Action": "bedrock-agentcore:*", "Resource": res,
             "Condition": {"StringNotEquals": {"aws:SourceVpce": vpce}}}]}
        ac.put_resource_policy(resourceArn=res, policy=json.dumps(policy))
        log("resource policy attached", res.split(":")[-1])


if __name__ == "__main__":
    main()
