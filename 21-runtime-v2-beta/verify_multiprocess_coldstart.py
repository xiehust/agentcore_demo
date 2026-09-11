"""Independently recompute and accept saved eight-process benchmark evidence."""
import argparse
from collections import Counter, defaultdict
import hashlib
import json
from pathlib import Path
from statistics import median, quantiles

from verify_coldstart import stats

HERE = Path(__file__).resolve().parent

def percentiles(values):
    if not values:
        return None
    return {"p50": round(median(values), 3),
            "p90": round(quantiles(values, n=100, method="inclusive")[89], 3) if len(values) > 1 else values[0],
            "max": round(max(values), 3)}


def overlap(events):
    points = sorted([(e["started_perf"], 1) for e in events] +
                    [(e["completed_perf"], -1) for e in events])
    active = peak = 0
    for _, delta in points:
        active += delta
        peak = max(peak, active)
    return peak


def verify_session(row, events, arn):
    assert len(row["session_id"]) == 40
    assert all(e["session_id"] == row["session_id"] and e["runtime_arn"] == arn for e in events)
    invokes = sorted((e for e in events if e["operation"] == "invoke"), key=lambda e: e["attempt"])
    stops = [e for e in events if e["operation"] == "stop"]
    assert [e["attempt"] for e in invokes] == ([1, 2] if row["success"] else [1])
    assert len(stops) == 1
    assert len(events) == len(invokes) + 1
    assert bool(stops[0].get("valid")) == row["stopped"]
    if not row["stopped"]:
        assert row["stop_absent"] and stops[0]["error_code"] == "ResourceNotFoundException"
        assert not row["success"] and invokes[0]["error_type"] == "throttle"
    for e in events:
        assert e["metadata"]["RetryAttempts"] == 0
        assert len(e["before_send_perf"]) == len(e["response_received_perf"]) == 1
        assert e["started_perf"] <= e["before_send_perf"][0] <= e["response_received_perf"][0] <= e["completed_perf"]
        assert abs((e["completed_perf"] - e["started_perf"]) * 1000 - e["elapsed_ms"]) < 1e-6
        if e.get("valid"):
            assert e["status"] == 200
            if e["operation"] == "invoke":
                assert e["body"]["message"] == "pong" and e["body"]["echo"] == {"ping": "coldstart"}
    first = invokes[0]
    assert bool(first.get("valid")) == row["success"]
    if row["success"]:
        assert round(first["elapsed_ms"], 1) == row["cold_ms"]
        assert first["body"]["proc_start_ts"] == row["proc_start_ts"]
        assert first["body"]["request_ts"] == row["request_ts"]
        warm = invokes[1]
        assert bool(warm.get("valid")) == (row["warm_ms"] is not None)
        if row["warm_ms"] is not None:
            assert round(warm["elapsed_ms"], 1) == row["warm_ms"]
            assert warm["body"]["proc_start_ts"] == row["proc_start_ts"]
        assert warm["started_perf"] >= first["completed_perf"]
    else:
        assert row["cold_ms"] is None and row["warm_ms"] is None
        assert row["error_type"] == first["error_type"]
    assert stops[0]["started_perf"] >= invokes[-1]["completed_perf"]


