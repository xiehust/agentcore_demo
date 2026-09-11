"""Full image/concurrency multiprocess matrix with explicit immutable reuse.

Creates only the 12 missing cells. Existing 500mb c50/c100/c200 evidence is
referenced, never rewritten. No invoke retries, image rebuilds or IAM changes.
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
from botocore.exceptions import ClientError
from botocore.validate import validate_parameters

import matrix_multiprocess_client as client
import coldstart_v2_multiprocess as previous

HERE = Path(__file__).resolve().parent
SIZES = ("500mb", "1gb", "2gb")
LEVELS = (1, 10, 50, 100, 200)
REUSE = HERE / "results/coldstart_v2_multiprocess_500mb_2026-09-11"
TEMPLATES = HERE / "results/coldstart_v2_c200_2026-09-11"
save = client.base.shared.original.save

def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def cell_plan(out):
    cells = {}
    for size in SIZES:
        for concurrency in LEVELS:
            reused = size == "500mb" and concurrency in (50, 100, 200)
            folder = REUSE / f"c{concurrency}" if reused else out / f"{size}_c{concurrency}"
            key = f"{size}_c{concurrency}"
            cells[key] = {"size": size, "concurrency": concurrency, "reused": reused,
                          "folder": str(folder.relative_to(HERE))}
    return cells


def preflight(out):
    from verify_multiprocess_coldstart import verify
    with contextlib.redirect_stdout(io.StringIO()):
        verify(REUSE)
    templates = json.loads((TEMPLATES / "create_requests.json").read_text())
    old_images = json.loads((TEMPLATES / "images.json").read_text())
    reuse_dep = json.loads((REUSE / "deployments.json").read_text())
    region, account = reuse_dep["region"], reuse_dep["account"]
    assert region == "us-west-2"
    session = boto3.Session(region_name=region)
    cfg = Config(connect_timeout=15, read_timeout=60, retries={"total_max_attempts": 1})
    assert session.client("sts", config=cfg).get_caller_identity()["Account"] == account
    ctl = session.client("bedrock-agentcore-control", config=cfg)
    ecr = session.client("ecr", config=cfg)
    images = {}
    for size in SIZES:
        uri = templates[size]["agentRuntimeArtifact"]["containerConfiguration"]["containerUri"]
        image = ecr.describe_images(repositoryName=uri.split("/", 1)[1].split("@")[0],
            imageIds=[{"imageDigest": uri.split("@")[1]}])["imageDetails"][0]
        assert all(image[k] == old_images[size][k] for k in ("imageDigest", "imageSizeInBytes"))
        images[size] = image
    cells = cell_plan(out)
    requests = {}
    for key, cell in cells.items():
        if cell["reused"]:
            source = REUSE / f"c{cell['concurrency']}"
            cell["evidence_sha256"] = {str(p.relative_to(HERE)): digest(p) for p in source.glob("*.json")}
            cell["evidence_sha256"].update({str(p.relative_to(HERE)): digest(p)
                for p in [REUSE / "run.json", REUSE / "deployments.json", REUSE / "create_requests.json", REUSE / "images.json"]})
            cell["runtime"] = reuse_dep["runtimes"][str(cell["concurrency"])]
            cell["summary"] = json.loads((source / "summary.json").read_text())
            continue
        request = {**templates[cell["size"]], "agentRuntimeName": f"mpmatrix_{key}_{uuid.uuid4().hex[:8]}",
                   "clientToken": str(uuid.uuid4()), "description": "Temporary full multiprocess V2 matrix"}
        validate_parameters(request, ctl.meta.service_model.operation_model("CreateAgentRuntime").input_shape)
        requests[key] = request
    sources = [Path(__file__), Path(client.__file__), Path(client.base.__file__), Path(previous.__file__),
               Path(client.base.shared.__file__), HERE / "coldstart_v2.py", HERE / "check_v2.py",
               client.base.shared.original.BASELINE / "coldstart_test.py"]
    state = {"started_iso": client.base.shared.bench.utc_iso(), "region": region, "account": account,
             "cells": cells, "requests": requests, "images": images, "sizes": list(SIZES), "levels": list(LEVELS),
             "process_rule": "min(8, concurrency)", "settle_min_seconds": 180,
             "source_sha256": {str(p.relative_to(HERE.parent)): digest(p) for p in sources},
             "quotas": client.base.shared.read_quotas(session), "boto3": boto3.__version__,
             "botocore": client.base.shared.botocore.__version__, "python": sys.version,
             "cpu_affinity": sorted(os.sched_getaffinity(0))}
    save(out / "matrix.json", state)
    return state, ctl


def cleanup_cell(ctl, state, key, out):
    cell = state["cells"][key]
    if cell["reused"] or "runtime" not in cell or cell["runtime"].get("deleted"):
        return
    folder = HERE / cell["folder"]
    cell["runtime"]["deleted"] = client.base.shared.original.cleanup(
        ctl, {"runtimes": {key: cell["runtime"]}}, folder)
    save(out / "matrix.json", state)


def reconcile_create(ctl, state, out):
    key = state.get("create_inflight")
    if not key:
        return
    name = state["requests"][key]["agentRuntimeName"]
    token = None
    for _ in range(10):
        response = ctl.list_agent_runtimes(maxResults=100, **({"nextToken": token} if token else {}))
        matches = [r for r in response.get("agentRuntimes", []) if r["agentRuntimeName"] == name]
        if matches:
            assert len(matches) == 1
            r = matches[0]
            actual = ctl.get_agent_runtime(agentRuntimeId=r["agentRuntimeId"])
            assert actual["agentRuntimeName"] == name and actual.get("platformVersion") == "V2"
            assert actual["agentRuntimeArtifact"] == state["requests"][key]["agentRuntimeArtifact"]
            state["cells"][key]["runtime"] = {"id": r["agentRuntimeId"], "arn": r["agentRuntimeArn"],
                                              "reconciled_response": actual}
            state.pop("create_inflight")
            save(out / "matrix.json", state)
            return
        token = response.get("nextToken")
        if not token:
            break
    state["reconciliation_pending"] = key
    save(out / "matrix.json", state)


def run(out):
    state, ctl = preflight(out)
    rc = 1
    try:
        # Create each independent cell, but keep invoke traffic serial. Deployment
        # can settle in parallel; actual smoke-to-burst wait is retained per cell.
        for key, cell in state["cells"].items():
            if cell["reused"]:
                continue
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
            if cell["reused"]:
                continue
            runtime = cell["runtime"]
            remaining = max(1, deadline - time.monotonic())
            ready = client.base.shared.original.wait_ready(ctl, runtime["id"], remaining)
            client.base.shared.original.wait_endpoint_ready(ctl, runtime["id"], max(1, deadline - time.monotonic()))
            assert ready.get("platformVersion") == "V2"
            runtime["ready_response"] = ready
            save(out / "matrix.json", state)
            raw = client.base.make_client(state["region"], 2)
            recorder = client.base.Recorder(raw)
            sid = client.base.shared.bench.new_session_id()
            try:
                smoke = client.base.probe(recorder, runtime["arn"], 1, 0, sid, threading.Barrier(1))
            finally:
                raw.close()
            smoke["size"] = cell["size"]
            save(HERE / cell["folder"] / "smoke.json", {"request": smoke, "events": recorder.events})
            assert smoke["success"] and smoke["warm_ms"] is not None and smoke["stopped"]
            cell["smoke_stopped_perf"] = recorder.events[-1]["completed_perf"]
            save(out / "matrix.json", state)
            print("SMOKE", key, smoke["cold_ms"], flush=True)
        print("SETTLE 180s after last smoke; no target invocations", flush=True)
        time.sleep(180)
        for key, cell in state["cells"].items():
            if cell["reused"]:
                continue
            folder = HERE / cell["folder"]
            runtime = cell["runtime"]
            cell["observed_settle_seconds"] = time.perf_counter() - cell["smoke_stopped_perf"]
            assert cell["observed_settle_seconds"] >= 180
            save(out / "matrix.json", state)
            try:
                cell["summary"] = client.run_burst(cell["size"], state["region"], runtime["arn"],
                                                   cell["concurrency"], folder)
            except BaseException:
                previous.stop_unresolved(state["region"], runtime["arn"], folder)
                raise
            save(out / "matrix.json", state)
            cleanup_cell(ctl, state, key, out)
            if not runtime["deleted"]:
                raise RuntimeError("Runtime deletion incomplete")
            # Avoid overlapping account-level bursts; not a guarantee that quota tokens refill.
            time.sleep(10)
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
                reconcile_create(ctl, state, out)
            except Exception as exc:
                state["reconcile_error"] = type(exc).__name__
        for key, cell in state["cells"].items():
            if not cell["reused"]:
                try:
                    cleanup_cell(ctl, state, key, out)
                except Exception as exc:
                    cell["cleanup_error"] = type(exc).__name__
        state["cleanup_complete"] = not state.get("create_inflight") and all(
            c["reused"] or "runtime" not in c or c["runtime"].get("deleted") for c in state["cells"].values())
        state["finished_iso"] = client.base.shared.bench.utc_iso()
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
        parser.error("output must be under the project directory")
    out.mkdir(parents=True, exist_ok=False)
    with (out / "run.log").open("w", buffering=1) as stream:
        with contextlib.redirect_stdout(client.base.shared.original.Tee(sys.stdout, stream)):
            with contextlib.redirect_stderr(client.base.shared.original.Tee(sys.stderr, stream)):
                return run(out)


if __name__ == "__main__":
    sys.exit(main())
