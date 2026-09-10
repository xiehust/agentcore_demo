#!/usr/bin/env python3
"""Plan/deploy/run/clean up an isolated JuiceFS vs S3 lab. Plan is read-only."""
from __future__ import annotations

import argparse
import json
import os
import secrets
import shlex
import subprocess
import sys
import time
import uuid
from contextlib import ExitStack
from datetime import datetime, timezone
from pathlib import Path

import boto3
from botocore.config import Config

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "deploy/juicefs"))
from stack import build_template
from runtime_cmd import RuntimeSession, retry_conflicts

def save(path: Path, record: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(record, indent=2, ensure_ascii=False) + "\n")
    os.replace(temp, path)


def api(service, region):
    return boto3.client(service, region_name=region,
                        config=Config(retries={"mode": "standard", "total_max_attempts": 3}))


def ssm_command(client, instance: str, script: str, timeout: int = 900) -> dict:
    command = client.send_command(
        InstanceIds=[instance], DocumentName="AWS-RunShellScript",
        Parameters={"commands": [script], "executionTimeout": [str(timeout)]},
        TimeoutSeconds=60)["Command"]["CommandId"]
    deadline = time.monotonic() + timeout + 120
    while time.monotonic() < deadline:
        try:
            result = client.get_command_invocation(CommandId=command, InstanceId=instance)
        except client.exceptions.InvocationDoesNotExist:
            time.sleep(2)
            continue
        if result["Status"] in {"Success", "Failed", "Cancelled", "TimedOut", "Cancelling"}:
            if result["Status"] != "Success" or result.get("ResponseCode") != 0:
                # Keep raw output in SSM; do not risk printing credentials in process errors.
                raise RuntimeError(f"SSM command {command}: {result['Status']}; inspect SSM output")
            return {"command_id": command, "status": result["Status"],
                    "stdout": result.get("StandardOutputContent", "")}
        time.sleep(3)
    raise TimeoutError(f"SSM command timeout: {command}")


def wait_runtime(client, runtime_id):
    deadline = time.monotonic() + 900
    while time.monotonic() < deadline:
        response = client.get_agent_runtime(agentRuntimeId=runtime_id)
        if response["status"] == "READY":
            return response
        if "FAILED" in response["status"]:
            raise RuntimeError(f"Runtime {runtime_id} failed")
        time.sleep(5)
    raise TimeoutError(f"Runtime {runtime_id} not ready")


