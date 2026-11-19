"""AgentCore Managed Harness (gpt-6.1-terra) that searches the web through the routing Gateway.

    .venv/bin/python harness_demo.py            # create the harness if needed, then run the two prompts
    .venv/bin/python harness_demo.py "<prompt>" # run one custom prompt

Requires deploy.py to have run (reads gateway_id from state.json). Writes harness ids to state.json.
"""

import json
import re
import sys
import time
import uuid
from pathlib import Path

import boto3

REGION = "us-east-1"
PREFIX = "ic-reroute-poc"
MODEL_ID = "us.openai.gpt-6.1-terra"
STATE = Path(__file__).parent / "state.json"
state = json.loads(STATE.read_text())

sess = boto3.Session(region_name=REGION)
iam, ctl = sess.client("iam"), sess.client("bedrock-agentcore-control")
dp = sess.client("bedrock-agentcore")
ACCOUNT = sess.client("sts").get_caller_identity()["Account"]
GATEWAY_ARN = f"arn:aws:bedrock-agentcore:{REGION}:{ACCOUNT}:gateway/{state['gateway_id']}"

SYSTEM_PROMPT = (
    "You are a research assistant. Use the web search tool for any question about facts or current "
    "information, then answer concisely in the user's language and cite the URLs you used."
)
PROMPTS = [
    "杭州西湖一年中什么时候去最好？",
    "What are AgentCore Gateway interceptors used for?",
]


def ensure_role():
    name = f"{PREFIX}-harness-role"
    trust = {"Version": "2012-10-17", "Statement": [{
        "Effect": "Allow", "Principal": {"Service": "bedrock-agentcore.amazonaws.com"}, "Action": "sts:AssumeRole",
        "Condition": {"StringEquals": {"aws:SourceAccount": ACCOUNT},
                      "ArnLike": {"aws:SourceArn": f"arn:aws:bedrock-agentcore:{REGION}:{ACCOUNT}:*"}},
    }]}
    try:
        arn = iam.get_role(RoleName=name)["Role"]["Arn"]
    except iam.exceptions.NoSuchEntityException:
        arn = iam.create_role(RoleName=name, AssumeRolePolicyDocument=json.dumps(trust))["Role"]["Arn"]
        time.sleep(10)
    model_name = MODEL_ID.split(".", 1)[1]  # openai.gpt-6.1-terra
    iam.put_role_policy(RoleName=name, PolicyName="inline", PolicyDocument=json.dumps({
        "Version": "2012-10-17", "Statement": [
            {"Sid": "Model", "Effect": "Allow",
             "Action": ["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"],
             "Resource": [f"arn:aws:bedrock:{REGION}:{ACCOUNT}:inference-profile/{MODEL_ID}",
                          f"arn:aws:bedrock:*::foundation-model/{model_name}"]},
            {"Sid": "Gateway", "Effect": "Allow", "Action": "bedrock-agentcore:InvokeGateway",
             "Resource": GATEWAY_ARN},
            {"Sid": "Logs", "Effect": "Allow",
             "Action": ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"],
             "Resource": f"arn:aws:logs:{REGION}:{ACCOUNT}:log-group:/aws/bedrock-agentcore/*"},
        ]}))
    return arn


def ensure_harness():
    if "harness_arn" in state:
        return state["harness_arn"]
    role = ensure_role()
    for _ in range(6):
        try:
            h = ctl.create_harness(
                harnessName=f"{PREFIX.replace('-', '_')}_harness",
                executionRoleArn=role,
                model={"bedrockModelConfig": {"modelId": MODEL_ID, "maxTokens": 4096}},
                systemPrompt=[{"text": SYSTEM_PROMPT}],
                tools=[{
                    "type": "agentcore_gateway", "name": "search",
                    "config": {"agentCoreGateway": {"gatewayArn": GATEWAY_ARN, "outboundAuth": {"awsIam": {}}}},
                }],
                maxIterations=8,
                memory={"disabled": {}},  # stateless demo; otherwise a managed memory is created
            )
            break
        except ctl.exceptions.ValidationException as e:
            if "assume" not in str(e).lower():
                raise
            time.sleep(10)  # role not assumable yet
    harness = h["harness"]
    state.update(harness_arn=harness["arn"], harness_id=harness["harnessId"])
    STATE.write_text(json.dumps(state, indent=2))
    for _ in range(60):
        status = ctl.get_harness(harnessId=state["harness_id"])["harness"]["status"]
        if status == "READY":
            break
        if "FAIL" in status:
            raise RuntimeError(ctl.get_harness(harnessId=state["harness_id"]))
        time.sleep(5)
    return state["harness_arn"]


def ask(harness_arn, prompt):
    print(f"\n=== {prompt}")
    resp = dp.invoke_harness(
        harnessArn=harness_arn,
        runtimeSessionId=f"demo-{uuid.uuid4()}",  # must be >= 33 chars
        messages=[{"role": "user", "content": [{"text": prompt}]}],
    )
    blocks, buf, answer = {}, {}, []
    for ev in resp["stream"]:
        if "contentBlockStart" in ev:
            idx = ev["contentBlockStart"]["contentBlockIndex"]
            start = ev["contentBlockStart"]["start"]
            blocks[idx], buf[idx] = start, []
            if "toolUse" in start:
                print(f"[tool call] {start['toolUse']['name']}", end=" ")
        elif "contentBlockDelta" in ev:
            idx = ev["contentBlockDelta"]["contentBlockIndex"]
            d = ev["contentBlockDelta"]["delta"]
            if "toolUse" in d:
                buf.setdefault(idx, []).append(d["toolUse"]["input"])
            elif "toolResult" in d:
                for r in d["toolResult"]:
                    buf.setdefault(idx, []).append(r.get("text") or json.dumps(r.get("json"), ensure_ascii=False))
            elif "text" in d:
                answer.append(d["text"])
        elif "contentBlockStop" in ev:
            idx = ev["contentBlockStop"]["contentBlockIndex"]
            kind, raw = blocks.get(idx, {}), "".join(buf.get(idx, []))
            if "toolUse" in kind:
                print(raw)
            elif "toolResult" in kind:
                print(f"[tool result] {summarize(raw)}")
        elif "messageStop" in ev and ev["messageStop"]["stopReason"] not in ("end_turn", "tool_use", "tool_result"):
            print(f"[stop] {ev['messageStop']['stopReason']}")
        for err in ("validationException", "internalServerException", "runtimeClientError"):
            if err in ev:
                print(f"[{err}] {ev[err]}")
    print("[answer]\n" + "".join(answer).strip())


def summarize(raw):
    """Show provider + first URLs for unified search results, else a short preview."""
    start = raw.find('{"provider"')
    if start >= 0:
        try:
            data, _ = json.JSONDecoder().raw_decode(raw[start:])
            return f"provider={data['provider']} urls={[r['url'] for r in data['results']][:3]}"
        except (ValueError, KeyError):
            m = re.search(r'"provider": "([^"]+)"', raw)
            urls = re.findall(r'"url": "([^"]+)"', raw)[:3]
            return f"provider={m.group(1) if m else '?'} urls={urls} (truncated/offloaded)"
    return raw[:160].replace("\n", " ")


arn = ensure_harness()
for p in sys.argv[1:] or PROMPTS:
    ask(arn, p)
