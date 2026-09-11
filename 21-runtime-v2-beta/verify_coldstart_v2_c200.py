"""Offline independent acceptance of a c200 experiment, including throttled attempts."""
import argparse
from collections import Counter, defaultdict
from datetime import datetime
import hashlib
import json
from pathlib import Path
import statistics

from verify_coldstart import stats

HERE = Path(__file__).resolve().parent

def verify(out):
    meta = json.loads((out / "run.json").read_text())
    dep = json.loads((out / "deployments.json").read_text())
    cleanup = json.loads((out / "cleanup.json").read_text())
    requests = json.loads((out / "create_requests.json").read_text())
    assert meta["measurement_complete"] and meta["cleanup_complete"]
    assert meta["exit_code"] in (0, 1) and not meta.get("error")
    assert meta["concurrency"] == 200 and meta["rounds"] == 1
    assert meta["max_pool_connections"] == 400 and meta["retries"]["total_max_attempts"] == 1
    for name, digest in meta["source_sha256"].items():
        assert hashlib.sha256((HERE.parent / name).read_bytes()).hexdigest() == digest, name
    assert set(dep["runtimes"]) == {"500mb", "1gb", "2gb"}
    assert {r["id"] for r in cleanup["runtimes"]} == {r["id"] for r in dep["runtimes"].values()}
    assert all(r["deleted"] for r in cleanup["runtimes"])
    events = [json.loads(line) for line in (out / "api_events.jsonl").read_text().splitlines()]
    by_session = defaultdict(list)
    for event in events:
        by_session[event["session_id"]].append(event)
    summary = {c["size"]: c for c in json.loads((out / "summary.json").read_text())["cells"]}
    assert set(summary) == set(dep["runtimes"])
    assert len(list((out / "raw").glob("*.json"))) == 3
    all_rows, table = [], []
    for size, runtime in dep["runtimes"].items():
        ready = runtime["ready_response"]
        assert ready["status"] == "READY" and ready["platformVersion"] == "V2"
        for field in ["agentRuntimeArtifact", "roleArn", "networkConfiguration",
                      "protocolConfiguration", "lifecycleConfiguration"]:
            assert ready[field] == requests[size][field]
        assert ready["agentRuntimeArtifact"]["containerConfiguration"]["containerUri"].endswith(runtime["image_digest"])
        cell = json.loads((out / "raw" / (size + "_c200.json")).read_text())
        assert cell["meta"]["concurrency"] == 200 and cell["meta"]["rounds"] == 1
        assert cell["meta"]["runtime_arn"] == runtime["arn"]
        assert not cell["worker_errors"]
        rows = cell["requests"]
        assert len(rows) == 200 and {r["request_idx"] for r in rows} == set(range(200))
        assert all(r["round"] == 1 and r["concurrency"] == 200 and r["size"] == size for r in rows)
        for key, value in stats(rows).items():
            assert summary[size][key] == value, (size, key)
        assert summary[size]["warm_success"] == sum(r["warm_ms"] is not None for r in rows)
        assert summary[size]["stop_success"] == sum(r["stopped"] for r in rows)
        smoke = json.loads((out / ("smoke_" + size + ".json")).read_text())["requests"]
        assert len(smoke) == 1 and smoke[0]["success"] and smoke[0]["warm_ms"] is not None
        for row in rows + smoke:
            assert len(row["session_id"]) == 40
            group = by_session[row["session_id"]]
            invokes = sorted((e for e in group if e["operation"] == "invoke"), key=lambda e: e["attempt"])
            stops = [e for e in group if e["operation"] == "stop"]
            assert len(stops) == 1
            if row["stopped"]:
                assert stops[0]["response"]["ResponseMetadata"]["HTTPStatusCode"] == 200
            else:
                # Never relabel this as a successful stop. Only accept known-absent
                # sessions after a rejected cold request; runtime deletion is also required.
                assert not row["success"] and row["error_type"] == "throttle"
                assert stops[0].get("error", "").startswith("ResourceNotFoundException:")
            assert [e["attempt"] for e in invokes] == ([1, 2] if row["success"] else [1])
            assert all(e["runtime_arn"] == runtime["arn"] for e in group)
            assert all(e.get("metadata", {}).get("RetryAttempts", 0) == 0 for e in invokes)
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
                assert datetime.fromisoformat(warm["started_iso"]) >= datetime.fromisoformat(first["finished_iso"])
            else:
                assert first["error_type"] == row["error_type"]
                assert row["cold_ms"] is None and row["warm_ms"] is None
            for event in invokes:
                if event.get("valid"):
                    assert event["status"] == 200 and event["body"]["message"] == "pong"
                    assert event["body"]["echo"] == {"ping": "coldstart"}
            all_rows.append(row)
        starts = [next(e["started_perf"] for e in by_session[r["session_id"]]
                       if e["operation"] == "invoke" and e["attempt"] == 1) for r in rows]
        successful = [r["cold_ms"] for r in rows if r["success"]]
        errors = Counter(next(e.get("error_code", e.get("error_type", "success"))
                             for e in by_session[r["session_id"]] if e["operation"] == "invoke"
                             and e["attempt"] == 1) for r in rows)
        table.append({**summary[size], "dispatch_spread_ms": (max(starts) - min(starts)) * 1000,
                      "p95_ms": percentile(successful, 95), "p99_ms": percentile(successful, 99),
                      "errors": dict(errors)})
    assert len(all_rows) == 603 and len({r["session_id"] for r in all_rows}) == 603
    assert set(by_session) == {r["session_id"] for r in all_rows}
    matrix = [r for r in all_rows if r["concurrency"] == 200]
    assert meta["all_invocations_successful"] == all(r["success"] and r["warm_ms"] is not None for r in matrix)
    print("PASS: 600 matrix attempts, 3 smoke, 603 unique sessions; full API evidence reconciled")
    assert meta["exit_code"] == (0 if all(r["stopped"] for r in matrix) else 1)
    print("PASS: source hashes, identical image digests, V2 and 3 deletions")
    print("STOP OUTCOMES:", sum(r["stopped"] for r in all_rows), "HTTP 200;",
          sum(not r["stopped"] for r in all_rows), "ResourceNotFound after cold throttle")
    print(json.dumps(table, indent=2))
    return table


def percentile(values, p):
    if not values:
        return None
    if len(values) == 1:
        return values[0]
    return round(statistics.quantiles(values, n=100, method="inclusive")[p - 1], 1)



if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("out", type=Path)
    args = parser.parse_args()
    verify(args.out)