def verify(out):
    meta = json.loads((out / "run.json").read_text())
    dep = json.loads((out / "deployments.json").read_text())
    requests = json.loads((out / "create_requests.json").read_text())
    assert meta["measurement_complete"] and meta["cleanup_complete"] and not meta.get("error")
    assert meta["levels"] == [50, 100, 200] and meta["process_count"] == 8 and meta["start_method"] == "spawn"
    for name, digest in meta["source_sha256"].items():
        assert hashlib.sha256((HERE.parent / name).read_bytes()).hexdigest() == digest, name
    assert set(dep["runtimes"]) == {"50", "100", "200"}
    all_ids, output = [], []
    for concurrency in meta["levels"]:
        key = str(concurrency)
        runtime = dep["runtimes"][key]
        assert runtime["deleted"] and runtime["ready_response"]["platformVersion"] == "V2"
        assert runtime["ready_response"]["status"] == "READY"
        for field in ("agentRuntimeArtifact", "roleArn", "networkConfiguration",
                      "protocolConfiguration", "lifecycleConfiguration"):
            assert requests[key][field] == runtime["ready_response"][field]
        folder = out / ("c" + key)
        cleanup = json.loads((folder / "cleanup.json").read_text())["runtimes"]
        assert len(cleanup) == 1 and cleanup[0]["id"] == runtime["id"] and cleanup[0]["deleted"]
        result = json.loads((folder / "client_result.json").read_text())
        raw = json.loads((folder / "raw.json").read_text())
        summary = json.loads((folder / "summary.json").read_text())
        plan = json.loads((folder / "client_plan.json").read_text())
        assert result["plan"] == plan and plan["concurrency"] == concurrency and plan["process_count"] == 8
        assert not result["failure"] and result["exitcodes"] == [0] * 8
        reports = result["reports"]
        assert len(reports) == len({r["pid"] for r in reports}) == 8
        assert not any(r["errors"] for r in reports)
        assert sum(r["pool_size"] for r in reports) == 2 * concurrency
        for report in reports:
            assert report["client_config"]["max_pool_connections"] == report["pool_size"]
            assert report["client_config"]["retries"]["total_max_attempts"] == 1
            assert report["client_ready_perf"] <= result["release_perf"]
            assert all(e["pid"] == report["pid"] for e in report["events"])
            assert {r["request_idx"] for r in report["rows"]} == set(report["indices"])
        assert sorted(raw["requests"], key=lambda r: r["request_idx"]) == sorted(
            [r for report in reports for r in report["rows"]], key=lambda r: r["request_idx"])
        assert raw["events"] == [e for report in reports for e in report["events"]]
        rows, events = raw["requests"], raw["events"]
        assert len(rows) == concurrency and {r["request_idx"] for r in rows} == set(range(concurrency))
        planned = {i: sid for a in plan["assignments"] for i, sid in a["indices_sessions"]}
        assert {r["request_idx"]: r["session_id"] for r in rows} == planned
        for row in rows:
            assert row["concurrency"] == concurrency and row["round"] == 1
        for field, value in stats(rows).items():
            assert summary[field] == value, (concurrency, field)
        assert summary["warm_success"] == sum(r["warm_ms"] is not None for r in rows)
        assert summary["stop_success"] == sum(r["stopped"] for r in rows)
        groups = defaultdict(list)
        for event in events:
            assert event["started_perf"] >= result["release_perf"]
            groups[event["session_id"]].append(event)
        assert set(groups) == {r["session_id"] for r in rows}
        for row in rows:
            verify_session(row, groups[row["session_id"]], runtime["arn"])
        smoke = json.loads((folder / "smoke.json").read_text())
        assert smoke["request"]["success"] and smoke["request"]["warm_ms"] is not None and smoke["request"]["stopped"]
        verify_session(smoke["request"], smoke["events"], runtime["arn"])
        all_ids.extend([r["session_id"] for r in rows] + [smoke["request"]["session_id"]])
        first = [e for e in events if e["operation"] == "invoke" and e["attempt"] == 1]
        good = [e for e in first if e.get("valid")]
        api_spread = (max(e["started_perf"] for e in first) - min(e["started_perf"] for e in first)) * 1000
        send_spread = (max(e["before_send_perf"][0] for e in first) - min(e["before_send_perf"][0] for e in first)) * 1000
        output.append({**summary, "api_start_spread_ms": round(api_spread, 3),
            "before_send_spread_ms": round(send_spread, 3),
            "tight_burst_target_met": send_spread <= plan["before_send_spread_target_ms"],
            "first_peak_overlap": overlap(first),
            "pre_transport_ms": percentiles([(e["before_send_perf"][0] - e["started_perf"]) * 1000 for e in good]),
            "transport_through_sdk_response_ms": percentiles([(e["response_received_perf"][0] - e["before_send_perf"][0]) * 1000 for e in good]),
            "response_to_body_ms": percentiles([(e["completed_perf"] - e["response_received_perf"][0]) * 1000 for e in good]),
            "release_to_body_ms": percentiles([(e["completed_perf"] - result["release_perf"]) * 1000 for e in good]),
            "worker_cpu_seconds": round(sum(r["cpu_seconds"] for r in reports), 4),
            "worker_peak_rss_sum_mib": round(sum(r["max_rss_bytes"] for r in reports) / 1048576, 2)})
    assert len(all_ids) == len(set(all_ids)) == 353
    expected_success = all(c["success"] == c["warm_success"] == c["samples"] for c in output)
    assert meta["all_invocations_successful"] == expected_success
    assert meta["exit_code"] == (0 if expected_success and all(c["stop_success"] == c["samples"] for c in output) else 1)
    print("PASS: 350 planned first calls + 3 smoke; 24 spawned workers, complete API/timing/source/cleanup evidence")
    print(json.dumps(output, indent=2))
    return output



if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("out", type=Path)
    verify(parser.parse_args().out)
