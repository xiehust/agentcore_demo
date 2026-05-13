"""
Invoke the deployed AgentCore Runtime agent.

Uses raw boto3 so this script works without the starter toolkit config file
-- useful for CI or quick checks from any environment that has the
RUNTIME_AGENT_ARN.

Usage:
    python invoke.py                          # default prompt on default URL
    python invoke.py --url https://foo/bar    # test a different paid URL
    python invoke.py --prompt "...free text..."
    python invoke.py --session-id s-xxx       # reuse a conversation session
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import uuid
from pathlib import Path

import boto3
from dotenv import load_dotenv


ROOT = Path(__file__).parent
DEFAULT_URL = "https://drvd12nxpcyd5.cloudfront.net/market-recap"


def _require(name: str) -> str:
    val = os.environ.get(name)
    if not val:
        sys.exit(f"ERROR: {name} is not set. Run `python deploy.py` first.")
    return val


def invoke(prompt: str, session_id: str | None = None) -> str:
    region = os.environ.get("AWS_REGION", "us-west-2")
    agent_arn = _require("RUNTIME_AGENT_ARN")

    client = boto3.client("bedrock-agentcore", region_name=region)
    session_id = session_id or f"invoke-{uuid.uuid4()}"
    payload = json.dumps({"prompt": prompt}).encode("utf-8")

    print(f"Invoking {agent_arn}")
    print(f"  session:    {session_id}")
    print(f"  prompt:     {prompt!r}\n")

    resp = client.invoke_agent_runtime(
        agentRuntimeArn=agent_arn,
        runtimeSessionId=session_id,
        qualifier="DEFAULT",
        payload=payload,
    )

    body_stream = resp.get("response")
    if body_stream is None:
        # Some SDK versions put bytes directly under a different key
        print("WARN: unexpected response shape:")
        print({k: v for k, v in resp.items() if k != "ResponseMetadata"})
        return ""

    raw = body_stream.read() if hasattr(body_stream, "read") else body_stream
    text = raw.decode("utf-8") if isinstance(raw, bytes) else str(raw)

    # The runtime returns JSON per our entrypoint; pretty-print if so.
    try:
        obj = json.loads(text)
        print("=== Runtime response ===")
        if isinstance(obj, dict) and "result" in obj:
            print(obj["result"])
        else:
            print(json.dumps(obj, indent=2, ensure_ascii=False))
    except json.JSONDecodeError:
        print("=== Runtime response (raw) ===")
        print(text)

    return text


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--url", help="paid endpoint to GET")
    parser.add_argument("--prompt", help="custom prompt (overrides --url)")
    parser.add_argument("--session-id", help="reuse a runtime session id")
    args = parser.parse_args()

    load_dotenv(ROOT / ".env")
    load_dotenv(ROOT / ".env.local", override=True)

    if args.prompt:
        prompt = args.prompt
    else:
        url = args.url or os.environ.get("PAID_URL", DEFAULT_URL)
        prompt = f"GET {url} and show me the response body exactly."

    invoke(prompt, session_id=args.session_id)


if __name__ == "__main__":
    main()
