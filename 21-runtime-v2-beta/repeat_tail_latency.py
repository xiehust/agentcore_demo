"""Three independent repeats of 500mb/c100 and 2gb/c200, without retries.

Existing matrix/client files remain unchanged for source-hash verification.
All artifacts use the compatible matrix schema, with six new owned cells.
"""
import argparse
import contextlib
import hashlib
import io
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

import coldstart_v2_matrix as matrix

HERE = Path(__file__).resolve().parent
ORIGINAL = HERE / "results/coldstart_v2_matrix_2026-09-11"
CASES = (("500mb", 100), ("2gb", 200))
ROUNDS = 3
QUIET_SECONDS = 180
save = matrix.save

def planned_cells(out):
    return {f"{size}_c{concurrency}_r{rnd}": {
        "size": size, "concurrency": concurrency, "repeat": rnd, "reused": False,
        "folder": str((out / f"{size}_c{concurrency}_r{rnd}").relative_to(HERE))}
        for rnd in range(1, ROUNDS + 1) for size, concurrency in CASES}


def preflight(out):
    from verify_coldstart_v2_matrix import verify
    with contextlib.redirect_stdout(io.StringIO()):
        verify(ORIGINAL)
    original = json.loads((ORIGINAL / "matrix.json").read_text())
    region, account = original["region"], original["account"]
    assert region == "us-west-2"
    session = boto3.Session(region_name=region)
    cfg = Config(connect_timeout=15, read_timeout=60, retries={"total_max_attempts": 1})
    assert session.client("sts", config=cfg).get_caller_identity()["Account"] == account
    control = session.client("bedrock-agentcore-control", config=cfg)
    ecr = session.client("ecr", config=cfg)
    cells = planned_cells(out)
    templates, images, evidence_hashes = {}, {}, {}
    for size, concurrency in CASES:
        old_cell = original["cells"][f"{size}_c{concurrency}"]
        ready = old_cell["runtime"]["ready_response"]
        templates[size] = {field: ready[field] for field in (
            "agentRuntimeArtifact", "roleArn", "networkConfiguration", "protocolConfiguration",
            "lifecycleConfiguration", "platformVersion")}
        uri = templates[size]["agentRuntimeArtifact"]["containerConfiguration"]["containerUri"]
        image = ecr.describe_images(repositoryName=uri.split("/", 1)[1].split("@")[0],
            imageIds=[{"imageDigest": uri.split("@")[1]}])["imageDetails"][0]
        assert all(image[k] == original["images"][size][k] for k in ("imageDigest", "imageSizeInBytes"))
        images[size] = image
        for path in (HERE / old_cell["folder"]).glob("*.json"):
            evidence_hashes[str(path.relative_to(HERE))] = matrix.digest(path)
    evidence_hashes[str((ORIGINAL / "matrix.json").relative_to(HERE))] = matrix.digest(ORIGINAL / "matrix.json")
    requests = {}
    for key, cell in cells.items():
        request = {**templates[cell["size"]], "agentRuntimeName": f"tail_{key}_{uuid.uuid4().hex[:8]}",
                   "clientToken": str(uuid.uuid4()), "description": "Temporary repeated V2 tail-latency experiment"}
        validate_parameters(request, control.meta.service_model.operation_model("CreateAgentRuntime").input_shape)
        requests[key] = request
    sources = [Path(__file__), Path(matrix.__file__), Path(matrix.client.__file__),
               Path(matrix.client.base.__file__), Path(matrix.previous.__file__),
               Path(matrix.client.base.shared.__file__), HERE / "coldstart_v2.py", HERE / "check_v2.py",
               matrix.client.base.shared.original.BASELINE / "coldstart_test.py"]
    state = {"started_iso": matrix.client.base.shared.bench.utc_iso(), "region": region, "account": account,
             "cells": cells, "requests": requests, "images": images, "rounds_per_case": ROUNDS,
             "quiet_seconds": QUIET_SECONDS, "original_matrix": str((ORIGINAL / "matrix.json").relative_to(HERE)),
             "baseline_evidence_sha256": evidence_hashes,
             "source_sha256": {str(p.relative_to(HERE.parent)): matrix.digest(p) for p in sources},
             "quotas": matrix.client.base.shared.read_quotas(session), "python": sys.version,
             "boto3": boto3.__version__, "botocore": matrix.client.base.shared.botocore.__version__,
             "cpu_affinity": sorted(os.sched_getaffinity(0)), "tail_threshold_ms": [4000, 5000]}
    save(out / "matrix.json", state)
    return state, control


