"""Supplemental V2 c=200 burst, preserving the original benchmark and evidence.

Three existing image sizes, one round each; no invoke retries or quota changes.
Successful first calls receive a warm follow-up. All sessions get a stop attempt.
"""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
from pathlib import Path
import sys
import threading
import time
import uuid

import boto3
import botocore
from botocore.config import Config
from botocore.validate import validate_parameters

import coldstart_v2 as original

bench = original.bench
HERE = Path(__file__).resolve().parent
PRIOR = HERE / "results" / "coldstart_2026-09-11"
SIZES = ("500mb", "1gb", "2gb")
CONCURRENCY = 200

class Recorder:
    def __init__(self, raw, out):
        self.raw, self.out = raw, out
        self.lock = threading.Lock()
        self.counts = {}
        self.events = []

    def record(self, event):
        with self.lock:
            self.events.append(event)
            with (self.out / "api_events.jsonl").open("a") as stream:
                stream.write(json.dumps(event, default=str) + "\n")

    def timed_invoke(self, client, arn, session_id):
        with self.lock:
            attempt = self.counts.get(session_id, 0) + 1
            self.counts[session_id] = attempt
        event = {"operation": "invoke", "runtime_arn": arn, "session_id": session_id,
                 "attempt": attempt, "started_iso": bench.utc_iso()}
        start = time.perf_counter()
        event["started_perf"] = start
        try:
            response = self.raw.invoke_agent_runtime(agentRuntimeArn=arn,
                runtimeSessionId=session_id, qualifier="DEFAULT", contentType="application/json",
                accept="application/json", payload=json.dumps({"ping": "coldstart"}).encode())
            event["metadata"] = response["ResponseMetadata"]
            stream = response["response"]
            try:
                payload = stream.read()
                elapsed = (time.perf_counter() - start) * 1000
            finally:
                stream.close()
            event["elapsed_ms"] = elapsed
            status = response.get("statusCode") or response["ResponseMetadata"]["HTTPStatusCode"]
            body = json.loads(payload)
            event.update(status=status, body=body)
            original.check_payload(status, body)
            if body.get("echo") != {"ping": "coldstart"}:
                raise ValueError("Unexpected echo")
            event["valid"] = True
            return elapsed, status, body
        except Exception as exc:
            event["error_type"], event["error"] = bench.classify_error(exc)
            if isinstance(getattr(exc, "response", None), dict):
                event["metadata"] = exc.response.get("ResponseMetadata", {})
                event["error_code"] = exc.response.get("Error", {}).get("Code")
            raise
        finally:
            event.setdefault("elapsed_ms", (time.perf_counter() - start) * 1000)
            event["finished_iso"] = bench.utc_iso()
            self.record(event)

    def stop_runtime_session(self, **kwargs):
        event = {"operation": "stop", "session_id": kwargs["runtimeSessionId"],
                 "runtime_arn": kwargs["agentRuntimeArn"], "started_iso": bench.utc_iso()}
        try:
            response = self.raw.stop_runtime_session(**kwargs)
            event["response"] = response
            if response["ResponseMetadata"]["HTTPStatusCode"] != 200:
                raise ValueError("Unexpected stop status")
            return response
        except Exception as exc:
            event["error_type"], event["error"] = bench.classify_error(exc)
            raise
        finally:
            event["finished_iso"] = bench.utc_iso()
            self.record(event)


