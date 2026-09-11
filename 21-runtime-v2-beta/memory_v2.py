"""Same-image memory comparison: existing default runtimes versus temporary V2.

Run with the private .venv SDK. Cloud mutations require explicit approval.
Raw evidence stays in a separate, private, git-ignored output directory.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import sys
import time
from types import SimpleNamespace
import uuid

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError
from botocore.validate import validate_parameters

HERE = Path(__file__).resolve().parent
BASELINE = HERE.parent / "23-runtime-memory-usage"
sys.path.insert(0, str(BASELINE))
import lab
import analyze

CASES = [("small", "baseline"), ("small", "anonymous"),
         ("small", "file_cache"), ("padded", "baseline"), ("padded", "image_read")]
PHASES = {"baseline": ["baseline", "idle_control"],
          "anonymous": ["baseline", "allocated", "released"],
          "file_cache": ["baseline", "file_cached", "after_fadvise", "file_closed"],
          "image_read": ["baseline", "file_cached", "after_fadvise", "file_closed"]}

def bind_output(out):
    # Both reused modules must resolve evidence here, never in the baseline lab.
    lab.STATE = analyze.STATE = out


def client(state, service):
    return boto3.client(service, region_name=state["region"], config=Config(
        connect_timeout=10, read_timeout=300 if service == "bedrock-agentcore" else 30,
        retries={"total_max_attempts": 1}))


def save(state):
    lab.save(state)


def error_code(exc):
    return exc.response["Error"]["Code"] if isinstance(exc, ClientError) else type(exc).__name__


def source_hashes():
    return {str(p.relative_to(HERE.parent)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in [Path(__file__), BASELINE / "lab.py", BASELINE / "analyze.py",
                      BASELINE / "probe.py", BASELINE / "Dockerfile",
                      BASELINE / ".state" / "resources.json"]}


def validate_runtime(runtime, original, platform):
    assert runtime["status"] == "READY", "Runtime is not READY"
    assert runtime["agentRuntimeArtifact"] == original["agentRuntimeArtifact"], "Image mismatch"
    for field in ["roleArn", "networkConfiguration", "protocolConfiguration", "lifecycleConfiguration"]:
        assert runtime[field] == original[field], field + " differs"
    if platform == "V2":
        assert runtime.get("platformVersion") == "V2", "V2 was not confirmed by GetAgentRuntime"
    else:
        assert runtime.get("platformVersion") in (None, "V1"), "Unexpected control platform"


def prepare(out):
    baseline = json.loads((BASELINE / ".state" / "resources.json").read_text())
    state = {"name": "memv2-" + uuid.uuid4().hex[:8], "region": baseline["region"],
             "account": baseline["account"], "runtimes": {}, "sessions": [],
             "source_sha256": source_hashes(), "started": time.time(), "images": {},
             "boto3_version": boto3.__version__, "python_version": sys.version}
    assert state["region"] == "us-west-2", "Region differs from approval"
    assert client(state, "sts").get_caller_identity()["Account"] == state["account"], "Account mismatch"
    ctl, ecr = client(state, "bedrock-agentcore-control"), client(state, "ecr")
    shape = ctl.meta.service_model.operation_model("CreateAgentRuntime").input_shape
    assert "platformVersion" in shape.members, "Private SDK required"
    for variant in ("small", "padded"):
        original = deepcopy(baseline["runtimes"][variant])
        current = ctl.get_agent_runtime(agentRuntimeId=original["id"])
        validate_runtime(current, original["configuration"], "default")
        assert current["lifecycleConfiguration"] == {
            "idleRuntimeSessionTimeout": 60, "maxLifetime": 600}
        image = ecr.describe_images(repositoryName=baseline["repository"]["repositoryName"],
            imageIds=[{"imageTag": original["image"].rsplit(":", 1)[1]}])["imageDetails"][0]
        expected_size = 46204449 if variant == "small" else 314721975
        expected_digest = {"small": "sha256:4de7934d3eba693c147a4f79700cb07a21d80c9364b8fb278fd392bcdafb000f",
            "padded": "sha256:d4f779a5aa067ee33bf26074b98bb936eaecd4771c3edc7123bca1179d124922"}[variant]
        assert image["imageSizeInBytes"] == expected_size and image["imageDigest"] == expected_digest
        source_tag = hashlib.sha256((BASELINE / "probe.py").read_bytes() +
            (BASELINE / "Dockerfile").read_bytes() + str(original["pad_mib"]).encode()).hexdigest()[:12]
        assert source_tag == original["image"].rsplit(":", 1)[1], "Probe source differs from image tag"
        state["images"][variant] = image
        original.update(configuration=current, platform="default", image_variant=variant, owned=False)
        state["runtimes"]["default_" + variant] = original
        request = {"agentRuntimeName": (state["name"] + "_" + variant).replace("-", "_"),
            "agentRuntimeArtifact": current["agentRuntimeArtifact"], "roleArn": current["roleArn"],
            "networkConfiguration": current["networkConfiguration"],
            "protocolConfiguration": current["protocolConfiguration"],
            "lifecycleConfiguration": current["lifecycleConfiguration"],
            "platformVersion": "V2", "clientToken": str(uuid.uuid4()),
            "description": "Temporary approved same-image memory comparison",
            "tags": {"Project": "memory-v2-comparison"}}
        validate_parameters(request, shape)
        state["runtimes"]["v2_" + variant] = {"owned": True, "platform": "V2",
            "image_variant": variant, "image": original["image"], "create_request": request}
    save(state)
    print("PREFLIGHT passed; images/digests, controls, account, private SDK checked", flush=True)
    return state


def deploy(state):
    ctl = client(state, "bedrock-agentcore-control")
    for key, runtime in state["runtimes"].items():
        if not runtime["owned"]:
            continue
        # Persist the exact name/token/request before the call. Never auto-retry creation.
        runtime["create_started"] = time.time()
        save(state)
        created = ctl.create_agent_runtime(**runtime["create_request"])
        runtime.update(id=created["agentRuntimeId"], arn=created["agentRuntimeArn"],
                       create_response=created)
        save(state)
        print("CREATED", key, runtime["id"], flush=True)
    deadline = time.monotonic() + 600
    for key, runtime in state["runtimes"].items():
        if not runtime["owned"]:
            continue
        while True:
            current = ctl.get_agent_runtime(agentRuntimeId=runtime["id"])
            if current["status"] == "READY":
                original = state["runtimes"]["default_" + runtime["image_variant"]]["configuration"]
                validate_runtime(current, original, "V2")
                endpoint = ctl.get_agent_runtime_endpoint(agentRuntimeId=runtime["id"], endpointName="DEFAULT")
                if endpoint["status"] == "READY":
                    runtime.update(configuration=current, endpoint=endpoint, ready_at=time.time())
                    save(state)
                    break
                if endpoint["status"].endswith("FAILED"):
                    raise RuntimeError("Endpoint failed")
            elif current["status"].endswith("FAILED"):
                raise RuntimeError("Runtime failed")
            if time.monotonic() >= deadline:
                raise TimeoutError("V2 READY deadline")
            time.sleep(5)
        lab.configure_logging(state, key, runtime)
        save(state)
        print("READY", key, "platformVersion=V2; usage logging configured", flush=True)


def validate_body(body, session):
    assert body["run_id"] == session["id"] and body["kind"] == session["kind"]
    assert body["mib"] == 256
    assert [p["phase"] for p in body["phases"]] == PHASES[session["kind"]]
    assert session["start"] - 2 <= body["started"] <= body["ended"] <= time.time() + 2
    for phase in body["phases"]:
        assert 29 <= phase["end"] - phase["start"] <= 40
        samples = [s for s in body["samples"] if s["phase"] == phase["phase"]]
        assert len(samples) >= 25
        assert all(s["run_id"] == session["id"] for s in samples)


def invoke_case(state, key, kind):
    runtime = state["runtimes"][key]
    session = {"id": str(uuid.uuid4()), "variant": key, "kind": kind,
               "start": time.time()}
    state["sessions"].append(session)
    save(state)
    data = client(state, "bedrock-agentcore")
    print("INVOKE", key, kind, session["id"], flush=True)
    try:
        response = data.invoke_agent_runtime(agentRuntimeArn=runtime["arn"], qualifier="DEFAULT",
            runtimeSessionId=session["id"], contentType="application/json", accept="application/json",
            payload=json.dumps({"kind": kind, "mib": 256, "phase_seconds": 30,
                                "run_id": session["id"]}).encode())
        session["invoke_metadata"] = response["ResponseMetadata"]
        stream = response["response"]
        try:
            body = json.loads(stream.read())
        finally:
            stream.close()
        # Preserve returned evidence even when validation fails.
        lab.write_json(lab.STATE / (session["id"] + ".json"), body)
        session["result_file"] = session["id"] + ".json"
        assert response["ResponseMetadata"]["HTTPStatusCode"] == 200
        validate_body(body, session)
        session["validated"] = True
        print("SAMPLES", key, kind, len(body["samples"]), flush=True)
    except Exception as exc:
        session["error"] = error_code(exc)
        raise
    finally:
        session["end"] = time.time()
        # Do not let a failed evidence write prevent the stop attempt.
        try:
            session["stop_response"] = data.stop_runtime_session(agentRuntimeArn=runtime["arn"],
                runtimeSessionId=session["id"], qualifier="DEFAULT")
            session["stop_requested_at"] = time.time()
        except Exception as exc:
            session["stop_error"] = error_code(exc)
        save(state)
    assert session.get("stop_response", {}).get("ResponseMetadata", {}).get("HTTPStatusCode") == 200


def run_cases(state):
    for index, (variant, kind) in enumerate(CASES):
        # Serial pairs, alternating platform order to reduce time/order confounding.
        platforms = ("default", "v2") if index % 2 == 0 else ("v2", "default")
        for platform in platforms:
            invoke_case(state, platform + "_" + variant, kind)


def cleanup(state):
    ctl = client(state, "bedrock-agentcore-control")
    complete = True
    for key, runtime in state["runtimes"].items():
        if not runtime["owned"] or runtime.get("deleted"):
            continue
        if "id" not in runtime:
            # An ambiguous create is not silently retried or assumed absent.
            if "create_started" in runtime:
                runtime["cleanup_pending"] = "Create outcome unknown; reconcile saved request name/token"
                complete = False
            continue
        try:
            runtime["delete_response"] = ctl.delete_agent_runtime(agentRuntimeId=runtime["id"])
            save(state)
            deadline = time.monotonic() + 180
            while time.monotonic() < deadline:
                current = ctl.get_agent_runtime(agentRuntimeId=runtime["id"])
                if current["status"] == "DELETE_FAILED":
                    raise RuntimeError("DELETE_FAILED")
                time.sleep(5)
            raise TimeoutError("Deletion not confirmed")
        except ClientError as exc:
            if error_code(exc) == "ResourceNotFoundException":
                runtime["deleted"] = True
                runtime["deleted_at"] = time.time()
            else:
                runtime["cleanup_error"] = error_code(exc)
                complete = False
        except Exception as exc:
            runtime["cleanup_error"] = error_code(exc)
            complete = False
        save(state)
        print("CLEANUP", key, "deleted" if runtime.get("deleted") else "pending", flush=True)
    state["cleanup_complete"] = complete
    save(state)
    return complete


def retain_new_logs(state):
    logs = client(state, "logs")
    evidence = []
    for runtime in state["runtimes"].values():
        if not runtime["owned"] or "id" not in runtime:
            continue
        prefixes = ["/aws/bedrock-agentcore/runtimes/" + runtime["id"] + "-"]
        if runtime.get("usage_log_group"):
            prefixes.append(runtime["usage_log_group"])
        for prefix in prefixes:
            for page in logs.get_paginator("describe_log_groups").paginate(logGroupNamePrefix=prefix):
                for group in page["logGroups"]:
                    logs.put_retention_policy(logGroupName=group["logGroupName"], retentionInDays=7)
                    evidence.append(group["logGroupName"])
    state["seven_day_log_groups"] = evidence
    save(state)


def assess(state, telemetry):
    report = analyze.summarize(state, telemetry)
    records = []
    for runtime in telemetry["runtimes"].values():
        parsed, _ = analyze.parse_events(runtime["events"])
        records.extend(parsed)
    complete = len(report["sessions"]) == 10 and not report["parse_errors"]
    for session in report["sessions"]:
        for phase in session["phases"]:
            selected = sorted((r for r in records if r["session_id"] == session["session_id"]
                and phase["window_start"] <= r["timestamp"] <= phase["window_end"]),
                key=lambda r: r["timestamp"])
            gaps = [b["timestamp"] - a["timestamp"] for a, b in zip(selected, selected[1:])]
            phase["aws"]["spacing_seconds"] = analyze.stats(gaps)
            continuous = (len(selected) >= 23 and all(.95 <= gap <= 1.05 for gap in gaps)
                and all(.95 <= r["elapsed_seconds"] <= 1.05 for r in selected)
                and selected[0]["timestamp"] - phase["window_start"] <= 1.1
                and phase["window_end"] - selected[-1]["timestamp"] <= 1.1)
            phase["aws"]["continuous"] = continuous
            complete &= continuous and .99 <= phase["aws"]["coverage_fraction"] <= 1.05
    report["complete"] = bool(complete)
    return report


def collect(wait_seconds):
    state = lab.load_state()
    deadline = time.monotonic() + wait_seconds
    while True:
        telemetry = lab.collect(SimpleNamespace())
        report = assess(state, telemetry)
        lab.write_json(lab.STATE / "comparison.json", report)
        phases = [p for s in report["sessions"] for p in s["phases"]]
        print("COVERAGE", sum(p["aws"]["continuous"] for p in phases), "/", len(phases),
              "complete", report["complete"], "parse_errors", len(report["parse_errors"]), flush=True)
        if report["complete"]:
            return 0
        if report["parse_errors"]:
            print("Schema mismatch; raw telemetry saved, manual review required", flush=True)
            return 2
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return 2
        time.sleep(min(60, remaining))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["run", "collect", "cleanup"])
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--wait-seconds", type=int, default=3900, metavar="0..3900")
    args = parser.parse_args()
    if not 0 <= args.wait_seconds <= 3900:
        parser.error("--wait-seconds must be 0..3900")
    os.umask(0o077)
    out = args.out.resolve()
    if out == BASELINE / ".state" or BASELINE in out.parents:
        parser.error("Output must not be inside the baseline experiment")
    if args.command == "run":
        out.mkdir(mode=0o700, parents=True, exist_ok=False)
    elif not (out / "resources.json").is_file():
        parser.error("Existing resources.json required")
    bind_output(out)
    if args.command == "collect":
        return collect(args.wait_seconds)
    if args.command == "cleanup":
        return 0 if cleanup(lab.load_state()) else 1
    state = prepare(out)
    try:
        deploy(state)
        run_cases(state)
        state["calls_complete"] = True
    except BaseException as exc:
        state["run_error"] = error_code(exc)
        raise
    finally:
        try:
            retain_new_logs(state)
        finally:
            cleanup(state)
            state["finished"] = time.time()
            save(state)
    return 0 if state.get("calls_complete") and state.get("cleanup_complete") else 1


if __name__ == "__main__":
    sys.exit(main())

