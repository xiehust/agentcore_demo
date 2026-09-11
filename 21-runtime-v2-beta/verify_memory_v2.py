"""Independently verify saved memory comparison evidence. No AWS calls or writes."""
import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
from statistics import median

HERE = Path(__file__).resolve().parent
MIB = 1024 ** 2

def verify(out):
    state = json.loads((out / "resources.json").read_text())
    telemetry = json.loads((out / "telemetry.json").read_text())
    report = json.loads((out / "comparison.json").read_text())
    assert state["calls_complete"] and state["cleanup_complete"]
    assert state["region"] == "us-west-2"
    assert len(state["runtimes"]) == 4 and len(state["sessions"]) == 10
    assert len({s["id"] for s in state["sessions"]}) == 10
    for name, digest in state["source_sha256"].items():
        assert hashlib.sha256((HERE.parent / name).read_bytes()).hexdigest() == digest, name
    for variant in ["small", "padded"]:
        default, v2 = [state["runtimes"][p + "_" + variant] for p in ["default", "v2"]]
        assert not default["owned"] and v2["owned"] and v2["deleted"]
        assert default["configuration"].get("platformVersion") in (None, "V1")
        assert v2["configuration"]["platformVersion"] == "V2"
        for field in ["agentRuntimeArtifact", "roleArn", "networkConfiguration",
                      "protocolConfiguration", "lifecycleConfiguration"]:
            assert default["configuration"][field] == v2["configuration"][field]
        assert v2["configuration"]["status"] == v2["endpoint"]["status"] == "READY"
        assert default["image"] == v2["image"]
        assert default["delivery_id"] != v2["delivery_id"]
    expected = {(platform + "_" + variant, kind) for platform in ["default", "v2"]
        for variant, kind in [("small", "baseline"), ("small", "anonymous"),
            ("small", "file_cache"), ("padded", "baseline"), ("padded", "image_read")]}
    assert Counter((s["variant"], s["kind"]) for s in state["sessions"]) == Counter(expected)
    assert report["complete"] and not report["parse_errors"]
    assert len(report["sessions"]) == 10
    assert sum(len(s["phases"]) for s in report["sessions"]) == 30
    total_samples, total_phase_logs = 0, 0
    rows = []
    for session in state["sessions"]:
        assert session["validated"] and not session.get("error") and not session.get("stop_error")
        assert session["invoke_metadata"]["HTTPStatusCode"] == 200
        assert session["stop_response"]["ResponseMetadata"]["HTTPStatusCode"] == 200
        body = json.loads((out / session["result_file"]).read_text())
        assert body["run_id"] == session["id"] and body["kind"] == session["kind"] and body["mib"] == 256
        assert session["start"] <= body["started"] <= body["ended"] <= session["end"]
        total_samples += len(body["samples"])
        saved = next(s for s in report["sessions"] if s["session_id"] == session["id"])
        usage = []
        seen = set()
        for event in telemetry["runtimes"][session["variant"]]["events"]:
            value = json.loads(event["message"])
            attrs = value["attributes"]
            if attrs["session.id"] != session["id"]:
                continue
            timestamp = value["event_timestamp"]
            assert abs(timestamp - event["timestamp"]) <= 1
            duration = float(attrs["time_elapsed_seconds"])
            memory = float(value["metrics"]["agent.runtime.memory.gb_hours.used"])
            key = (timestamp, duration, memory)
            if key not in seen:
                seen.add(key)
                usage.append((timestamp / 1000, duration, memory))
        for phase in body["phases"]:
            start, end = phase["start"] + 3, phase["end"] - 3
            app = [s for s in body["samples"] if s["phase"] == phase["phase"]
                   and start <= s["timestamp"] <= end]
            aws = sorted(r for r in usage if start <= r[0] <= end)
            assert 29 <= phase["end"] - phase["start"] <= 40
            assert len(app) >= 23 and len(aws) >= 23
            assert all(.95 <= b[0] - a[0] <= 1.05 for a, b in zip(aws, aws[1:]))
            assert all(.95 <= r[1] <= 1.05 for r in aws)
            assert aws[0][0] - start <= 1.1 and end - aws[-1][0] <= 1.1
            assert .99 <= sum(r[1] for r in aws) / (end - start) <= 1.05
            rss = median(s["smaps_rollup"]["Rss"] / MIB for s in app)
            mem = median(r[2] * 3600 / r[1] for r in aws)
            stored = next(p for p in saved["phases"] if p["phase"] == phase["phase"])
            assert abs(rss - stored["application"]["rss_mib"]["median"]) < 1e-9
            assert abs(mem - stored["aws"]["memory_gb_equivalent"]["median"]) < 1e-9
            total_phase_logs += len(aws)
            rows.append((session["variant"], session["kind"], phase["phase"], rss, mem))
    print("PASS: exact 10 sessions, 30 aligned phases,", total_samples, "application samples,",
          total_phase_logs, "phase usage logs; successful stops and V2 deletions")
    print("PASS: matching artifacts/configurations, explicit V2, unchanged source hashes;")
    print("      independently recomputed medians and continuous per-second coverage")
    display(rows)
    return rows


def display(rows):
    print("variant | kind | phase | RSS MiB | AWS GB-equivalent")
    for variant, kind, phase, rss, memory in rows:
        print(f"{variant} | {kind} | {phase} | {rss:.3f} | {memory:.6f}")
    print("\nV2 minus default (AWS GB-equivalent):")
    for variant, kind, phase, rss, memory in rows:
        if variant.startswith("v2_"):
            control = next(r for r in rows if r[:3] == (variant.replace("v2_", "default_"), kind, phase))
            print(variant, kind, phase, f"{memory - control[4]:+.6f}",
                  f"{(memory / control[4] - 1) * 100:+.1f}%")



if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("out", type=Path)
    args = parser.parse_args()
    verify(args.out)
