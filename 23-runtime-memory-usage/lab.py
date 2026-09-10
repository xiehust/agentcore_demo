"""Bounded AWS memory experiment. Uses the existing local boto3 and Docker."""
import argparse
import base64
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import subprocess
import time
import uuid

import boto3
from botocore.config import Config

ROOT = Path(__file__).resolve().parent
STATE = ROOT / ".state"

def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, default=str) + "\n")


def load_state():
    path = STATE / "resources.json"
    return json.loads(path.read_text()) if path.exists() else {}


def save(state):
    STATE.mkdir(mode=0o700, exist_ok=True)
    write_json(STATE / "resources.json", state)


def client(state, service):
    return boto3.client(service, region_name=state["region"],
                        config=Config(connect_timeout=15, read_timeout=300,
                                      retries={"total_max_attempts": 1}))


def policy(statements):
    return json.dumps({"Version": "2012-10-17", "Statement": statements})


def allow(actions, resource="*"):
    return {"Effect": "Allow", "Action": actions, "Resource": resource}


def deploy(args):
    state = load_state()
    if not state:
        state = {"region": args.region, "name": "memprobe23-" + uuid.uuid4().hex[:8],
                 "runtimes": {}, "sessions": []}
        state["account"] = client(state, "sts").get_caller_identity()["Account"]
        save(state)
    if state["region"] != args.region:
        raise ValueError("Existing state belongs to a different region")
    iam, ecr, control = (client(state, s) for s in
                         ["iam", "ecr", "bedrock-agentcore-control"])
    name, region, account = state["name"], state["region"], state["account"]
    if "repository" not in state:
        state["repository"] = ecr.create_repository(repositoryName=name,
            imageTagMutability="IMMUTABLE", tags=[{"Key": "Project", "Value": "memory-probe-23"}])["repository"]
        save(state)
    if "role" not in state:
        state["role"] = iam.create_role(RoleName=name,
            AssumeRolePolicyDocument=policy([{"Effect": "Allow", "Principal": {
                "Service": "bedrock-agentcore.amazonaws.com"}, "Action": "sts:AssumeRole",
                "Condition": {"StringEquals": {"aws:SourceAccount": account}, "ArnLike": {
                    "aws:SourceArn": f"arn:aws:bedrock-agentcore:{region}:{account}:*"}}}]))["Role"]
        save(state)
    iam.put_role_policy(RoleName=name, PolicyName="MemoryProbe", PolicyDocument=policy([
        allow(["ecr:GetAuthorizationToken"]),
        allow(["ecr:BatchGetImage", "ecr:GetDownloadUrlForLayer"], state["repository"]["repositoryArn"]),
        allow(["logs:DescribeLogGroups"]),
        allow(["logs:CreateLogGroup", "logs:DescribeLogStreams", "logs:CreateLogStream", "logs:PutLogEvents"],
              f"arn:aws:logs:{region}:{account}:log-group:/aws/bedrock-agentcore/runtimes/*")]))
    for variant, pad in [("small", 0), ("padded", 256)]:
        if variant in state["runtimes"]:
            continue
        digest = hashlib.sha256((ROOT / "probe.py").read_bytes() +
            (ROOT / "Dockerfile").read_bytes() + str(pad).encode()).hexdigest()[:12]
        image = state["repository"]["repositoryUri"] + ":" + digest
        subprocess.run(["docker", "build", "--platform", "linux/arm64", "--build-arg",
                        f"PAD_MIB={pad}", "-t", image, str(ROOT)], check=True)
        auth = ecr.get_authorization_token()["authorizationData"][0]
        user, password = base64.b64decode(auth["authorizationToken"]).decode().split(":", 1)
        config = STATE / "docker"
        config.mkdir(mode=0o700, exist_ok=True)
        subprocess.run(["docker", "--config", str(config), "login", "--username", user,
                        "--password-stdin", auth["proxyEndpoint"]], input=password, text=True, check=True)
        try:
            subprocess.run(["docker", "--config", str(config), "push", image], check=True)
        finally:
            subprocess.run(["docker", "--config", str(config), "logout", auth["proxyEndpoint"]], check=True)
        # Bounded pause for IAM propagation; deploy is run as a background shell task.
        time.sleep(10)
        runtime = control.create_agent_runtime(agentRuntimeName=(name + "_" + variant).replace("-", "_"),
            agentRuntimeArtifact={"containerConfiguration": {"containerUri": image}},
            roleArn=state["role"]["Arn"], networkConfiguration={"networkMode": "PUBLIC"},
            protocolConfiguration={"serverProtocol": "HTTP"},
            lifecycleConfiguration={"idleRuntimeSessionTimeout": 60, "maxLifetime": 600},
            tags={"Project": "memory-probe-23"})
        state["runtimes"][variant] = {"id": runtime["agentRuntimeId"],
            "arn": runtime["agentRuntimeArn"], "image": image, "pad_mib": pad}
        save(state)
    for variant, runtime in state["runtimes"].items():
        for _ in range(90):
            response = control.get_agent_runtime(agentRuntimeId=runtime["id"])
            print(variant, response["status"], flush=True)
            if response["status"] == "READY":
                runtime["configuration"] = response
                break
            if response["status"] not in {"CREATING", "UPDATING"}:
                raise RuntimeError(json.dumps(response, default=str))
            time.sleep(10)
        else:
            raise TimeoutError("Runtime not ready after 15 minutes")
        configure_logging(state, variant, runtime)
        save(state)
    print("Deployment ready:", state["name"], flush=True)

