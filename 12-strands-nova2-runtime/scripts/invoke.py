#!/usr/bin/env python3
"""
Invoke the deployed PUBLIC AgentCore Runtime.

Usage:
  python scripts/invoke.py                      # run the built-in prompt suite
  python scripts/invoke.py "your prompt here"   # one-off prompt
"""

import json
import os
import sys
import time
import uuid

import boto3

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# The first four exercise the Lambda-target chain; the last one names an
# apiGateway-target tool so the API Gateway + VPC Link chain is covered too.
PROMPTS = [
    "How many orders are pending, and what are their order references?",
    "Which order was cancelled and how much was it for?",
    "What MySQL version is the database running, and what host is it on?",
    "What is the total value of all shipped orders?",
    "Call rdsApi___getDbInfo and report the ec2_private_ip field verbatim.",
]


def load_runtime():
    with open(os.path.join(ROOT, "runtime.json")) as fh:
        return json.load(fh)


def invoke(client, arn, prompt, session_id):
    started = time.time()
    resp = client.invoke_agent_runtime(
        agentRuntimeArn=arn,
        runtimeSessionId=session_id,
        payload=json.dumps({"prompt": prompt}).encode(),
        contentType="application/json",
        accept="application/json",
    )
    body = resp["response"].read()
    elapsed = (time.time() - started) * 1000
    try:
        return json.loads(body), elapsed
    except ValueError:
        return {"raw": body.decode(errors="replace")}, elapsed


def main():
    rt = load_runtime()
    client = boto3.client("bedrock-agentcore", region_name=rt["region"])
    prompts = [sys.argv[1]] if len(sys.argv) > 1 else PROMPTS

    print(f"runtime : {rt['runtimeName']}  ({rt['networkMode']} network mode)")
    print(f"model   : {rt['modelId']}")
    print(f"gateway : {rt['gatewayUrl']}\n")

    results = []
    for i, prompt in enumerate(prompts, 1):
        # A fresh session per prompt, so each run exercises a cold MCP session.
        session_id = f"verify-{uuid.uuid4().hex}"
        out, elapsed = invoke(client, rt["runtimeArn"], prompt, session_id)
        calls = [c.get("name") for c in out.get("tool_calls", [])]
        print(f"[{i}] {prompt}")
        print(f"    answer     : {str(out.get('result', out)).strip()}")
        print(f"    tools used : {calls or 'NONE'}")
        print(f"    latency    : {elapsed:.0f} ms\n")
        results.append({"prompt": prompt, "response": out,
                        "latency_ms": round(elapsed, 1)})

    if len(prompts) > 1:
        path = os.path.join(ROOT, "results", "invocations.json")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as fh:
            json.dump({"runtime": rt, "results": results}, fh, indent=2)
        print(f"Wrote {path}")

    # Non-zero exit if any prompt failed or answered without touching a tool.
    bad = [r for r in results
           if "error" in r["response"] or not r["response"].get("tool_calls")]
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