def deploy(args) -> int:
    if not args.approve_costs:
        raise ValueError("deployment requires --approve-costs after reviewing plan")
    if args.state.exists():
        raise ValueError("state file exists; refusing to overwrite an existing deployment")
    if not (ROOT / "build/juicefs-fixtures/fixture.json").exists():
        raise ValueError("run scripts/07-prepare-workspace-fixture.py before deployment")
    region = args.region
    identity = api("sts", region).get_caller_identity()
    deployment = "jfsbench-" + uuid.uuid4().hex[:10]
    state = {"region": region, "account": identity["Account"], "deployment": deployment,
             "schema_version": 2, "runtime": None, "created_at": datetime.now(timezone.utc).isoformat()}
    save(args.state, state)
    cf = api("cloudformation", region)
    ssm = api("ssm", region)
    ami = ssm.get_parameter(Name="/aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-arm64")["Parameter"]["Value"]
    state["ami"] = ami
    caller = identity["Arn"]
    if ":assumed-role/" in caller:
        role_name = caller.split(":assumed-role/", 1)[1].rsplit("/", 1)[0].rsplit("/", 1)[-1]
        caller = api("iam", region).get_role(RoleName=role_name)["Role"]["Arn"]
    elif ":user/" not in caller:
        raise ValueError("deploy as a trusted IAM user or assumed role, not root")
    state["controller_arn"] = caller
    result = cf.create_stack(StackName=deployment, TemplateBody=json.dumps(build_template()),
        Parameters=[{"ParameterKey": "AmiId", "ParameterValue": ami},
                    {"ParameterKey": "ControllerArn", "ParameterValue": caller}],
        Capabilities=["CAPABILITY_IAM"], OnFailure="DO_NOTHING",
        Tags=[{"Key": "Purpose", "Value": "juicefs-benchmark"}])
    state["stack_id"] = result["StackId"]
    save(args.state, state)
    print(f"Creating {deployment}; state saved to {args.state}", flush=True)
    cf.get_waiter("stack_create_complete").wait(StackName=state["stack_id"], WaiterConfig={"Delay": 10, "MaxAttempts": 120})
    stack = cf.describe_stacks(StackName=state["stack_id"])["Stacks"][0]
    state["outputs"] = {entry["OutputKey"]: entry["OutputValue"] for entry in stack["Outputs"]}
    save(args.state, state)
    output = state["outputs"]
    sm = api("secretsmanager", region)
    sm.put_secret_value(SecretId=output["AdminSecretArn"], SecretString=json.dumps({
        "AccessKeyId": "admin-" + secrets.token_hex(8), "SecretAccessKey": secrets.token_hex(24)}))
    for suffix in ("A", "B"):
        sm.put_secret_value(SecretId=output[f"Tenant{suffix}SecretArn"], SecretString=json.dumps({
            "credentials": {"AccessKeyId": "tenant-" + secrets.token_hex(8), "SecretAccessKey": secrets.token_hex(24)}}))
    deadline = time.monotonic() + 600
    while time.monotonic() < deadline:
        instances = ssm.describe_instance_information(Filters=[{"Key": "InstanceIds", "Values": [output["GatewayInstanceId"]]}])["InstanceInformationList"]
        if instances and instances[0]["PingStatus"] == "Online":
            break
        time.sleep(5)
    else:
        raise TimeoutError("gateway SSM agent not online")
    variables = {"AWS_REGION": region, "DATA_BUCKET": output["DataBucket"], "DEPLOYMENT_ID": deployment,
        "ADMIN_SECRET_ARN": output["AdminSecretArn"], "TENANT_A_SECRET_ARN": output["TenantASecretArn"],
        "TENANT_B_SECRET_ARN": output["TenantBSecretArn"]}
    script = "\n".join(f"export {key}={shlex.quote(value)}" for key, value in variables.items())
    script += "\n" + (ROOT / "deploy/juicefs/bootstrap.sh").read_text()
    # Run with bash explicitly; SSM shell does not imply bash.
    command = "/bin/bash -c " + shlex.quote(script)
    state["bootstrap"] = ssm_command(ssm, output["GatewayInstanceId"], command, timeout=1200)
    save(args.state, state)
    repository = output["RepositoryUri"]
    image = repository + ":demo"
    auth = api("ecr", region).get_authorization_token()["authorizationData"][0]
    import base64
    username, password = base64.b64decode(auth["authorizationToken"]).decode().split(":", 1)
    # Use an isolated Docker config so this demo never replaces the operator's login.
    docker_config = ROOT / "build" / deployment / "docker-config"
    docker_config.mkdir(parents=True, exist_ok=True, mode=0o700)
    docker = ["docker", "--config", str(docker_config)]
    subprocess.run(docker + ["login", "--username", username, "--password-stdin", auth["proxyEndpoint"]],
                   input=password, text=True, check=True, stdout=subprocess.DEVNULL)
    try:
        subprocess.run(docker + ["build", "--platform", "linux/arm64", "-f", "deploy/juicefs/Dockerfile", "-t", image, "."], cwd=ROOT, check=True)
        subprocess.run(docker + ["push", image], check=True)
    finally:
        subprocess.run(docker + ["logout", auth["proxyEndpoint"]], check=False, stdout=subprocess.DEVNULL)
    control = api("bedrock-agentcore-control", region)
    created = control.create_agent_runtime(
        agentRuntimeName=deployment.replace("-", "_"),
        agentRuntimeArtifact={"containerConfiguration": {"containerUri": image}},
        roleArn=output["RuntimeRole"], protocolConfiguration={"serverProtocol": "HTTP"},
        networkConfiguration={"networkMode": "VPC", "networkModeConfig": {
            "subnets": [output["SubnetId"]], "securityGroups": [output["RuntimeSecurityGroup"]]}},
        environmentVariables={"AWS_REGION": region},
        lifecycleConfiguration={"idleRuntimeSessionTimeout": 3600, "maxLifetime": 3600},
        tags={"Purpose": "juicefs-benchmark"})
    state["runtime"] = {"id": created["agentRuntimeId"], "arn": created["agentRuntimeArn"]}
    save(args.state, state)
    wait_runtime(control, created["agentRuntimeId"])
    state.update(ready=True, benchmark_engine="s5cmd", image_uri=image)
    save(args.state, state)
    print(f"READY: {args.state}; no benchmark has run yet")
    return 0


