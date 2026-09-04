#!/usr/bin/env python3
"""Delete resources recorded by deploy_real.py.

This is intentionally not run automatically. Review .deployment.json first and invoke
this script only when the demo is no longer needed.
"""

from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STATE_FILE = ROOT / ".deployment.json"


def aws(profile: str, region: str, service: str, operation: str, *args: str) -> None:
    command = [
        "aws",
        service,
        operation,
        *args,
        "--region",
        region,
        "--profile",
        profile,
        "--no-cli-pager",
    ]
    completed = subprocess.run(command, text=True, capture_output=True, check=False)
    if completed.returncode and "ResourceNotFound" not in completed.stderr and "NotFound" not in completed.stderr:
        raise RuntimeError(f"Cleanup failed: {' '.join(command[:4])}\n{completed.stderr}")


def main() -> None:
    if not STATE_FILE.exists():
        raise SystemExit(f"No deployment state found at {STATE_FILE}")
    state = json.loads(STATE_FILE.read_text())
    created = state["created"]
    profile = state["profile"]
    region = state["region"]

    if created.get("targetId"):
        aws(
            profile,
            region,
            "bedrock-agentcore-control",
            "delete-gateway-target",
            "--gateway-identifier",
            created["gatewayId"],
            "--target-id",
            created["targetId"],
        )
        time.sleep(10)
    if created.get("gatewayId"):
        aws(
            profile,
            region,
            "bedrock-agentcore-control",
            "delete-gateway",
            "--gateway-identifier",
            created["gatewayId"],
        )
        time.sleep(10)
    for provider_key in ("oauthProviderNameV2", "oauthProviderName"):
        if created.get(provider_key):
            aws(
                profile,
                region,
                "bedrock-agentcore-control",
                "delete-oauth2-credential-provider",
                "--name",
                created[provider_key],
            )
    if created.get("testWorkloadName"):
        aws(
            profile,
            region,
            "bedrock-agentcore-control",
            "delete-workload-identity",
            "--name",
            created["testWorkloadName"],
        )
    if created.get("lambdaFunctionName"):
        aws(profile, region, "lambda", "delete-function", "--function-name", created["lambdaFunctionName"])
    if created.get("lambdaRoleName"):
        aws(
            profile,
            region,
            "iam",
            "delete-role-policy",
            "--role-name",
            created["lambdaRoleName"],
            "--policy-name",
            "agentcore-obo-demo-runtime",
        )
        aws(profile, region, "iam", "delete-role", "--role-name", created["lambdaRoleName"])
    if created.get("gatewayRoleName"):
        aws(
            profile,
            region,
            "iam",
            "delete-role-policy",
            "--role-name",
            created["gatewayRoleName"],
            "--policy-name",
            "agentcore-obo-demo-gateway",
        )
        aws(profile, region, "iam", "delete-role", "--role-name", created["gatewayRoleName"])
    if created.get("kmsAlias"):
        aws(profile, region, "kms", "delete-alias", "--alias-name", created["kmsAlias"])
    if created.get("kmsKeyArn"):
        aws(
            profile,
            region,
            "kms",
            "schedule-key-deletion",
            "--key-id",
            created["kmsKeyArn"],
            "--pending-window-in-days",
            "7",
        )
    print(f"Cleanup requested. KMS deletion is scheduled for 7 days. State retained at {STATE_FILE}")


if __name__ == "__main__":
    main()