def run_burst(client, dep, size, out):
    runtime = dep["runtimes"][size]
    meta = {"size": size, "concurrency": CONCURRENCY, "rounds": 1,
            "runtime_name": runtime["name"], "runtime_arn": runtime["arn"],
            "region": dep["region"], "started_iso": bench.utc_iso()}
    barrier = threading.Barrier(CONCURRENCY, timeout=30)
    results = [None] * CONCURRENCY
    failures = []
    lock = threading.Lock()

    def worker(index):
        try:
            # Bounded barrier before probe creation, so a failed launch sends no request.
            barrier.wait()
            result = bench.probe(client, runtime["arn"], size, CONCURRENCY, 1, index, None)
            results[index] = result
        except Exception as exc:
            with lock:
                failures.append({"index": index, "error": type(exc).__name__})

    threads = [threading.Thread(target=worker, args=(i,), daemon=True)
               for i in range(CONCURRENCY)]
    launched = []
    try:
        try:
            for thread in threads:
                thread.start()
                launched.append(thread)
        except Exception:
            barrier.abort()
            raise
        deadline = time.monotonic() + 720
        for thread in launched:
            thread.join(timeout=max(0, deadline - time.monotonic()))
        if any(t.is_alive() for t in launched):
            raise TimeoutError("Workers exceeded 12-minute deadline")
        if failures or any(r is None for r in results):
            raise RuntimeError("Incomplete burst; see worker_errors")
    finally:
        meta["finished_iso"] = bench.utc_iso()
        original.save(out / "raw" / (size + "_c200.json"), {
            "meta": meta, "requests": [r for r in results if r is not None],
            "worker_errors": failures})
    summary = bench.summarize_cell(size, CONCURRENCY, results)
    summary["warm_success"] = sum(r["warm_ms"] is not None for r in results)
    summary["stop_success"] = sum(r["stopped"] for r in results)
    print("BURST", json.dumps(summary), flush=True)
    return summary


def read_quotas(session):
    quotas = session.client("service-quotas", config=Config(connect_timeout=10,
        read_timeout=20, retries={"total_max_attempts": 1}))
    names = {"Rate of new Runtime session creation", "Rate of Runtime data plane APIs",
             "Active Session Workloads per Account"}
    result = []
    for page in quotas.get_paginator("list_service_quotas").paginate(
            ServiceCode="bedrock-agentcore", PaginationConfig={"MaxItems": 200, "PageSize": 100}):
        result.extend({k: q.get(k) for k in ["QuotaName", "QuotaCode", "Value", "Unit"]}
                      for q in page["Quotas"] if q["QuotaName"] in names)
    if {q["QuotaName"] for q in result} != names:
        raise RuntimeError("Required quota information missing")
    return result


