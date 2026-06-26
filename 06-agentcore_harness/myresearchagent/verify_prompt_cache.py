#!/usr/bin/env python3
"""Verify Bedrock AgentCore prompt caching via the InvokeHarness API.

The InvokeHarness data-plane API streams back `metadata` events whose
`usage` block reports per-model-call token accounting:

    usage = {
        "inputTokens":          <uncached prompt tokens billed at full rate>,
        "cacheWriteInputTokens": <tokens written into the prompt cache (seeding)>,
        "cacheReadInputTokens":  <tokens served FROM the cache (a cache HIT)>,
        "outputTokens":         <generated tokens>,
        "totalTokens":          <sum>,
    }

Prompt caching is *working* when we observe `cacheReadInputTokens > 0` on any
model call after the prefix has been seeded — either on a later agent-loop
iteration within one invocation, or on a subsequent invocation that reuses the
same system-prompt + tool-definition prefix (cross-request, within the cache TTL).

This script invokes the harness one or more times with an identical prompt and a
shared session id, collects every `metadata` event, prints a per-model-call
table, and renders a verdict.

Usage:
    python verify_prompt_cache.py
    python verify_prompt_cache.py --rounds 3 --prompt "Research ..." --region us-west-2
"""

from __future__ import annotations

import argparse
import sys
import time
import uuid

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError, EventStreamError

DEFAULT_HARNESS_ARN = (
    "arn:aws:bedrock-agentcore:us-west-2:434444145045:harness/"
    "myresearchagent_myresearchagent-WNvP7rHZZG"
)
DEFAULT_REGION = "us-west-2"
# A short, deterministic prompt keeps each round to a single fast model call.
# The harness's own system prompt + tool definitions already form a large
# (~17.7k token) prefix — far above any model cache minimum — so we don't need
# tool use to grow it; we just need to send the identical prefix repeatedly and
# watch whether `inputTokens` drops / `cacheReadInputTokens` appears on round 2+.
# For a tool-use probe instead, pass e.g.
#   --prompt "Research three tropical vacation options under $1000 near NYC." --max-tokens 2048
DEFAULT_PROMPT = "Reply with exactly: OK"

# Streaming response keys that signal a terminal error rather than content.
ERROR_EVENT_KEYS = ("validationException", "runtimeClientError", "internalServerException")


def make_session_id() -> str:
    """Session id must match [a-zA-Z0-9][a-zA-Z0-9-_]* and be 33-100 chars."""
    sid = f"cachetest-{uuid.uuid4().hex}"  # 10 + 32 = 42 chars, starts with a letter
    assert 33 <= len(sid) <= 100, len(sid)
    return sid


def invoke_round(client, harness_arn: str, session_id: str, prompt: str,
                 max_tokens: int | None, timeout_seconds: int | None,
                 bypass_skill: bool) -> dict:
    """Run one InvokeHarness call, drain the event stream, return collected data."""
    kwargs: dict = {
        "harnessArn": harness_arn,
        "runtimeSessionId": session_id,
        "messages": [{"role": "user", "content": [{"text": prompt}]}],
    }
    if max_tokens is not None:
        kwargs["maxTokens"] = max_tokens
    if timeout_seconds is not None:
        kwargs["timeoutSeconds"] = timeout_seconds
    if bypass_skill:
        # The deployed harness's default skill fails to load ("No SKILL.md found
        # in fetched skill"), which aborts the whole invocation. Overriding skills
        # with an empty list bypasses it so the agent loop can actually run.
        kwargs["skills"] = []

    started = time.monotonic()
    response = client.invoke_harness(**kwargs)

    usages: list[dict] = []      # one entry per `metadata` event == per model call
    text_parts: list[str] = []
    stop_reason: str | None = None
    error: str | None = None

    try:
        for event in response["stream"]:
            # Each streamed event is a dict with exactly one of the union keys.
            if "metadata" in event:
                usage = event["metadata"].get("usage", {}) or {}
                metrics = event["metadata"].get("metrics", {}) or {}
                usages.append({
                    "input": usage.get("inputTokens", 0) or 0,
                    "cacheRead": usage.get("cacheReadInputTokens", 0) or 0,
                    "cacheWrite": usage.get("cacheWriteInputTokens", 0) or 0,
                    "output": usage.get("outputTokens", 0) or 0,
                    "total": usage.get("totalTokens", 0) or 0,
                    "latencyMs": metrics.get("latencyMs", 0) or 0,
                })
            elif "contentBlockDelta" in event:
                delta = event["contentBlockDelta"].get("delta", {}) or {}
                if "text" in delta:
                    text_parts.append(delta["text"])
            elif "messageStop" in event:
                stop_reason = event["messageStop"].get("stopReason")
            else:
                for key in ERROR_EVENT_KEYS:
                    if key in event:
                        error = f"{key}: {event[key].get('message', event[key])}"
                        break
    except EventStreamError as exc:
        error = f"EventStreamError: {exc}"

    return {
        "usages": usages,
        "text": "".join(text_parts),
        "stop_reason": stop_reason,
        "error": error,
        "wall_ms": int((time.monotonic() - started) * 1000),
    }


