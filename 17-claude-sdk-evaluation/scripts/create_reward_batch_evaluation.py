"""Create the completed batch evaluation that the recommendation path uses as its reward source.

During the Optimization preview, `StartRecommendation` only identifies sessions whose
instrumentation scope is on a service-side allowlist (Strands and LangChain — see the
README). Claude Agent SDK / OpenInference spans are rejected with "No sessions were
identified from input agent traces", so the recommendation reward signal has to come from
an AgentCore Runtime trace source instead. This script runs a batch evaluation over
existing Runtime sessions and prints the ARN to pass to
`claude-sdk-eval --recommendation-batch-evaluation-arn`.

Batch evaluation resolves its sessions from CloudWatch Logs, so the sessions must still be
within the log group's retention window.

Usage:
  python scripts/create_reward_batch_evaluation.py \
    --log-group /aws/bedrock-agentcore/runtimes/<agent-id>-DEFAULT \
    --service-name <agent-name>.DEFAULT \
    --session-ids-file results/runtime_sessions_live.json
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

TERMINAL = {"COMPLETED", "COMPLETED_WITH_ERRORS", "FAILED", "STOPPED"}


def _session_ids(path: Path, explicit: list[str] | None) -> list[str]:
    if explicit:
        return explicit
    payload = json.loads(path.read_text())
    sessions = payload["sessions"] if isinstance(payload, dict) else payload
    ids = [s["session_id"] for s in sessions if s.get("ok", True)]
    if not ids:
        raise SystemExit(f"ERROR: no successful sessions found in {path}")
    return ids


def _job_name() -> str:
    # Batch evaluation names must match [a-zA-Z][a-zA-Z0-9_]{0,47}.
    return f"eval_claudrec_{uuid.uuid4().hex[:8]}"[:48]


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--log-group", required=True, help="AgentCore Runtime log group name.")
    parser.add_argument("--service-name", required=True, help="Observability service.name.")
    parser.add_argument(
        "--session-ids-file",
        type=Path,
        default=Path("results/runtime_sessions_live.json"),
        help="JSON file with a `sessions` list of {session_id, ok} objects.",
    )
    parser.add_argument("--session-id", action="append", help="Explicit session ID; repeatable.")
    parser.add_argument("--evaluator", default="Builtin.GoalSuccessRate")
    parser.add_argument("--region", default="us-west-2")
    parser.add_argument("--timeout", type=float, default=900.0)
    parser.add_argument("--poll-interval", type=float, default=15.0)
    parser.add_argument(
        "--output", type=Path, default=Path("results/reward_batch_evaluation.json")
    )
    args = parser.parse_args(argv[1:])

    session_ids = _session_ids(args.session_ids_file, args.session_id)
    client = boto3.client("bedrock-agentcore", region_name=args.region)

    print(f"Evaluating {len(session_ids)} sessions with {args.evaluator}")
    print(f"  serviceName={args.service_name}  logGroup={args.log_group}")
    job = client.start_batch_evaluation(
        batchEvaluationName=_job_name(),
        evaluators=[{"evaluatorId": args.evaluator}],
        dataSourceConfig={
            "cloudWatchLogs": {
                "serviceNames": [args.service_name],
                "logGroupNames": [args.log_group],
                "filterConfig": {"sessionIds": session_ids},
            }
        },
        description="Reward source for the Claude SDK system prompt recommendation demo.",
    )
    batch_id = job["batchEvaluationId"]
    batch_arn = job["batchEvaluationArn"]
    print(f"Started batch evaluation: {batch_id}")

    detail: dict[str, Any] = {}
    status = "PENDING"
    deadline = time.monotonic() + args.timeout
    while time.monotonic() < deadline:
        detail = client.get_batch_evaluation(batchEvaluationId=batch_id)
        status = str(detail.get("status", "?"))
        results = detail.get("evaluationResults") or {}
        print(
            f"  status={status} total={results.get('totalNumberOfSessions')} "
            f"done={results.get('numberOfSessionsCompleted')} "
            f"failed={results.get('numberOfSessionsFailed')}",
            flush=True,
        )
        if status in TERMINAL:
            break
        time.sleep(args.poll_interval)

    results = detail.get("evaluationResults") or {}
    summaries = results.get("evaluatorSummaries") or []
    payload = {
        "batch_evaluation_id": batch_id,
        "batch_evaluation_arn": batch_arn,
        "status": status,
        "evaluator": args.evaluator,
        "sessions": {
            "total": results.get("totalNumberOfSessions"),
            "completed": results.get("numberOfSessionsCompleted"),
            "failed": results.get("numberOfSessionsFailed"),
        },
        "scores": {
            s.get("evaluatorId"): (s.get("statistics") or {}).get("averageScore")
            for s in summaries
        },
        "error_details": detail.get("errorDetails"),
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, indent=2, default=str) + "\n")
    print(f"\nWrote {args.output}")

    # The recommendation reuses these scores only if it asks for the same evaluator, and
    # only while the underlying session logs are still in CloudWatch retention.
    if status != "COMPLETED" or not summaries:
        print(f"Batch evaluation did not produce reusable scores (status={status}).")
        return 1
    print("Pass this to the demo:")
    print(f"  --recommendation-batch-evaluation-arn '{batch_arn}'")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