def run(out):
    baseline = json.loads((original.BASELINE / "deployments.json").read_text())
    prior_images = json.loads((PRIOR / "images.json").read_text())
    session = boto3.Session(region_name=baseline["region"])
    cfg = Config(connect_timeout=15, read_timeout=60, retries={"total_max_attempts": 1})
    account = session.client("sts", config=cfg).get_caller_identity()["Account"]
    if account != baseline["account"] or baseline["region"] != "us-west-2":
        raise RuntimeError("Unexpected AWS account or region")
    ctl, ecr = session.client("bedrock-agentcore-control", config=cfg), session.client("ecr", config=cfg)
    if "platformVersion" not in ctl.meta.service_model.operation_model("CreateAgentRuntime").input_shape.members:
        raise RuntimeError("Private SDK required")
    raw = bench.make_client(baseline["region"], CONCURRENCY)
    recorder = Recorder(raw, out)
    dep = {"account": account, "region": baseline["region"], "iam_role": baseline["iam_role"],
           "platform_version": "V2", "runtimes": {}}
    sources = [Path(__file__), HERE / "coldstart_v2.py", HERE / "check_v2.py",
               original.BASELINE / "coldstart_test.py", original.BASELINE / "deployments.json",
               PRIOR / "images.json"]
    meta = {"started_iso": bench.utc_iso(), "platform_version": "V2", "region": dep["region"],
            "account": account, "boto3": boto3.__version__, "botocore": botocore.__version__,
            "concurrency": CONCURRENCY, "rounds": 1, "sizes": list(SIZES),
            "settle_seconds": 180, "between_sizes_seconds": 10,
            "max_pool_connections": raw.meta.config.max_pool_connections,
            "retries": raw.meta.config.retries, "quotas": read_quotas(session),
            "source_sha256": {str(p.relative_to(HERE.parent)): hashlib.sha256(p.read_bytes()).hexdigest()
                              for p in sources}}
    images, requests = {}, {}
    for size in SIZES:
        image = ecr.describe_images(repositoryName=baseline["ecr_repo"].split("/")[-1],
                                     imageIds=[{"imageTag": size}])["imageDetails"][0]
        if any(image[key] != prior_images[size][key] for key in ["imageDigest", "imageSizeInBytes"]):
            raise RuntimeError("Image differs from historical V2 run: " + size)
        images[size] = image
        requests[size] = {"agentRuntimeName": f"coldstart_v2_c200_{size}_{uuid.uuid4().hex[:8]}",
            "agentRuntimeArtifact": {"containerConfiguration": {
                "containerUri": baseline["ecr_repo"] + "@" + image["imageDigest"]}},
            "roleArn": dep["iam_role"], "networkConfiguration": {"networkMode": "PUBLIC"},
            "protocolConfiguration": {"serverProtocol": "HTTP"},
            "lifecycleConfiguration": {"idleRuntimeSessionTimeout": 60, "maxLifetime": 600},
            "platformVersion": "V2", "clientToken": str(uuid.uuid4()),
            "description": "Temporary supplemental c200 V2 first-invocation benchmark"}
        validate_parameters(requests[size],
            ctl.meta.service_model.operation_model("CreateAgentRuntime").input_shape)
    original.save(out / "images.json", images)
    original.save(out / "create_requests.json", requests)
    original.save(out / "run.json", meta)
    original.save(out / "deployments.json", dep)
    print("PREFLIGHT", json.dumps(meta["quotas"]), "pool", meta["max_pool_connections"], flush=True)
    previous_invoke = bench.timed_invoke
    bench.timed_invoke = recorder.timed_invoke
    summaries = []
    rc = 1
    try:
        for size in SIZES:
            meta["create_inflight"] = size
            original.save(out / "run.json", meta)
            created = ctl.create_agent_runtime(**requests[size])
            dep["runtimes"][size] = {**baseline["runtimes"][size],
                "name": requests[size]["agentRuntimeName"], "id": created["agentRuntimeId"],
                "arn": created["agentRuntimeArn"], "create_response": created,
                "image_digest": images[size]["imageDigest"]}
            original.save(out / "deployments.json", dep)
            meta.pop("create_inflight")
            original.save(out / "run.json", meta)
            print("CREATED", size, created["agentRuntimeId"], flush=True)
        for size, runtime in dep["runtimes"].items():
            ready = original.wait_ready(ctl, runtime["id"], 600)
            original.wait_endpoint_ready(ctl, runtime["id"], 600)
            if ready.get("platformVersion") != "V2":
                raise RuntimeError("V2 not confirmed")
            runtime["ready_response"] = ready
            original.save(out / "deployments.json", dep)
            smoke = bench.probe(recorder, runtime["arn"], size, 1, 1, 0, None)
            original.save(out / ("smoke_" + size + ".json"), {"requests": [smoke]})
            print("SMOKE", size, json.dumps(smoke), flush=True)
            if not smoke["success"] or smoke["warm_ms"] is None or not smoke["stopped"]:
                raise RuntimeError("Smoke failed: " + size)
        print("SETTLE 180s before c200 bursts", flush=True)
        time.sleep(180)
        for index, size in enumerate(SIZES):
            summaries.append(run_burst(recorder, dep, size, out))
            original.save(out / "summary.json", {"cells": summaries})
            if index < len(SIZES) - 1:
                time.sleep(10)
        meta["measurement_complete"] = True
        meta["all_invocations_successful"] = all(s["success"] == s["warm_success"] == 200 for s in summaries)
        rc = 0 if all(s["stop_success"] == 200 for s in summaries) else 1
    except BaseException as exc:
        meta["error"] = f"{type(exc).__name__}: {exc}"
        print("ERROR", meta["error"], flush=True)
    finally:
        bench.timed_invoke = previous_invoke
        cleaned = original.cleanup(ctl, dep, out)
        meta.update(cleanup_complete=cleaned and "create_inflight" not in meta,
                    finished_iso=bench.utc_iso())
        meta["exit_code"] = rc if meta["cleanup_complete"] else 1
        original.save(out / "run.json", meta)
    return meta["exit_code"]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True, help="new output directory")
    args = parser.parse_args()
    os.umask(0o077)
    out = args.out.resolve()
    out.mkdir(parents=True, exist_ok=False)
    (out / "raw").mkdir()
    with (out / "run.log").open("w", buffering=1) as stream:
        with contextlib.redirect_stdout(original.Tee(sys.stdout, stream)):
            with contextlib.redirect_stderr(original.Tee(sys.stderr, stream)):
                return run(out)


if __name__ == "__main__":
    sys.exit(main())