def configure_logging(state, variant, runtime):
    logs = client(state, "logs")
    # Runtime creates its own stdout log group separately from the vended usage group.
    for page in logs.get_paginator("describe_log_groups").paginate(
            logGroupNamePrefix="/aws/bedrock-agentcore/runtimes/" + runtime["id"] + "-"):
        for group in page["logGroups"]:
            logs.put_retention_policy(logGroupName=group["logGroupName"], retentionInDays=7)
    if "delivery_id" in runtime:
        return
    prefix = state["name"] + "-" + variant
    group = "/aws/vendedlogs/bedrock-agentcore/" + prefix + "/usage"
    try:
        logs.create_log_group(logGroupName=group)
    except logs.exceptions.ResourceAlreadyExistsException:
        pass
    runtime["usage_log_group"] = group
    save(state)
    logs.put_retention_policy(logGroupName=group, retentionInDays=7)
    source_name, destination_name = prefix + "-source", prefix + "-destination"
    logs.put_delivery_source(name=source_name, resourceArn=runtime["arn"], logType="USAGE_LOGS")
    runtime["source_name"] = source_name
    save(state)
    destination = logs.put_delivery_destination(name=destination_name, outputFormat="json",
        deliveryDestinationType="CWL", deliveryDestinationConfiguration={
            "destinationResourceArn": f"arn:aws:logs:{state['region']}:{state['account']}:log-group:{group}"})
    runtime["destination_name"] = destination_name
    save(state)
    delivery = logs.create_delivery(deliverySourceName=source_name,
        deliveryDestinationArn=destination["deliveryDestination"]["arn"])
    runtime["delivery_id"] = delivery["delivery"]["id"]
    save(state)


def run(args):
    state = load_state()
    data = client(state, "bedrock-agentcore")
    cases = [("small", "baseline"), ("small", "anonymous"), ("small", "file_cache"),
             ("padded", "baseline"), ("padded", "image_read")]
    for variant, kind in cases:
        session = {"id": str(uuid.uuid4()), "variant": variant, "kind": kind,
                   "start": time.time()}
        state["sessions"].append(session)
        save(state)
        runtime = state["runtimes"][variant]
        print("Invoking", variant, kind, session["id"], flush=True)
        payload = {"kind": kind, "mib": 256, "phase_seconds": args.phase_seconds,
                   "run_id": session["id"]}
        try:
            response = data.invoke_agent_runtime(agentRuntimeArn=runtime["arn"], qualifier="DEFAULT",
                runtimeSessionId=session["id"], contentType="application/json", accept="application/json",
                payload=json.dumps(payload).encode())
            body = json.loads(response["response"].read())
            if "samples" not in body:
                raise RuntimeError(f"Probe did not return samples: {body}")
            path = STATE / (session["id"] + ".json")
            write_json(path, body)
            session["result_file"] = path.name
            session["request_id"] = response["ResponseMetadata"]["RequestId"]
            print("Received", len(body["samples"]), "samples", flush=True)
        except Exception as exc:
            session["error"] = str(exc)
            raise
        finally:
            session["end"] = time.time()
            save(state)
            try:
                stopped = data.stop_runtime_session(agentRuntimeArn=runtime["arn"],
                    runtimeSessionId=session["id"], qualifier="DEFAULT")
                session["stop_response"] = stopped
                session["stop_requested_at"] = time.time()
            except Exception as exc:
                session["stop_error"] = str(exc)
                print("Stop failed; 60s idle / 600s lifetime guard remains:", exc, flush=True)
            save(state)


