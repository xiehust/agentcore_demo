"""Invoke the deployed shopping agent on AgentCore Runtime, one session per prompt.

These sessions become the reward signal for the system prompt recommendation, so they
must exercise the *same* prompt and tool as the local Claude Agent SDK agent — that is
guaranteed by both agents importing `claude_sdk_evaluation.shopping`.

The prompts vary in how clearly they call for the tool and for arithmetic, so
`Builtin.GoalSuccessRate` has something to discriminate rather than scoring everything 1.0.

After this, wait for CloudWatch ingestion, then run
`scripts/create_reward_batch_evaluation.py`.

Usage:
  python scripts/generate_runtime_sessions.py --wait 200
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import uuid
from pathlib import Path
from typing import Any

import boto3

PROMPTS = [
    "What does one NOTEBOOK cost?",
    "Price 2 NOTEBOOK items and 3 PEN items, then give me the total.",
    "How much for 4 PEN?",
    "I need 1 STAPLER and 2 MARKER — what's the damage?",
    "Compare the price of a NOTEBOOK against a PEN.",
    "Give me the total for 3 NOTEBOOK, 1 STAPLER and 5 PEN.",
    "What's the cheapest item you sell?",
    "I want to buy 10 MARKER. Total?",
    "Price a WIDGET for me.",
    "Two STAPLER and one NOTEBOOK, and show your working.",
]


def _load_runtime_arn(deployment: Path, explicit: str | None) -> str:
    if explicit:
        return explicit
    if not deployment.exists():
        raise SystemExit(
            f"ERROR: {deployment} not found — run scripts/capture_runtime_deployment.py "
            "after `agentcore deploy`, or pass --runtime-arn."
        )
    return json.loads(deployment.read_text())["agent_runtime_arn"]


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime-arn")
    parser.add_argument("--deployment", type=Path, default=Path("runtime_deployment.json"))
    parser.add_argument("--region", default="us-west-2")
    parser.add_argument(
        "--wait",
        type=float,
        default=200.0,
        help="Seconds to wait after the last session for CloudWatch trace ingestion.",
    )
    parser.add_argument("--output", type=Path, default=Path("results/runtime_sessions_shop.json"))
    args = parser.parse_args(argv[1:])

    runtime_arn = _load_runtime_arn(args.deployment, args.runtime_arn)
    client = boto3.client("bedrock-agentcore", region_name=args.region)
    stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime())

    sessions: list[dict[str, Any]] = []
    for index, prompt in enumerate(PROMPTS, start=1):
        # AgentCore requires a session id of at least 33 characters.
        session_id = f"shop-{stamp}-{index:02d}-{uuid.uuid4().hex}"
        record: dict[str, Any] = {"session_id": session_id, "prompt": prompt}
        try:
            response = client.invoke_agent_runtime(
                agentRuntimeArn=runtime_arn,
                runtimeSessionId=session_id,
                payload=json.dumps({"prompt": prompt}).encode(),
            )
            body = response["response"].read().decode()
            record.update(ok=True, response=body)
            print(f"[{index:02d}/{len(PROMPTS)}] ok   {prompt[:52]}", flush=True)
        except Exception as exc:  # noqa: BLE001 - record and continue over the whole set
            record.update(ok=False, error=f"{type(exc).__name__}: {exc}")
            print(f"[{index:02d}/{len(PROMPTS)}] FAIL {type(exc).__name__}: {exc}", flush=True)
        sessions.append(record)

    payload = {"runtime_arn": runtime_arn, "sessions": sessions}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n")

    ok_count = sum(1 for s in sessions if s.get("ok"))
    print(f"\n{ok_count}/{len(sessions)} sessions succeeded -> {args.output}")
    if ok_count == 0:
        return 1
    if args.wait > 0:
        print(f"Waiting {args.wait:g}s for CloudWatch trace ingestion...")
        time.sleep(args.wait)
    print("Next: python scripts/create_reward_batch_evaluation.py --log-group ... --service-name ...")  # noqa: E501
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
