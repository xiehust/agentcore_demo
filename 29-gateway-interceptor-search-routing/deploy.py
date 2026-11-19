"""Deploy the reroute PoC: 1 Lambda (interceptor), 2 IAM roles, 1 Gateway (AWS_IAM inbound), targets.

    .venv/bin/python deploy.py [--cn-provider anysearch|bocha|exa]

--cn-provider picks where Chinese queries go (sets the interceptor's DST_TOOL); default anysearch.
Writes resource IDs to state.json so the test scripts / cleanup.py can find them.
"""

import argparse
import io
import json
import time
import zipfile
from pathlib import Path

import boto3

REGION = "us-east-1"
PREFIX = "ic-reroute-poc"
HERE = Path(__file__).parent
STATE = HERE / "state.json"

ap = argparse.ArgumentParser()
ap.add_argument("--cn-provider", choices=["anysearch", "bocha", "exa"], default="anysearch")
CN_PROVIDER = ap.parse_args().cn_provider

sess = boto3.Session(region_name=REGION)
iam = sess.client("iam")
lam = sess.client("lambda")
ctl = sess.client("bedrock-agentcore-control")
ACCOUNT = sess.client("sts").get_caller_identity()["Account"]

state = json.loads(STATE.read_text()) if STATE.exists() else {}


def save():
    STATE.write_text(json.dumps(state, indent=2))


def ensure_role(name, trust, inline=None, managed=None):
    try:
        arn = iam.get_role(RoleName=name)["Role"]["Arn"]
    except iam.exceptions.NoSuchEntityException:
        arn = iam.create_role(RoleName=name, AssumeRolePolicyDocument=json.dumps(trust))["Role"]["Arn"]
        time.sleep(10)  # nosemgrep: arbitrary-sleep -- wait for new IAM role to propagate
    if inline:
        iam.put_role_policy(RoleName=name, PolicyName="inline", PolicyDocument=json.dumps(inline))
    for m in managed or []:
        iam.attach_role_policy(RoleName=name, PolicyArn=m)
    return arn


def zip_of(*paths):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for path in paths:
            z.write(path, path.name)
    return buf.getvalue()


def ensure_lambda(name, src, role_arn, extra=()):
    code = zip_of(src, *extra)
    try:
        lam.get_function(FunctionName=name)
        lam.update_function_code(FunctionName=name, ZipFile=code)
    except lam.exceptions.ResourceNotFoundException:
        for i in range(10):
            try:
                lam.create_function(
                    FunctionName=name, Runtime="python3.12", Role=role_arn,
                    Handler=f"{src.stem}.handler", Code={"ZipFile": code}, Timeout=10,
                )
                break
            except lam.exceptions.InvalidParameterValueException:
                time.sleep(5)  # nosemgrep: arbitrary-sleep -- role not assumable yet; retry
    lam.get_waiter("function_active_v2").wait(FunctionName=name)
    lam.get_waiter("function_updated_v2").wait(FunctionName=name)
    return lam.get_function(FunctionName=name)["Configuration"]["FunctionArn"]


# 1. Lambda execution role (logs only)
lambda_role = ensure_role(
    f"{PREFIX}-lambda-role",
    {"Version": "2012-10-17", "Statement": [{"Effect": "Allow", "Principal": {"Service": "lambda.amazonaws.com"}, "Action": "sts:AssumeRole"}]},
    managed=["arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"],
)

# 2. Lambda (interceptor)
interceptor_arn = ensure_lambda(f"{PREFIX}-interceptor", HERE / "lambdas/interceptor.py", lambda_role,
                                extra=[HERE / "lambdas/normalize.py"])
state.update(interceptor_arn=interceptor_arn)
save()

# 3. Gateway service role: invoke the interceptor + Web Search, nothing else
gw_role = ensure_role(
    f"{PREFIX}-gateway-role",
    {"Version": "2012-10-17", "Statement": [{
        "Effect": "Allow", "Principal": {"Service": "bedrock-agentcore.amazonaws.com"}, "Action": "sts:AssumeRole",
        "Condition": {"StringEquals": {"aws:SourceAccount": ACCOUNT},
                      "ArnLike": {"aws:SourceArn": f"arn:aws:bedrock-agentcore:{REGION}:{ACCOUNT}:*"}},
    }]},
    inline={"Version": "2012-10-17", "Statement": [
        {"Effect": "Allow", "Action": "lambda:InvokeFunction", "Resource": [interceptor_arn]},
        {"Effect": "Allow", "Action": "bedrock-agentcore:InvokeWebSearch",
         "Resource": f"arn:aws:bedrock-agentcore:{REGION}:aws:tool/web-search.v1"},
    ]},
)

# 4. Gateway with REQUEST + RESPONSE interceptor, AWS_IAM inbound auth
INTERCEPTION_POINTS = {"REQUEST", "RESPONSE"}
interceptor_config = [{
    "interceptor": {"lambda": {"arn": interceptor_arn}},
    "interceptionPoints": sorted(INTERCEPTION_POINTS),
    "inputConfiguration": {"passRequestHeaders": False},
}]
if "gateway_id" not in state:
    gw = ctl.create_gateway(
        name=f"{PREFIX}-gw", roleArn=gw_role, protocolType="MCP", authorizerType="AWS_IAM",
        interceptorConfigurations=interceptor_config,
    )
    state.update(gateway_id=gw["gatewayId"], gateway_url=gw["gatewayUrl"])
    save()


