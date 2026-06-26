"""Run an AgentCore batch evaluation over generated sessions and capture aggregate scores.

Discovers the sessions from CloudWatch Logs (by session id + log group + service name),
runs the built-in LLM-as-a-judge evaluators, polls to a terminal state, and writes
results/<tag>_scores.json.

Usage: run_evaluation.py [--tag baseline|improved]
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _common import REPO_ROOT, data_client, load_deployment  # noqa: E402
from botocore.exceptions import ClientError  # noqa: E402

RESULTS = REPO_ROOT / "results"
EVALUATORS = ["Builtin.GoalSuccessRate", "Builtin.Helpfulness", "Builtin.Faithfulness"]
TERMINAL = {"COMPLETED", "COMPLETED_WITH_ERRORS", "FAILED", "STOPPED"}
POLL_TIMEOUT_S = 900
INGEST_RETRIES = 3


def eval_coords(dep: dict, target: str) -> tuple[str, str]:
    """Return (serviceName, logGroup) for the eval, by target.

    Observability service.name is "<agent-NAME>.DEFAULT" for a Runtime agent and
    "harness_<HarnessName>.DEFAULT" for a managed Harness — verified from aws/spans.
    """
    if target == "harness":
        sn = dep.get("harness_service_name") or f"harness_{dep.get('harness_name', 'AcmeSupportHarness')}.DEFAULT"
        lg = dep.get("harness_log_group")
        if not lg:
            raise RuntimeError("deployment.json missing harness_log_group — run scripts/harness_create.py.")
        return sn, lg
    return f"{dep['agent_name']}.DEFAULT", dep["log_group"]


def job_name(tag: str) -> str:
    # Must match [a-zA-Z][a-zA-Z0-9_]{0,47}: letters/digits/underscore only.
    return f"eval_{tag}_{uuid.uuid4().hex[:8]}"[:48]


def start_job(client, dep, session_ids, name, target):
    sn, lg = eval_coords(dep, target)
    return client.start_batch_evaluation(
        batchEvaluationName=name,
        evaluators=[{"evaluatorId": e} for e in EVALUATORS],
        dataSourceConfig={
            "cloudWatchLogs": {
                "serviceNames": [sn],
                "logGroupNames": [lg],
                "filterConfig": {"sessionIds": session_ids},
            }
        },
        description="Baseline/improved support-agent evaluation (Supergoal demo).",
    )


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tag", default="baseline")
    args = ap.parse_args(argv[1:])

    dep = load_deployment()
    sess_file = RESULTS / f"{args.tag}_sessions.json"
    if not sess_file.exists():
        print(f"ERROR: {sess_file.name} missing — run generate_sessions.py --tag {args.tag} first.")
        return 1
    sess_data = json.loads(sess_file.read_text())
    target = sess_data.get("target", "harness")
    session_ids = [s["session_id"] for s in sess_data["sessions"] if s["ok"]]
    sn, lg = eval_coords(dep, target)
    print(f"Evaluating {len(session_ids)} {target} sessions (tag={args.tag}) with {EVALUATORS}")
    print(f"  serviceName={sn}  logGroup={lg}")

    client = data_client()

    # Start the job. Shape errors (ValidationException) raise immediately; transient
    # "not found / not ready" conditions get a bounded ingestion retry.
    job = None
    for attempt in range(1, INGEST_RETRIES + 1):
        try:
            job = start_job(client, dep, session_ids, job_name(args.tag), target)
            break
        except ClientError as exc:
            code = exc.response["Error"]["Code"]
            msg = exc.response["Error"]["Message"]
            print(f"  start attempt {attempt} failed: {code}: {msg[:160]}")
            if code == "ValidationException":
                raise  # request shape problem — retrying won't help
            if attempt < INGEST_RETRIES:
                print("  waiting 60s for CloudWatch ingestion before retry...")
                time.sleep(60)
            else:
                raise
    job_id = job.get("batchEvaluationId") or job.get("batchEvaluationArn")
    print(f"Started batch evaluation: {job_id}")

    # Poll to terminal.
    deadline = time.monotonic() + POLL_TIMEOUT_S
    status = "PENDING"
    detail = {}
    while time.monotonic() < deadline:
        detail = client.get_batch_evaluation(batchEvaluationId=job["batchEvaluationId"])
        status = detail.get("status", "?")
        res = detail.get("evaluationResults", {}) or {}
        print(
            f"  status={status} sessions: total={res.get('totalNumberOfSessions')} "
            f"done={res.get('numberOfSessionsCompleted')} failed={res.get('numberOfSessionsFailed')}"
        )
        if status in TERMINAL:
            break
        time.sleep(15)

    res = detail.get("evaluationResults", {}) or {}
    summaries = res.get("evaluatorSummaries", [])
    scores = {
        s["evaluatorId"]: {
            "averageScore": (s.get("statistics") or {}).get("averageScore"),
            "totalEvaluated": s.get("totalEvaluated"),
            "totalFailed": s.get("totalFailed"),
        }
        for s in summaries
    }
    out_obj = {
        "tag": args.tag,
        "batch_evaluation_id": detail.get("batchEvaluationId"),
        "final_status": status,
        "sessions": {
            "total": res.get("totalNumberOfSessions"),
            "completed": res.get("numberOfSessionsCompleted"),
            "failed": res.get("numberOfSessionsFailed"),
            "ignored": res.get("numberOfSessionsIgnored"),
        },
        "evaluators": EVALUATORS,
        "scores": scores,
    }
    RESULTS.mkdir(exist_ok=True)
    out = RESULTS / f"{args.tag}_scores.json"
    out.write_text(json.dumps(out_obj, indent=2) + "\n")

    print(f"\nFinal status: {status}")
    print("Aggregate scores:")
    for ev, sc in scores.items():
        print(f"  {ev}: avg={sc['averageScore']}  (evaluated {sc['totalEvaluated']}, failed {sc['totalFailed']})")
    print(f"Wrote {out}")

    ok = status in {"COMPLETED", "COMPLETED_WITH_ERRORS"} and bool(scores)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