def collect(args):
    state = load_state()
    logs, cw = client(state, "logs"), client(state, "cloudwatch")
    if not state["sessions"]:
        raise ValueError("No sessions have been invoked yet")
    start = min(s["start"] for s in state["sessions"]) - 120
    end = min(time.time(), max(s.get("end", s["start"] + 600) for s in state["sessions"]) + 660)
    # CloudWatch GetMetricStatistics permits at most 1,440 data points.
    period = max(60, ((int(end - start) // 1440 + 59) // 60) * 60)
    evidence = {"collected_at": time.time(), "start": start, "end": end, "runtimes": {}}
    for variant, runtime in state["runtimes"].items():
        events = []
        for page in logs.get_paginator("filter_log_events").paginate(
                logGroupName=runtime["usage_log_group"], startTime=int(start * 1000), endTime=int(end * 1000)):
            events.extend(page["events"])
        metrics = []
        for page in cw.get_paginator("list_metrics").paginate(MetricName="MemoryUsed-GBHours",
                Dimensions=[{"Name": "Service", "Value": "AgentCore.Runtime"},
                            {"Name": "Resource", "Value": runtime["arn"]}]):
            for metric in page["Metrics"]:
                stats = cw.get_metric_statistics(Namespace=metric["Namespace"],
                    MetricName=metric["MetricName"], Dimensions=metric["Dimensions"],
                    StartTime=datetime.fromtimestamp(start, timezone.utc),
                    EndTime=datetime.fromtimestamp(end, timezone.utc), Period=period, Statistics=["Sum"])
                metrics.append({"metric": metric, "statistics": stats})
        evidence["runtimes"][variant] = {"events": events, "metrics": metrics}
        print(variant, len(events), "usage events;", len(metrics), "metric series", flush=True)
    write_json(STATE / "telemetry.json", evidence)
    print("Saved .state/telemetry.json; no data yet is NOT zero usage.", flush=True)
    return evidence


def collect_wait(args):
    deadline = time.monotonic() + args.wait_seconds
    while True:
        evidence = collect(args)
        state = load_state()
        from analyze import summarize
        report = summarize(state, evidence)
        phases = [p for s in report["sessions"] for p in s["phases"]]
        if (phases and len(report["sessions"]) == len(state["sessions"])
                and not report["parse_errors"]
                and all(p["aws"]["coverage_fraction"] >= 0.99 for p in phases)):
            print("All phase interiors have >=99% telemetry coverage; session tails may still arrive later.", flush=True)
            return
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            print("Bounded wait ended; telemetry remains incomplete.", flush=True)
            return
        time.sleep(min(60, remaining))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    deploy_parser = commands.add_parser("deploy", help="Creates two microVM runtimes, IAM role, ECR and logs")
    deploy_parser.add_argument("--region", default="us-west-2")
    run_parser = commands.add_parser("run", help="Runs 5 sequential, fresh-session experiments")
    run_parser.add_argument("--phase-seconds", type=int, default=30, choices=range(10, 46))
    collect_parser = commands.add_parser("collect", help="Read-only usage-log and CloudWatch collection; safe to rerun")
    collect_parser.add_argument("--wait-seconds", type=int, default=0, choices=range(0, 3901))
    args = parser.parse_args()
    {"deploy": deploy, "run": run, "collect": collect_wait}[args.command](args)


if __name__ == "__main__":
    main()

