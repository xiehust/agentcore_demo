"""Command-line orchestration for the no-CloudWatch evaluation pipeline."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import secrets
from pathlib import Path
from typing import Any

import boto3
from langfuse import get_client, propagate_attributes
from openinference.instrumentation.claude_agent_sdk import ClaudeAgentSDKInstrumentor

from . import DEFAULT_MODEL
from .agent import SYSTEM_PROMPT, run_agent
from .evaluator import evaluate_session_spans
from .langfuse_bridge import read_session_span_logs
from .recommendation import (
    RecommendationFailed,
    RecommendationTimeout,
    recommend_system_prompt,
)

DEFAULT_PROMPT = (
    "Use the catalog tool to find the price of 2 NOTEBOOK items and 3 PEN items, "
    "then calculate the total cost."
)


def _required_environment() -> None:
    required = [
        "ANTHROPIC_BASE_URL",
        "ANTHROPIC_API_KEY",
        "LANGFUSE_PUBLIC_KEY",
        "LANGFUSE_SECRET_KEY",
    ]
    missing = [name for name in required if not os.environ.get(name)]
    if not (os.environ.get("LANGFUSE_BASE_URL") or os.environ.get("LANGFUSE_HOST")):
        missing.append("LANGFUSE_BASE_URL (or LANGFUSE_HOST)")
    if missing:
        raise RuntimeError("missing required environment variables: " + ", ".join(missing))


def _default_region() -> str:
    return (
        os.environ.get("AWS_REGION")
        or os.environ.get("AWS_DEFAULT_REGION")
        or boto3.Session().region_name
        or "us-west-2"
    )


def _trace_source(args: argparse.Namespace) -> dict[str, Any]:
    """Describe which traces the recommendation actually analyzed."""
    arn = args.recommendation_batch_evaluation_arn
    if arn:
        return {"type": "batchEvaluation", "batch_evaluation_arn": arn}
    return {"type": "sessionSpans"}


def _cloudwatch_used(args: argparse.Namespace) -> bool:
    """A batch evaluation resolves its sessions from CloudWatch Logs; inline spans do not."""
    return bool(args.recommendation_batch_evaluation_arn)


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False, default=str) + "\n")


async def _run(args: argparse.Namespace) -> dict[str, Any]:
    _required_environment()
    langfuse = get_client()
    if not langfuse.auth_check():
        raise RuntimeError("Langfuse authentication failed")

    ClaudeAgentSDKInstrumentor().instrument()
    session_id = args.session_id or f"claude-sdk-eval-{secrets.token_hex(8)}"
    trace_id = secrets.token_hex(16)

    with langfuse.start_as_current_observation(
        as_type="span",
        name="claude-sdk-agentcore-evaluation",
        trace_context={"trace_id": trace_id},
        input=args.prompt,
        metadata={"pipeline": "langfuse-to-agentcore", "cloudwatch": False},
    ) as root:
        with propagate_attributes(
            session_id=session_id,
            trace_name="claude-sdk-agentcore-evaluation",
            tags=["claude-agent-sdk", "agentcore-evaluation", "no-cloudwatch"],
            metadata={"requested_model": DEFAULT_MODEL},
        ):
            agent_run = await run_agent(args.prompt)
        root.update(output=agent_run.response)

    langfuse.flush()
    session_logs = read_session_span_logs(
        langfuse,
        session_id=session_id,
        trace_id=trace_id,
        timeout_seconds=args.langfuse_timeout,
    )
    _write_json(args.spans_output, session_logs.spans)

    evaluation_results: dict[str, list[dict[str, Any]]] = {}
    if not args.skip_evaluation:
        evaluation_results = evaluate_session_spans(
            session_logs.spans,
            evaluator_ids=args.evaluator,
            region=args.region,
        )

    recommendation_result: dict[str, Any] | None = None
    if args.recommend_system_prompt:
        try:
            recommendation_result = recommend_system_prompt(
                None if args.recommendation_batch_evaluation_arn else session_logs.spans,
                system_prompt=SYSTEM_PROMPT,
                evaluator_id=args.recommendation_evaluator,
                region=args.region,
                batch_evaluation_arn=args.recommendation_batch_evaluation_arn,
                timeout_seconds=args.recommendation_timeout,
                poll_seconds=args.recommendation_poll_interval,
            )
        except RecommendationFailed as exc:
            recommendation_result = {
                "recommendation_id": exc.recommendation_id,
                "status": "FAILED",
                "error_code": exc.error_code,
                "error_message": exc.error_message,
                "langfuse_session_id": session_id,
                "langfuse_trace_id": trace_id,
                "trace_source": _trace_source(args),
                "cloudwatch_used": _cloudwatch_used(args),
            }
            _write_json(args.recommendation_output, recommendation_result)
            raise
        except RecommendationTimeout as exc:
            recommendation_result = {
                "recommendation_id": exc.recommendation_id,
                "status": exc.status,
                "error_code": "TIMEOUT",
                "error_message": str(exc),
                "langfuse_session_id": session_id,
                "langfuse_trace_id": trace_id,
                "trace_source": _trace_source(args),
                "cloudwatch_used": _cloudwatch_used(args),
            }
            _write_json(args.recommendation_output, recommendation_result)
            raise
        recommendation_result.update(
            {
                "langfuse_session_id": session_id,
                "langfuse_trace_id": trace_id,
                "cloudwatch_used": _cloudwatch_used(args),
            }
        )
        _write_json(args.recommendation_output, recommendation_result)

    result = {
        "model": DEFAULT_MODEL,
        "region": args.region,
        "session_id": session_id,
        "trace_id": trace_id,
        "claude_session_id": agent_run.claude_session_id,
        "agent_turns": agent_run.turns,
        "agent_response": agent_run.response,
        "span_count": len(session_logs.spans),
        "span_kinds": [
            span["attributes"]["openinference.span.kind"] for span in session_logs.spans
        ],
        "evaluation_results": evaluation_results,
        "recommendation": recommendation_result,
        "cloudwatch_used": _cloudwatch_used(args) if args.recommend_system_prompt else False,
    }
    _write_json(args.output, result)
    return result


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run Claude Agent SDK → Langfuse → AgentCore Evaluate without CloudWatch."
    )
    parser.add_argument("--prompt", default=DEFAULT_PROMPT)
    parser.add_argument("--session-id")
    parser.add_argument("--region", default=_default_region())
    parser.add_argument(
        "--evaluator",
        action="append",
        default=None,
        help="AgentCore evaluator ID; repeat to run multiple evaluators.",
    )
    parser.add_argument("--langfuse-timeout", type=float, default=60.0)
    parser.add_argument("--spans-output", type=Path, default=Path("results/session_spans.json"))
    parser.add_argument("--output", type=Path, default=Path("results/evaluation.json"))
    parser.add_argument("--skip-evaluation", action="store_true")
    parser.add_argument(
        "--recommend-system-prompt",
        action="store_true",
        help="Generate an AgentCore system prompt recommendation from the Langfuse spans.",
    )
    parser.add_argument(
        "--recommendation-evaluator",
        default="Builtin.GoalSuccessRate",
        help="Single numeric evaluator ID or ARN used as the recommendation reward signal.",
    )
    parser.add_argument(
        "--recommendation-batch-evaluation-arn",
        help=(
            "Use a completed AgentCore batch evaluation as the recommendation trace source "
            "instead of the inline Langfuse spans."
        ),
    )
    parser.add_argument("--recommendation-timeout", type=float, default=900.0)
    parser.add_argument("--recommendation-poll-interval", type=float, default=15.0)
    parser.add_argument(
        "--recommendation-output",
        type=Path,
        default=Path("results/recommendation.json"),
    )
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    if args.evaluator is None:
        args.evaluator = ["Builtin.Helpfulness"]
    try:
        result = asyncio.run(_run(args))
    except Exception as exc:
        parser.exit(1, f"ERROR: {exc}\n")

    print(json.dumps(result, indent=2, ensure_ascii=False, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
