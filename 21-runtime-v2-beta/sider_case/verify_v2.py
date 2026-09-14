"""Collect exact-runtime usage records and verify Sider A/B phase coverage offline.

Platform GB-labelled interval means are not guest GiB or actual billing data.
"""
import argparse
import json
import math
from pathlib import Path
import statistics
import time

from run_v2 import client, write

def normalize(event, arn, sids):
    r = json.loads(event["message"])
    attrs, metrics = r.get("attributes"), r.get("metrics")
    if not isinstance(attrs, dict) or not isinstance(metrics, dict):
        raise ValueError("Expected nested usage attributes and metrics")
    for container in (attrs, metrics, r.get("resource", {})):
        if "resource_arn" in container or "event_timestamp" in container:
            raise ValueError("Conflicting usage identity fields")
    if attrs.get("session.id") not in sids or r.get("resource_arn") != arn:
        return None
    ts = float(r["event_timestamp"])
    if ts > 1e11:
        ts /= 1000
    dt = float(attrs.get("time_elapsed_seconds", attrs.get("elapsed_time_seconds", 0)))
    hours = float(metrics["agent.runtime.memory.gb_hours.used"])
    assert all(math.isfinite(v) for v in (ts, dt, hours)) and dt > 0 and hours >= 0
    return {"sid": attrs["session.id"], "ts": ts, "seconds": dt,
            "gb_hours": hours, "gb": hours * 3600 / dt,
            "event_id": event["eventId"]}


def phase_stats(rows, start, end):
    selected = [r for r in rows if start <= r["ts"] <= end]
    if not selected:
        return {"count": 0, "start": start, "end": end}
    duration = sum(r["seconds"] for r in selected)
    mean = sum(r["gb_hours"] for r in selected) * 3600 / duration
    x = [r["ts"] - start for r in selected]
    y = [r["gb"] for r in selected]
    xm, ym = statistics.mean(x), statistics.mean(y)
    denom = sum((v - xm) ** 2 for v in x)
    slope = 60 * sum((a - xm) * (b - ym) for a, b in zip(x, y)) / denom if denom else None
    return {"count": len(selected), "start": start, "end": end, "seconds": duration,
            "mean_gb": mean, "min_gb": min(y), "max_gb": max(y), "slope_gb_per_min": slope}


