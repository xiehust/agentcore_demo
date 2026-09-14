"""Offline independent acceptance of this bounded us-east-1 evidence set."""
from collections import defaultdict
import hashlib
import json
from pathlib import Path
import statistics

OUT = Path(__file__).resolve().parent
ROOT = OUT.parents[2]


def load(name):
    return json.loads((OUT / name).read_text())


def stats(rows):
    cold = [r["cold_ms"] for r in rows]
    warm = [r["warm_ms"] for r in rows]
    return {"samples": len(rows), "success": len(cold), "throttles": 0, "other_errors": 0,
            "cold_p50_ms": round(statistics.median(cold), 1),
            "cold_p90_ms": round(statistics.quantiles(cold, n=10, method="inclusive")[8], 1),
            "cold_max_ms": round(max(cold), 1), "cold_mean_ms": round(statistics.mean(cold), 1),
            "warm_p50_ms": round(statistics.median(warm), 1)}


def verify():
    run, dep = load("run.json"), load("deployments.json")
    assert run["exit_code"] == 0 and run["cleanup_complete"] and run["measurement_complete"]
    assert run["region"] == dep["region"] == "us-east-1"
    assert run["account"] == dep["account"] == "434444145045"
    for path, digest in run["source_sha256"].items():
        assert hashlib.sha256((ROOT / path).read_bytes()).hexdigest() == digest, path
    assert set(dep["runtimes"]) == {"500mb"}
    runtime = dep["runtimes"]["500mb"]
    assert runtime["ready_response"]["platformVersion"] == "V2"
    assert runtime["ready_response"]["status"] == "READY"
    cleanup = load("cleanup.json")["runtimes"]
    assert len(cleanup) == 1 and cleanup[0]["deleted"] and cleanup[0]["id"] == runtime["id"]
    request = load("create_request.json")
    assert request["platformVersion"] == "V2"
    assert ".ecr.us-east-1." in request["agentRuntimeArtifact"]["containerConfiguration"]["containerUri"]
    assert request["agentRuntimeArtifact"]["containerConfiguration"]["containerUri"].endswith(
        "@" + load("images.json")["500mb"]["imageDigest"])
    smoke = load("smoke.json")
    groups = [("smoke", [smoke["request"]], smoke["events"])]
    serial = load("serial.json")
    groups.append(("serial", serial["requests"], serial["events"]))
    assert len(serial["requests"]) == 10
    assert {r["round"] for r in serial["requests"]} == set(range(1, 11))
    spreads = []
    for rnd in (1, 2):
        folder = f"c10_round{rnd}"
        result, raw = load(f"{folder}/client_result.json"), load(f"{folder}/raw.json")
        plan = result["plan"]
        assert plan == load(f"{folder}/client_plan.json")
        assert plan["region"] == "us-east-1" and plan["runtime_arn"] == runtime["arn"]
        assert plan["process_count"] == 8 and plan["concurrency"] == 10
        assert result["failure"] is None and result["exitcodes"] == [0] * 8
        assert len(result["reports"]) == 8 and all(not r["errors"] for r in result["reports"])
        assigned = {sid for a in plan["assignments"] for _, sid in a["indices_sessions"]}
        assert len(raw["requests"]) == 10 and assigned == {r["session_id"] for r in raw["requests"]}
        assert sorted([r for p in result["reports"] for r in p["rows"]], key=lambda r: r["request_idx"]) == raw["requests"]
        assert [e for p in result["reports"] for e in p["events"]] == raw["events"]
        sends = [e["before_send_perf"][0] for e in raw["events"] if e["operation"] == "invoke" and e["attempt"] == 1]
        assert len(sends) == 10 and min(sends) >= result["release_perf"] > 0
        spreads.append(round((max(sends) - min(sends)) * 1000, 3))
        groups.append((folder, raw["requests"], raw["events"]))
    summaries = {s["group"]: s for s in load("summary.json")["cells"]}
    seen = set()
    for name, rows, events in groups:
        by_session = defaultdict(list)
        assert len(events) == 3 * len(rows)
        for e in events:
            assert e["valid"] and e["status"] == 200 and not e.get("error")
            assert e["runtime_arn"] == runtime["arn"]
            assert e["metadata"]["RetryAttempts"] == 0 and e["metadata"]["RequestId"]
            assert len(e["before_send_perf"]) == len(e["response_received_perf"]) == 1
            assert abs(e["elapsed_ms"] - (e["completed_perf"] - e["started_perf"]) * 1000) < .000001
            by_session[e["session_id"]].append(e)
        assert set(by_session) == {r["session_id"] for r in rows}
        for r in rows:
            sid = r["session_id"]
            assert len(sid) == 40 and sid not in seen
            seen.add(sid)
            assert r["success"] and r["stopped"] and not r["error_msg"]
            cold, warm, stop = by_session[sid]
            assert [e["operation"] for e in (cold, warm, stop)] == ["invoke", "invoke", "stop"]
            assert [cold["attempt"], warm["attempt"]] == [1, 2]
            assert r["cold_ms"] == round(cold["elapsed_ms"], 1)
            assert r["warm_ms"] == round(warm["elapsed_ms"], 1)
            for e in (cold, warm):
                body = e["body"]
                assert body["message"] == "pong" and body["echo"] == {"ping": "coldstart"}
                assert body["proc_start_ts"] == r["proc_start_ts"] <= body["request_ts"]
            assert cold["body"]["request_ts"] == r["request_ts"]
        if name != "smoke":
            for key, value in stats(rows).items():
                assert summaries[name][key] == value, (name, key, value)
    assert len(seen) == 31
    print("PASS: source hashes, V2/us-east-1, 31 unique sessions, 62 valid invokes, 31 stops, deletion")
    print("PASS: independent statistics, paired process timestamps, no SDK retries, both 8-process bursts")
    print("BEFORE_SEND_SPREAD_MS", spreads)
    for name, rows, _ in groups[1:]:
        print(name, json.dumps(stats(rows)))
    print("C10_POOLED", json.dumps(stats([r for _, rows, _ in groups[2:] for r in rows])))
    print("TIME", run["started_iso"], run["finished_iso"], "READY_SECONDS", run["ready_seconds"])
    return groups

if __name__ == "__main__":
    verify()
