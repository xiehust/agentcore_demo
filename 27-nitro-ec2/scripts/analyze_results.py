#!/usr/bin/env python3
"""Recompute report numbers and audit every measured cold/warm pair."""
import argparse
import hashlib
import importlib.util
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("benchmark", ROOT / "benchmark.py")
benchmark = importlib.util.module_from_spec(spec)
spec.loader.exec_module(benchmark)

def analyze(evidence):
    output = {"units": "milliseconds", "percentile_method": "nearest-rank", "batches": {}, "audit": {}}
    all_boots = set()
    sample_total = 0
    failures = 0
    rounds_total = 0
    max_residual = 0.0
    images = set()
    for name in ("startup-confidential", "startup-direct", "agent-direct-batch2"):
        file = evidence / name / "results.json"
        data = json.loads(file.read_text())
        records = data["records"]
        recomputed = benchmark.summarize(records)
        if recomputed != data["summary"]:
            raise ValueError(f"stored summary differs from raw samples: {name}")
        if data["configuration"]["debug_mode"]:
            raise ValueError("debug samples mixed into a formal batch")
        images.add(data["host"]["initrd"]["sha256"])
        if str(data["host"]["slots_after"]) != "0":
            raise ValueError(f"leaked hardware Sandbox slot: {name}")
        residuals = []
        by_round = {}
        for row in records:
            sample_total += 1
            failures += not row["ok"]
            by_round.setdefault(row["round"], {})[row["phase"]] = row
            if benchmark.completed(row):
                if row["slots_in_use"] != 1:
                    raise ValueError("missing hardware Sandbox proof")
                if name == "agent-direct-batch2" and not isinstance(row.get("answer_matches_expected"), bool):
                    raise ValueError("completed agent answer lacks a quality check")
                if row["phase"] == "cold":
                    residual = abs(row["launch_to_response_ms"] - row["sandbox_ready_ms"]
                                   - row["ready_to_invoke_ms"] - row["request_ms"])
                    residuals.append(residual)
                    max_residual = max(max_residual, residual)
                    if residual > 1e-5:
                        raise ValueError("cold end-to-end components do not reconcile")
        for pair in by_round.values():
            cold, warm = pair.get("cold"), pair.get("warm")
            if cold and benchmark.completed(cold):
                boot = cold["boot_id"]
                if boot in all_boots:
                    raise ValueError("a cold round reused an existing VM boot")
                all_boots.add(boot)
                if warm and benchmark.completed(warm) and warm["boot_id"] != boot:
                    raise ValueError("warm invocation moved to a different VM")
        rounds_total += len(by_round)
        output["batches"][name] = {
            "requested_rounds": data["requested_rounds"], "executed_rounds": data["executed_rounds"],
            "attempted_records": len(records), "failed_records": sum(not r["ok"] for r in records),
            "tested_at": data["tested_at"], "config": data["configuration"], "summary": recomputed,
            "completion_summary": benchmark.completion_summary(records),
            "execution_failed_records": sum(not benchmark.completed(row) for row in records),
            "answer_mismatch_records": sum(benchmark.answer_mismatch(row) for row in records),
            "first_round": [r for r in records if r["round"] == 1],
            "subsequent_summary": benchmark.summarize([r for r in records if r["round"] > 1]),
            "loadavg_start": data["host"]["loadavg_start"], "loadavg_end": data["host"]["loadavg_end"],
            "kernel_image": data["host"]["kernel_image"], "initrd": data["host"]["initrd"],
            "firecracker": data["host"]["firecracker"],
            "raw_sha256": hashlib.sha256(file.read_bytes()).hexdigest(),
            "max_reconciliation_residual_ms": max(residuals, default=None),
            "request_non_dsh_range_ms": [min((r["request_non_dsh_ms"] for r in records if r["ok"]), default=None),
                                         max((r["request_non_dsh_ms"] for r in records if r["ok"]), default=None)],
            "stopped_early": data.get("stopped_early"),
        }
    if len(images) != 1:
        raise ValueError("formal batches did not use the same guest image")
    output["audit"] = {"executed_rounds": rounds_total, "attempted_records": sample_total,
                       "failed_records": failures, "successful_records": sample_total - failures,
                       "distinct_cold_boots": len(all_boots), "same_image_all_batches": True,
                       "max_reconciliation_residual_ms": max_residual,
                       "after_all_batches_slots": (evidence / "host/slots-after.txt").read_text().strip()}
    return output

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("evidence", type=Path)
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()
    text = json.dumps(analyze(args.evidence), indent=2) + "\n"
    if args.out:
        if args.out.exists():
            raise ValueError("refusing to overwrite analysis")
        args.out.write_text(text)
        print(f"Analysis saved to {args.out}")
    else:
        print(text)
