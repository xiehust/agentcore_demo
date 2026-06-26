"""Generate agent sessions over the eval dataset, against the harness (default) or runtime.

Each prompt runs in its own session (unique id) so AgentCore Evaluations can score them.
Session ids are written to results/<tag>_sessions.json. After the last invoke, waits for
CloudWatch to ingest the telemetry before the evaluation step queries it.

Usage: generate_sessions.py [--tag baseline|improved] [--target harness|runtime] [--wait SECONDS]
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _common import REPO_ROOT, invoke_runtime, load_deployment, new_session_id  # noqa: E402
from harness_agent import invoke_harness_loop  # noqa: E402

DATASET = REPO_ROOT / "dataset" / "eval_prompts.json"
RESULTS = REPO_ROOT / "results"


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tag", default="baseline")
    # Eval/optimization runs on the runtime mirror (eval-mappable spans); the managed
    # harness is the primary deployed agent but its telemetry isn't scoreable by
    # AgentCore Evaluations yet (AgentSpanMappingException). Use --target harness to
    # generate sessions against the harness agent itself.
    ap.add_argument("--target", choices=["harness", "runtime"], default="runtime")
    ap.add_argument("--wait", type=int, default=180, help="seconds to wait for CloudWatch ingestion")
    args = ap.parse_args(argv[1:])

    dep = load_deployment()
    prompts = json.loads(DATASET.read_text())["prompts"]
    RESULTS.mkdir(exist_ok=True)

    if args.target == "harness":
        arn = dep.get("harness_arn")
        if not arn:
            print("ERROR: deployment.json has no harness_arn — run scripts/harness_create.py first.")
            return 1
    else:
        arn = dep["agent_runtime_arn"]

    print(f"Generating {len(prompts)} sessions against {args.target}: {arn}\n")
    sessions = []
    for p in prompts:
        sid = new_session_id(args.tag)
        ok, err, tools = True, "", []
        try:
            if args.target == "harness":
                answer, tools = invoke_harness_loop(arn, sid, p["prompt"])
            else:
                answer = invoke_runtime(arn, sid, p["prompt"])
        except Exception as exc:  # noqa: BLE001 - record per-prompt failures, keep going
            ok, err, answer = False, f"{type(exc).__name__}: {exc}", ""
        sessions.append({"prompt_id": p["id"], "session_id": sid, "prompt": p["prompt"], "ok": ok, "tools": tools})
        snippet = (answer or err).replace("\n", " ")[:70]
        print(f"  {p['id']}  {sid}  ok={ok}  tools={tools}  | {snippet}")

    out = RESULTS / f"{args.tag}_sessions.json"
    out.write_text(json.dumps({"tag": args.tag, "target": args.target, "sessions": sessions}, indent=2) + "\n")
    n_ok = sum(s["ok"] for s in sessions)
    print(f"\n{n_ok}/{len(sessions)} sessions ok. Wrote {out.name}")

    if args.wait > 0:
        print(f"Waiting {args.wait}s for CloudWatch ingestion", end="", flush=True)
        for _ in range(args.wait // 10):
            time.sleep(10)
            print(".", end="", flush=True)
        print(" done")
    return 0 if n_ok > 0 else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
