"""Derive statistics and evidence checks from saved raw events."""
import argparse
import json
import math
from pathlib import Path

from client import save


def percentiles(values):
    values = sorted(values)
    if not values:
        return {"n": 0, "p50": None, "p95": None, "p99": None, "max": None}
    return {"n": len(values), **{f"p{p}": values[math.ceil(len(values) * p / 100) - 1]
        for p in (50, 95, 99)}, "max": values[-1]}


def peak(intervals):
    points = sorted([(start, 1) for start, _ in intervals] + [(end, -1) for _, end in intervals])
    active = maximum = 0
    for _, delta in points:
        active += delta
        maximum = max(maximum, active)
    return maximum


def spread(values):
    return (max(values) - min(values)) * 1000 if values else None


def summarize(raw):
    plan = raw["plan"]
    events = [e for r in raw["reports"] for e in r["events"]]
    cold = [e for e in events if e["phase"] == "cold"]
    warm = [e for e in events if e["phase"] == "warm"]
    good = [e for e in cold if e["valid"]]
    rows = [r for report in raw["reports"] for r in report["rows"]]
    pids = {r["pid"] for r in raw["reports"]}
    n = plan["concurrency"]
    checks = {
        "workers_complete": not raw["failure"] and raw["exitcodes"] == [0] * plan["process_count"],
        "independent_processes": len(pids) == plan["process_count"],
        "cold_count": len(cold) == n,
        "unique_sessions": len({e["session_id"] for e in cold}) == n,
        "unique_instances": len({e["body"]["instance_id"] for e in good}) == n,
        "cold_success": len(good) == n,
        "warm_success": sum(e["valid"] for e in warm) == n,
        "same_instances": len(rows) == n and all(r.get("same_instance") for r in rows),
        "sessions_stopped": len(rows) == n and all(r["stop_confirmed"] for r in rows),
        "no_retries": all(e.get("metadata", {}).get("RetryAttempts", 0) == 0
                          and len(e["before_send_perf"]) <= 1 for e in events),
        "complete_cold_timing": all(len(e["before_send_perf"]) == 1 for e in cold) and len(cold) == n,
        "warm_after_all_cold": bool(warm) and bool(cold) and min(e["started_perf"] for e in warm)
                              >= max(e["completed_perf"] for e in cold),
    }
    cold_start = min((e["started_perf"] for e in cold), default=0)
    cold_end = max((e["completed_perf"] for e in cold), default=0)
    cpu = []
    for a, b in zip(raw["cpu_samples"], raw["cpu_samples"][1:]):
        if b["perf"] < cold_start or a["perf"] > cold_end:
            continue
        # guest/guest_nice already included in user/nice; avoid counting twice.
        delta = [y - x for x, y in zip(a["cpu"][:8], b["cpu"][:8])]
        total = sum(delta)
        if total:
            cpu.append({"busy_pct": 100 * (total - delta[3] - delta[4]) / total,
                        "steal_pct": 100 * delta[7] / total})
    return {"concurrency": n, "process_count": len(pids), "hold_seconds": plan["hold_seconds"],
        "cold_success": len(good), "cold_failures": n - len(good),
        "cold_ms": percentiles([e["elapsed_ms"] for e in good]),
        "warm_ms": percentiles([e["elapsed_ms"] for e in warm if e["valid"]]),
        "api_start_spread_ms": spread([e["started_perf"] for e in cold]),
        "before_send_spread_ms": spread([e["before_send_perf"][0] for e in cold if e["before_send_perf"]]),
        "client_peak": peak([(e["started_perf"], e["completed_perf"]) for e in cold]),
        "handler_peak": peak([(e["body"]["handler_started_unix_s"],
                               e["body"]["handler_finished_unix_s"]) for e in good]),
        "worker_cpu_seconds": sum(r["cpu_seconds"] for r in raw["reports"]),
        "worker_peak_rss_sum_bytes": sum(r["max_rss_bytes"] for r in raw["reports"]),
        "cold_cpu_busy_pct": percentiles([s["busy_pct"] for s in cpu]),
        "cold_cpu_steal_pct": percentiles([s["steal_pct"] for s in cpu]),
        "checks": checks, "all_checks_pass": all(checks.values())}


def analyze(root):
    root = Path(root)
    cells = {}
    grouped = {}
    for path in sorted(root.glob("cells/*/raw.json")):
        raw = json.loads(path.read_text())
        result = summarize(raw)
        cells[path.parent.name] = result
        save(path.parent / "summary.json", result)
        if raw["plan"]["hold_seconds"]:
            continue
        level = str(result["concurrency"])
        bucket = grouped.setdefault(level, {"cold": [], "warm": [], "failures": 0, "rounds": 0})
        bucket["rounds"] += 1
        bucket["failures"] += result["cold_failures"]
        for report in raw["reports"]:
            for event in report["events"]:
                if event["phase"] in ("cold", "warm") and event["valid"]:
                    bucket[event["phase"]].append(event["elapsed_ms"])
    aggregate = {k: {"rounds": v["rounds"], "cold_failures": v["failures"],
        "cold_ms": percentiles(v["cold"]), "warm_ms": percentiles(v["warm"])}
        for k, v in grouped.items()}
    run_path = root / "run.json"
    expected = set(json.loads(run_path.read_text())["config"]["runtimes"]) if run_path.exists() else set()
    output = {"cells": cells, "aggregate": aggregate,
              "all_checks_pass": set(cells) == expected and bool(expected)
                                and all(c["all_checks_pass"] for c in cells.values())}
    save(root / "summary.json", output)
    return output


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("directory", type=Path)
    print(json.dumps(analyze(parser.parse_args().directory), indent=2))
