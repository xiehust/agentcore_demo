#!/usr/bin/env python3
"""Preflight: verify all real-cloud prerequisites for the AgentCore eval/opti demo.

Checks AWS identity/region, Bedrock Claude model access, CloudWatch Transaction
Search status, the AgentCore CLI, and probes boto3 for the harness / evaluation /
optimization API operations. Writes config.json and prints a report.

Hard-blocks (exit 1) ONLY on missing Bedrock Claude model access — every other
probe degrades to a recorded status so the report is always produced. Idempotent.
"""
from __future__ import annotations

import importlib.metadata as md
import json
import shutil
import subprocess
import sys
from pathlib import Path

import boto3
from botocore.exceptions import ClientError

REGION = "us-west-2"
CONFIG_PATH = Path(__file__).parent / "config.json"

# Preference order for the (cheap) agent model — matched as a substring against
# inference-profile / model ids returned by Bedrock.
MODEL_PREFS = [
    "claude-haiku-4-5",
    "claude-haiku",
    "claude-sonnet-4-6",
    "claude-sonnet",
    "claude",
]


def hr(title: str) -> None:
    print(f"\n--- {title} ---")


def check_identity(session: boto3.Session) -> dict:
    hr("AWS identity")
    sts = session.client("sts")
    ident = sts.get_caller_identity()
    print(f"AWS identity: {ident['Account']}  (arn {ident['Arn']})")
    print(f"region: {session.region_name}")
    return {"account": ident["Account"], "arn": ident["Arn"], "region": session.region_name}


def pick_model(session: boto3.Session) -> tuple[str | None, list[str]]:
    """Return (chosen_model_id, candidate_ids) preferring cheap Claude profiles."""
    bedrock = session.client("bedrock")
    candidates: list[str] = []
    # Inference profiles (cross-region ids — the right thing to invoke).
    try:
        try:
            profs = bedrock.list_inference_profiles(maxResults=1000).get(
                "inferenceProfileSummaries", []
            )
        except ClientError:
            profs = bedrock.list_inference_profiles().get("inferenceProfileSummaries", [])
        for p in profs:
            pid = p.get("inferenceProfileId", "")
            if "anthropic" in pid or "claude" in pid:
                candidates.append(pid)
    except ClientError as e:
        print(f"  (list_inference_profiles failed: {e.response['Error']['Code']})")
    # Foundation models (fallback / extra candidates).
    try:
        fms = bedrock.list_foundation_models(byProvider="anthropic").get("modelSummaries", [])
        for m in fms:
            mid = m.get("modelId", "")
            if "claude" in mid:
                candidates.append(mid)
    except ClientError as e:
        print(f"  (list_foundation_models failed: {e.response['Error']['Code']})")

    candidates = list(dict.fromkeys(candidates))  # dedupe, keep order
    chosen = None
    for pref in MODEL_PREFS:
        for c in candidates:
            if pref in c:
                chosen = c
                break
        if chosen:
            break
    return chosen, candidates


def verify_model_access(session: boto3.Session, model_id: str) -> tuple[bool, str]:
    """Tiny converse call to confirm the model is actually enabled."""
    rt = session.client("bedrock-runtime")
    try:
        rt.converse(
            modelId=model_id,
            messages=[{"role": "user", "content": [{"text": "ping"}]}],
            inferenceConfig={"maxTokens": 5},
        )
        return True, "ok"
    except ClientError as e:
        return False, f"{e.response['Error']['Code']}: {e.response['Error']['Message']}"


def check_transaction_search(session: boto3.Session) -> str:
    """Best-effort detect (and enable) X-Ray/CloudWatch Transaction Search."""
    try:
        xray = session.client("xray")
        dest = xray.get_trace_segment_destination()
        status = dest.get("Status", "")
        d = dest.get("Destination", "")
        if d == "CloudWatchLogs" and status in ("ACTIVE", "PENDING"):
            return f"on ({status})"
        # Try to enable it.
        try:
            xray.update_trace_segment_destination(Destination="CloudWatchLogs")
            return "enabling (just turned on CloudWatchLogs destination)"
        except ClientError as e:
            return f"off — enable failed: {e.response['Error']['Code']}"
    except ClientError as e:
        return f"unknown: {e.response['Error']['Code']}"
    except Exception as e:  # noqa: BLE001 - best-effort probe
        return f"unknown: {type(e).__name__}"