def print_round(round_no: int, result: dict) -> None:
    print(f"\n{'=' * 78}")
    print(f"ROUND {round_no}  (wall time {result['wall_ms'] / 1000:.1f}s, "
          f"stop_reason={result['stop_reason']})")
    print("=" * 78)
    if result["error"]:
        print(f"  !! ERROR: {result['error']}")

    usages = result["usages"]
    if not usages:
        print("  (no metadata/usage events received)")
        return

    header = f"  {'call':>4} | {'input':>8} | {'cacheRead':>10} | {'cacheWrite':>11} | {'output':>7} | {'total':>7}"
    print(header)
    print("  " + "-" * (len(header) - 2))
    for i, u in enumerate(usages, 1):
        flag = "  <-- CACHE HIT" if u["cacheRead"] > 0 else ""
        print(f"  {i:>4} | {u['input']:>8} | {u['cacheRead']:>10} | "
              f"{u['cacheWrite']:>11} | {u['output']:>7} | {u['total']:>7}{flag}")

    r_read = sum(u["cacheRead"] for u in usages)
    r_write = sum(u["cacheWrite"] for u in usages)
    r_input = sum(u["input"] for u in usages)
    print("  " + "-" * (len(header) - 2))
    print(f"  round totals: input={r_input}  cacheRead={r_read}  cacheWrite={r_write}  "
          f"(model calls={len(usages)})")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--harness-arn", default=DEFAULT_HARNESS_ARN)
    parser.add_argument("--region", default=DEFAULT_REGION)
    parser.add_argument("--prompt", default=DEFAULT_PROMPT)
    parser.add_argument("--rounds", type=int, default=3,
                        help="number of back-to-back invocations (default 3)")
    parser.add_argument("--session-id", default=None,
                        help="reuse a specific session id (default: generated)")
    parser.add_argument("--max-tokens", type=int, default=64)
    parser.add_argument("--timeout-seconds", type=int, default=None)
    parser.add_argument("--use-harness-skills", action="store_true",
                        help="do NOT send skills=[]; use the harness's configured "
                             "skills (currently fails to load on this harness)")
    parser.add_argument("--show-text", action="store_true",
                        help="print each round's assistant response text")
    args = parser.parse_args()

    session_id = args.session_id or make_session_id()
    # Increase read timeout: the agent loop with tool use can stream for a while.
    client = boto3.client(
        "bedrock-agentcore",
        region_name=args.region,
        config=Config(read_timeout=900, connect_timeout=20, retries={"max_attempts": 0}),
    )

    print("InvokeHarness prompt-cache verification")
    print(f"  harness : {args.harness_arn}")
    print(f"  region  : {args.region}")
    print(f"  session : {session_id}")
    print(f"  rounds  : {args.rounds}")
    print(f"  prompt  : {args.prompt!r}")
    print(f"  skills  : {'harness default' if args.use_harness_skills else 'overridden to [] (bypass broken skill)'}")
    print("\nNote: rounds share one session id and an identical prompt, so the stable")
    print("system-prompt + tool-definition prefix should be cached on round 1 and")
    print("reused (cacheRead > 0) on later rounds / later agent-loop iterations.")

    all_results: list[dict] = []
    for r in range(1, args.rounds + 1):
        try:
            result = invoke_round(client, args.harness_arn, session_id, args.prompt,
                                  args.max_tokens, args.timeout_seconds,
                                  bypass_skill=not args.use_harness_skills)
        except ClientError as exc:
            print(f"\nROUND {r}: API call failed: {exc}", file=sys.stderr)
            return 2
        all_results.append(result)
        print_round(r, result)
        if args.show_text:
            print(f"\n  --- response ---\n{result['text']}\n  --- end ---")

    # ---- Verdict ---------------------------------------------------------
    total_read = sum(u["cacheRead"] for res in all_results for u in res["usages"])
    total_write = sum(u["cacheWrite"] for res in all_results for u in res["usages"])
    total_input = sum(u["input"] for res in all_results for u in res["usages"])
    total_calls = sum(len(res["usages"]) for res in all_results)

    print(f"\n{'#' * 78}")
    print("VERDICT")
    print("#" * 78)
    print(f"  model calls observed : {total_calls}")
    print(f"  total inputTokens    : {total_input}  (billed at full rate)")
    print(f"  total cacheWrite     : {total_write}  (tokens seeded into cache)")
    print(f"  total cacheRead      : {total_read}  (tokens served from cache = hits)")

    if total_read > 0:
        billable = total_input + total_write + total_read
        pct = (total_read / billable * 100) if billable else 0
        print(f"\n  ✅ PROMPT CACHING IS WORKING.")
        print(f"     {total_read} tokens were served from cache "
              f"({pct:.0f}% of prompt tokens were cache hits).")
        return 0
    if total_write > 0:
        print(f"\n  ⚠️  Cache WRITES seen but no cache READS.")
        print("     The prefix is being cached but wasn't reused. Try --rounds 3+,")
        print("     run again quickly (cache TTL is ~5 min), or use a prompt that")
        print("     triggers multi-iteration tool use to grow the cacheable prefix.")
        return 0
    print(f"\n  ❌ NO CACHE ACTIVITY (cacheRead and cacheWrite are both 0).")
    print("     The full prompt was billed at the standard input rate on every round")
    print("     (inputTokens did not drop on repeated identical requests), and the")
    print("     metadata never reported cacheReadInputTokens/cacheWriteInputTokens.")
    print("     => Prompt caching is NOT taking effect for this harness. The managed")
    print("        harness runtime is not setting cache points on the system prompt /")
    print("        tool definitions, and InvokeHarness exposes no cache-config knob.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
