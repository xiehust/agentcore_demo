"""Create/deploy the customer-support agent as a managed AgentCore Harness.

This is the primary "create and deploy" step (replaces the Strands-on-Runtime deploy):
declares the agent as harness configuration — model + weak baseline system prompt + 5
inline-function tools — via CreateHarness/UpdateHarness. Then it warms the harness with one
invoke (so its runtime log group exists) and records the harness ARN + the observability
coordinates (service name + log group) the evaluation step needs into deployment.json.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _common import REPO_ROOT, load_deployment, new_session_id, save_deployment  # noqa: E402
from harness_agent import HARNESS_NAME, create_or_update_harness, invoke_harness_loop  # noqa: E402

sys.path.insert(0, str(REPO_ROOT))
from agent.prompts import get_active_prompt  # noqa: E402


def discover_harness_log_group(region: str) -> str | None:
    import boto3

    logs = boto3.client("logs", region_name=region)
    prefix = f"/aws/bedrock-agentcore/runtimes/harness_{HARNESS_NAME}"
    groups = logs.describe_log_groups(logGroupNamePrefix=prefix).get("logGroups", [])
    # newest first
    groups.sort(key=lambda g: g.get("creationTime", 0), reverse=True)
    return groups[0]["logGroupName"] if groups else None


def main() -> int:
    prompt = get_active_prompt()
    arn = create_or_update_harness(prompt)
    print(f"Harness ready: {arn}")

    # Warm it so the runtime log group is created, then confirm a tool call works.
    sid = new_session_id("warm")
    text, tools = invoke_harness_loop(arn, sid, "What's the status of order ORD-1001?")
    print(f"Warm invoke — tools used: {tools}\n  answer: {text[:160]}")

    dep = load_deployment()
    dep["harness_arn"] = arn
    dep["harness_name"] = HARNESS_NAME
    dep["harness_service_name"] = f"harness_{HARNESS_NAME}.DEFAULT"
    lg = discover_harness_log_group(dep["region"])
    if lg:
        dep["harness_log_group"] = lg
        print(f"Harness log group: {lg}")
    else:
        print("WARN: harness log group not found yet (may take a minute); re-run if eval can't find sessions.")
    save_deployment(dep)
    print("\ndeployment.json updated with harness eval coordinates:")
    print(json.dumps({k: dep[k] for k in dep if k.startswith("harness")}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