def update_runtime(args):
    """Replace only this demo runtime image; never create a second runtime."""
    if not args.approve_costs:
        raise ValueError("update requires --approve-costs and no active benchmark")
    state = json.loads(args.state.read_text())
    region, output = state["region"], state["outputs"]
    if api("sts", region).get_caller_identity()["Account"] != state["account"]:
        raise ValueError("wrong AWS account")
    image = output["RepositoryUri"] + ":s5cmd-" + uuid.uuid4().hex[:10]
    import base64
    import fcntl
    with (ROOT / "build" / (state["deployment"] + ".lock")).open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        auth = api("ecr", region).get_authorization_token()["authorizationData"][0]
        username, password = base64.b64decode(auth["authorizationToken"]).decode().split(":", 1)
        config = ROOT / "build" / state["deployment"] / "docker-config"
        config.mkdir(parents=True, exist_ok=True, mode=0o700)
        docker = ["docker", "--config", str(config)]
        subprocess.run(docker + ["login", "--username", username, "--password-stdin", auth["proxyEndpoint"]],
                       input=password, text=True, check=True, stdout=subprocess.DEVNULL)
        try:
            subprocess.run(docker + ["build", "--platform", "linux/arm64", "-f", "deploy/juicefs/Dockerfile", "-t", image, "."], cwd=ROOT, check=True)
            subprocess.run(docker + ["push", image], check=True)
        finally:
            subprocess.run(docker + ["logout", auth["proxyEndpoint"]], stdout=subprocess.DEVNULL, check=False)
        control = api("bedrock-agentcore-control", region)
        previous = control.get_agent_runtime(agentRuntimeId=state["runtime"]["id"])
        state["previous_artifact"] = previous["agentRuntimeArtifact"]
        state["ready"] = False
        save(args.state, state)
        control.update_agent_runtime(agentRuntimeId=state["runtime"]["id"],
            agentRuntimeArtifact={"containerConfiguration": {"containerUri": image}},
            roleArn=output["RuntimeRole"], networkConfiguration={"networkMode": "VPC", "networkModeConfig": {
                key: previous["networkConfiguration"]["networkModeConfig"][key]
                for key in ("subnets", "securityGroups")}},
            protocolConfiguration={"serverProtocol": "HTTP"}, environmentVariables={"AWS_REGION": region},
            lifecycleConfiguration={"idleRuntimeSessionTimeout": 3600, "maxLifetime": 3600})
        ready = wait_runtime(control, state["runtime"]["id"])
        state.update(ready=True, benchmark_engine="s5cmd", image_uri=image,
                     runtime_version=ready["agentRuntimeVersion"])
        save(args.state, state)
    print("Single runtime updated to s5cmd; run a new session for new tests")
    return 0


def invoke(session: RuntimeSession, payload: dict) -> dict:
    response = retry_conflicts(lambda: session.client.invoke_agent_runtime(
        agentRuntimeArn=session.runtime_arn, runtimeSessionId=session.session_id,
        qualifier="DEFAULT", contentType="application/json", accept="application/json",
        payload=json.dumps(payload).encode()))
    stream = response["response"]
    try:
        status = response.get("statusCode", response.get("ResponseMetadata", {}).get("HTTPStatusCode"))
        if status != 200 or response.get("runtimeSessionId") != session.session_id:
            raise RuntimeError("invoke response did not confirm status/session routing")
        result = json.loads(stream.read())
    finally:
        stream.close()
    if not isinstance(result, dict):
        raise RuntimeError("runtime returned non-object response")
    return result


