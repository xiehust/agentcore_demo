"""
Deploy agent_runtime.py to AgentCore Runtime.

Uses bedrock-agentcore-starter-toolkit to configure + launch the agent.
All PAYMENT_* IDs from .env.local are passed to the container as env vars.

Usage:
    python deploy.py              # configure + launch (creates or updates runtime)

Side effects:
    - Creates an ECR repository (first deploy)
    - Creates an IAM execution role (first deploy) -- auto-created, but we
      attach an extra inline policy for payment data-plane permissions
    - Writes the deployed runtime ARN to .env.local as RUNTIME_AGENT_ARN
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

import boto3
from botocore.exceptions import ClientError
from dotenv import dotenv_values, load_dotenv

from bedrock_agentcore_starter_toolkit import Runtime


ROOT = Path(__file__).parent
ENV_LOCAL = ROOT / ".env.local"

# Minimum permissions the runtime needs on top of the toolkit default role:
# - payment data plane (process payment, session, instrument reads)
# - Bedrock model invoke (for the Claude model Strands Agent uses)
EXTRA_POLICY = {
    "Version": "2012-10-17",
    "Statement": [
        {
            "Sid": "PaymentDataPlane",
            "Effect": "Allow",
            "Action": [
                "bedrock-agentcore:ProcessPayment",
                "bedrock-agentcore:GetPaymentSession",
                "bedrock-agentcore:GetPaymentInstrument",
                "bedrock-agentcore:GetPaymentInstrumentBalance",
                "bedrock-agentcore:ListPaymentInstruments",
                "bedrock-agentcore:ListPaymentSessions",
            ],
            "Resource": "*",
        },
        {
            "Sid": "BedrockInvoke",
            "Effect": "Allow",
            "Action": [
                "bedrock:InvokeModel",
                "bedrock:InvokeModelWithResponseStream",
            ],
            "Resource": "*",
        },
    ],
}


def _write_local(updates: dict[str, str]) -> None:
    existing: dict[str, str] = {}
    if ENV_LOCAL.exists():
        for k, v in dotenv_values(ENV_LOCAL).items():
            if v is not None:
                existing[k] = v
    existing.update(updates)
    ENV_LOCAL.write_text("\n".join(f"{k}={v}" for k, v in existing.items()) + "\n")
    for k, v in updates.items():
        os.environ[k] = v
        print(f"  wrote {k} to .env.local")


def _runtime_env_vars() -> dict[str, str]:
    """The environment variables the container needs to run the agent."""
    keys = [
        "PAYMENT_MANAGER_ARN",
        "PAYMENT_INSTRUMENT_ID",
        "PAYMENT_SESSION_ID",
        "USER_ID",
        "NETWORK_PREFERENCES",
    ]
    out = {}
    for k in keys:
        v = os.environ.get(k)
        if v:
            out[k] = v
    # AgentCore auto-injects AWS_REGION in the container; no need to pass it.
    missing = [k for k in ("PAYMENT_MANAGER_ARN", "PAYMENT_INSTRUMENT_ID",
                           "PAYMENT_SESSION_ID", "USER_ID") if k not in out]
    if missing:
        sys.exit(f"ERROR: missing env vars for runtime: {missing}. "
                 f"Run `python setup.py all` first.")
    return out


def _attach_policy(role_arn: str, region: str) -> None:
    """Attach EXTRA_POLICY to the auto-created execution role."""
    if not role_arn:
        return
    role_name = role_arn.rsplit("/", 1)[-1]
    iam = boto3.client("iam", region_name=region)
    try:
        iam.put_role_policy(
            RoleName=role_name,
            PolicyName="AgentCorePaymentDemoExtras",
            PolicyDocument=json.dumps(EXTRA_POLICY),
        )
        print(f"  attached inline policy 'AgentCorePaymentDemoExtras' to {role_name}")
    except ClientError as e:
        print(f"  WARN: could not attach inline policy: {e}")


def deploy() -> dict[str, Any]:
    region = os.environ.get("AWS_REGION", "us-west-2")
    agent_name = os.environ.get("RUNTIME_AGENT_NAME", "agentcorePaymentDemo")

    runtime = Runtime()

    print(f"Configuring runtime '{agent_name}' in {region}...")
    cfg = runtime.configure(
        entrypoint="agent_runtime.py",
        auto_create_execution_role=True,
        auto_create_ecr=True,
        requirements_file="requirements.txt",
        region=region,
        agent_name=agent_name,
        disable_otel=False,
    )
    print(f"  configure OK. execution role: {getattr(cfg, 'execution_role', None)}")

    print("Launching (build + deploy, typically 2-5 minutes)...")
    launch = runtime.launch(env_vars=_runtime_env_vars())

    agent_arn = getattr(launch, "agent_arn", None) or getattr(launch, "agent_runtime_arn", None)
    agent_id = getattr(launch, "agent_id", None) or getattr(launch, "agent_runtime_id", None)

    if not agent_arn:
        print("WARN: could not extract agent ARN from launch result; printing full object:")
        print(launch)
        return {}

    print(f"  agent ARN: {agent_arn}")
    updates = {"RUNTIME_AGENT_ARN": agent_arn}
    if agent_id:
        updates["RUNTIME_AGENT_ID"] = agent_id
    _write_local(updates)

    # The auto-created execution role only has minimal permissions. Fetch it
    # from the runtime description and add payment + Bedrock permissions.
    role_arn = _fetch_runtime_role(agent_arn, region)
    if role_arn:
        _attach_policy(role_arn, region)

    return {"agent_arn": agent_arn, "agent_id": agent_id, "role_arn": role_arn}


def _fetch_runtime_role(agent_arn: str, region: str) -> str | None:
    ctrl = boto3.client("bedrock-agentcore-control", region_name=region)
    runtime_id = agent_arn.rsplit("/", 1)[-1]
    try:
        resp = ctrl.get_agent_runtime(agentRuntimeId=runtime_id)
        return resp.get("roleArn")
    except ClientError as e:
        print(f"  WARN: could not fetch runtime role: {e}")
        return None


def main() -> None:
    argparse.ArgumentParser(description=__doc__,
                            formatter_class=argparse.RawDescriptionHelpFormatter
                            ).parse_args()
    load_dotenv(ROOT / ".env")
    load_dotenv(ENV_LOCAL, override=True)

    deploy()
    print("\nReady. Test with: python invoke.py")


if __name__ == "__main__":
    main()
