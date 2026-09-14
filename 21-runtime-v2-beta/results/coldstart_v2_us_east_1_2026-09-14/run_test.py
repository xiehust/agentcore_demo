"""Bounded us-east-1 V2 test: 10 serial sessions, two c10 bursts, one smoke.

Run from the project with .venv/bin/python -B <this file>. Reuses the recorded
client without changing historical benchmark sources. All times are E2E.
"""
from pathlib import Path
import contextlib
import hashlib
import json
import os
import sys
import threading
import time
import uuid

OUT = Path(__file__).resolve().parent
HERE = OUT.parent.parent
sys.path.insert(0, str(HERE))
import boto3
from botocore.config import Config
from botocore.validate import validate_parameters
import coldstart_v2 as original
import multiprocess_coldstart_client as client
from coldstart_v2_multiprocess import stop_unresolved

def run():
    save = original.save
    region, account = "us-east-1", "434444145045"
    session = boto3.Session(region_name=region)
    cfg = Config(connect_timeout=15, read_timeout=60, retries={"total_max_attempts": 1})
    assert session.client("sts", config=cfg).get_caller_identity()["Account"] == account
    control = session.client("bedrock-agentcore-control", config=cfg)
    image = session.client("ecr", config=cfg).describe_images(
        repositoryName="agentcore-coldstart-pingpong", imageIds=[{"imageTag": "500mb"}])["imageDetails"][0]
    assert image["imageDigest"] == "sha256:8fce75a892c741d4712f9c3bdd2d0a8f429e69bebdd0393ea96c82f461029d87"
    role = session.client("iam", config=cfg).get_role(RoleName="AgentCoreColdstartRole")["Role"]["Arn"]
    request = {
        "agentRuntimeName": "coldstart_v2_east1_" + uuid.uuid4().hex[:8],
        "clientToken": str(uuid.uuid4()), "platformVersion": "V2",
        "agentRuntimeArtifact": {"containerConfiguration": {"containerUri":
            f"{account}.dkr.ecr.{region}.amazonaws.com/agentcore-coldstart-pingpong@{image['imageDigest']}"}},
        "roleArn": role, "networkConfiguration": {"networkMode": "PUBLIC"},
        "protocolConfiguration": {"serverProtocol": "HTTP"},
        "lifecycleConfiguration": {"idleRuntimeSessionTimeout": 60, "maxLifetime": 600},
        "description": "Temporary us-east-1 V2 500mb cold-start sample"}
    validate_parameters(request, control.meta.service_model.operation_model("CreateAgentRuntime").input_shape)
    sources = [Path(__file__), Path(original.__file__), Path(client.__file__),
               Path(client.shared.__file__), HERE / "check_v2.py",
               HERE / "coldstart_v2_multiprocess.py", original.BASELINE / "coldstart_test.py"]
    meta = {"started_iso": original.bench.utc_iso(), "region": region, "account": account,
            "platform_version": "V2", "size": "500mb", "settle_seconds": 180,
            "round_pause_seconds": 5, "serial_samples": 10, "burst_rounds": 2,
            "burst_concurrency": 10, "burst_processes": 8,
            "boto3": boto3.__version__, "botocore": original.botocore.__version__,
            "measurement": "new-session first invocation E2E through full body read",
            "source_sha256": {str(p.relative_to(HERE.parent)): hashlib.sha256(p.read_bytes()).hexdigest()
                              for p in sources}}
    dep = {"region": region, "account": account, "runtimes": {}}
    save(OUT / "run.json", meta)
    save(OUT / "images.json", {"500mb": image})
    save(OUT / "create_request.json", request)
    save(OUT / "deployments.json", dep)
    raw, rc = None, 1
    try:
        meta["create_inflight"] = True
        save(OUT / "run.json", meta)
        started = time.perf_counter()
        created = control.create_agent_runtime(**request)
        rt = {"id": created["agentRuntimeId"], "arn": created["agentRuntimeArn"],
              "create_response": created, "name": request["agentRuntimeName"]}
        dep["runtimes"]["500mb"] = rt
        save(OUT / "deployments.json", dep)
        meta["create_inflight"] = False
        print("CREATED", rt["id"], flush=True)
        ready = original.wait_ready(control, rt["id"], 600)
        original.wait_endpoint_ready(control, rt["id"], 600)
        meta["ready_seconds"] = time.perf_counter() - started
        rt["ready_response"] = ready
        save(OUT / "deployments.json", dep)
        assert ready["platformVersion"] == "V2"
        print("READY V2", meta["ready_seconds"], "seconds", flush=True)
        raw = client.make_client(region, 20)
        recorder = client.Recorder(raw)
        smoke = client.probe(recorder, rt["arn"], 1, 0, original.bench.new_session_id(), threading.Barrier(1))
        save(OUT / "smoke.json", {"request": smoke, "events": recorder.events})
        assert smoke["success"] and smoke["warm_ms"] is not None and smoke["stopped"]
        print("SMOKE", smoke["cold_ms"], "ms; SETTLE 180s", flush=True)
        meta["settle_started_iso"] = original.bench.utc_iso()
        save(OUT / "run.json", meta)
        time.sleep(180)
        recorder = client.Recorder(raw)
        serial = []
        for index in range(10):
            row = client.probe(recorder, rt["arn"], 1, index,
                               original.bench.new_session_id(), threading.Barrier(1))
            row["round"] = index + 1
            serial.append(row)
            save(OUT / "serial.json", {"requests": serial, "events": recorder.events})
            print("SERIAL", index + 1, row["cold_ms"], "ms warm", row["warm_ms"],
                  "error", row["error_msg"], flush=True)
            if index < 9:
                time.sleep(5)
        summaries = [{**original.bench.summarize_cell("500mb", 1, serial), "group": "serial"}]
        for rnd in (1, 2):
            time.sleep(5)
            folder = OUT / f"c10_round{rnd}"
            folder.mkdir()
            try:
                summary = client.run_burst(region, rt["arn"], 10, folder, process_count=8)
            except BaseException:
                stop_unresolved(region, rt["arn"], folder)
                raise
            summaries.append({**summary, "group": f"c10_round{rnd}"})
        save(OUT / "summary.json", {"cells": summaries})
        meta["measurement_complete"] = True
        rc = 0 if all(c["samples"] == c["success"] for c in summaries) and all(
            r["warm_ms"] is not None and r["stopped"] for r in serial) and all(
            c["warm_success"] == c["stop_success"] == c["samples"] for c in summaries[1:]) else 1
    except BaseException as exc:
        meta["error"] = f"{type(exc).__name__}: {exc}"
        print("ERROR", meta["error"], flush=True)
    finally:
        if raw is not None:
            raw.close()
        cleaned = original.cleanup(control, dep, OUT)
        meta.update(cleanup_complete=cleaned and not meta.get("create_inflight"),
                    finished_iso=original.bench.utc_iso())
        meta["exit_code"] = rc if meta["cleanup_complete"] else 1
        save(OUT / "run.json", meta)
    return meta["exit_code"]

if __name__ == "__main__":
    os.umask(0o077)
    if (OUT / "run.json").exists():
        raise SystemExit("Refusing to overwrite an existing run")
    with (OUT / "run.log").open("x", buffering=1) as stream:
        with contextlib.redirect_stdout(original.Tee(sys.stdout, stream)):
            with contextlib.redirect_stderr(original.Tee(sys.stderr, stream)):
                sys.exit(run())
