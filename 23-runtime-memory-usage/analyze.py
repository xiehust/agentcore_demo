"""Compare phase-level application memory with session-specific AWS usage logs."""
from collections import defaultdict
from datetime import datetime, timezone
import json
import math
from statistics import median

from lab import STATE, load_state, write_json

MIB = 1024 ** 2

def parse_events(events):
    """Parse the observed USAGE_LOGS schema; retain failures rather than invent units."""
    records, errors, seen = [], [], set()
    for event in events:
        try:
            body = json.loads(event["message"])
            attrs, metrics = body["attributes"], body["metrics"]
            session_id = attrs["session.id"]
            timestamp_ms = body["event_timestamp"]
            elapsed = float(attrs["time_elapsed_seconds"])
            memory = float(metrics["agent.runtime.memory.gb_hours.used"])
            if (not 1e12 < timestamp_ms < 1e14 or not math.isfinite(elapsed)
                    or not math.isfinite(memory) or elapsed < 0 or memory < 0):
                raise ValueError("Unexpected timestamp, interval, or usage")
            if abs(timestamp_ms - event["timestamp"]) > 1:
                raise ValueError("Payload and CloudWatch timestamps disagree")
            key = (session_id, timestamp_ms, elapsed, memory)
            if key in seen:
                continue
            seen.add(key)
            records.append({"session_id": session_id, "timestamp": timestamp_ms / 1000,
                "elapsed_seconds": elapsed, "memory_gb_hours": memory,
                # AWS rounds elapsed to two decimals: 0.00 can have nonzero usage.
                # Keep that usage in totals, but do not invent a gauge or divide by zero.
                "memory_gb_equivalent": memory * 3600 / elapsed if elapsed else None,
                "delivery_delay_seconds": (event["ingestionTime"] - timestamp_ms) / 1000})
        except (KeyError, TypeError, ValueError) as exc:
            errors.append({"event_id": event.get("eventId"), "error": str(exc)})
    return sorted(records, key=lambda r: r["timestamp"]), errors

def stats(values):
    values = [v for v in values if isinstance(v, (int, float))]
    return {"median": median(values), "min": min(values), "max": max(values)} if values else None


def application_summary(body):
    result = []
    for phase in body["phases"]:
        # Use stable interior windows to avoid assignment of transition seconds.
        start, end = phase["start"] + 3, phase["end"] - 3
        samples = [s for s in body["samples"] if s["phase"] == phase["phase"]
                   and start <= s["timestamp"] <= end]
        values = defaultdict(list)
        for sample in samples:
            cg, mem = sample["cgroup"], sample["guest_meminfo"]
            candidates = {"rss_mib": sample["smaps_rollup"].get("Rss"),
                "pss_mib": sample["smaps_rollup"].get("Pss"),
                "rss_hwm_mib": sample["process"].get("VmHWM"),
                "cgroup_current_mib": cg.get("memory.current", cg.get("memory.usage_in_bytes")),
                "cgroup_peak_mib": cg.get("memory.peak", cg.get("memory.max_usage_in_bytes")),
                "cgroup_anon_mib": cg.get("memory.stat", {}).get("anon"),
                "cgroup_file_mib": cg.get("memory.stat", {}).get("file"),
                "guest_cached_mib": mem.get("Cached"),
                "guest_anon_mib": mem.get("AnonPages"),
                "guest_used_available_mib": (mem["MemTotal"] - mem["MemAvailable"])
                    if "MemTotal" in mem and "MemAvailable" in mem else None}
            for key, value in candidates.items():
                if isinstance(value, (int, float)):
                    values[key].append(value / MIB)
        result.append({"phase": phase["phase"], "window_start": start, "window_end": end,
                       "sample_count": len(samples),
                       "application": {key: stats(v) for key, v in values.items()}})
    return result

def summarize(state, telemetry):
    result = {"generated_at": datetime.now(timezone.utc).isoformat(), "sessions": [],
              "parse_errors": [], "note": "Telemetry is not an authoritative bill; GB byte convention unspecified."}
    all_records = []
    for runtime in telemetry.get("runtimes", {}).values():
        records, errors = parse_events(runtime["events"])
        all_records.extend(records)
        result["parse_errors"].extend(errors)
    for session in state["sessions"]:
        if "result_file" not in session:
            continue
        body = json.loads((STATE / session["result_file"]).read_text())
        records = [r for r in all_records if r["session_id"] == session["id"]]
        phases = application_summary(body)
        for phase in phases:
            selected = [r for r in records if phase["window_start"] <= r["timestamp"] <= phase["window_end"]]
            seconds = sum(r["elapsed_seconds"] for r in selected)
            span = phase["window_end"] - phase["window_start"]
            phase["aws"] = {"record_count": len(selected), "covered_seconds": seconds,
                "window_seconds": span, "coverage_fraction": seconds / span if span > 0 else 0,
                "memory_gb_equivalent": stats([r["memory_gb_equivalent"] for r in selected]),
                "memory_gb_hours": sum(r["memory_gb_hours"] for r in selected),
                "elapsed_seconds_values": sorted({r["elapsed_seconds"] for r in selected})}
        result["sessions"].append({"session_id": session["id"], "variant": session["variant"],
            "kind": session["kind"], "phases": phases, "aws_record_count": len(records),
            "aws_memory_gb_hours_observed": sum(r["memory_gb_hours"] for r in records),
            "aws_preinvoke_record_count": sum(r["timestamp"] < session["start"] for r in records),
            "delivery_delay_seconds": stats([r["delivery_delay_seconds"] for r in records]),
            "stop_http_status": session.get("stop_response", {}).get("ResponseMetadata", {}).get("HTTPStatusCode")})
    return result


def main():
    state = load_state()
    telemetry_path = STATE / "telemetry.json"
    telemetry = json.loads(telemetry_path.read_text()) if telemetry_path.exists() else {}
    report = summarize(state, telemetry)
    write_json(STATE / "comparison.json", report)
    print("variant kind phase | RSS MiB | cgroup MiB | guest Cached MiB | AWS GB-equivalent | coverage")
    for session in report["sessions"]:
        for phase in session["phases"]:
            app, aws = phase["application"], phase["aws"]
            cols = [app.get(k, {}).get("median") for k in ["rss_mib", "cgroup_current_mib", "guest_cached_mib"]]
            cols.append((aws["memory_gb_equivalent"] or {}).get("median"))
            display = " | ".join("missing" if v is None else f"{v:.3f}" for v in cols)
            print(session["variant"], session["kind"], phase["phase"], "|", display,
                  f"| {aws['coverage_fraction']:.0%}")
    if report["parse_errors"]:
        print("WARNING: Unparsed usage records:", len(report["parse_errors"]))
    if any(p["aws"]["coverage_fraction"] < 0.9 for s in report["sessions"] for p in s["phases"]):
        print("INCOMPLETE TELEMETRY: some phase windows have <90% coverage. Rerun collect later.")
    print("Saved .state/comparison.json; AWS GB-equivalent is not application GiB or final billed memory.")


if __name__ == "__main__":
    main()

