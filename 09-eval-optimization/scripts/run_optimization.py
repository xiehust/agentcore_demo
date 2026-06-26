"""AgentCore Optimization: turn the baseline evaluation into an improved system prompt.

Calls the Recommendations API (StartRecommendation) against the agent's baseline batch
evaluation, targeting the goal-success evaluator. Saves the recommended prompt + rationale to
results/recommendation.json and writes it into agent/prompts.py as OPTIMIZED_PROMPT, so the
next deploy uses the improved prompt. This is the "improve" step of the observe->evaluate->
improve loop.
"""
from __future__ import annotations

import json
import re
import sys
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _common import REPO_ROOT, data_client  # noqa: E402
from botocore.exceptions import ClientError  # noqa: E402

RESULTS = REPO_ROOT / "results"
PROMPTS_PY = REPO_ROOT / "agent" / "prompts.py"
GOAL_EVALUATOR_ARN = "arn:aws:bedrock-agentcore:::evaluator/Builtin.GoalSuccessRate"
POLL_TIMEOUT_S = 600
TERMINAL_OK = {"COMPLETED"}
TERMINAL_BAD = {"FAILED", "STOPPED"}


def baseline_eval_arn(client) -> str:
    scores = json.loads((RESULTS / "baseline_scores.json").read_text())
    bid = scores["batch_evaluation_id"]
    return client.get_batch_evaluation(batchEvaluationId=bid)["batchEvaluationArn"]


def current_baseline_prompt() -> str:
    sys.path.insert(0, str(REPO_ROOT))
    from agent.prompts import BASELINE_PROMPT

    return BASELINE_PROMPT


def write_optimized_prompt(text: str) -> None:
    content = PROMPTS_PY.read_text()
    # Use a function replacement so backslashes in repr(text) (e.g. \n) are NOT
    # re-interpreted by re.sub's replacement-string escape processing.
    new = re.sub(
        r"OPTIMIZED_PROMPT: str \| None = .*",
        lambda _m: f"OPTIMIZED_PROMPT: str | None = {text!r}",
        content,
        count=1,
    )
    PROMPTS_PY.write_text(new)


def main() -> int:
    client = data_client()
    baseline_prompt = current_baseline_prompt()
    eval_arn = baseline_eval_arn(client)
    print(f"Baseline prompt: {baseline_prompt!r}")
    print(f"Optimizing against baseline eval: {eval_arn}")
    print(f"Target evaluator: {GOAL_EVALUATOR_ARN}\n")

    name = f"acmeopt_{uuid.uuid4().hex[:8]}"
    try:
        rec = client.start_recommendation(
            name=name,
            type="SYSTEM_PROMPT_RECOMMENDATION",
            recommendationConfig={
                "systemPromptRecommendationConfig": {
                    "systemPrompt": {"text": baseline_prompt},
                    "agentTraces": {"batchEvaluation": {"batchEvaluationArn": eval_arn}},
                    "evaluationConfig": {"evaluators": [{"evaluatorArn": GOAL_EVALUATOR_ARN}]},
                }
            },
        )
    except ClientError as e:
        print(f"StartRecommendation failed: {e.response['Error']['Code']}: {e.response['Error']['Message']}")
        raise
    rec_id = rec["recommendationId"]
    print(f"Started recommendation: {rec_id}")

    deadline = time.monotonic() + POLL_TIMEOUT_S
    detail = {}
    status = rec.get("status", "PENDING")
    while time.monotonic() < deadline:
        detail = client.get_recommendation(recommendationId=rec_id)
        status = detail.get("status", "?")
        print(f"  status={status}")
        if status in TERMINAL_OK or status in TERMINAL_BAD:
            break
        time.sleep(15)

    result = (detail.get("recommendationResult") or {}).get("systemPromptRecommendationResult") or {}
    recommended = result.get("recommendedSystemPrompt")
    explanation = result.get("explanation")
    err = result.get("errorMessage")

    out = {
        "recommendation_id": rec_id,
        "status": status,
        "baseline_prompt": baseline_prompt,
        "recommended_system_prompt": recommended,
        "explanation": explanation,
        "error": err,
        "target_evaluator": GOAL_EVALUATOR_ARN,
    }
    RESULTS.mkdir(exist_ok=True)
    (RESULTS / "recommendation.json").write_text(json.dumps(out, indent=2) + "\n")

    if not recommended:
        print(f"\nNo recommended prompt produced (status={status}, error={err}).")
        return 1

    write_optimized_prompt(recommended)
    print("\n=== RECOMMENDED SYSTEM PROMPT ===")
    print(recommended)
    print("\n=== RATIONALE ===")
    print((explanation or "")[:1200])
    print("\nWrote results/recommendation.json and updated OPTIMIZED_PROMPT in agent/prompts.py")

    # Apply the improved prompt to the managed harness via UpdateHarness (no redeploy needed).
    from harness_agent import create_or_update_harness

    print("\nApplying optimized prompt to the harness (UpdateHarness)...")
    arn = create_or_update_harness(recommended)
    print(f"Harness updated: {arn}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
