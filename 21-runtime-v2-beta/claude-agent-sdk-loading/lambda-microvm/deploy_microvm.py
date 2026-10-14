"""Build the probed Claude Agent SDK agent as three Lambda MicroVM images.

Same agent (../app/main.py) and model as the AgentCore V2 test; the variants
differ only in image environment variables:
  lazy            AGENT_START=lazy,  /validate only pings
  lazy-validated  AGENT_START=lazy,  /validate starts the Claude CLI once
  eager           AGENT_START=eager, /ready waits for the CLI (CLI in snapshot)

Steps (idempotent): S3 artifact bucket, build + execution roles, one artifact
zip, create or update the three images, poll the builds, write
microvm_deployment.json (with snapshot sizes from GetMicrovmImageBuild).

    ../../.venv/bin/python deploy_microvm.py
"""

from __future__ import annotations

import hashlib
import io
import json
import time
import zipfile
from pathlib import Path

import boto3
from botocore.exceptions import ClientError

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
DEPLOYMENT = HERE / "microvm_deployment.json"
REGION = "us-west-2"
MODEL_ID = "global.anthropic.claude-haiku-5-5"
BASE_IMAGE_ARN = f"arn:aws:lambda:{REGION}:aws:microvm-image:al2023-1"
BUILD_ROLE = "ClaudeSdkMicrovmBuildRole"
EXEC_ROLE = "ClaudeSdkMicrovmExecRole"
MEMORY_MIB = 4096  # 2 vCPU baseline (2 GB = 1 vCPU), same vCPU count as the AgentCore V2 VMs
VARIANTS = {
    "lazy": {"AGENT_START": "lazy", "VALIDATE_AGENT": "0"},
    "lazy-validated": {"AGENT_START": "lazy", "VALIDATE_AGENT": "1"},
    "eager": {"AGENT_START": "eager", "VALIDATE_AGENT": "0"},
}
HOOKS = {
    "port": 9000,
    "microvmImageHooks": {"ready": "ENABLED", "readyTimeoutInSeconds": 300,
                          "validate": "ENABLED", "validateTimeoutInSeconds": 300},
    "microvmHooks": {"run": "ENABLED", "runTimeoutInSeconds": 5,
                     "resume": "ENABLED", "resumeTimeoutInSeconds": 5,
                     "suspend": "ENABLED", "suspendTimeoutInSeconds": 5,
                     "terminate": "ENABLED", "terminateTimeoutInSeconds": 5},
}


def artifact() -> tuple[bytes, str]:
    """Deterministic zip of the build context; its hash names the S3 key."""
    files = {"Dockerfile": HERE / "Dockerfile", "hooks.py": HERE / "hooks.py",
             "main.py": ROOT / "app" / "main.py", "requirements.txt": ROOT / "requirements.txt"}
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for name, path in sorted(files.items()):
            info = zipfile.ZipInfo(name, date_time=(2026, 1, 1, 0, 0, 0))
            info.external_attr = 0o644 << 16
            z.writestr(info, path.read_bytes())
    data = buf.getvalue()
    return data, hashlib.sha256(data).hexdigest()[:16]


def ensure_bucket(s3, bucket: str) -> None:
    try:
        s3.head_bucket(Bucket=bucket)
    except ClientError:
        s3.create_bucket(Bucket=bucket, CreateBucketConfiguration={"LocationConstraint": REGION})
        s3.put_public_access_block(Bucket=bucket, PublicAccessBlockConfiguration={
            "BlockPublicAcls": True, "IgnorePublicAcls": True,
            "BlockPublicPolicy": True, "RestrictPublicBuckets": True})
        print(f"created bucket {bucket}")


def ensure_role(iam, name: str, account: str, statements: list[dict]) -> str:
    trust = {"Version": "2012-10-17", "Statement": [{
        "Effect": "Allow", "Principal": {"Service": "lambda.amazonaws.com"},
        "Action": ["sts:AssumeRole", "sts:TagSession"],
        "Condition": {"StringEquals": {"aws:SourceAccount": account}}}]}
    try:
        arn = iam.create_role(RoleName=name, AssumeRolePolicyDocument=json.dumps(trust),
                              Description="Claude Agent SDK probe on Lambda MicroVMs")["Role"]["Arn"]
        print(f"created role {name}")
    except iam.exceptions.EntityAlreadyExistsException:
        iam.update_assume_role_policy(RoleName=name, PolicyDocument=json.dumps(trust))
        arn = iam.get_role(RoleName=name)["Role"]["Arn"]
    iam.put_role_policy(RoleName=name, PolicyName="Permissions",
                        PolicyDocument=json.dumps({"Version": "2012-10-17", "Statement": statements}))
    return arn


