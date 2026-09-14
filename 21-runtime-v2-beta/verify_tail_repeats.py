"""Offline verification and per-round/pooled tail analysis, no AWS or writes."""
import argparse
from collections import defaultdict
from datetime import datetime
import hashlib
import json
from pathlib import Path
from statistics import median, quantiles

from verify_coldstart_v2_matrix import check_cell

HERE = Path(__file__).resolve().parent

def tail_stats(values):
    if not values:
        return {"successes": 0, "p50_ms": None, "p90_ms": None, "p99_ms": None, "max_ms": None,
                "over_4s": 0, "over_5s": 0}
    return {"successes": len(values), "p50_ms": round(median(values), 3),
            "p90_ms": round(quantiles(values, n=100, method="inclusive")[89], 3) if len(values) > 1 else None,
            "p99_ms": round(quantiles(values, n=100, method="inclusive")[98], 3) if len(values) > 1 else None,
            "max_ms": round(max(values), 3), "over_4s": sum(v > 4000 for v in values),
            "over_5s": sum(v > 5000 for v in values)}


def analyze_cell(cell):
    folder = HERE / cell["folder"]
    result = json.loads((folder / "client_result.json").read_text())
    raw = json.loads((folder / "raw.json").read_text())
    first = [e for e in raw["events"] if e["operation"] == "invoke" and e["attempt"] == 1]
    good = sorted((e for e in first if e.get("valid")), key=lambda e: e["elapsed_ms"])
    reports = {p["pid"]: p for p in result["reports"]}
    cpu = sum(p["cpu_seconds"] for p in reports.values())
    wall = max(p["finished_perf"] for p in reports.values()) - min(p["client_ready_perf"] for p in reports.values())
    selected = {e["session_id"] for e in good if e["elapsed_ms"] > 4000}
    if good:
        selected.add(good[-1]["session_id"])
        position = (len(good) - 1) * .99
        selected.update(good[i]["session_id"] for i in {int(position), min(int(position) + 1, len(good) - 1)})
    tails = []
    for e in good:
        if e["session_id"] not in selected:
            continue
        p = reports[e["pid"]]
        tails.append({"session_id": e["session_id"], "request_id": e["metadata"]["RequestId"], "pid": e["pid"],
            "e2e_ms": e["elapsed_ms"], "release_to_call_ms": (e["started_perf"] - result["release_perf"]) * 1000,
            "pre_send_ms": (e["before_send_perf"][0] - e["started_perf"]) * 1000,
            "send_to_response_ms": (e["response_received_perf"][0] - e["before_send_perf"][0]) * 1000,
            "response_to_body_ms": (e["completed_perf"] - e["response_received_perf"][0]) * 1000,
            "worker_cpu_seconds": p["cpu_seconds"], "worker_wall_seconds": p["wall_seconds"]})
    details = {**tail_stats([e["elapsed_ms"] for e in good]), "attempts": len(first),
        "worker_cpu_seconds": round(cpu, 4), "worker_window_seconds": round(wall, 4),
        "average_core_equivalents": round(cpu / wall, 4), "tail_requests": tails,
        "median_pre_send_ms": round(median((e["before_send_perf"][0] - e["started_perf"]) * 1000 for e in good), 3) if good else None}
    return details, [e["elapsed_ms"] for e in good], raw["events"]


def verify(out):
    state = json.loads((out / "matrix.json").read_text())
    assert state["measurement_complete"] and state["cleanup_complete"] and not state.get("error")
    assert state["region"] == "us-west-2" and state["rounds_per_case"] == 3 and state["quiet_seconds"] == 180
    expected = [f"{size}_c{c}_r{r}" for r in range(1, 4) for size, c in (("500mb", 100), ("2gb", 200))]
    assert list(state["cells"]) == expected and set(state["requests"]) == set(expected)
    for name, sha in state["source_sha256"].items():
        assert hashlib.sha256((HERE.parent / name).read_bytes()).hexdigest() == sha, name
    for name, sha in state["baseline_evidence_sha256"].items():
        assert hashlib.sha256((HERE / name).read_bytes()).hexdigest() == sha, name
    original = json.loads((HERE / state["original_matrix"]).read_text())
    all_ids, runtime_ids, rows, all_events = [], [], [], []
    pooled = defaultdict(list)
    last_end = None
    baseline = {}
    for name in ("500mb_c100", "2gb_c200"):
        cell = original["cells"][name]
        check_cell(cell, original["images"][cell["size"]])
        baseline[name] = analyze_cell(cell)[0]
    for key in expected:
        cell = state["cells"][key]
        assert not cell["reused"] and key == f"{cell['size']}_c{cell['concurrency']}_r{cell['repeat']}"
        assert (HERE / cell["folder"]).parent == out.resolve()
        prior = original["cells"][f"{cell['size']}_c{cell['concurrency']}"]["runtime"]["ready_response"]
        ready = cell["runtime"]["ready_response"]
        for field in ("agentRuntimeArtifact", "roleArn", "networkConfiguration", "protocolConfiguration",
                      "lifecycleConfiguration", "platformVersion"):
            assert ready[field] == prior[field] == state["requests"][key][field]
        checked, ids = check_cell(cell, state["images"][cell["size"]])
        details, values, events = analyze_cell(cell)
        all_events.extend(events)
        first_start = min(datetime.fromisoformat(e["started_iso"]) for e in events)
        if last_end is not None:
            assert (first_start - last_end).total_seconds() >= 180
            details["inter_round_quiet_seconds"] = (first_start - last_end).total_seconds()
        assert cell["observed_quiet_seconds"] >= 180
        last_end = max(datetime.fromisoformat(e["finished_iso"]) for e in events)
        rows.append({"case": key, "repeat": cell["repeat"], "throttles": checked["throttles"],
                     "other_errors": checked["other_errors"], "before_send_spread_ms": checked["before_send_spread_ms"],
                     "actual_settle_seconds": checked["actual_settle_seconds"], "peak_inflight": checked["peak_inflight"], **details})
        pooled[f"{cell['size']}_c{cell['concurrency']}"] += values
        all_ids += ids
        runtime_ids.append(cell["runtime"]["id"])
        smoke = json.loads((HERE / cell["folder"] / "smoke.json").read_text())
        if key == expected[0]:
            all_smoke_ends = [datetime.fromisoformat(json.loads((HERE / c["folder"] / "smoke.json").read_text())["events"][-1]["finished_iso"])
                              for c in state["cells"].values()]
            assert (first_start - max(all_smoke_ends)).total_seconds() >= 180
    assert len(all_ids) == len(set(all_ids)) == 906
    assert len(set(runtime_ids)) == 6 and sum(r["attempts"] for r in rows) == 900
    success = all(c["summary"]["success"] == c["summary"]["warm_success"] == c["summary"]["samples"] for c in state["cells"].values())
    assert state["all_invocations_successful"] == success
    assert state["exit_code"] == (0 if success and all(c["summary"]["stop_success"] == c["summary"]["samples"] for c in state["cells"].values()) else 1)
    report = {"rounds": rows, "new_rounds_pooled": {k: tail_stats(v) for k, v in pooled.items()},
              "original_separate": baseline}
    print("PASS: six independent repeats, 900 first attempts + six smoke, exact source/config/API/cleanup evidence")
    print(json.dumps(report, indent=2))
    return report



if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("out", type=Path)
    verify(parser.parse_args().out)
