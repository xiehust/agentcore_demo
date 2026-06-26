"""Invoke the deployed agent: the managed Harness (default) or the Runtime alternative.

If deployment.json has a harness_arn, this runs the harness tool loop (InvokeHarness +
client-side tool execution). Use --target runtime to invoke the Strands-on-Runtime agent
(InvokeAgentRuntime) instead.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _common import invoke_runtime, load_deployment, new_session_id  # noqa: E402
from harness_agent import invoke_harness_loop  # noqa: E402


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("prompt", nargs="?", default="What's the status of order ORD-1001?")
    ap.add_argument("--target", choices=["harness", "runtime"], default="harness")
    args = ap.parse_args(argv[1:])

    dep = load_deployment()
    sid = new_session_id()
    print(f"Target: {args.target}  ·  session: {sid} ({len(sid)} chars)")
    print(f"PROMPT: {args.prompt}\n")

    if args.target == "harness":
        arn = dep.get("harness_arn")
        if not arn:
            print("ERROR: no harness_arn in deployment.json — run scripts/harness_create.py first.")
            return 1
        text, tools = invoke_harness_loop(arn, sid, args.prompt)
        print(f"TOOLS USED: {tools}")
        print(f"RESPONSE:\n{text}")
    else:
        arn = dep.get("agent_runtime_arn")
        if not arn:
            print("ERROR: no agent_runtime_arn in deployment.json.")
            return 1
        print(f"RESPONSE:\n{invoke_runtime(arn, sid, args.prompt)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