def wait_build(mvm, arn: str, version: str, timeout: int = 2700) -> dict:
    deadline = time.time() + timeout
    while True:
        v = mvm.get_microvm_image_version(imageIdentifier=arn, imageVersion=version)
        if v["state"] == "SUCCESSFUL":
            return v
        if v["state"] == "FAILED":
            raise SystemExit(f"{arn} v{version} FAILED: {v.get('stateReason')}")
        if time.time() > deadline:
            raise TimeoutError(f"{arn} v{version} still {v['state']}")
        time.sleep(15)


def main() -> None:
    session = boto3.Session(region_name=REGION)
    account = session.client("sts").get_caller_identity()["Account"]
    bucket = f"casdk-microvm-{account}-{REGION}"
    s3, iam, mvm = session.client("s3"), session.client("iam"), session.client("lambda-microvms")

    ensure_bucket(s3, bucket)
    logs = {"Sid": "Logs", "Effect": "Allow",
            "Action": ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents",
                       "logs:DescribeLogStreams", "logs:DescribeLogGroups"],
            "Resource": f"arn:aws:logs:{REGION}:{account}:*"}
    model = MODEL_ID.split(".", 1)[1]
    build_role = ensure_role(iam, BUILD_ROLE, account, [
        {"Sid": "Artifact", "Effect": "Allow", "Action": ["s3:GetObject", "s3:GetObjectVersion"],
         "Resource": f"arn:aws:s3:::{bucket}/*"}, logs])
    exec_role = ensure_role(iam, EXEC_ROLE, account, [logs, {
        "Sid": "BedrockInvoke", "Effect": "Allow",
        "Action": ["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"],
        "Resource": [f"arn:aws:bedrock:*:{account}:inference-profile/{MODEL_ID}",
                     f"arn:aws:bedrock:*::foundation-model/{model}"]}])

    data, sha = artifact()
    key = f"claude-sdk-v2-probe/{sha}.zip"
    s3.put_object(Bucket=bucket, Key=key, Body=data)
    uri = f"s3://{bucket}/{key}"
    print(f"artifact {uri} ({len(data)} bytes)")

    images = {}
    for variant, env in VARIANTS.items():
        name = f"casdk-mvm-{variant}"
        arn = f"arn:aws:lambda:{REGION}:{account}:microvm-image:{name}"
        common = dict(baseImageArn=BASE_IMAGE_ARN, buildRoleArn=build_role, codeArtifact={"uri": uri},
                      description=f"Claude Agent SDK probe, {variant}, artifact {sha}")
        spec = dict(resources=[{"minimumMemoryInMiB": MEMORY_MIB}], cpuConfigurations=[{"architecture": "ARM_64"}],
                    hooks=HOOKS, environmentVariables={**env, "MODEL_ID": MODEL_ID})  # AWS_* keys are reserved
        for attempt in range(6):
            try:
                try:
                    mvm.get_microvm_image(imageIdentifier=arn)
                    version = mvm.update_microvm_image(imageIdentifier=arn, **common, **spec)["imageVersion"]
                    action = "updated"
                except mvm.exceptions.ResourceNotFoundException:
                    version = mvm.create_microvm_image(name=name, **common, **spec)["imageVersion"]
                    action = "created"
                break
            except ClientError as e:  # new roles need a few seconds before Lambda can assume them
                if "role" not in str(e).lower() or attempt == 5:
                    raise
                time.sleep(10)
        print(f"{action} {name} v{version}")
        images[variant] = {"name": name, "arn": arn, "version": version, "env": env, "submitted": time.time()}

    for variant, img in images.items():
        v = wait_build(mvm, img["arn"], img["version"])
        builds = mvm.list_microvm_image_builds(imageIdentifier=img["arn"], imageVersion=img["version"])["items"]
        build = mvm.get_microvm_image_build(imageIdentifier=img["arn"], imageVersion=img["version"],
                                            buildId=builds[0]["buildId"])
        img.update(build_s=round(time.time() - img.pop("submitted"), 1),
                   created_at=str(v["createdAt"]), updated_at=str(v["updatedAt"]),
                   snapshot=build.get("snapshotBuild"), chipset=build.get("chipsetGeneration"))
        print(f"{img['name']} v{img['version']} SUCCESSFUL in ~{img['build_s']}s, snapshot {img['snapshot']}")

    DEPLOYMENT.write_text(json.dumps({
        "region": REGION, "account": account, "bucket": bucket, "artifact_uri": uri,
        "base_image_arn": BASE_IMAGE_ARN, "memory_mib": MEMORY_MIB, "model_id": MODEL_ID,
        "build_role": {"name": BUILD_ROLE, "arn": build_role},
        "execution_role": {"name": EXEC_ROLE, "arn": exec_role},
        "images": images, "boto3": boto3.__version__,
    }, indent=2, default=str) + "\n")
    print(f"wrote {DEPLOYMENT.name}")


if __name__ == "__main__":
    main()