def session_bootstrap(state, tenant, session_id, token, *, refresh=False):
    """Trusted controller only; never persist or log this return value."""
    suffix = {"tenant-a": "A", "tenant-b": "B"}[tenant]
    output, region = state["outputs"], state["region"]
    gateway = json.loads(api("secretsmanager", region).get_secret_value(
        SecretId=output[f"Tenant{suffix}SecretArn"])["SecretString"])
    scoped = api("sts", region).assume_role(
        RoleArn=output[f"TenantDataRole{suffix}"], RoleSessionName=f"jfs-{tenant}-{session_id[-12:]}",
        DurationSeconds=3600)["Credentials"]
    return {"action": "refresh-credentials" if refresh else "initialize",
            "tenant": tenant, "session_id": session_id, "session_token": token,
            "storage": {"region": region, "data_bucket": output["DataBucket"], "juicefs": gateway,
                "s3": {"credentials": {key: scoped[key] for key in ("AccessKeyId", "SecretAccessKey", "SessionToken")},
                       "expires_at": scoped["Expiration"].timestamp()}}}


def open_tenant_sessions(stack, state):
    """One runtime ARN, a new server-generated session ID for each demo user."""
    return {tenant: stack.enter_context(RuntimeSession(state["runtime"]["arn"], state["region"], read_timeout=900))
            for tenant in ("tenant-a", "tenant-b")}


def fresh_gateway_cache(state, label):
    # Rotate to a new directory; never delete cache or metadata to manufacture a result.
    if not label.replace("-", "").isalnum():
        raise ValueError("invalid cache label")
    script = f"""set -eu
mkdir -p /etc/systemd/system/juicefs-demo.service.d
mkdir /opt/juicefs-demo/cache-{label}
cat > /etc/systemd/system/juicefs-demo.service.d/cache.conf <<'EOF'
[Service]
ExecStart=
ExecStart=/opt/juicefs-demo/juicefs gateway --multi-buckets --object-meta --keep-etag --no-banner --cache-dir /opt/juicefs-demo/cache-{label} --cache-size 1024 --refresh-iam-interval 1s sqlite3:///opt/juicefs-demo/metadata.db 0.0.0.0:9000
EOF
systemctl daemon-reload
systemctl restart juicefs-demo
for i in $(seq 1 60); do
  if curl -fsS --cacert /opt/juicefs-demo/.minio/certs/public.crt https://127.0.0.1:9000/minio/health/live >/dev/null; then break; fi
  sleep 1
done
curl -fsS --cacert /opt/juicefs-demo/.minio/certs/public.crt https://127.0.0.1:9000/minio/health/live >/dev/null
printf 'new-empty-disk-cache={label}\\n'
"""
    return ssm_command(api("ssm", state["region"]), state["outputs"]["GatewayInstanceId"], script, 120)


def write_report(path: Path, record: dict):
    import statistics
    groups = {}
    for row in record["measurements"]:
        key = (row["workload"], row["workers"], row["label"])
        groups.setdefault(key, {}).setdefault(row["backend"], []).append(row)
    lines = ["# s5cmd：原生 S3 / JuiceFS Gateway 测试结果", "",
        f"- run: `{record['run_id']}`；region: `{record['region']}`；success: `{record.get('success', False)}`。",
        "- 只接受 s5cmd-workspace-v1 结果，不包含 SDK 文件传输基准。",
        "- file_batch 包含 s5cmd 启动、连接和批量文件复制；wall 额外包含 manifest、模式/哈希校验。",
        "- 冷缓存仅指网关进程及本地缓存，热恢复仍下载至全新目录。", "",
        "| 工作区 | workers | 阶段 | 指标 | S3 秒 | JuiceFS 秒 | S3/JuiceFS |",
        "|---|---:|---|---|---:|---:|---:|"]
    for (workload, workers, label), backends in sorted(groups.items()):
        for metric in ("file_batch_seconds", "wall_seconds"):
            values = {}
            for backend, rows in backends.items():
                if (len(rows) == record["repetitions"] and
                    {r["repetition"] for r in rows} == set(range(record["repetitions"])) and
                    all(r["success"] and r.get("engine") == "s5cmd" for r in rows)):
                    values[backend] = statistics.median(r[metric] for r in rows)
            a, b = values.get("s3"), values.get("juicefs")
            ratio = f"{a/b:.2f}x" if a is not None and b else "N/A"
            left = f"{a:.4f}" if a is not None else "N/A"
            right = f"{b:.4f}" if b is not None else "N/A"
            lines.append(f"| {workload} | {workers} | {label} | {metric} | {left} | {right} | {ratio} |")
    lines += ["", "按轮次取中位数；n=1 时仅为单次观察。缺失或失败的组不计算倍率。",
              "没有单文件 p95 或实际 SDK 重试计数；不能从聚合 CLI 时间反推这些指标。",
              "每个文件仍是独立对象；不是 ZIP 打包传输或 POSIX 挂载测试。", ""]
    path.write_text("\n".join(lines))


