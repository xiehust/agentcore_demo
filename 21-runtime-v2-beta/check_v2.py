"""Verify whether this account can use AgentCore Runtime **platformVersion=V2**.

Runtime V2 is enabled by a single extra parameter on CreateAgentRuntime /
UpdateAgentRuntime: ``platformVersion="V2"``. The field only exists in the
private botocore model shipped in Boto3CliV2Artifacts.zip, so run this with
the interpreter from ``21-runtime-v2-beta/.venv``.

What the script does (each step prints PASS / FAIL):

1. model   - the loaded botocore model exposes ``platformVersion``.
2. create  - CreateAgentRuntime(platformVersion="V2") is accepted. An account
             that is not allowlisted is expected to be rejected here
             (ValidationException / AccessDeniedException), which is the
             actual allowlist check.
3. ready   - the runtime reaches READY and GetAgentRuntime echoes
             ``platformVersion == "V2"``.
4. invoke  - InvokeAgentRuntime on the DEFAULT endpoint returns the agent's
             ping-pong payload, i.e. a V2 runtime actually serves traffic.
5. cleanup - the runtime is deleted unless ``--keep`` is given.

The agent image is the ping-pong container from 10-runtime-coldstart
(BedrockAgentCoreApp; returns {"message": "pong", ...}); pass ``--image`` and
``--role`` to use something else.

Usage:
    .venv/bin/python check_v2.py                 # full check, deletes the runtime
    .venv/bin/python check_v2.py --keep          # leave the V2 runtime deployed
    .venv/bin/python check_v2.py --region us-east-2 --image ... --role ...
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import uuid

import boto3
from botocore.exceptions import ClientError

ACCOUNT_DEFAULT_IMAGE = (
    "{account}.dkr.ecr.{region}.amazonaws.com/agentcore-coldstart-pingpong:500mb"
)
DEFAULT_ROLE = "arn:aws:iam::{account}:role/AgentCoreColdstartRole"


def log(step: str, ok: bool | None, msg: str) -> None:
    tag = "PASS" if ok else ("FAIL" if ok is False else "INFO")
    print(f"[{tag}] {step:<8} {msg}", flush=True)


def err_text(e: ClientError) -> str:
    return f"{e.response['Error']['Code']}: {e.response['Error']['Message']}"


def wait_ready(ctl, runtime_id: str, timeout: int) -> dict:
    deadline = time.time() + timeout
    while True:
        rt = ctl.get_agent_runtime(agentRuntimeId=runtime_id)
        status = rt["status"]
        if status == "READY":
            return rt
        if status.endswith("FAILED"):
            raise RuntimeError(f"runtime {status}: {rt.get('failureReason')}")
        if time.time() > deadline:
            raise TimeoutError(f"runtime still {status} after {timeout}s")
        time.sleep(5)


def wait_endpoint_ready(ctl, runtime_id: str, timeout: int) -> None:
    deadline = time.time() + timeout
    while True:
        ep = ctl.get_agent_runtime_endpoint(agentRuntimeId=runtime_id, endpointName="DEFAULT")
        if ep["status"] == "READY":
            return
        if ep["status"].endswith("FAILED"):
            raise RuntimeError(f"DEFAULT endpoint {ep['status']}: {ep.get('failureReason')}")
        if time.time() > deadline:
            raise TimeoutError(f"DEFAULT endpoint still {ep['status']} after {timeout}s")
        time.sleep(5)


def invoke_ping(data, runtime_arn: str, retries: int = 6) -> dict:
    """Invoke the agent; retry on the transient errors seen right after READY."""
    session_id = uuid.uuid4().hex + uuid.uuid4().hex[:8]  # >= 33 chars required
    last: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            t0 = time.time()
            resp = data.invoke_agent_runtime(
                agentRuntimeArn=runtime_arn,
                runtimeSessionId=session_id,
                contentType="application/json",
                accept="application/json",
                payload=json.dumps({"prompt": "ping", "attempt": attempt}).encode(),
            )
            body = resp["response"].read().decode()
            return {"latency_s": round(time.time() - t0, 2), "status": resp["statusCode"], "body": body}
        except ClientError as e:
            last = e
            code = e.response["Error"]["Code"]
            if code in ("RuntimeClientError", "ServiceUnavailableException", "ThrottlingException", "InternalServerException"):
                time.sleep(min(2**attempt, 20))
                continue
            raise
    assert last is not None
    raise last


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--region", default="us-west-2")
    ap.add_argument("--image", help="ECR image URI (must live in --region)")
    ap.add_argument("--role", help="runtime execution role ARN")
    ap.add_argument("--name", default=None, help="runtime name (default rtv2_check_<rand>)")
    ap.add_argument("--platform-version", default="V2", help="V1 or V2 (default V2)")
    ap.add_argument("--keep", action="store_true", help="do not delete the runtime afterwards")
    ap.add_argument("--timeout", type=int, default=600)
    args = ap.parse_args()

    account = boto3.client("sts", region_name=args.region).get_caller_identity()["Account"]
    image = args.image or ACCOUNT_DEFAULT_IMAGE.format(account=account, region=args.region)
    role = args.role or DEFAULT_ROLE.format(account=account)
    name = args.name or f"rtv2_check_{uuid.uuid4().hex[:6]}"

    ctl = boto3.client("bedrock-agentcore-control", region_name=args.region)
    data = boto3.client("bedrock-agentcore", region_name=args.region)
    print(f"account={account} region={args.region} botocore={boto3.__version__}\nimage={image}\nrole={role}\n")

    # 1. model -------------------------------------------------------------
    members = ctl.meta.service_model.operation_model("CreateAgentRuntime").input_shape.members
    if "platformVersion" not in members:
        log("model", False, "botocore model has no platformVersion; run with 21-runtime-v2-beta/.venv/bin/python")
        return 2
    log("model", True, "CreateAgentRuntime accepts platformVersion")

    # 2. create ------------------------------------------------------------
    try:
        created = ctl.create_agent_runtime(
            agentRuntimeName=name,
            agentRuntimeArtifact={"containerConfiguration": {"containerUri": image}},
            roleArn=role,
            networkConfiguration={"networkMode": "PUBLIC"},
            platformVersion=args.platform_version,
            description=f"Runtime {args.platform_version} availability check",
        )
    except ClientError as e:
        log("create", False, f"CreateAgentRuntime(platformVersion={args.platform_version}) rejected -> {err_text(e)}")
        print("\nRESULT: account is NOT able to use Runtime", args.platform_version, "in", args.region)
        return 1
    runtime_id = created["agentRuntimeId"]
    runtime_arn = created["agentRuntimeArn"]
    log("create", True, f"accepted; id={runtime_id} status={created['status']}")

    rc = 0
    try:
        # 3. ready ---------------------------------------------------------
        t0 = time.time()
        rt = wait_ready(ctl, runtime_id, args.timeout)
        wait_endpoint_ready(ctl, runtime_id, args.timeout)
        echoed = rt.get("platformVersion")
        ok = echoed == args.platform_version
        log("ready", ok, f"READY in {time.time() - t0:.0f}s; GetAgentRuntime.platformVersion={echoed!r}")
        if not ok:
            rc = 1

        # 4. invoke --------------------------------------------------------
        try:
            res = invoke_ping(data, runtime_arn)
            body_ok = '"pong"' in res["body"]
            log("invoke", body_ok, f"HTTP {res['status']} in {res['latency_s']}s body={res['body'][:200]}")
            if not body_ok:
                rc = 1
            # second call in a fresh session is usually warm-pool served; just informative
            res2 = invoke_ping(data, runtime_arn)
            log("invoke2", None, f"HTTP {res2['status']} in {res2['latency_s']}s")
        except ClientError as e:
            log("invoke", False, err_text(e))
            rc = 1
    except Exception as e:  # noqa: BLE001
        log("ready", False, str(e))
        rc = 1
    finally:
        # 5. cleanup -------------------------------------------------------
        if args.keep:
            log("cleanup", None, f"kept runtime {runtime_arn}")
        else:
            try:
                ctl.delete_agent_runtime(agentRuntimeId=runtime_id)
                log("cleanup", True, f"delete requested for {runtime_id}")
            except ClientError as e:
                log("cleanup", False, err_text(e))

    verdict = "IS" if rc == 0 else "is NOT (see FAIL lines above)"
    print(f"\nRESULT: account {account} {verdict} able to use Runtime {args.platform_version} in {args.region}")
    return rc


if __name__ == "__main__":
    sys.exit(main())
