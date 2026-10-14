"""Build the probed Claude Agent SDK image and deploy it as AgentCore Runtime V2.

Steps (each idempotent; re-running updates in place):
  1. ECR repository + docker build (linux/arm64) + push, pinned by digest
  2. IAM execution role: ECR pull, runtime logs, Bedrock invoke on MODEL_ID
  3. One runtime per AGENT_START mode (lazy, eager), platformVersion=V2,
     submitted together, then wait for each runtime and DEFAULT endpoint READY
  4. deployment.json for bench.py / cleanup.py

Run with the project interpreter (public boto3 >= 1.43.95 has platformVersion):
    ../.venv/bin/python deploy.py
    ../.venv/bin/python deploy.py --skip-build      # reuse the last pushed digest
"""

from __future__ import annotations

import argparse
import base64
import json
import subprocess
import time
from pathlib import Path

import boto3
from botocore.exceptions import ClientError

HERE = Path(__file__).resolve().parent
DEPLOYMENT = HERE / "deployment.json"
REPO = "claude-sdk-v2-probe"
ROLE = "ClaudeSdkV2ProbeRole"
MODEL_ID = "global.anthropic.claude-haiku-5-5"
MODES = ("lazy", "eager")


def run(*cmd: str, stdin: str | None = None) -> str:
    return subprocess.run(cmd, input=stdin, text=True, check=True, capture_output=True).stdout


def build_and_push(session: boto3.Session, account: str, region: str) -> dict:
    ecr = session.client("ecr")
    try:
        ecr.create_repository(repositoryName=REPO, imageScanningConfiguration={"scanOnPush": False})
        print(f"created ECR repository {REPO}")
    except ecr.exceptions.RepositoryAlreadyExistsException:
        pass
    host = f"{account}.dkr.ecr.{region}.amazonaws.com"
    tag = time.strftime("%Y%m%dT%H%M%S", time.gmtime())
    uri = f"{host}/{REPO}:{tag}"
    token = ecr.get_authorization_token()["authorizationData"][0]["authorizationToken"]
    password = base64.b64decode(token).decode().split(":", 1)[1]  # "AWS:<password>"
    run("docker", "login", "--username", "AWS", "--password-stdin", host, stdin=password)
    print(f"building {uri}")
    subprocess.run(["docker", "build", "--platform", "linux/arm64", "-t", uri, str(HERE)], check=True)
    arch = run("docker", "image", "inspect", "--format", "{{.Architecture}}", uri).strip()
    if arch != "arm64":
        raise SystemExit(f"image architecture is {arch}; AgentCore Runtime needs arm64")
    subprocess.run(["docker", "push", uri], check=True)
    image = ecr.describe_images(repositoryName=REPO, imageIds=[{"imageTag": tag}])["imageDetails"][0]
    return {"uri": f"{host}/{REPO}@{image['imageDigest']}", "tag": tag,
            "digest": image["imageDigest"], "compressed_bytes": image["imageSizeInBytes"]}


def ensure_role(session: boto3.Session, account: str, region: str) -> str:
    iam = session.client("iam")
    trust = {"Version": "2012-10-17", "Statement": [{
        "Effect": "Allow",
        "Principal": {"Service": "bedrock-agentcore.amazonaws.com"},
        "Action": "sts:AssumeRole",
        "Condition": {"StringEquals": {"aws:SourceAccount": account},
                      "ArnLike": {"aws:SourceArn": f"arn:aws:bedrock-agentcore:{region}:{account}:*"}},
    }]}
    model = MODEL_ID.split(".", 1)[1]  # global.anthropic.x -> anthropic.x
    policy = {"Version": "2012-10-17", "Statement": [
        {"Sid": "EcrPull", "Effect": "Allow",
         "Action": ["ecr:BatchGetImage", "ecr:GetDownloadUrlForLayer"],
         "Resource": f"arn:aws:ecr:{region}:{account}:repository/{REPO}"},
        {"Sid": "EcrToken", "Effect": "Allow", "Action": "ecr:GetAuthorizationToken", "Resource": "*"},
        {"Sid": "Logs", "Effect": "Allow",
         "Action": ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents", "logs:DescribeLogStreams"],
         "Resource": f"arn:aws:logs:{region}:{account}:log-group:/aws/bedrock-agentcore/runtimes/*"},
        {"Sid": "LogGroups", "Effect": "Allow", "Action": "logs:DescribeLogGroups",
         "Resource": f"arn:aws:logs:{region}:{account}:log-group:*"},
        # A global inference profile needs both the profile and the foundation model.
        {"Sid": "BedrockInvoke", "Effect": "Allow",
         "Action": ["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"],
         "Resource": [f"arn:aws:bedrock:*:{account}:inference-profile/{MODEL_ID}",
                      f"arn:aws:bedrock:*::foundation-model/{model}"]},
    ]}
    try:
        arn = iam.create_role(RoleName=ROLE, AssumeRolePolicyDocument=json.dumps(trust),
                              Description="Claude Agent SDK probe on AgentCore Runtime V2")["Role"]["Arn"]
        print(f"created role {ROLE}")
    except iam.exceptions.EntityAlreadyExistsException:
        iam.update_assume_role_policy(RoleName=ROLE, PolicyDocument=json.dumps(trust))
        arn = iam.get_role(RoleName=ROLE)["Role"]["Arn"]
    iam.put_role_policy(RoleName=ROLE, PolicyName="RuntimeExecution", PolicyDocument=json.dumps(policy))
    return arn


