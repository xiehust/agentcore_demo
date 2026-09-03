import asyncio
from types import SimpleNamespace

import pytest

from claude_sdk_evaluation import DEFAULT_MODEL
from claude_sdk_evaluation import agent as agent_module
from claude_sdk_evaluation.cli import _cloudwatch_used, _trace_source, build_parser


def test_cli_rejects_model_override():
    parser = build_parser()

    with pytest.raises(SystemExit):
        parser.parse_args(["--model", "different-model"])


def test_recommendation_is_opt_in_with_goal_success_default():
    args = build_parser().parse_args([])

    assert args.recommend_system_prompt is False
    assert args.recommendation_evaluator == "Builtin.GoalSuccessRate"
    assert args.recommendation_batch_evaluation_arn is None
    assert args.recommendation_timeout == 900.0
    assert args.recommendation_poll_interval == 15.0


class _ResultMessage:
    is_error = False
    errors = None
    subtype = "success"
    result = "The total is $12.75."
    session_id = "claude-session"
    num_turns = 3


class _FakeClient:
    def __init__(self, *, options):
        self.options = options

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return None

    async def query(self, prompt):
        assert prompt == "price the products"

    async def receive_response(self):
        yield _ResultMessage()


def test_agent_options_use_exact_model(monkeypatch):
    captured: dict[str, object] = {}

    def options_factory(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(**kwargs)

    monkeypatch.setattr(agent_module, "ClaudeAgentOptions", options_factory)
    monkeypatch.setattr(agent_module, "ClaudeSDKClient", _FakeClient)
    monkeypatch.setattr(agent_module, "ResultMessage", _ResultMessage)
    monkeypatch.setattr(agent_module, "create_sdk_mcp_server", lambda **_kwargs: object())

    result = asyncio.run(agent_module.run_agent("price the products"))

    assert captured["model"] == DEFAULT_MODEL == "claude-sonnet-5"
    assert captured["system_prompt"] == agent_module.SYSTEM_PROMPT
    assert result.claude_session_id == "claude-session"


def test_batch_trace_source_reports_cloudwatch_honestly():
    """Batch evaluation resolves its sessions from CloudWatch Logs; inline spans do not."""
    parser = build_parser()
    batch_arn = "arn:aws:bedrock-agentcore:us-west-2:123:batch-evaluate/eval-completed"

    inline = parser.parse_args(["--recommend-system-prompt"])
    assert _cloudwatch_used(inline) is False
    assert _trace_source(inline) == {"type": "sessionSpans"}

    batch = parser.parse_args(
        ["--recommend-system-prompt", "--recommendation-batch-evaluation-arn", batch_arn]
    )
    assert _cloudwatch_used(batch) is True
    assert _trace_source(batch) == {
        "type": "batchEvaluation",
        "batch_evaluation_arn": batch_arn,
    }
