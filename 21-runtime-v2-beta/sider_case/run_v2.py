"""Bounded us-east-1 Sider A/B V2 run; preserve evidence and clean owned resources."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import time
import uuid

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError
from botocore.validate import validate_parameters

HERE = Path(__file__).resolve().parent
REGION = "us-east-1"
ACCOUNT = "434444145045"
ROLE = f"arn:aws:iam::{ACCOUNT}:role/AgentCoreColdstartRole"
REPO = "agentcore-coldstart-pingpong"

def write(out, name, value):
    path = out / name
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(value, indent=2, default=str, ensure_ascii=False) + "\n")
    tmp.replace(path)


def client(service):
    return boto3.client(service, region_name=REGION, config=Config(
        connect_timeout=10, read_timeout=30, retries={"total_max_attempts": 1}))


def save(out, state):
    write(out, "state.json", state)


def wait_ready(ctl, rid):
    deadline = time.monotonic() + 600
    while time.monotonic() < deadline:
        rt = ctl.get_agent_runtime(agentRuntimeId=rid)
        if rt["status"] == "READY":
            ep = ctl.get_agent_runtime_endpoint(agentRuntimeId=rid, endpointName="DEFAULT")
            if ep["status"] == "READY":
                assert rt.get("platformVersion") == "V2"
                return rt, ep
            if ep["status"].endswith("FAILED"):
                raise RuntimeError(str(ep))
        if rt["status"].endswith("FAILED"):
            raise RuntimeError(str(rt))
        time.sleep(5)
    raise TimeoutError("Runtime/endpoint READY timeout")

def deploy(out, state, image):
    assert client("sts").get_caller_identity()["Account"] == ACCOUNT
    prefix = f"{ACCOUNT}.dkr.ecr.{REGION}.amazonaws.com/{REPO}@sha256:"
    assert image.startswith(prefix), "Use a pinned regional image digest"
    detail = client("ecr").describe_images(repositoryName=REPO,
        imageIds=[{"imageDigest": image.split("@", 1)[1]}])["imageDetails"][0]
    state["image"] = detail
    name = "sider_v2_" + uuid.uuid4().hex[:10]
    request = dict(agentRuntimeName=name,
        agentRuntimeArtifact={"containerConfiguration": {"containerUri": image}},
        roleArn=ROLE, networkConfiguration={"networkMode": "PUBLIC"},
        protocolConfiguration={"serverProtocol": "HTTP"},
        lifecycleConfiguration={"idleRuntimeSessionTimeout": 60, "maxLifetime": 600},
        platformVersion="V2", clientToken=str(uuid.uuid4()),
        description="Temporary Sider 4GiB free A/B memory validation")
    ctl = client("bedrock-agentcore-control")
    validate_parameters(request, ctl.meta.service_model.operation_model("CreateAgentRuntime").input_shape)
    state["create_request"] = request
    save(out, state)
    rt = ctl.create_agent_runtime(**request)
    state.update(runtime_id=rt["agentRuntimeId"], runtime_arn=rt["agentRuntimeArn"], create_response=rt)
    save(out, state)
    print("CREATED", state["runtime_id"], flush=True)
    rt, ep = wait_ready(ctl, state["runtime_id"])
    for field in ("roleArn", "agentRuntimeArtifact", "networkConfiguration", "protocolConfiguration", "lifecycleConfiguration"):
        assert rt[field] == request[field], field
    state.update(runtime=rt, endpoint=ep, ready_at=time.time())
    save(out, state)
    logs = client("logs")
    lg = "/aws/vendedlogs/bedrock-agentcore/" + name
    state["log_group"] = lg
    state["source_name"] = name + "_usage"
    state["destination_name"] = name + "_cwl"
    save(out, state)
    logs.create_log_group(logGroupName=lg)
    logs.put_retention_policy(logGroupName=lg, retentionInDays=30)
    state["source"] = logs.put_delivery_source(name=state["source_name"],
        resourceArn=state["runtime_arn"], logType="USAGE_LOGS")["deliverySource"]
    save(out, state)
    state["destination"] = logs.put_delivery_destination(name=state["destination_name"],
        outputFormat="json", deliveryDestinationType="CWL",
        deliveryDestinationConfiguration={"destinationResourceArn":
            f"arn:aws:logs:{REGION}:{ACCOUNT}:log-group:{lg}:*"})["deliveryDestination"]
    save(out, state)
    state["delivery"] = logs.create_delivery(deliverySourceName=state["source_name"],
        deliveryDestinationArn=state["destination"]["arn"])["delivery"]
    save(out, state)
    print("READY V2; usage delivery enabled", lg, flush=True)

def cleanup(out, state):
    ctl, data = client("bedrock-agentcore-control"), client("bedrock-agentcore")
    # Recover an uncertain create response by the unique persisted name.
    if not state.get("runtime_id") and state.get("create_request"):
        matches = [r for pg in ctl.get_paginator("list_agent_runtimes").paginate()
                   for r in pg.get("agentRuntimes", [])
                   if r["agentRuntimeName"] == state["create_request"]["agentRuntimeName"]]
        if len(matches) == 1:
            state.update(runtime_id=matches[0]["agentRuntimeId"], runtime_arn=matches[0]["agentRuntimeArn"])
            save(out, state)
    if not state.get("runtime_id"):
        return
    events = out / "events.json"
    try:
        records = json.loads(events.read_text()) if events.exists() else []
    except Exception as exc:
        records = []
        state["cleanup_events_error"] = repr(exc)
        save(out, state)
    stops = state.setdefault("stops", {})
    for rec in records:
        sid = rec["sid"]
        if sid in stops and "error" not in stops[sid]:
            continue
        try:
            stops[sid] = data.stop_runtime_session(agentRuntimeArn=state["runtime_arn"],
                runtimeSessionId=sid, qualifier="DEFAULT")
        except Exception as exc:
            code = exc.response["Error"]["Code"] if isinstance(exc, ClientError) else type(exc).__name__
            stops[sid] = {"not_found": True} if code == "ResourceNotFoundException" else {"error": str(exc)}
        save(out, state)
    if not state.get("runtime_deleted"):
        try:
            state["delete_response"] = ctl.delete_agent_runtime(agentRuntimeId=state["runtime_id"])
            save(out, state)
        except ClientError as exc:
            if exc.response["Error"]["Code"] != "ResourceNotFoundException":
                raise
        deadline = time.monotonic() + 300
        while time.monotonic() < deadline:
            try:
                ctl.get_agent_runtime(agentRuntimeId=state["runtime_id"])
            except ClientError as exc:
                if exc.response["Error"]["Code"] != "ResourceNotFoundException":
                    raise
                state.update(runtime_deleted=True, runtime_deleted_at=time.time())
                save(out, state)
                print("Runtime deletion confirmed", flush=True)
                return
            time.sleep(5)
        raise TimeoutError("Runtime deletion not confirmed")


def remove_delivery(out, state):
    # Keep logs/evidence; remove only delivery resources created by this run.
    logs = client("logs")
    for key, method, arg in (("delivery", "delete_delivery", "id"),
                             ("source", "delete_delivery_source", "name"),
                             ("destination", "delete_delivery_destination", "name")):
        if key not in state or state.get(key + "_deleted"):
            continue
        try:
            getattr(logs, method)(**{arg: state[key][arg]})
        except ClientError as exc:
            if exc.response["Error"]["Code"] != "ResourceNotFoundException":
                raise
        state[key + "_deleted"] = True
        save(out, state)

def run(out, image):
    out.mkdir(parents=True, exist_ok=False)
    state = {"region": REGION, "account": ACCOUNT, "started": time.time(),
             "boto3_version": boto3.__version__, "source_sha256": {
                 p.name: hashlib.sha256(p.read_bytes()).hexdigest()
                 for p in [HERE / n for n in ("main.py", "Dockerfile", "requirements.txt", "hyst.py", "run_v2.py")]}}
    save(out, state)
    try:
        deploy(out, state, image)
        state["test_started"] = time.time()
        save(out, state)
        cmd = [sys.executable, "-u", str(HERE / "hyst.py"), "--region", REGION,
               "--arn", state["runtime_arn"], "--endpoint", "DEFAULT", "--out", str(out)]
        result = subprocess.run(cmd, timeout=570, check=False)
        state.update(test_ended=time.time(), test_exit_code=result.returncode)
        save(out, state)
        if result.returncode:
            raise RuntimeError(f"A/B client failed: {result.returncode}")
        samples = [json.loads(line) for line in (out / "memory_samples.jsonl").read_text().splitlines()]
        for arm, phases in (("A", ("pre", "held_noop", "tail")), ("B", ("pre", "held", "tail"))):
            for phase, count in zip(phases, (60, 60, 240)):
                assert sum(s["_arm"] == arm and s["_phase"] == phase for s in samples) == count
        state["samples_complete"] = True
        save(out, state)
        print("A/B complete: 720 samples", flush=True)
    except BaseException as exc:
        state["error"] = repr(exc)
        save(out, state)
        raise
    finally:
        cleanup(out, state)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["run", "cleanup", "remove-delivery"])
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--image")
    args = parser.parse_args()
    if args.command == "run":
        if not args.image:
            parser.error("run requires --image")
        run(args.out.resolve(), args.image)
    else:
        state = json.loads((args.out / "state.json").read_text())
        if args.command == "cleanup":
            cleanup(args.out, state)
        else:
            remove_delivery(args.out, state)

