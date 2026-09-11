"""Read-only independent verification of all 15 multiprocess matrix cells."""
import argparse
from collections import Counter, defaultdict
from datetime import datetime
import hashlib
import json
from pathlib import Path
from statistics import median, quantiles

from verify_coldstart import stats
from verify_multiprocess_coldstart import verify_session, overlap, percentiles

HERE = Path(__file__).resolve().parent
SIZES = ("500mb", "1gb", "2gb")
LEVELS = (1, 10, 50, 100, 200)

def check_cell(cell, image):
    size, concurrency = cell["size"], cell["concurrency"]
    count = min(8, concurrency)
    folder = HERE / cell["folder"]
    runtime = cell["runtime"]
    ready = runtime["ready_response"]
    assert runtime["deleted"] and ready["status"] == "READY" and ready["platformVersion"] == "V2"
    assert ready["agentRuntimeArtifact"]["containerConfiguration"]["containerUri"].endswith("@" + image["imageDigest"])
    result = json.loads((folder / "client_result.json").read_text())
    raw = json.loads((folder / "raw.json").read_text())
    summary = json.loads((folder / "summary.json").read_text())
    plan = json.loads((folder / "client_plan.json").read_text())
    assert plan == result["plan"] and plan["runtime_arn"] == runtime["arn"]
    assert plan["region"] == "us-west-2" and plan["concurrency"] == concurrency
    assert plan["process_count"] == count and plan["start_method"] == "spawn"
    assert result["failure"] is None and result["exitcodes"] == [0] * count
    reports = result["reports"]
    assert len(reports) == len({p["pid"] for p in reports}) == count
    assert not any(p["errors"] for p in reports)
    assert sum(p["pool_size"] for p in reports) == 2 * concurrency
    assert sorted(raw["requests"], key=lambda r: r["request_idx"]) == sorted(
        [r for p in reports for r in p["rows"]], key=lambda r: r["request_idx"])
    assert raw["events"] == [e for p in reports for e in p["events"]]
    rows, events = raw["requests"], raw["events"]
    planned = {i: sid for assignment in plan["assignments"] for i, sid in assignment["indices_sessions"]}
    assert len(rows) == len(planned) == concurrency
    assert set(planned) == set(range(concurrency))
    assert {r["request_idx"]: r["session_id"] for r in rows} == planned
    for p in reports:
        assert p["client_config"]["max_pool_connections"] == p["pool_size"]
        assert p["client_config"]["retries"]["total_max_attempts"] == 1
        assert p["client_ready_perf"] <= result["release_perf"]
        assert p["indices"] and {r["request_idx"] for r in p["rows"]} == set(p["indices"])
        assert all(e["pid"] == p["pid"] for e in p["events"])
    groups = defaultdict(list)
    for event in events:
        assert event["started_perf"] >= result["release_perf"]
        groups[event["session_id"]].append(event)
    assert set(groups) == {r["session_id"] for r in rows}
    for row in rows:
        assert row["size"] == size and row["concurrency"] == concurrency and row["round"] == 1
        verify_session(row, groups[row["session_id"]], runtime["arn"])
    for key, value in stats(rows).items():
        assert summary[key] == value, (size, concurrency, key)
    assert summary == cell["summary"]
    assert summary["size"] == size and summary["concurrency"] == concurrency
    assert summary["warm_success"] == sum(r["warm_ms"] is not None for r in rows)
    assert summary["stop_success"] == sum(r["stopped"] for r in rows)
    assert summary["stop_absent"] == sum(r["stop_absent"] for r in rows)
    smoke = json.loads((folder / "smoke.json").read_text())
    assert smoke["request"]["size"] == size
    assert smoke["request"]["success"] and smoke["request"]["warm_ms"] is not None and smoke["request"]["stopped"]
    verify_session(smoke["request"], smoke["events"], runtime["arn"])
    cleanup = json.loads((folder / "cleanup.json").read_text())["runtimes"]
    assert len(cleanup) == 1 and cleanup[0]["deleted"] and cleanup[0]["id"] == runtime["id"]
    first = [e for e in events if e["operation"] == "invoke" and e["attempt"] == 1]
    successful = [e["elapsed_ms"] for e in first if e.get("valid")]
    first_iso = min(datetime.fromisoformat(e["started_iso"]) for e in first)
    stop_iso = datetime.fromisoformat(smoke["events"][-1]["finished_iso"])
    settle = (first_iso - stop_iso).total_seconds()
    assert settle >= 180, (size, concurrency, settle)
    start_spread = (max(e["started_perf"] for e in first) - min(e["started_perf"] for e in first)) * 1000
    send_spread = (max(e["before_send_perf"][0] for e in first) - min(e["before_send_perf"][0] for e in first)) * 1000
    row = {**summary, "reused": cell["reused"], "processes": count, "actual_settle_seconds": round(settle, 3),
           "api_start_spread_ms": round(start_spread, 3), "before_send_spread_ms": round(send_spread, 3),
           "tight_burst": send_spread <= plan["before_send_spread_target_ms"], "peak_inflight": overlap(first),
           "p95_ms": round(quantiles(successful, n=100, method="inclusive")[94], 1) if len(successful) > 1 else None,
           "p99_ms": round(quantiles(successful, n=100, method="inclusive")[98], 1) if len(successful) > 1 else None,
           "first_started_iso": first_iso.isoformat(), "last_finished_iso": max(e["finished_iso"] for e in events),
           "worker_cpu_seconds": round(sum(p["cpu_seconds"] for p in reports), 4),
           "errors": dict(Counter(e.get("error_code", e.get("error_type")) for e in first if not e.get("valid")))}
    return row, [r["session_id"] for r in rows] + [smoke["request"]["session_id"]]


