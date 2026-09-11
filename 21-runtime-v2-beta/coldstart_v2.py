"""Run the original cold-start matrix on isolated, temporary V2 runtimes."""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
from pathlib import Path
import sys
import threading
import time
import uuid

import boto3
import botocore
from botocore.config import Config
from botocore.exceptions import ClientError

HERE = Path(__file__).resolve().parent
BASELINE = HERE.parent / "10-runtime-coldstart"
sys.path.insert(0, str(BASELINE))
import coldstart_test as bench
from check_v2 import wait_endpoint_ready, wait_ready

class Tee:
    def __init__(self, *streams):
        self.streams = streams

    def write(self, text):
        for stream in self.streams:
            stream.write(text)
            stream.flush()
        return len(text)

    def flush(self):
        for stream in self.streams:
            stream.flush()


def save(path: Path, value: dict) -> None:
    path.write_text(json.dumps(value, indent=2, default=str) + "\n")


def check_payload(status: int, body: dict) -> None:
    if status != 200 or body.get("message") != "pong":
        raise ValueError(f"unexpected response: HTTP {status}, {body!r}")
    for key in ("proc_start_ts", "request_ts"):
        if not isinstance(body.get(key), (int, float)):
            raise ValueError(f"missing numeric {key}: {body!r}")
    if body["request_ts"] < body["proc_start_ts"]:
        raise ValueError(f"negative process age: {body!r}")


def cleanup(ctl, dep: dict, out: Path) -> bool:
    records = []
    for size, runtime in dep["runtimes"].items():
        rec = {"size": size, "id": runtime["id"], "deleted": False}
        records.append(rec)
        try:
            ctl.delete_agent_runtime(agentRuntimeId=runtime["id"])
            rec["delete_requested_iso"] = bench.utc_iso()
        except ClientError as exc:
            if exc.response["Error"]["Code"] == "ResourceNotFoundException":
                rec["deleted"] = True
            else:
                rec["error"] = str(exc)
        except Exception as exc:
            rec["error"] = str(exc)
        save(out / "cleanup.json", {"runtimes": records})
    deadline = time.monotonic() + 180
    for rec in records:
        while not rec["deleted"] and "error" not in rec:
            try:
                rt = ctl.get_agent_runtime(agentRuntimeId=rec["id"])
                rec["last_status"] = rt["status"]
                if rt["status"] == "DELETE_FAILED":
                    raise RuntimeError(rt.get("failureReason", "DELETE_FAILED"))
                if time.monotonic() >= deadline:
                    raise TimeoutError("delete confirmation timed out")
                time.sleep(5)
            except ClientError as exc:
                if exc.response["Error"]["Code"] == "ResourceNotFoundException":
                    rec["deleted"] = True
                    rec["confirmed_iso"] = bench.utc_iso()
                else:
                    rec["error"] = str(exc)
            except Exception as exc:
                rec["error"] = str(exc)
        print("CLEANUP", json.dumps(rec))
        save(out / "cleanup.json", {"runtimes": records})
    return all(rec["deleted"] for rec in records)


