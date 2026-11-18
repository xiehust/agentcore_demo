"""Deploy the reroute PoC: 2 Lambdas, 2 IAM roles, 1 Gateway (AWS_IAM inbound), 2 targets.

Writes resource IDs to state.json so test.py / cleanup.py can find them.
"""

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
        time.sleep(10)  # IAM propagation
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
                time.sleep(5)  # role not assumable yet
    lam.get_waiter("function_active_v2").wait(FunctionName=name)
    lam.get_waiter("function_updated_v2").wait(FunctionName=name)
    return lam.get_function(FunctionName=name)["Configuration"]["FunctionArn"]


# 1. Lambda execution role (logs only)
lambda_role = ensure_role(
    f"{PREFIX}-lambda-role",
    {"Version": "2012-10-17", "Statement": [{"Effect": "Allow", "Principal": {"Service": "lambda.amazonaws.com"}, "Action": "sts:AssumeRole"}]},
    managed=["arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"],
)

# 2. Lambdas
interceptor_arn = ensure_lambda(f"{PREFIX}-interceptor", HERE / "lambdas/interceptor.py", lambda_role,
                                extra=[HERE / "lambdas/normalize.py"])
mockexa_arn = ensure_lambda(f"{PREFIX}-mockexa", HERE / "lambdas/mock_exa.py", lambda_role)
state.update(interceptor_arn=interceptor_arn, mockexa_arn=mockexa_arn)
save()

# 3. Gateway service role: invoke the two Lambdas + Web Search, nothing else
gw_role = ensure_role(
    f"{PREFIX}-gateway-role",
    {"Version": "2012-10-17", "Statement": [{
        "Effect": "Allow", "Principal": {"Service": "bedrock-agentcore.amazonaws.com"}, "Action": "sts:AssumeRole",
        "Condition": {"StringEquals": {"aws:SourceAccount": ACCOUNT},
                      "ArnLike": {"aws:SourceArn": f"arn:aws:bedrock-agentcore:{REGION}:{ACCOUNT}:*"}},
    }]},
    inline={"Version": "2012-10-17", "Statement": [
        {"Effect": "Allow", "Action": "lambda:InvokeFunction", "Resource": [interceptor_arn, mockexa_arn]},
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
        time.sleep(5)
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
    time.sleep(5)
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

if "mockexa_target_id" not in state:
    t = ctl.create_gateway_target(
        gatewayIdentifier=gid, name="mockexa",
        targetConfiguration={"mcp": {"lambda": {
            "lambdaArn": mockexa_arn,
            "toolSchema": {"inlinePayload": [{
                "name": "web_search_exa",
                "description": "Mock Exa search (PoC).",
                "inputSchema": {"type": "object", "properties": {
                    "query": {"type": "string"}, "numResults": {"type": "integer"}},
                    "required": ["query"]},
            }]},
        }}},
        credentialProviderConfigurations=[{"credentialProviderType": "GATEWAY_IAM_ROLE"}],
    )
    state["mockexa_target_id"] = t["targetId"]
    save()

for key in ("websearch_target_id", "mockexa_target_id"):
    wait_ready(lambda k=key: ctl.get_gateway_target(gatewayIdentifier=gid, targetId=state[k]), key)

# 6. Real Exa MCP target, key held in AgentCore Identity (run store_exa_key.py first)
if "exa_provider_arn" in state:
    provider = ctl.get_api_key_credential_provider(name=f"{PREFIX}-exa")
    wid = ctl.get_gateway(gatewayIdentifier=gid)["workloadIdentityDetails"]["workloadIdentityArn"]
    iam.put_role_policy(
        RoleName=f"{PREFIX}-gateway-role", PolicyName="exa-identity",
        PolicyDocument=json.dumps({"Version": "2012-10-17", "Statement": [
            {"Effect": "Allow",
             "Action": ["bedrock-agentcore:GetResourceApiKey", "bedrock-agentcore:GetWorkloadAccessToken"],
             "Resource": [
                 f"arn:aws:bedrock-agentcore:{REGION}:{ACCOUNT}:token-vault/default",
                 state["exa_provider_arn"],
                 f"arn:aws:bedrock-agentcore:{REGION}:{ACCOUNT}:workload-identity-directory/default",
                 wid,
             ]},
            {"Effect": "Allow", "Action": "secretsmanager:GetSecretValue",
             "Resource": provider["apiKeySecretArn"]["secretArn"]},
        ]}),
    )
    if "exa_target_id" not in state:
        time.sleep(10)  # IAM propagation
        t = ctl.create_gateway_target(
            gatewayIdentifier=gid, name="exa",
            targetConfiguration={"mcp": {"mcpServer": {"endpoint": "https://mcp.exa.ai/mcp"}}},
            credentialProviderConfigurations=[{
                "credentialProviderType": "API_KEY",
                "credentialProvider": {"apiKeyCredentialProvider": {
                    "providerArn": state["exa_provider_arn"],
                    "credentialLocation": "HEADER",
                    "credentialParameterName": "x-api-key",
                }},
            }],
        )
        state["exa_target_id"] = t["targetId"]
        save()
    wait_ready(lambda: ctl.get_gateway_target(gatewayIdentifier=gid, targetId=state["exa_target_id"]), "exa")

print(json.dumps(state, indent=2))
