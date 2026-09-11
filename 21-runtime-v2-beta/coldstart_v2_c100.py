"""One 500mb V2 c100 burst; preserve all previous runners and evidence."""
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
from botocore.config import Config
from botocore.validate import validate_parameters

import coldstart_v2_c200 as shared

HERE = Path(__file__).resolve().parent
CONCURRENCY = 100

def run_burst(recorder, runtime, region, out):
    barrier = threading.Barrier(CONCURRENCY, timeout=30)
    rows = [None] * CONCURRENCY
    failures = []
    lock = threading.Lock()
    meta = {"size": "500mb", "concurrency": CONCURRENCY, "rounds": 1,
            "runtime_name": runtime["name"], "runtime_arn": runtime["arn"],
            "region": region, "started_iso": shared.bench.utc_iso()}

    def worker(index):
        try:
            barrier.wait()
            rows[index] = shared.bench.probe(recorder, runtime["arn"], "500mb",
                                            CONCURRENCY, 1, index, None)
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
        except BaseException:
            barrier.abort()
            raise
        deadline = time.monotonic() + 720
        for thread in launched:
            thread.join(timeout=max(0, deadline - time.monotonic()))
        if any(t.is_alive() for t in launched):
            raise TimeoutError("Workers exceeded 12-minute deadline")
        if failures or any(row is None for row in rows):
            raise RuntimeError("Incomplete burst")
    finally:
        meta["finished_iso"] = shared.bench.utc_iso()
        shared.original.save(out / "raw" / "500mb_c100.json", {
            "meta": meta, "requests": [r for r in rows if r is not None], "worker_errors": failures})
    summary = shared.bench.summarize_cell("500mb", CONCURRENCY, rows)
    summary["warm_success"] = sum(r["warm_ms"] is not None for r in rows)
    summary["stop_success"] = sum(r["stopped"] for r in rows)
    print("BURST", json.dumps(summary), flush=True)
    return summary