def probe_ops(session: boto3.Session) -> dict:
    """Record presence of the AgentCore API operations the demo needs."""
    flags: dict[str, bool] = {}
    try:
        ctrl = set(
            session.client("bedrock-agentcore-control").meta.service_model.operation_names
        )
    except Exception:  # noqa: BLE001
        ctrl = set()
    try:
        data = set(session.client("bedrock-agentcore").meta.service_model.operation_names)
    except Exception:  # noqa: BLE001
        data = set()
    flags["harness_api"] = {"CreateHarness", "GetHarness"}.issubset(ctrl) and (
        "InvokeHarness" in data
    )
    flags["runtime_api"] = ("CreateAgentRuntime" in ctrl) and ("InvokeAgentRuntime" in data)
    flags["evaluation_api"] = {"StartBatchEvaluation", "GetBatchEvaluation"}.issubset(data)
    flags["optimization_api"] = {"StartRecommendation", "GetRecommendation"}.issubset(data) and (
        "CreateConfigurationBundle" in ctrl
    )
    flags["abtest_api"] = "CreateABTest" in data
    return flags


def check_cli() -> tuple[str, str]:
    if shutil.which("agentcore"):
        try:
            ver = md.version("bedrock-agentcore-starter-toolkit")
        except md.PackageNotFoundError:
            ver = "?"
        # Confirm it runs.
        try:
            subprocess.run(["agentcore", "--help"], capture_output=True, timeout=30, check=False)
        except Exception:  # noqa: BLE001
            pass
        return "present", ver
    return "unavailable", "-"


def main() -> int:
    session = boto3.Session(region_name=REGION)
    print("=" * 64)
    print("AgentCore eval/opti demo — PREFLIGHT")
    print("=" * 64)

    ident = check_identity(session)

    hr("Bedrock model access")
    model_id, candidates = pick_model(session)
    print(f"candidate Claude ids: {candidates[:8]}{' ...' if len(candidates) > 8 else ''}")
    model_access = False
    access_msg = "no Claude candidate found"
    if model_id:
        model_access, access_msg = verify_model_access(session, model_id)
    print(f"chosen agent_model_id: {model_id}")
    print(f"Bedrock model access: {'ENABLED' if model_access else 'DISABLED'} ({access_msg})")

    hr("CloudWatch Transaction Search")
    ts = check_transaction_search(session)
    print(f"Transaction Search: {ts}")

    hr("AgentCore CLI")
    cli_status, cli_ver = check_cli()
    print(f"agentcore CLI: {cli_status} (starter-toolkit {cli_ver})")

    hr("boto3 AgentCore capability probe")
    flags = probe_ops(session)
    for k in ("harness_api", "runtime_api", "evaluation_api", "optimization_api", "abtest_api"):
        print(f"  {k}: {'present' if flags.get(k) else 'absent'}")

    # Deploy path: prefer the CLI, fall back to boto3 control plane.
    deploy_path = "agentcore-cli" if cli_status == "present" else "boto3-control"

    config = {
        "region": REGION,
        "account": ident["account"],
        "agent_model_id": model_id,
        "judge": "builtin-agentcore-evaluator",
        "deploy_path": deploy_path,
        "transaction_search": ts,
        "agentcore_cli": cli_status,
        "agentcore_cli_version": cli_ver,
        "capabilities": flags,
    }
    CONFIG_PATH.write_text(json.dumps(config, indent=2) + "\n")
    hr("config.json written")
    print(CONFIG_PATH.read_text().rstrip())

    print("\n" + "=" * 64)
    if not model_access:
        print("PREFLIGHT BLOCKED: Bedrock Claude model access is not enabled.")
        print("Remediation: open the Amazon Bedrock console in us-west-2 ->")
        print("  'Model access' -> enable an Anthropic Claude model (Haiku 4.5 or")
        print("  Sonnet 4.6), wait for 'Access granted', then re-run preflight.py.")
        print("=" * 64)
        return 1
    print("PREFLIGHT PASS: prerequisites satisfied.")
    print("=" * 64)
    return 0


if __name__ == "__main__":
    sys.exit(main())
