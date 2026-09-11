"""500mb V2 first-invocation comparison using eight spawned client processes.

Runs c50/c100/c200 on separate temporary runtimes with no invoke retries.
Existing images/role are reused. Each level gets one smoke and 180s settle.
"""
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

import multiprocess_coldstart_client as client

HERE = Path(__file__).resolve().parent
LEVELS = (50, 100, 200)
def stop_unresolved(region, arn, folder):
    """Only after run_burst has reaped its workers; preserve every recovery result."""
    path = folder / "client_result.json"
    if not path.exists():
        return
    result = json.loads(path.read_text())
    if not result["release_perf"]:
        return  # Global gate never opened, so no target session could be invoked.
    stopped = {e["session_id"] for r in result["reports"] for e in r["events"]
               if e["operation"] == "stop"}
    ids = [sid for assignment in result["plan"]["assignments"]
           for _, sid in assignment["indices_sessions"] if sid not in stopped]
    if not ids:
        return
    raw = client.make_client(region, 2)
    recorder = client.Recorder(raw)
    try:
        for sid in ids:
            recorder.call(arn, sid, "stop")
            client.shared.original.save(folder / "recovery_stops.json", {"events": recorder.events})
    finally:
        raw.close()



def run(out):
    prior = HERE / "results/coldstart_v2_c50_500mb_retest_2026-09-11"
    previous = json.loads((prior / "run.json").read_text())
    template = json.loads((prior / "create_requests.json").read_text())["500mb"]
    region, account = previous["region"], previous["account"]
    assert region == "us-west-2"
    session = boto3.Session(region_name=region)
    cfg = Config(connect_timeout=15, read_timeout=60, retries={"total_max_attempts": 1})
    assert session.client("sts", config=cfg).get_caller_identity()["Account"] == account
    control = session.client("bedrock-agentcore-control", config=cfg)
    uri = template["agentRuntimeArtifact"]["containerConfiguration"]["containerUri"]
    image = session.client("ecr", config=cfg).describe_images(
        repositoryName=uri.split("/", 1)[1].split("@", 1)[0],
        imageIds=[{"imageDigest": uri.rsplit("@", 1)[1]}])["imageDetails"][0]
    old_image = json.loads((prior / "images.json").read_text())["500mb"]
    assert all(image[k] == old_image[k] for k in ("imageDigest", "imageSizeInBytes"))
    sources = [Path(__file__), Path(client.__file__), Path(client.shared.__file__),
               HERE / "coldstart_v2.py", HERE / "check_v2.py",
               client.shared.original.BASELINE / "coldstart_test.py", prior / "create_requests.json"]
    meta = {"started_iso": client.shared.bench.utc_iso(), "region": region, "account": account,
            "levels": list(LEVELS), "process_count": 8, "start_method": "spawn", "settle_seconds": 180,
            "warm_policy": "immediate per session, same as historical benchmark",
            "result_persistence": "after all worker traffic completes",
            "cpu_count": os.cpu_count(), "affinity": sorted(os.sched_getaffinity(0)),
            "python": sys.version, "boto3": boto3.__version__, "botocore": client.shared.botocore.__version__,
            "quotas": client.shared.read_quotas(session),
            "source_sha256": {str(p.relative_to(HERE.parent)): hashlib.sha256(p.read_bytes()).hexdigest()
                              for p in sources}}
    dep = {"region": region, "account": account, "runtimes": {}}
    requests = {}
    for level in LEVELS:
        request = {**template, "agentRuntimeName": f"coldstart_v2_mp_c{level}_{uuid.uuid4().hex[:8]}",
                   "clientToken": str(uuid.uuid4()), "description": "Temporary eight-process 500mb benchmark"}
        validate_parameters(request, control.meta.service_model.operation_model("CreateAgentRuntime").input_shape)
        requests[str(level)] = request
    save = client.shared.original.save
    save(out / "run.json", meta)
    save(out / "deployments.json", dep)
    save(out / "create_requests.json", requests)
    save(out / "images.json", {"500mb": image})
    summaries = []
    rc = 1
    try:
        for level in LEVELS:
            key = str(level)
            folder = out / ("c" + key)
            folder.mkdir()
            meta["create_inflight"] = key
            save(out / "run.json", meta)
            created = control.create_agent_runtime(**requests[key])
            runtime = {"id": created["agentRuntimeId"], "arn": created["agentRuntimeArn"],
                       "create_response": created, "image_digest": image["imageDigest"]}
            dep["runtimes"][key] = runtime
            save(out / "deployments.json", dep)
            meta.pop("create_inflight")
            save(out / "run.json", meta)
            print("CREATED", level, runtime["id"], flush=True)
            ready = client.shared.original.wait_ready(control, runtime["id"], 600)
            client.shared.original.wait_endpoint_ready(control, runtime["id"], 600)
            assert ready["platformVersion"] == "V2"
            runtime["ready_response"] = ready
            save(out / "deployments.json", dep)
            raw = client.make_client(region, 2)
            recorder = client.Recorder(raw)
            sid = client.shared.bench.new_session_id()
            smoke = client.probe(recorder, runtime["arn"], 1, 0, sid, threading.Barrier(1))
            save(folder / "smoke.json", {"request": smoke, "events": recorder.events})
            raw.close()
            assert smoke["success"] and smoke["warm_ms"] is not None and smoke["stopped"]
            print("SMOKE", level, smoke["cold_ms"], "ms; SETTLE 180s", flush=True)
            time.sleep(180)
            try:
                summary = client.run_burst(region, runtime["arn"], level, folder)
            except BaseException:
                stop_unresolved(region, runtime["arn"], folder)
                raise
            summaries.append(summary)
            save(out / "summary.json", {"cells": summaries})
            # Delete each level promptly; keep the aggregate ownership ledger for failure cleanup.
            cleaned = client.shared.original.cleanup(control, {"runtimes": {key: runtime}}, folder)
            runtime["deleted"] = cleaned
            save(out / "deployments.json", dep)
            if not cleaned:
                raise RuntimeError("Runtime deletion incomplete")
        meta["measurement_complete"] = True
        meta["all_invocations_successful"] = all(
            c["success"] == c["warm_success"] == c["samples"] for c in summaries)
        rc = 0 if meta["all_invocations_successful"] and all(
            c["stop_success"] == c["samples"] for c in summaries) else 1
    except BaseException as exc:
        meta["error"] = f"{type(exc).__name__}: {exc}"
        print("ERROR", meta["error"], flush=True)
    finally:
        outstanding = {k: r for k, r in dep["runtimes"].items() if not r.get("deleted")}
        cleaned = True
        if outstanding:
            cleaned = client.shared.original.cleanup(control, {"runtimes": outstanding}, out)
        meta.update(cleanup_complete=cleaned and "create_inflight" not in meta,
                    finished_iso=client.shared.bench.utc_iso())
        meta["exit_code"] = rc if meta["cleanup_complete"] else 1
        save(out / "run.json", meta)
    return meta["exit_code"]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True, help="new evidence directory")
    args = parser.parse_args()
    os.umask(0o077)
    out = args.out.resolve()
    out.mkdir(parents=True, exist_ok=False)
    with (out / "run.log").open("w", buffering=1) as stream:
        with contextlib.redirect_stdout(client.shared.original.Tee(sys.stdout, stream)):
            with contextlib.redirect_stderr(client.shared.original.Tee(sys.stderr, stream)):
                return run(out)


if __name__ == "__main__":
    sys.exit(main())
