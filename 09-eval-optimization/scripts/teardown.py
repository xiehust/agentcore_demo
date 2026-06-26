"""Tear down the real AWS resources this demo created, to stop ongoing cost.

Reads deployment.json + results/*.json and deletes the AgentCore Runtime, the managed
Harness, and the batch-evaluation / recommendation records. DRY-RUN by default — pass --yes
to actually delete. Safe to re-run (skips already-gone resources).

Usage:
  uv run python scripts/teardown.py            # dry run: list what would be deleted
  uv run python scripts/teardown.py --yes      # actually delete
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _common import REPO_ROOT, control_client, data_client  # noqa: E402
from botocore.exceptions import ClientError  # noqa: E402

RESULTS = REPO_ROOT / "results"
DEPLOYMENT = REPO_ROOT / "deployment.json"


def gather_targets() -> list[tuple[str, str, str]]:
    """Return (resource_type, identifier, human_label) tuples to delete."""
    targets: list[tuple[str, str, str]] = []
    if DEPLOYMENT.exists():
        dep = json.loads(DEPLOYMENT.read_text())
        if dep.get("agent_id"):
            targets.append(("runtime", dep["agent_id"], f"AgentCore Runtime {dep['agent_id']}"))
        if dep.get("harness_arn"):
            hid = dep["harness_arn"].split("/")[-1]
            targets.append(("harness", hid, f"Managed Harness {hid}"))
    for tag in ("baseline", "improved"):
        f = RESULTS / f"{tag}_scores.json"
        if f.exists():
            bid = json.loads(f.read_text()).get("batch_evaluation_id")
            if bid:
                targets.append(("batch_evaluation", bid, f"Batch evaluation {bid} ({tag})"))
    rec = RESULTS / "recommendation.json"
    if rec.exists():
        rid = json.loads(rec.read_text()).get("recommendation_id")
        if rid:
            targets.append(("recommendation", rid, f"Recommendation {rid}"))
    return targets


def delete(resource_type: str, ident: str) -> str:
    ctrl, data = control_client(), data_client()
    try:
        if resource_type == "runtime":
            ctrl.delete_agent_runtime(agentRuntimeId=ident)
        elif resource_type == "harness":
            ctrl.delete_harness(harnessId=ident)
        elif resource_type == "batch_evaluation":
            data.delete_batch_evaluation(batchEvaluationId=ident)
        elif resource_type == "recommendation":
            data.delete_recommendation(recommendationId=ident)
        return "deleted"
    except ClientError as e:
        code = e.response["Error"]["Code"]
        if code in ("ResourceNotFoundException", "ValidationException"):
            return f"skipped ({code})"
        return f"error ({code})"


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--yes", action="store_true", help="actually delete (default: dry run)")
    args = ap.parse_args(argv[1:])

    targets = gather_targets()
    if not targets:
        print("No demo resources found (deployment.json / results missing). Nothing to tear down.")
        return 0

    mode = "DELETING" if args.yes else "DRY RUN (pass --yes to delete)"
    print(f"=== AgentCore demo teardown — {mode} ===\n")
    for rtype, ident, label in targets:
        if args.yes:
            status = delete(rtype, ident)
            print(f"  [{status}] {label}")
        else:
            print(f"  would delete: {label}  ({rtype} id={ident})")

    if not args.yes:
        print("\nDry run only — no resources deleted. Re-run with --yes to delete.")
        print("Note: `agentcore destroy` additionally removes the toolkit-created IAM role,")
        print("S3 bucket, and ECR repo if you want a fully clean account.")
    else:
        print("\nTeardown complete. Re-run `uv run python preflight.py` + redeploy to rebuild.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
