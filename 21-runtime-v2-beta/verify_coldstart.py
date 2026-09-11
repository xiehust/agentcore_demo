"""Independently verify a completed V2 cold-start run and print report tables."""

import argparse
from collections import Counter, defaultdict
import hashlib
import json
from pathlib import Path
import statistics

HERE = Path(__file__).resolve().parent
BASELINE = HERE.parent / "10-runtime-coldstart"

def pct(values, p):
    return statistics.quantiles(values, n=100, method="inclusive")[p - 1]


def stats(records):
    cold = [r["cold_ms"] for r in records if r["success"]]
    warm = [r["warm_ms"] for r in records if r["warm_ms"] is not None]
    return {
        "samples": len(records), "success": len(cold),
        "throttles": sum(r["error_type"] == "throttle" for r in records),
        "other_errors": sum(r["error_type"] in ("other", "timeout") for r in records),
        "fresh_boots": sum(r["success"] and r["proc_start_ts"] is not None
            and r["request_ts"] is not None
            and (r["request_ts"] - r["proc_start_ts"]) * 1000 < r["cold_ms"]
            for r in records),
        "cold_p50_ms": round(statistics.median(cold), 1) if cold else None,
        "cold_p90_ms": round(pct(cold, 90), 1) if len(cold) > 1 else (cold[0] if cold else None),
        "cold_max_ms": round(max(cold), 1) if cold else None,
        "cold_mean_ms": round(statistics.mean(cold), 1) if cold else None,
        "warm_p50_ms": round(statistics.median(warm), 1) if warm else None,
    }


def load_cells(out):
    cells = {}
    for path in sorted((out / "raw").glob("*.json")):
        if path.name.startswith("smoke"):
            continue
        value = json.loads(path.read_text())
        key = (value["meta"]["size"], value["meta"]["concurrency"])
        cells[key] = (path, value)
    return cells


def verify(out):
    run = json.loads((out / "run.json").read_text())
    dep = json.loads((out / "deployments.json").read_text())
    cleanup = json.loads((out / "cleanup.json").read_text())
    assert run["exit_code"] == 0 and run["cleanup_complete"], run
    assert len(cleanup["runtimes"]) == 3 and all(r["deleted"] for r in cleanup["runtimes"])
    for name, digest in run["source_sha256"].items():
        assert hashlib.sha256((HERE.parent / name).read_bytes()).hexdigest() == digest, name
    rounds = {1: 10, 5: 4, 10: 2, 50: 1}
    expected = {(s, c) for s in ("500mb", "1gb", "2gb") for c in rounds}
    cells = load_cells(out)
    assert set(cells) == expected
    assert len(list((out / "raw").glob("*.json"))) == 12
    summaries = {(r["size"], r["concurrency"]): r for r in
                 json.loads((out / "summary.json").read_text())["cells"]}
    records = []
    for (size, concurrency), (path, cell) in cells.items():
        rt = dep["runtimes"][size]
        assert rt["ready_response"]["platformVersion"] == "V2"
        assert rt["ready_response"]["status"] == "READY"
        assert cell["meta"]["runtime_arn"] == rt["arn"]
        assert cell["meta"]["region"] == dep["region"] == "us-west-2"
        requests = cell["requests"]
        assert len(requests) == concurrency * rounds[concurrency], path
        assert Counter(r["round"] for r in requests) == {
            rnd: concurrency for rnd in range(1, rounds[concurrency] + 1)}
        for rec in requests:
            assert rec["success"] and rec["status_code"] == 200 and rec["stopped"], rec
            assert rec["warm_ms"] is not None and not rec["error_msg"], rec
            assert len(rec["session_id"]) == 40
        calculated = stats(requests)
        for key, value in calculated.items():
            assert summaries[(size, concurrency)][key] == value, (path, key, value)
        records.extend(requests)
    assert len(records) == 300 and len({r["session_id"] for r in records}) == 300
    smoke = [json.loads(path.read_text())["requests"][0] for path in sorted(out.glob("smoke_*.json"))]
    assert len(smoke) == 3
    all_records = records + smoke
    assert len({r["session_id"] for r in all_records}) == 303
    responses = defaultdict(list)
    for line in (out / "responses.jsonl").read_text().splitlines():
        item = json.loads(line)
        responses[item["session_id"]].append(item)
        assert item["status"] == 200 and item["body"]["message"] == "pong", item
        assert item["body"]["echo"] == {"ping": "coldstart"}, item
    assert len(responses) == 303
    for rec in all_records:
        pair = responses[rec["session_id"]]
        assert len(pair) == 2, rec
        assert rec["success"] and rec["stopped"] and rec["warm_ms"] is not None
        assert round(pair[0]["elapsed_ms"], 1) == rec["cold_ms"]
        assert round(pair[1]["elapsed_ms"], 1) == rec["warm_ms"]
        assert pair[0]["body"]["proc_start_ts"] == rec["proc_start_ts"]
        assert pair[1]["body"]["proc_start_ts"] == rec["proc_start_ts"]
    baseline = load_cells(BASELINE / "results")
    baseline_summary = {(r["size"], r["concurrency"]): r for r in
        json.loads((BASELINE / "results" / "summary.json").read_text())["cells"]}
    for key, (_, cell) in baseline.items():
        for field, value in stats(cell["requests"]).items():
            assert baseline_summary[key][field] == value, (key, field)
    print("PASS: 12 cells, 300 unique matrix sessions, 3 smoke sessions, 606 valid responses")
    print("PASS: independent statistics match V2 and historical summaries; 3 V2 deployments confirmed deleted")
    print("PASS: source hashes, round counts, paired warm responses, successful stops and platformVersion checked")
    return cells, baseline, records, run, dep