def run(out: Path, settle_seconds: float) -> int:
    baseline = json.loads((BASELINE / "deployments.json").read_text())
    session = boto3.Session(region_name=baseline["region"])
    account = session.client("sts").get_caller_identity()["Account"]
    if account != baseline["account"]:
        raise RuntimeError("AWS account differs from the baseline; refusing deployment")
    ctl = session.client("bedrock-agentcore-control", config=Config(
        retries={"total_max_attempts": 1}, connect_timeout=30, read_timeout=60))
    ecr = session.client("ecr")
    dep = {"account": account, "region": baseline["region"],
           "platform_version": "V2", "iam_role": baseline["iam_role"],
           "runtimes": {}}
    run_meta = {
        "started_iso": bench.utc_iso(), "boto3": boto3.__version__,
        "botocore": botocore.__version__, "platform_version": "V2",
        "region": dep["region"], "account": account,
        "settle_seconds": settle_seconds, "pause_seconds": 5,
        "rounds": {"1": 10, "5": 4, "10": 2, "50": 1},
        "source_sha256": {str(path.relative_to(HERE.parent)):
            hashlib.sha256(path.read_bytes()).hexdigest() for path in
            (Path(__file__), HERE / "check_v2.py", BASELINE / "coldstart_test.py",
             BASELINE / "deployments.json", BASELINE / "results" / "summary.json")},
    }
    save(out / "run.json", run_meta)
    members = ctl.meta.service_model.operation_model("CreateAgentRuntime").input_shape.members
    if "platformVersion" not in members:
        raise RuntimeError("private SDK with platformVersion is required")
    images = {}
    for size, original in baseline["runtimes"].items():
        image = ecr.describe_images(repositoryName=baseline["ecr_repo"].split("/")[-1],
                                    imageIds=[{"imageTag": size}])["imageDetails"][0]
        if image["imageSizeInBytes"] != original["ecr_size_bytes"]:
            raise RuntimeError(f"{size} image size differs from baseline")
        images[size] = image
    save(out / "images.json", images)
    client = bench.make_client(dep["region"], 50)
    original_invoke = bench.timed_invoke
    payload_lock = threading.Lock()

    def recorded_invoke(client, arn, session_id):
        elapsed, status, body = original_invoke(client, arn, session_id)
        with payload_lock, (out / "responses.jsonl").open("a") as stream:
            stream.write(json.dumps({"runtime_arn": arn, "session_id": session_id,
                                     "elapsed_ms": elapsed, "status": status,
                                     "body": body}) + "\n")
        check_payload(status, body)
        return elapsed, status, body

    bench.timed_invoke = recorded_invoke
    rc = 1
    interrupted = False
    try:
        for size, original in baseline["runtimes"].items():
            name = f"coldstart_v2_{size}_{uuid.uuid4().hex[:8]}"
            started = bench.utc_iso()
            created = ctl.create_agent_runtime(
                agentRuntimeName=name,
                agentRuntimeArtifact={"containerConfiguration": {
                    "containerUri": original["image_uri"]}},
                roleArn=dep["iam_role"], networkConfiguration={"networkMode": "PUBLIC"},
                protocolConfiguration={"serverProtocol": "HTTP"},
                lifecycleConfiguration={"idleRuntimeSessionTimeout": 60},
                platformVersion="V2", description="Temporary V2 cold-start benchmark")
            dep["runtimes"][size] = {
                **original, "name": name, "id": created["agentRuntimeId"],
                "arn": created["agentRuntimeArn"], "create_started_iso": started,
                "create_response": created, "image_digest": images[size]["imageDigest"],
                "platform_version": "V2"}
            save(out / "deployments.json", dep)
            print("CREATED", size, created["agentRuntimeId"], flush=True)
        for size, rt in dep["runtimes"].items():
            ready = wait_ready(ctl, rt["id"], 600)
            wait_endpoint_ready(ctl, rt["id"], 600)
            if ready.get("platformVersion") != "V2":
                raise RuntimeError(f"{size}: expected V2, received {ready.get('platformVersion')!r}")
            rt["ready_observed_iso"] = bench.utc_iso()
            rt["ready_response"] = ready
            save(out / "deployments.json", dep)
            print("READY", size, "platformVersion=V2", flush=True)
            smoke = bench.probe(client, rt["arn"], size, 1, 1, 0, None)
            save(out / f"smoke_{size}.json", {"meta": {"mode": "post_deploy_smoke",
                 "platform_version": "V2", "region": dep["region"],
                 "runtime_arn": rt["arn"]}, "requests": [smoke]})
            print("SMOKE", size, json.dumps(smoke), flush=True)
            if not smoke["success"] or smoke["warm_ms"] is None:
                raise RuntimeError(f"{size} smoke failed; not running matrix")
        run_meta["settle_started_iso"] = bench.utc_iso()
        save(out / "run.json", run_meta)
        print(f"SETTLE {settle_seconds}s before matrix (no invocations)", flush=True)
        time.sleep(settle_seconds)
        run_meta["matrix_started_iso"] = bench.utc_iso()
        save(out / "run.json", run_meta)
        for size in dep["runtimes"]:
            for concurrency, rounds in ((1, 10), (5, 4), (10, 2), (50, 1)):
                bench.run_cell(client, dep, size, concurrency, rounds, 5, out)
                bench.rebuild_summary(out)
        run_meta["matrix_finished_iso"] = bench.utc_iso()
        cells = bench.rebuild_summary(out)
        bench.print_summary_table(cells)
        rc = 0 if len(cells) == 12 and all(
            c["success"] == c["samples"] for c in cells) else 1
    except KeyboardInterrupt:
        interrupted = True
        run_meta["error"] = "KeyboardInterrupt"
        rc = 130
    except Exception as exc:
        run_meta["error"] = f"{type(exc).__name__}: {exc}"
        print("ERROR", run_meta["error"], file=sys.stderr)
    finally:
        bench.timed_invoke = original_invoke
        try:
            bench.rebuild_summary(out, interrupted)
        finally:
            cleaned = cleanup(ctl, dep, out)
            run_meta.update(finished_iso=bench.utc_iso(), cleanup_complete=cleaned,
                            exit_code=rc if cleaned else 1)
            save(out / "run.json", run_meta)
    return run_meta["exit_code"]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True,
                        help="new output directory; must not already exist")
    parser.add_argument("--settle-seconds", type=float, default=180,
                        help="idle pause after smoke probes, before matrix (default 180)")
    args = parser.parse_args()
    if args.settle_seconds < 0:
        parser.error("--settle-seconds must be nonnegative")
    out = args.out.resolve()
    out.mkdir(parents=True, exist_ok=False)
    with (out / "run.log").open("w", buffering=1) as stream:
        with contextlib.redirect_stdout(Tee(sys.stdout, stream)):
            with contextlib.redirect_stderr(Tee(sys.stderr, stream)):
                return run(out, args.settle_seconds)


if __name__ == "__main__":
    sys.exit(main())
