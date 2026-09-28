#!/usr/bin/env python3
"""Run inside the China-region load generator. No local profile or static secrets."""
import argparse
import concurrent.futures
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import platform
import sys
import threading
import time
import traceback
import uuid

import boto3
import botocore
from botocore.config import Config


def now():
    return datetime.now(timezone.utc).isoformat()


def save(path, value):
    path = Path(path)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, indent=2, default=str) + "\n")
    temporary.replace(path)


def stats(values):
    if not values:
        return {"n": 0}
    ordered = sorted(values)
    return {"n": len(values), "min_ms": ordered[0], "max_ms": ordered[-1],
            "mean_ms": sum(ordered) / len(ordered),
            **{f"p{p}_ms": ordered[math.ceil(p / 100 * len(ordered)) - 1] for p in (50, 90, 95, 99)}}


def max_overlap(intervals):
    events = [(a, 1) for a, b in intervals] + [(b, -1) for a, b in intervals]
    active = peak = 0
    for _, change in sorted(events):  # Half-open [start, end): ends sort before starts.
        active += change
        peak = max(peak, active)
    return peak


class Benchmark:
    def __init__(self, settings):
        self.settings = settings
        self.lock = threading.RLock()
        self.rows = []
        self.sessions = {}
        self.config = Config(connect_timeout=10, read_timeout=120, max_pool_connections=60,
                             retries={"total_max_attempts": 1}, tcp_keepalive=True)
        self.aws = boto3.Session(region_name=settings["region"])
        self.client = self.aws.client("bedrock-agentcore", config=self.config)
        identity = self.aws.client("sts", config=self.config).get_caller_identity()
        if identity["Account"] != settings["account"]:
            raise RuntimeError("Wrong account")
        if settings["load_role_name"] not in identity["Arn"]:
            raise RuntimeError("Expected the dedicated load generator role")
        self.environment = {
            "at": now(), "identity": identity, "region": settings["region"],
            "endpoint": self.client.meta.endpoint_url, "python": sys.version,
            "platform": platform.platform(), "boto3": boto3.__version__, "botocore": botocore.__version__,
            "settings": settings, "total_max_attempts": 1, "read_timeout_s": 120,
            "measurement": "SDK invoke call start through full response body read; nearest-rank percentiles",
        }
        save("benchmark_environment.json", self.environment)

    def log(self, text):
        print(f"[{now()}] {text}", flush=True)

    def add_session(self, kind):
        sid = str(uuid.uuid4())
        with self.lock:
            self.sessions[sid] = {"runtime_arn": self.settings["runtimes"][kind],
                                  "created_at": now(), "stop_confirmed": False}
            save("runtime_sessions.json", self.sessions)
        return sid

    def invoke(self, sid, phase, index, hold=0, release=None):
        nonce = str(uuid.uuid4())
        payload = json.dumps({"nonce": nonce, "hold_seconds": hold}).encode()
        record = {"phase": phase, "index": index, "session_id": sid, "nonce": nonce,
                  "hold_seconds": hold, "client_start_unix_s": time.time()}
        start = time.perf_counter()
        if release is not None:
            record["launch_offset_ms"] = (start - release) * 1000
        try:
            response = self.client.invoke_agent_runtime(
                agentRuntimeArn=self.sessions[sid]["runtime_arn"],
                qualifier="DEFAULT", runtimeSessionId=sid, contentType="application/json",
                accept="application/json", payload=payload)
            record["headers_ms"] = (time.perf_counter() - start) * 1000
            stream = response["response"]
            try:
                raw = stream.read()
            finally:
                stream.close()
            record["latency_ms"] = (time.perf_counter() - start) * 1000
            record["metadata"] = response["ResponseMetadata"]
            record["runtime_session_response"] = response.get("runtimeSessionId")
            record["response_bytes"] = len(raw)
            body = json.loads(raw)
            record["body"] = body
            if response["ResponseMetadata"]["HTTPStatusCode"] != 200:
                raise AssertionError("Expected HTTP 200")
            if response["ResponseMetadata"]["RetryAttempts"] != 0:
                raise AssertionError("Unexpected retry")
            if body.get("ok") is not True or body.get("echo") != nonce:
                raise AssertionError("Echo payload mismatch")
            if not body.get("instance_id") or not body.get("process_id") or not body.get("boot_id"):
                raise AssertionError("Instance identity missing")
            record["status"] = "PASS"
        except Exception as exc:
            record["status"] = "FAIL"
            record["error"] = {"type": type(exc).__name__, "message": str(exc),
                               "response": getattr(exc, "response", None)}
        finally:
            record["call_end_unix_s"] = time.time()
            record.setdefault("latency_ms", (time.perf_counter() - start) * 1000)
            with self.lock:
                self.rows.append(record)
                with open("requests.jsonl", "a") as out:
                    out.write(json.dumps(record, default=str) + "\n")
        return record

    def stop(self, sid):
        row = self.sessions[sid]
        if row["stop_confirmed"]:
            return
        try:
            response = self.client.stop_runtime_session(
                agentRuntimeArn=row["runtime_arn"], qualifier="DEFAULT", runtimeSessionId=sid)
            with self.lock:
                row.update(stop_confirmed=True, stop_response=response, stopped_at=now())
                row.pop("stop_error", None)
        except Exception as exc:
            with self.lock:
                row["stop_error"] = {"message": str(exc), "response": getattr(exc, "response", None)}
        finally:
            with self.lock:
                save("runtime_sessions.json", self.sessions)

    def run(self):
        self.log("Cold serial samples started")
        for i in range(self.settings["cold_samples"]):
            sid = self.add_session("baseline")
            try:
                row = self.invoke(sid, "cold", i)
                if row["status"] != "PASS":
                    self.log(f"cold {i}: {row['error']}")
            finally:
                self.stop(sid)
            if (i + 1) % 10 == 0:
                self.log(f"cold: {i+1}/{self.settings['cold_samples']}")
        self.log("Warm same-session samples started")
        sid = self.add_session("baseline")
        try:
            warmup = self.invoke(sid, "warmup", 0)
            if warmup["status"] == "PASS":
                for i in range(self.settings["warm_samples"]):
                    self.invoke(sid, "warm", i)
                    if (i + 1) % 100 == 0:
                        self.log(f"warm: {i+1}/{self.settings['warm_samples']}")
        finally:
            self.stop(sid)
        count = self.settings["concurrency"]
        self.log(f"Fresh scale runtime: 0 user sessions -> {count} concurrent sessions")
        barrier = threading.Barrier(count + 1)
        release = {}
        ids = [self.add_session("scale") for _ in range(count)]

        def worker(i):
            barrier.wait(timeout=60)
            return self.invoke(ids[i], "scale", i, hold=5, release=release["at"])

        try:
            with concurrent.futures.ThreadPoolExecutor(max_workers=count) as pool:
                futures = [pool.submit(worker, i) for i in range(count)]
                release["at"] = time.perf_counter()
                barrier.wait(timeout=60)
                for future in concurrent.futures.as_completed(futures):
                    future.result()
        finally:
            with concurrent.futures.ThreadPoolExecutor(max_workers=10) as pool:
                list(pool.map(self.stop, ids))
        self.log("All samples completed")

    def summarize(self):
        phases = {}
        for phase in ("cold", "warmup", "warm", "scale"):
            rows = sorted((r for r in self.rows if r["phase"] == phase), key=lambda r: r["index"])
            good = [r for r in rows if r["status"] == "PASS"]
            phases[phase] = {
                "attempts": len(rows), "successes": len(good), "failures": len(rows) - len(good),
                "latency": stats([r["latency_ms"] for r in good]),
                "headers": stats([r["headers_ms"] for r in good]),
                "handler": stats([r["body"]["handler_ms"] for r in good]),
                "process_age_at_request": stats([r["body"]["process_age_at_request_ms"] for r in good]),
                "unique_instances": len({r["body"]["instance_id"] for r in good}),
                "unique_snapshot_process_ids": len({r["body"]["process_id"] for r in good}),
                "unique_boot_ids": len({r["body"]["boot_id"] for r in good}),
                "all_first_request": bool(good) and all(r["body"]["request_index"] == 1 for r in good),
                "client_call_peak_overlap": max_overlap(
                    [(r["client_start_unix_s"], r["call_end_unix_s"]) for r in rows]),
                "handler_peak_overlap": max_overlap(
                    [(r["body"]["handler_started_unix_s"], r["body"]["handler_finished_unix_s"]) for r in good]),
            }
        cold, warm, scale = (phases[p] for p in ("cold", "warm", "scale"))
        cold_ok = (cold["successes"] == self.settings["cold_samples"] and cold["failures"] == 0
                   and cold["unique_instances"] == self.settings["cold_samples"] and cold["all_first_request"])
        warmup = next((r for r in self.rows if r["phase"] == "warmup" and r["status"] == "PASS"), None)
        warm_rows = sorted((r for r in self.rows if r["phase"] == "warm"), key=lambda r: r["index"])
        warm_ok = (warmup is not None and warm["successes"] == self.settings["warm_samples"]
                   and warm["failures"] == 0
                   and all(r["body"]["instance_id"] == warmup["body"]["instance_id"]
                           and r["body"]["process_id"] == warmup["body"]["process_id"]
                           and r["body"]["boot_id"] == warmup["body"]["boot_id"]
                           and r["body"]["request_index"] == r["index"] + 2 for r in warm_rows))
        scale_ok = (scale["successes"] == self.settings["concurrency"] and scale["failures"] == 0
                    and scale["all_first_request"] and scale["unique_instances"] == self.settings["concurrency"]
                    and scale["client_call_peak_overlap"] == self.settings["concurrency"])
        tests = {}
        for key, phase, metric, target, integrity in [
            ("7.1", cold, "p50_ms", 3000, cold_ok), ("7.2", cold, "p99_ms", 5000, cold_ok),
            ("7.3", warm, "p99_ms", 200, warm_ok),
        ]:
            value = phase["latency"].get(metric)
            tests[key] = {"status": "PASS" if integrity and value is not None and value < target else "FAIL",
                          "metric": metric, "observed_ms": value, "strict_target_ms": target,
                          "sample_integrity": integrity, "samples": phase["successes"]}
        tests["7.4"] = {"status": "PASS" if scale_ok else "FAIL", "samples": scale["attempts"],
                        "errors": scale["failures"],
                        "error_rate": scale["failures"] / scale["attempts"] if scale["attempts"] else None,
                        "unique_instances": scale["unique_instances"],
                        "client_call_peak_overlap": scale["client_call_peak_overlap"],
                        "handler_peak_overlap": scale["handler_peak_overlap"],
                        "scope": "0 user sessions on a never-invoked runtime; platform prewarmed pool unobservable"}
        result = {"at": now(), "tests": tests, "phases": phases,
                  "all_sessions_stop_confirmed": all(r["stop_confirmed"] for r in self.sessions.values()),
                  "session_count": len(self.sessions),
                  "method": "nearest-rank; no retries; full body latency; all samples retained"}
        save("benchmark_summary.json", result)
        save("benchmark_results.json", {"environment": self.environment, "summary": result,
                                        "requests": self.rows, "sessions": self.sessions})
        return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="benchmark_config.json")
    args = parser.parse_args()
    settings = json.loads(Path(args.config).read_text())
    lab = Benchmark(settings)
    try:
        lab.run()
    except Exception:
        Path("benchmark_fatal.txt").write_text(traceback.format_exc())
        raise
    finally:
        for sid, row in lab.sessions.items():
            if not row["stop_confirmed"]:
                lab.stop(sid)
        result = lab.summarize()
    return 0 if all(r["status"] == "PASS" for r in result["tests"].values()) else 1


if __name__ == "__main__":
    sys.exit(main())