def run(out):
    state, ctl = preflight(out)
    rc = 1
    try:
        for key, cell in state["cells"].items():
            (HERE / cell["folder"]).mkdir()
            state["create_inflight"] = key
            save(out / "matrix.json", state)
            created = ctl.create_agent_runtime(**state["requests"][key])
            cell["runtime"] = {"id": created["agentRuntimeId"], "arn": created["agentRuntimeArn"],
                               "create_response": created, "image_digest": state["images"][cell["size"]]["imageDigest"]}
            save(out / "matrix.json", state)
            state.pop("create_inflight")
            save(out / "matrix.json", state)
            print("CREATED", key, created["agentRuntimeId"], flush=True)
        deadline = time.monotonic() + 1200
        for key, cell in state["cells"].items():
            runtime = cell["runtime"]
            ready = matrix.client.base.shared.original.wait_ready(ctl, runtime["id"], max(1, deadline - time.monotonic()))
            matrix.client.base.shared.original.wait_endpoint_ready(ctl, runtime["id"], max(1, deadline - time.monotonic()))
            assert ready.get("platformVersion") == "V2"
            runtime["ready_response"] = ready
            save(out / "matrix.json", state)
            raw = matrix.client.base.make_client(state["region"], 2)
            recorder = matrix.client.base.Recorder(raw)
            sid = matrix.client.base.shared.bench.new_session_id()
            try:
                smoke = matrix.client.base.probe(recorder, runtime["arn"], 1, 0, sid, threading.Barrier(1))
            finally:
                raw.close()
            smoke["size"] = cell["size"]
            save(HERE / cell["folder"] / "smoke.json", {"request": smoke, "events": recorder.events})
            assert smoke["success"] and smoke["warm_ms"] is not None and smoke["stopped"]
            cell["smoke_finished_perf"] = time.perf_counter()
            save(out / "matrix.json", state)
            print("SMOKE", key, smoke["cold_ms"], flush=True)
        last_traffic_end = time.perf_counter()
        for key, cell in state["cells"].items():
            # The test sends no target calls during this interval. Other account
            # traffic and undocumented limiter refill are not controlled.
            remaining = max(0, QUIET_SECONDS - (time.perf_counter() - last_traffic_end))
            print("QUIET", key, round(remaining, 1), "seconds", flush=True)
            time.sleep(remaining)
            cell["observed_quiet_seconds"] = time.perf_counter() - last_traffic_end
            assert cell["observed_quiet_seconds"] >= QUIET_SECONDS
            folder = HERE / cell["folder"]
            try:
                cell["summary"] = matrix.client.run_burst(cell["size"], state["region"],
                    cell["runtime"]["arn"], cell["concurrency"], folder)
            except BaseException:
                matrix.previous.stop_unresolved(state["region"], cell["runtime"]["arn"], folder)
                raise
            last_traffic_end = time.perf_counter()
            cell["traffic_ended_perf"] = last_traffic_end
            save(out / "matrix.json", state)
            matrix.cleanup_cell(ctl, state, key, out)
            if not cell["runtime"]["deleted"]:
                raise RuntimeError("Runtime deletion incomplete")
        state["measurement_complete"] = True
        summaries = [c["summary"] for c in state["cells"].values()]
        state["all_invocations_successful"] = all(s["success"] == s["warm_success"] == s["samples"] for s in summaries)
        rc = 0 if state["all_invocations_successful"] and all(s["stop_success"] == s["samples"] for s in summaries) else 1
    except BaseException as exc:
        state["error"] = f"{type(exc).__name__}: {exc}"
        print("ERROR", state["error"], flush=True)
    finally:
        if state.get("create_inflight"):
            try:
                matrix.reconcile_create(ctl, state, out)
            except Exception as exc:
                state["reconcile_error"] = type(exc).__name__
        for key in state["cells"]:
            try:
                matrix.cleanup_cell(ctl, state, key, out)
            except Exception as exc:
                state["cells"][key]["cleanup_error"] = type(exc).__name__
        state["cleanup_complete"] = not state.get("create_inflight") and all(
            "runtime" not in c or c["runtime"].get("deleted") for c in state["cells"].values())
        state["finished_iso"] = matrix.client.base.shared.bench.utc_iso()
        state["exit_code"] = rc if state["cleanup_complete"] else 1
        save(out / "matrix.json", state)
    return state["exit_code"]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True, help="new evidence directory under this project")
    args = parser.parse_args()
    os.umask(0o077)
    out = args.out.resolve()
    if HERE not in out.parents:
        parser.error("output must be under the project")
    out.mkdir(parents=True, exist_ok=False)
    with (out / "run.log").open("w", buffering=1) as stream:
        with contextlib.redirect_stdout(matrix.client.base.shared.original.Tee(sys.stdout, stream)):
            with contextlib.redirect_stderr(matrix.client.base.shared.original.Tee(sys.stderr, stream)):
                return run(out)


if __name__ == "__main__":
    sys.exit(main())