def wait_ready(get, label):
    for _ in range(60):
        s = get()["status"]
        if s == "READY":
            return
        if "FAIL" in s:
            raise RuntimeError(f"{label} status {s}: {get()}")
        time.sleep(5)  # nosemgrep: arbitrary-sleep -- polling interval while waiting for READY
    raise TimeoutError(label)


gid = state["gateway_id"]
wait_ready(lambda: ctl.get_gateway(gatewayIdentifier=gid), "gateway")

# 4b. Make sure the interceptor runs at both REQUEST and RESPONSE
gw = ctl.get_gateway(gatewayIdentifier=gid)
if set(gw["interceptorConfigurations"][0]["interceptionPoints"]) != INTERCEPTION_POINTS:
    ctl.update_gateway(
        gatewayIdentifier=gid, name=gw["name"], roleArn=gw["roleArn"],
        protocolType=gw["protocolType"], authorizerType=gw["authorizerType"],
        interceptorConfigurations=interceptor_config,
    )
    time.sleep(5)  # nosemgrep: arbitrary-sleep -- let update_gateway leave READY before polling
    wait_ready(lambda: ctl.get_gateway(gatewayIdentifier=gid), "gateway update")

# 5. Targets
if "websearch_target_id" not in state:
    t = ctl.create_gateway_target(
        gatewayIdentifier=gid, name="websearch",
        targetConfiguration={"mcp": {"connector": {
            "source": {"connectorId": "web-search"},
            "configurations": [{"name": "WebSearch", "parameterValues": {}}],
        }}},
        credentialProviderConfigurations=[{"credentialProviderType": "GATEWAY_IAM_ROLE"}],
    )
    state["websearch_target_id"] = t["targetId"]
    save()

wait_ready(lambda: ctl.get_gateway_target(gatewayIdentifier=gid, targetId=state["websearch_target_id"]), "websearch")

# 6. Third-party MCP targets, keys held in AgentCore Identity
#    (run store_anysearch_key.py / store_bocha_key.py / store_exa_key.py first; a target is skipped if its key is not stored)
MCP_TARGETS = {
    "exa": {"endpoint": "https://mcp.exa.ai/mcp", "header": "x-api-key", "prefix": None},
    "anysearch": {"endpoint": "https://api.anysearch.com/mcp", "header": "Authorization", "prefix": "Bearer "},
    "bocha": {"endpoint": "https://mcp.bochaai.com/mcp", "header": "Authorization", "prefix": "Bearer "},
}
wid = ctl.get_gateway(gatewayIdentifier=gid)["workloadIdentityDetails"]["workloadIdentityArn"]


def ensure_mcp_target(name, cfg):
    provider_arn = state[f"{name}_provider_arn"]
    provider = ctl.get_api_key_credential_provider(name=f"{PREFIX}-{name}")
    iam.put_role_policy(
        RoleName=f"{PREFIX}-gateway-role", PolicyName=f"{name}-identity",
        PolicyDocument=json.dumps({"Version": "2012-10-17", "Statement": [
            {"Effect": "Allow",
             "Action": ["bedrock-agentcore:GetResourceApiKey", "bedrock-agentcore:GetWorkloadAccessToken"],
             "Resource": [
                 f"arn:aws:bedrock-agentcore:{REGION}:{ACCOUNT}:token-vault/default",
                 provider_arn,
                 f"arn:aws:bedrock-agentcore:{REGION}:{ACCOUNT}:workload-identity-directory/default",
                 wid,
             ]},
            {"Effect": "Allow", "Action": "secretsmanager:GetSecretValue",
             "Resource": provider["apiKeySecretArn"]["secretArn"]},
        ]}),
    )
    if f"{name}_target_id" not in state:
        time.sleep(10)  # nosemgrep: arbitrary-sleep -- wait for the identity policy to propagate
        api_key_cfg = {"providerArn": provider_arn, "credentialLocation": "HEADER",
                       "credentialParameterName": cfg["header"]}
        if cfg["prefix"]:
            api_key_cfg["credentialPrefix"] = cfg["prefix"]
        t = ctl.create_gateway_target(
            gatewayIdentifier=gid, name=name,
            targetConfiguration={"mcp": {"mcpServer": {"endpoint": cfg["endpoint"]}}},
            credentialProviderConfigurations=[{
                "credentialProviderType": "API_KEY",
                "credentialProvider": {"apiKeyCredentialProvider": api_key_cfg},
            }],
        )
        state[f"{name}_target_id"] = t["targetId"]
        save()
    wait_ready(lambda: ctl.get_gateway_target(gatewayIdentifier=gid, targetId=state[f"{name}_target_id"]), name)


for name, cfg in MCP_TARGETS.items():
    if f"{name}_provider_arn" in state:
        ensure_mcp_target(name, cfg)

# 7. Point the interceptor's Chinese route at the chosen provider
DST_TOOLS = {"exa": "exa___web_search_exa", "anysearch": "anysearch___search", "bocha": "bocha___bocha_web_search"}
cn = CN_PROVIDER
if f"{cn}_target_id" not in state:
    raise SystemExit(f"--cn-provider {cn}: target not deployed, store its key first")
lam.update_function_configuration(FunctionName=f"{PREFIX}-interceptor",
                                  Environment={"Variables": {"DST_TOOL": DST_TOOLS[cn]}})
lam.get_waiter("function_updated_v2").wait(FunctionName=f"{PREFIX}-interceptor")
state["cn_provider"] = cn
save()

print(json.dumps(state, indent=2))