def wait_ready(ctl, runtime_id: str, timeout: int = 900) -> dict:
    deadline = time.time() + timeout
    while True:
        rt = ctl.get_agent_runtime(agentRuntimeId=runtime_id)
        ep = ctl.get_agent_runtime_endpoint(agentRuntimeId=runtime_id, endpointName="DEFAULT")
        if rt["status"] == "READY" and ep["status"] == "READY":
            return rt
        for name, obj in (("runtime", rt), ("endpoint", ep)):
            if obj["status"].endswith("FAILED"):
                raise RuntimeError(f"{name} {obj['status']}: {obj.get('failureReason')}")
        if time.time() > deadline:
            raise TimeoutError(f"runtime {rt['status']} / endpoint {ep['status']} after {timeout}s")
        time.sleep(5)


def submit_runtime(ctl, mode: str, image_uri: str, role_arn: str, region: str, platform: str) -> dict:
    name = f"casdk_{platform.lower()}_{mode}"
    spec = {
        "agentRuntimeArtifact": {"containerConfiguration": {"containerUri": image_uri}},
        "roleArn": role_arn,
        "networkConfiguration": {"networkMode": "PUBLIC"},
        "protocolConfiguration": {"serverProtocol": "HTTP"},
        "lifecycleConfiguration": {"idleRuntimeSessionTimeout": 120, "maxLifetime": 900},
        "environmentVariables": {"AGENT_START": mode, "MODEL_ID": MODEL_ID,
                                 "AWS_REGION": region, "AWS_DEFAULT_REGION": region},
        "platformVersion": platform,
        "description": f"Claude Agent SDK latency probe, AGENT_START={mode}",
    }
    existing = {r["agentRuntimeName"]: r["agentRuntimeId"]
                for page in ctl.get_paginator("list_agent_runtimes").paginate()
                for r in page["agentRuntimes"]}
    for attempt in range(10):
        try:
            if name in existing:
                runtime_id = ctl.update_agent_runtime(agentRuntimeId=existing[name], **spec)["agentRuntimeId"]
                action = "updated"
            else:
                runtime_id = ctl.create_agent_runtime(agentRuntimeName=name, **spec)["agentRuntimeId"]
                action = "created"
            break
        except ClientError as e:  # fresh IAM roles take a few seconds to become assumable
            if "Role validation failed" not in e.response["Error"]["Message"] or attempt == 9:
                raise
            time.sleep(10)
    print(f"{action} {name} ({runtime_id})")
    return {"name": name, "id": runtime_id, "action": action, "submitted": time.time()}


def finish_runtime(ctl, submitted: dict, platform: str) -> dict:
    rt = wait_ready(ctl, submitted["id"])
    ready_s = round(time.time() - submitted.pop("submitted"), 1)
    if rt.get("platformVersion") != platform:
        raise SystemExit(f"{submitted['name']}: GetAgentRuntime.platformVersion="
                         f"{rt.get('platformVersion')!r}, want {platform}")
    print(f"{submitted['name']} READY in {ready_s}s, platformVersion={rt['platformVersion']}, "
          f"version={rt['agentRuntimeVersion']}")
    return {**submitted, "arn": rt["agentRuntimeArn"], "platformVersion": rt["platformVersion"],
            "version": rt["agentRuntimeVersion"], "ready_s": ready_s,
            "ready_iso": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--region", default="us-west-2")
    ap.add_argument("--platform-version", default="V2")
    ap.add_argument("--modes", default=",".join(MODES))
    ap.add_argument("--skip-build", action="store_true")
    args = ap.parse_args()

    session = boto3.Session(region_name=args.region)
    account = session.client("sts").get_caller_identity()["Account"]
    previous = json.loads(DEPLOYMENT.read_text()) if DEPLOYMENT.exists() else {}
    if args.skip_build:
        image = previous["image"]
    else:
        image = build_and_push(session, account, args.region)
    role_arn = ensure_role(session, account, args.region)
    ctl = session.client("bedrock-agentcore-control")
    runtimes = dict(previous.get("runtimes", {}))
    submitted = {mode: submit_runtime(ctl, mode, image["uri"], role_arn, args.region, args.platform_version)
                 for mode in args.modes.split(",")}
    for mode, sub in submitted.items():
        runtimes[mode] = finish_runtime(ctl, sub, args.platform_version)
    DEPLOYMENT.write_text(json.dumps({
        "region": args.region, "account": account, "model_id": MODEL_ID, "repository": REPO,
        "role_name": ROLE, "role_arn": role_arn, "image": image, "runtimes": runtimes,
        "boto3": boto3.__version__,
    }, indent=2) + "\n")
    print(f"wrote {DEPLOYMENT.relative_to(HERE)}")


if __name__ == "__main__":
    main()