def run(out):
    prior = HERE / "results" / "coldstart_v2_c200_500mb_retest_2026-09-11"
    previous_meta = json.loads((prior / "run.json").read_text())
    previous_dep = json.loads((prior / "deployments.json").read_text())
    request = json.loads((prior / "create_requests.json").read_text())["500mb"]
    request.update(agentRuntimeName="coldstart_v2_c100_500mb_" + uuid.uuid4().hex[:8],
                   clientToken=str(uuid.uuid4()), description="Temporary 500mb V2 c100 benchmark")
    region, account = previous_meta["region"], previous_meta["account"]
    if region != "us-west-2":
        raise RuntimeError("Unexpected region")
    session = boto3.Session(region_name=region)
    cfg = Config(connect_timeout=15, read_timeout=60, retries={"total_max_attempts": 1})
    if session.client("sts", config=cfg).get_caller_identity()["Account"] != account:
        raise RuntimeError("Unexpected account")
    ctl = session.client("bedrock-agentcore-control", config=cfg)
    validate_parameters(request, ctl.meta.service_model.operation_model("CreateAgentRuntime").input_shape)
    image_uri = request["agentRuntimeArtifact"]["containerConfiguration"]["containerUri"]
    repo = image_uri.split("/", 1)[1].split("@", 1)[0]
    image = session.client("ecr", config=cfg).describe_images(repositoryName=repo,
        imageIds=[{"imageDigest": image_uri.rsplit("@", 1)[1]}])["imageDetails"][0]
    old_image = json.loads((prior / "images.json").read_text())["500mb"]
    assert all(image[k] == old_image[k] for k in ["imageDigest", "imageSizeInBytes"])
    raw = shared.bench.make_client(region, CONCURRENCY)
    recorder = shared.Recorder(raw, out)
    sources = [Path(__file__), Path(shared.__file__), HERE / "coldstart_v2.py", HERE / "check_v2.py",
               shared.original.BASELINE / "coldstart_test.py", prior / "create_requests.json",
               prior / "images.json"]
    meta = {"started_iso": shared.bench.utc_iso(), "region": region, "account": account,
            "platform_version": "V2", "sizes": ["500mb"], "concurrency": CONCURRENCY, "rounds": 1,
            "settle_seconds": 180, "boto3": boto3.__version__, "botocore": shared.botocore.__version__,
            "max_pool_connections": raw.meta.config.max_pool_connections,
            "retries": raw.meta.config.retries, "quotas": shared.read_quotas(session),
            "source_sha256": {str(p.relative_to(HERE.parent)): hashlib.sha256(p.read_bytes()).hexdigest()
                              for p in sources}}
    dep = {"account": account, "region": region, "iam_role": request["roleArn"],
           "platform_version": "V2", "runtimes": {}}
    shared.original.save(out / "create_requests.json", {"500mb": request})
    shared.original.save(out / "images.json", {"500mb": image})
    shared.original.save(out / "deployments.json", dep)
    shared.original.save(out / "run.json", meta)
    previous_invoke = shared.bench.timed_invoke
    shared.bench.timed_invoke = recorder.timed_invoke
    rc = 1
    try:
        meta["create_inflight"] = "500mb"
        shared.original.save(out / "run.json", meta)
        created = ctl.create_agent_runtime(**request)
        runtime = {k: previous_dep["runtimes"]["500mb"][k]
                   for k in ["docker_size_bytes", "ecr_size_bytes"]}
        runtime.update(name=request["agentRuntimeName"], id=created["agentRuntimeId"],
                       arn=created["agentRuntimeArn"], create_response=created,
                       image_digest=image["imageDigest"])
        dep["runtimes"]["500mb"] = runtime
        shared.original.save(out / "deployments.json", dep)
        meta.pop("create_inflight")
        shared.original.save(out / "run.json", meta)
        print("CREATED", runtime["id"], flush=True)
        ready = shared.original.wait_ready(ctl, runtime["id"], 600)
        shared.original.wait_endpoint_ready(ctl, runtime["id"], 600)
        assert ready["platformVersion"] == "V2"
        runtime["ready_response"] = ready
        shared.original.save(out / "deployments.json", dep)
        smoke = shared.bench.probe(recorder, runtime["arn"], "500mb", 1, 1, 0, None)
        shared.original.save(out / "smoke_500mb.json", {"requests": [smoke]})
        print("SMOKE", json.dumps(smoke), flush=True)
        if not smoke["success"] or smoke["warm_ms"] is None or not smoke["stopped"]:
            raise RuntimeError("Smoke failed")
        print("SETTLE 180s; quotas", json.dumps(meta["quotas"]), flush=True)
        time.sleep(180)
        summary = run_burst(recorder, runtime, region, out)
        shared.original.save(out / "summary.json", {"cells": [summary]})
        meta["measurement_complete"] = True
        meta["all_invocations_successful"] = summary["success"] == summary["warm_success"] == CONCURRENCY
        rc = 0 if summary["stop_success"] == CONCURRENCY else 1
    except BaseException as exc:
        meta["error"] = f"{type(exc).__name__}: {exc}"
        print("ERROR", meta["error"], flush=True)
    finally:
        shared.bench.timed_invoke = previous_invoke
        cleaned = shared.original.cleanup(ctl, dep, out)
        meta.update(cleanup_complete=cleaned and "create_inflight" not in meta,
                    finished_iso=shared.bench.utc_iso())
        meta["exit_code"] = rc if meta["cleanup_complete"] else 1
        shared.original.save(out / "run.json", meta)
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
        with contextlib.redirect_stdout(shared.original.Tee(sys.stdout, stream)):
            with contextlib.redirect_stderr(shared.original.Tee(sys.stderr, stream)):
                return run(out)


if __name__ == "__main__":
    sys.exit(main())
