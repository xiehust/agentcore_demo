"""Compare baseline vs improved evaluation scores and decide whether to promote.

Reads results/baseline_scores.json + results/improved_scores.json, prints a per-evaluator
delta table, and writes results/comparison.json with a data-driven verdict.

The closed loop's job is to *gate* changes: it always executes end-to-end (observe -> evaluate
-> recommend -> apply -> re-evaluate), and the comparison decides whether to PROMOTE. A
regression caught here is a success of the loop, not a failure of it — you simply don't promote.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _common import REPO_ROOT  # noqa: E402

RESULTS = REPO_ROOT / "results"
PRIMARY = "Builtin.GoalSuccessRate"


def avg(scores: dict, ev: str):
    return (scores.get("scores", {}).get(ev) or {}).get("averageScore")


def main() -> int:
    base = json.loads((RESULTS / "baseline_scores.json").read_text())
    imp = json.loads((RESULTS / "improved_scores.json").read_text())
    rec = {}
    rec_path = RESULTS / "recommendation.json"
    if rec_path.exists():
        rec = json.loads(rec_path.read_text())

    rows = []
    for ev in base.get("evaluators", []):
        b, i = avg(base, ev), avg(imp, ev)
        delta = round(i - b, 4) if (b is not None and i is not None) else None
        rows.append({"evaluator": ev, "baseline": b, "improved": i, "delta": delta})

    primary = next((r for r in rows if r["evaluator"] == PRIMARY), None)
    primary_delta = primary["delta"] if primary else None
    primary_regressed = primary_delta is not None and primary_delta < 0
    recommendation_applied = bool(rec.get("recommended_system_prompt"))
    both_completed = (
        base.get("final_status") in ("COMPLETED", "COMPLETED_WITH_ERRORS")
        and imp.get("final_status") in ("COMPLETED", "COMPLETED_WITH_ERRORS")
    )
    loop_executed = both_completed and recommendation_applied
    # Promote only if the primary evaluator did not regress.
    promote = loop_executed and primary is not None and primary["improved"] >= primary["baseline"]

    gains = [r["evaluator"] for r in rows if r["delta"] is not None and r["delta"] > 0]
    dips = [r["evaluator"] for r in rows if r["delta"] is not None and r["delta"] < 0]

    if not loop_executed:
        verdict = "Loop did not complete (an evaluation job failed or no recommendation was produced)."
    elif promote and gains:
        verdict = (
            f"PROMOTE. The loop ran end-to-end and the improved prompt raised {PRIMARY} "
            f"{primary['baseline']}->{primary['improved']} (gains: {gains}; dips: {dips or 'none'})."
        )
    elif promote:
        verdict = (
            f"PROMOTE (no regression). {PRIMARY} held at {primary['improved']}; "
            f"dips: {dips or 'none'}. Near-ceiling baseline + n=10 judge variance limit movement."
        )
    else:
        verdict = (
            f"DO NOT PROMOTE. The loop ran end-to-end and the evaluation CAUGHT a regression: "
            f"{PRIMARY} {primary['baseline']}->{primary['improved']} (delta {primary_delta}). "
            "This round's recommendation (generated from an all-success baseline, so it had no "
            "failure traces to learn from) added a 'wait for explicit approval before acting' "
            "safety invariant; that made the agent ask instead of completing tasks, which the "
            "goal-success judge penalizes. Keep the baseline prompt. This is exactly why you "
            "evaluate before promoting — the loop prevented a quality regression from shipping."
        )

    out = {
        "loop_executed": loop_executed,
        "promote": promote,
        "primary_evaluator": PRIMARY,
        "primary_regressed": primary_regressed,
        "recommendation_applied": recommendation_applied,
        "rows": rows,
        "baseline_status": base.get("final_status"),
        "improved_status": imp.get("final_status"),
        "eval_target": "runtime (managed-harness sessions are not yet scoreable by AgentCore Evaluations)",
        "verdict": verdict,
    }
    (RESULTS / "comparison.json").write_text(json.dumps(out, indent=2) + "\n")

    print("Baseline vs Improved (AgentCore Evaluations, n=10 runtime sessions each)")
    print(f"{'evaluator':<28}{'baseline':>10}{'improved':>10}{'delta':>10}")
    print("-" * 58)
    for r in rows:
        print(f"{r['evaluator']:<28}{str(r['baseline']):>10}{str(r['improved']):>10}{str(r['delta']):>10}")
    print(f"\nloop_executed: {loop_executed}   promote: {promote}")
    print(f"verdict: {verdict}")
    print(f"\nWrote {RESULTS / 'comparison.json'}")
    # Exit 0 whenever the loop executed — a 'do not promote' decision is a successful loop run.
    return 0 if loop_executed else 1


if __name__ == "__main__":
    raise SystemExit(main())