def safe_exception(exc) -> dict:
    response = getattr(exc, "response", {})
    return {"type": type(exc).__name__, "code": response.get("Error", {}).get("Code"),
            "status": response.get("ResponseMetadata", {}).get("HTTPStatusCode"),
            "request_id": response.get("ResponseMetadata", {}).get("RequestId")}


def run(args) -> int:
    # Single-controller-host demo: process lock prevents overlapping cache resets.
    # Cross-host invocation of this deployment is unsupported; use a distributed lease in production.
    import fcntl
    state = json.loads(args.state.read_text())
    lock_dir = ROOT / "build"
    lock_dir.mkdir(exist_ok=True)
    with (lock_dir / (state["deployment"] + ".lock")).open("w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError("another local controller is running this deployment") from None
        return run_locked(args)


def run_locked(args) -> int:
    state = json.loads(args.state.read_text())
    if (not state.get("ready") or state.get("schema_version") != 2 or not state.get("runtime")
            or state.get("benchmark_engine") != "s5cmd"):
        raise ValueError("update/deploy the single runtime to s5cmd before running")
    if args.out.exists():
        raise ValueError("output exists; choose a new --out to preserve prior evidence")
    if not 1 <= args.repetitions <= 3:
        raise ValueError("repetitions must be 1..3")
    record = {"run_id": "run-" + uuid.uuid4().hex, "region": state["region"],
        "started_at": datetime.now(timezone.utc).isoformat(), "repetitions": args.repetitions,
        "deployment": state["deployment"], "measurements": [], "isolation": [], "cache_resets": [],
        "schema": "s5cmd-workspace-v1", "engine": "s5cmd", "workers": args.workers,
        "versions": {"juicefs": "1.4.1", "s5cmd": "v2.3.0"},
        "writeback": False, "gateway_instance_type": "m7g.large", "metadata": "SQLite-on-EBS",
        "success": False}
    save(args.out, record)
    deadline = time.monotonic() + 45 * 60
    sessions = {}
    tokens = {tenant: secrets.token_hex(32) for tenant in ("tenant-a", "tenant-b")}
    expires = {}
    bindings = {}

    def initialize(tenant, *, refresh=False):
        session = sessions[tenant]
        bootstrap = session_bootstrap(state, tenant, session.session_id, tokens[tenant], refresh=refresh)
        result = invoke(session, bootstrap)
        if (not result.get("success") or result.get("tenant") != tenant or result.get("session_id") != session.session_id
                or result.get("schema") != "s5cmd-workspace-v1"):
            raise RuntimeError("session initialization/routing failed")
        if refresh and result.get("process_id") != bindings[tenant]["process_id"]:
            raise RuntimeError("session process changed; cannot continue frozen snapshot benchmark")
        expires[tenant] = bootstrap["storage"]["s3"]["expires_at"]
        bindings[tenant] = {key: result[key] for key in ("tenant", "session_id", "process_id")}
        # bootstrap and tokens deliberately never enter state or report files.

    def call(tenant, payload):
        if time.monotonic() > deadline:
            raise TimeoutError("45-minute experiment budget exhausted")
        if expires[tenant] - time.time() < 1200:
            initialize(tenant, refresh=True)
        result = invoke(sessions[tenant], {**payload, "session_token": tokens[tenant]})
        if time.monotonic() > deadline:
            raise TimeoutError("experiment completed an operation after deadline")
        if not result.get("success"):
            record["failed_response"] = result
            save(args.out, record)
            raise RuntimeError("runtime operation failed; see saved report")
        return result

    try:
        with ExitStack() as stack:
            sessions = open_tenant_sessions(stack, state)
            for tenant in sessions:
                initialize(tenant)
                call(tenant, {"action": "ping"})
            if len({session.session_id for session in sessions.values()}) != 2:
                raise RuntimeError("distinct user session IDs required")
            if len({item["process_id"] for item in bindings.values()}) != 2:
                raise RuntimeError("sessions unexpectedly share an agent process")
            record["runtime_arn"] = state["runtime"]["arn"]
            record["sessions"] = bindings.copy()
            record["routing_note"] = "one ARN, distinct context session IDs/process UUIDs; not hardware microVM attestation"
            # A valid A token sent to B's session must fail before any storage operation.
            cross = invoke(sessions["tenant-b"], {"action": "ping", "session_token": tokens["tenant-a"]})
            if cross != {"success": False, "error": "session_token_mismatch"}:
                raise RuntimeError("cross-session token unexpectedly accepted")
            record["cross_session_token_rejected"] = True
            # Warm auth/transport separately, then test real denial against existing markers.
            for backend in ("s3", "juicefs"):
                for tenant in sessions:
                    call(tenant, {"action": "seed", "backend": backend, "run_id": record["run_id"]})
                for tenant in sessions:
                    result = call(tenant, {"action": "isolation", "backend": backend, "run_id": record["run_id"]})
                    record["isolation"].append({"tenant": tenant, "backend": backend, **result})
                for tenant in sessions:
                    call(tenant, {"action": "verify-marker", "backend": backend, "run_id": record["run_id"]})
            save(args.out, record)
            record["local_workspace_baselines"] = [call("tenant-a", {
                "action": "prepare-workspace", "workload": kind}) for kind in ("git-clone", "unzip")]
            workloads = ("git-clone", "unzip")
            worker_counts = (args.workers,)
            phases = [("write", "persist-small-files"), ("restore", "cold-first-pass"), ("restore", "warm-repeat")]
            for repetition in range(args.repetitions):
                for workers in worker_counts:
                    for workload in workloads:
                        cell_id = f"{record['run_id']}-r{repetition}-w{workers}"
                        order = ("s3", "juicefs") if repetition % 2 == 0 else ("juicefs", "s3")
                        request = {"action": "workspace", "run_id": cell_id, "workload": workload, "workers": workers}
                        for phase, label in phases:
                            if label == "cold-first-pass":
                                reset = fresh_gateway_cache(state, uuid.uuid4().hex)
                                record["cache_resets"].append(reset)
                            # Each backend starts a fresh s5cmd process; startup/TLS is included equally.
                            for backend in order:
                                result = call("tenant-a", {**request, "phase": phase, "backend": backend})
                                baseline = next(item for item in record["local_workspace_baselines"] if item["kind"] == workload)
                                if result.get("schema") != "s5cmd-workspace-v1" or result["manifest_sha256"] != baseline["manifest_sha256"]:
                                    raise RuntimeError("wrong engine or changed workspace snapshot")
                                record["measurements"].append({"repetition": repetition, "label": label, **result})
                                save(args.out, record)
                                print(f"s5cmd {backend:7} {workload:11} w={workers} {label:15} batch={result['file_batch_seconds']:.3f}s wall={result['wall_seconds']:.3f}s", flush=True)
            record["success"] = True
    except Exception as exc:
        record["error"] = safe_exception(exc)
        raise
    finally:
        record["session_cleanup"] = {
            tenant: {key: value for key, value in (session.stop_result or {}).items()
                     if key in {"attempted", "success", "status_code", "runtime_session_id"}}
            for tenant, session in sessions.items()}
        if any(not result or not result.get("success") for result in record["session_cleanup"].values()):
            record["success"] = False
        record["finished_at"] = datetime.now(timezone.utc).isoformat()
        save(args.out, record)
        write_report(args.out.with_suffix(".md"), record)
    return 0 if record["success"] else 1


def cleanup(args) -> int:
    if not args.destroy_demo:
        raise ValueError("cleanup requires --destroy-demo; gateway SQLite metadata will be destroyed")
    state = json.loads(args.state.read_text())
    if api("sts", state["region"]).get_caller_identity()["Account"] != state["account"]:
        raise ValueError("AWS account differs from deployment")
    control = api("bedrock-agentcore-control", state["region"])
    runtimes = [state["runtime"]] if state.get("runtime") else list(state.get("runtimes", {}).values())
    for runtime in runtimes:
        try:
            control.delete_agent_runtime(agentRuntimeId=runtime["id"])
        except control.exceptions.ResourceNotFoundException:
            continue
        deadline = time.monotonic() + 900
        while time.monotonic() < deadline:
            try:
                control.get_agent_runtime(agentRuntimeId=runtime["id"])
            except control.exceptions.ResourceNotFoundException:
                break
            time.sleep(5)
        else:
            raise TimeoutError("runtime deletion not finished; refusing to delete its network")
    if state.get("stack_id"):
        cf = api("cloudformation", state["region"])
        inventory = []
        for page in cf.get_paginator("list_stack_resources").paginate(StackName=state["stack_id"]):
            inventory.extend({key: item.get(key) for key in ("LogicalResourceId", "PhysicalResourceId", "ResourceType", "ResourceStatus")}
                             for item in page["StackResourceSummaries"])
        state["resource_inventory_before_cleanup"] = inventory
        save(args.state, state)  # Also preserves IDs from partial/failed creates with no Outputs.
        cf.delete_stack(StackName=state["stack_id"])
        cf.get_waiter("stack_delete_complete").wait(StackName=state["stack_id"], WaiterConfig={"Delay": 10, "MaxAttempts": 120})
    state["infrastructure_deleted"] = True
    save(args.state, state)
    print("Infrastructure removed. S3 bucket, ECR repository and Secrets Manager secrets are RETAINED.")
    print("SQLite metadata on the gateway EBS is destroyed. Export it before cleanup if data must remain usable.")
    print("Retained resources still incur storage charges; inspect state outputs before explicitly deleting data.")
    return 0

def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["plan", "deploy", "update", "run", "cleanup"])
    parser.add_argument("--region", default="us-west-2")
    parser.add_argument("--state", type=Path, default=ROOT / "build/juicefs-state.json")
    parser.add_argument("--out", type=Path, default=ROOT / "results/s5cmd-cloud.json")
    parser.add_argument("--repetitions", type=int, default=3)
    parser.add_argument("--workers", type=int, choices=[8, 32, 64, 128, 256], default=32, help="s5cmd file worker count")
    parser.add_argument("--approve-costs", action="store_true", help="explicit approval for cloud resource creation")
    parser.add_argument("--destroy-demo", action="store_true", help="confirm deletion of runtime, gateway and its SQLite metadata")
    args = parser.parse_args()
    try:
        if args.action == "plan":
            template = build_template()
            print(json.dumps({"region": args.region, "resources": {
                name: resource["Type"] for name, resource in template["Resources"].items()},
                "additional_resources": "1 AgentCore runtime; tenant-a and tenant-b invoke the same ARN with distinct session IDs",
                "data_budget": "s5cmd only: Django clone/unzip, ~6900 files each, workers=32, 3 repetitions; HEAD/GET request counts differ from file count",
                "costs": "m7g.large + 30 GiB gp3 + public IPv4 + 4 interface endpoints + S3/ECR/Secrets + Runtime/logs; no NAT or LLM",
                "cleanup": "Requires --destroy-demo; destroys SQLite metadata, retains S3/ECR/Secrets"}, indent=2))
            return 0
        return {"deploy": deploy, "update": update_runtime, "run": run, "cleanup": cleanup}[args.action](args)
    except Exception as exc:
        # Detailed SSM logs stay at their source; SDK exceptions may expose request details.
        print(f"FAILED: {type(exc).__name__}; inspect state/report and AWS/SSM events. Resources may still incur charges.", file=sys.stderr)
        return 1

if __name__ == "__main__":
    sys.exit(main())