def analyze(out, events):
    state = json.loads((out / "state.json").read_text())
    records = json.loads((out / "events.json").read_text())
    samples = [json.loads(line) for line in (out / "memory_samples.jsonl").read_text().splitlines()]
    assert state["runtime"]["platformVersion"] == "V2"
    assert len(records) == 2 and {r["arm"] for r in records} == {"A", "B"}
    sids = {r["sid"] for r in records}
    assert len(sids) == 2
    unique = {e["eventId"]: e for e in events}
    rows = [r for e in unique.values() if (r := normalize(e, state["runtime_arn"], sids))]
    rows.sort(key=lambda r: r["ts"])
    # Reject semantic duplicate intervals, even if delivery assigned new IDs.
    keys = [(r["sid"], r["ts"]) for r in rows]
    assert len(keys) == len(set(keys)), "Duplicate session interval timestamps"
    report = {"raw_events": len(events), "unique_events": len(unique), "matched_records": len(rows),
              "complete": True, "arms": {}}
    for rec in records:
        arm, sid = rec["arm"], rec["sid"]
        ss = sorted([s for s in samples if s["_session_id"] == sid], key=lambda s: s["ts"])
        rr = [r for r in rows if r["sid"] == sid]
        assert len(ss) == 360 and all(s["session_id"] == sid and s["_arm"] == arm for s in ss)
        expected_events = ["connect"] if arm == "A" else ["connect", "alloc", "free"]
        assert [e[0] for e in rec["events"]] == expected_events, "Unexpected arm commands"
        assert len(rec["responses"]) == (1 if arm == "A" else 3)
        cadence = [b["ts"] - a["ts"] for a, b in zip(ss, ss[1:])]
        assert all(0 < d <= 10 for d in cadence), "Guest cadence/phase transition gap"
        boot = rec["responses"][0]["process_start_ts"]
        assert all(abs(s["ts"] - s["uptime_s"] - boot) < .1 for s in ss), "Process restart or inconsistent clock"
        a = {"session_id": sid, "sample_count": len(ss), "usage_count": len(rr), "phases": {},
             "process_start_consistent": True}
        phases = ("pre", "held_noop" if arm == "A" else "held", "tail")
        assert [s["_phase"] for s in ss] == [p for p, n in zip(phases, (60, 60, 240)) for _ in range(n)]
        if arm == "B":
            for response, sample in zip(rec["responses"][1:], (ss[60], ss[120])):
                assert 0 <= sample["ts"] - response["ts"] < 3, "Command/sample transition mismatch"
        windows = {}
        for phase, count in zip(phases, (60, 60, 240)):
            ps = [s for s in ss if s["_phase"] == phase]
            assert len(ps) == count and [s["seq"] for s in ps] == list(range(count))
            start, end = ps[0]["ts"], ps[-1]["ts"]
            assert count - 2 <= end - start <= count + 15, "Unexpected guest phase duration"
            stats = phase_stats(rr, start + 3, end - 3)
            inside = [r for r in rr if start + 3 <= r["ts"] <= end - 3]
            gaps = [b["ts"] - a["ts"] for a, b in zip(inside, inside[1:])]
            coverage = bool(inside and len(inside) >= .95 * (end - start - 6)
                and inside[0]["ts"] <= start + 5 and inside[-1]["ts"] >= end - 5
                and max(gaps, default=0) <= 1.1
                and all(.95 <= r["seconds"] <= 1.05 for r in inside)
                and sum(r["seconds"] for r in inside) >= .95 * (end - start - 6))
            report["complete"] &= coverage
            stats.update(coverage_ok=coverage, guest_start=start, guest_end=end,
                         max_usage_gap_s=max(gaps, default=None),
                         guest={k: statistics.mean(s[k] for s in ps if s.get(k) is not None)
                                for k in ("proc_rss_mb", "vm_mem_used_mb", "vm_mem_cached_mb", "vm_mem_free_mb")})
            a["phases"][phase] = stats
            if phase == "pre": windows["pre_last30"] = (end - 30, end - 3)
            if phase in ("held", "held_noop"): windows["held_last30"] = (end - 30, end - 3)
            if phase == "tail":
                windows["tail_10_60"] = (start + 10, start + 60)
                windows["tail_180_240"] = (start + 180, min(start + 240, end - 3))
        a["windows"] = {k: phase_stats(rr, *v) for k, v in windows.items()}
        a["series"] = rr
        a["ping"] = rec["responses"][0]
        report["arms"][arm] = a
    b = report["arms"]["B"]
    br = next(r for r in records if r["arm"] == "B")
    assert [e[0] for e in br["events"]] == ["connect", "alloc", "free"]
    assert [r["held_mb"] for r in br["responses"][1:]] == [4096, 0]
    bp = b["phases"]
    assert bp["held"]["guest"]["proc_rss_mb"] - bp["pre"]["guest"]["proc_rss_mb"] > 3900
    assert bp["held"]["guest"]["proc_rss_mb"] - bp["tail"]["guest"]["proc_rss_mb"] > 3900
    if report["complete"]:
        w = b["windows"]
        pre, held, late = (w[k]["mean_gb"] for k in ("pre_last30", "held_last30", "tail_180_240"))
        b["held_to_late_drop_gb"] = held - late
        b["late_minus_pre_gb"] = late - pre
        b["fraction_excess_recovered"] = (held - late) / (held - pre) if held > pre else None
    return report


def collect(out, wait_seconds):
    state = json.loads((out / "state.json").read_text())
    logs = client("logs")
    deadline = time.monotonic() + wait_seconds
    previous = None
    stable = 0
    while True:
        events = [e for pg in logs.get_paginator("filter_log_events").paginate(
            logGroupName=state["log_group"], startTime=int((state["started"] - 60) * 1000),
            endTime=int((state["runtime_deleted_at"] + 60) * 1000)) for e in pg["events"]]
        write(out, "usage_raw.json", events)
        report = analyze(out, events)
        ids = {e["eventId"] for e in events}
        stable = stable + 1 if ids == previous else 0
        previous = ids
        report.update(collected_at=time.time(), stable_polls=stable)
        write(out, "analysis.json", report)
        print("USAGE", len(events), "coverage", report["complete"], "stable", stable, flush=True)
        if report["complete"] and stable >= 2:
            return report
        if time.monotonic() >= deadline:
            raise TimeoutError("Usage logs incomplete or still arriving; raw evidence preserved")
        time.sleep(min(60, max(0, deadline - time.monotonic())))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["collect", "verify"])
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--wait-seconds", type=int, default=3900)
    args = parser.parse_args()
    if args.command == "collect":
        if not 0 <= args.wait_seconds <= 3900:
            parser.error("wait-seconds must be 0..3900")
        report = collect(args.out, args.wait_seconds)
    else:
        report = analyze(args.out, json.loads((args.out / "usage_raw.json").read_text()))
        assert report["complete"], "Incomplete usage coverage"
        # Verification is read-only; keep collected_at/stable_polls evidence intact.
    print(json.dumps({"complete": report["complete"], "matched_records": report["matched_records"],
        "arms": {k: {f: v for f, v in a.items() if f not in ("series", "ping")} for k, a in report["arms"].items()}}, indent=2))