def verify(out):
    state = json.loads((out / "matrix.json").read_text())
    expected = {f"{size}_c{c}" for size in SIZES for c in LEVELS}
    assert set(state["cells"]) == expected
    assert state["measurement_complete"] and state["cleanup_complete"] and not state.get("error")
    assert state["region"] == "us-west-2" and state["sizes"] == list(SIZES) and state["levels"] == list(LEVELS)
    assert not state.get("create_inflight")
    for name, digest in state["source_sha256"].items():
        assert hashlib.sha256((HERE.parent / name).read_bytes()).hexdigest() == digest, name
    output, session_ids, runtime_ids = [], [], []
    reused = {k for k, c in state["cells"].items() if c["reused"]}
    assert reused == {"500mb_c50", "500mb_c100", "500mb_c200"}
    assert set(state["requests"]) == expected - reused
    for size in SIZES:
        for concurrency in LEVELS:
            key = f"{size}_c{concurrency}"
            cell = state["cells"][key]
            assert cell["size"] == size and cell["concurrency"] == concurrency
            if cell["reused"]:
                for name, digest in cell["evidence_sha256"].items():
                    assert hashlib.sha256((HERE / name).read_bytes()).hexdigest() == digest, name
            else:
                assert (HERE / cell["folder"]).parent == out.resolve()
                for field in ("agentRuntimeArtifact", "roleArn", "networkConfiguration",
                              "protocolConfiguration", "lifecycleConfiguration"):
                    assert state["requests"][key][field] == cell["runtime"]["ready_response"][field]
            row, ids = check_cell(cell, state["images"][size])
            output.append(row)
            session_ids.extend(ids)
            runtime_ids.append(cell["runtime"]["id"])
    assert len(runtime_ids) == len(set(runtime_ids)) == 15
    assert len(session_ids) == len(set(session_ids)) == 1098
    assert sum(r["samples"] for r in output) == 1083
    assert sum(r["samples"] for r in output if not r["reused"]) == 733
    successful = all(r["success"] == r["warm_success"] == r["samples"] for r in output)
    assert state["all_invocations_successful"] == successful
    assert state["exit_code"] == (0 if successful and all(r["stop_success"] == r["samples"] for r in output) else 1)
    print("PASS: all 15 multiprocess cells, 1083 first attempts + 15 smoke, 1098 unique sessions")
    print("PASS: 3 immutable reused cells, 12 new cells; source hashes, images, timings, stops and deletions")
    print(json.dumps(output, indent=2))
    return output



if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("out", type=Path)
    verify(parser.parse_args().out)