def report(cells, baseline, records, run, dep):
    print("\nMATRIX: image | c | OK/n | p50 | p90 | max | mean | warm p50 | fresh heuristic")
    for key, (_, cell) in cells.items():
        s = stats(cell["requests"])
        print(f"| {key[0]} | {key[1]} | {s['success']}/{s['samples']} | "
              f"{s['cold_p50_ms']} | {s['cold_p90_ms']} | {s['cold_max_ms']} | "
              f"{s['cold_mean_ms']} | {s['warm_p50_ms']} | {s['fresh_boots']} |")
    print("\nCOMPARISON: image | c | historical p50 | V2 p50 | decrease % | ratio old/new")
    for key, (_, cell) in cells.items():
        old = stats(baseline[key][1]["requests"])["cold_p50_ms"]
        new = stats(cell["requests"])["cold_p50_ms"]
        print(f"| {key[0]} | {key[1]} | {old} | {new} | {(1-new/old)*100:.1f}% | {old/new:.2f} |")
    print("\nPOOLED_V2", json.dumps(stats(records)))
    print("TIME", run["started_iso"], run["matrix_started_iso"],
          run["matrix_finished_iso"], run["finished_iso"])
    for size in ("500mb", "1gb", "2gb"):
        rows = [r for r in records if r["size"] == size]
        ages = [(r["request_ts"] - r["proc_start_ts"]) for r in rows if r["success"]]
        print("IMAGE", size, "PROCESS_TIMESTAMPS", len({r["proc_start_ts"] for r in rows}),
              "AGE_SECONDS min/p50/max", min(ages), statistics.median(ages), max(ages),
              "POOLED", json.dumps(stats(rows)))
        rt = dep["runtimes"][size]
        ready = rt["ready_response"]
        print("DEPLOYMENT", size, rt["id"], rt["create_started_iso"],
              rt["ready_observed_iso"], str(ready.get("lastUpdatedAt")))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("out", type=Path)
    args = parser.parse_args()
    report(*verify(args.out))


if __name__ == "__main__":
    main()
