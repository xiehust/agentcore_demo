#!/usr/bin/env python3
"""Manage the two same-region EC2 clients and short-lived timeout test resources."""
import argparse
import concurrent.futures
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import tarfile
import time
import uuid

import boto3
from botocore.config import Config

ROOT = Path(__file__).resolve().parent
CHINA = ROOT.parent.parent
OUT = ROOT / "results/20260922"
CLIENTS = {
    "cn-northwest-1": (CHINA / "ec2_benchmark/results/20260922/resources.json",
                      "/opt/cn-agentcore-benchmark/venv/bin/python"),
    "cn-north-1": (CHINA / "cn-north-1/results/20260922/ec2/resources.json",
                   "/opt/beijing-agentcore/venv/bin/python"),
}


def now():
    return datetime.now(timezone.utc).isoformat()


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str) + "\n")
    tmp.replace(path)


class Lab:
    def __init__(self, region):
        self.region = region
        self.out = OUT / region
        self.path = self.out / "resources.json"
        self.aws = boto3.Session(profile_name="agentcore_cn", region_name=region)
        self.cfg = Config(connect_timeout=10, read_timeout=120, retries={"total_max_attempts": 1})
        identity = self.client("sts").get_caller_identity()
        assert identity["Account"] == "447150580482"
        self.state = json.loads(self.path.read_text()) if self.path.exists() else {
            "name": "ci_timeout_" + uuid.uuid4().hex[:10], "region": region, "created_at": now()}
        self.persist()

    def client(self, service):
        return self.aws.client(service, config=self.cfg)

    def persist(self):
        save(self.path, self.state)

    def setup(self):
        prior_path, python = CLIENTS[self.region]
        prior = json.loads(prior_path.read_text())
        self.state.update(instance_id=prior["instance"]["InstanceId"], role_name=prior["role"]["RoleName"],
                          python=python)
        self.persist()
        if "bucket" not in self.state:
            bucket = self.state["name"].replace("_", "-") + "-447150580482"
            self.state["bucket_intent"] = bucket
            self.persist()
            self.client("s3").create_bucket(Bucket=bucket,
                CreateBucketConfiguration={"LocationConstraint": self.region})
            self.state["bucket"] = bucket
            self.persist()
            self.client("s3").put_public_access_block(Bucket=bucket, PublicAccessBlockConfiguration={
                "BlockPublicAcls": True, "IgnorePublicAcls": True,
                "BlockPublicPolicy": True, "RestrictPublicBuckets": True})
            self.client("s3").put_bucket_encryption(Bucket=bucket, ServerSideEncryptionConfiguration={
                "Rules": [{"ApplyServerSideEncryptionByDefault": {"SSEAlgorithm": "AES256"}}]})
        control = self.client("bedrock-agentcore-control")
        if "interpreter" not in self.state:
            self.state["interpreter"] = control.create_code_interpreter(
                name=self.state["name"], networkConfiguration={"networkMode": "PUBLIC"})
            self.persist()
        cid = self.state["interpreter"]["codeInterpreterId"]
        custom_arns = [self.state["interpreter"]["codeInterpreterArn"],
                       f"arn:aws-cn:bedrock-agentcore:{self.region}:447150580482:code-interpreter/{cid}"]
        system_arns = [f"arn:aws-cn:bedrock-agentcore:{self.region}:{account}:code-interpreter/aws.codeinterpreter.v1"
                       for account in ("aws", "447150580482")]
        policy = {"Version": "2012-10-17", "Statement": [
            {"Effect": "Allow", "Action": ["bedrock-agentcore:StartCodeInterpreterSession",
                "bedrock-agentcore:InvokeCodeInterpreter", "bedrock-agentcore:GetCodeInterpreterSession",
                "bedrock-agentcore:StopCodeInterpreterSession", "bedrock-agentcore:ListCodeInterpreterSessions"],
             "Resource": custom_arns + system_arns},
            {"Effect": "Allow", "Action": ["s3:GetObject", "s3:PutObject"],
             "Resource": f"arn:aws-cn:s3:::{self.state['bucket']}/*"},
            {"Effect": "Allow", "Action": ["s3:ListBucket"],
             "Resource": f"arn:aws-cn:s3:::{self.state['bucket']}"},
        ]}
        self.client("iam").put_role_policy(RoleName=self.state["role_name"],
            PolicyName="CodeInterpreterTimeoutRetest", PolicyDocument=json.dumps(policy))
        save(self.out / "instance-policy.json", policy)
        instance = self.client("ec2").describe_instances(InstanceIds=[self.state["instance_id"]])["Reservations"][0]["Instances"][0]
        if instance["State"]["Name"] == "stopped":
            save(self.out / "start-instance.json", self.client("ec2").start_instances(InstanceIds=[self.state["instance_id"]]))
        deadline = time.monotonic() + 300
        while time.monotonic() < deadline:
            info = self.client("ssm").describe_instance_information(
                Filters=[{"Key": "InstanceIds", "Values": [self.state["instance_id"]]}])
            if any(r["PingStatus"] == "Online" for r in info["InstanceInformationList"]):
                save(self.out / "ssm-online.json", info)
                break
            print(self.region, "waiting for SSM", flush=True)
            time.sleep(5)
        else:
            raise RuntimeError("SSM did not become online")
        self.state["ready_interpreter"] = control.get_code_interpreter(codeInterpreterId=cid)
        assert self.state["ready_interpreter"]["status"] == "READY"
        self.persist()
        print(self.region, "READY", self.state["instance_id"], flush=True)

    def run(self):
        if self.state.get("command_id"):
            raise RuntimeError("An execution has already been submitted")
        config = {"region": self.region, "instance_id": self.state["instance_id"],
                  "account": "447150580482", "bucket": self.state["bucket"],
                  "interpreter": self.state["interpreter"]["codeInterpreterId"],
                  "ttl_seconds": 60, "work_seconds": 180, "observe_seconds": 210}
        save(self.out / "config.json", config)
        files = {"config.json": self.out / "config.json", "verify_timeout.py": ROOT / "verify_timeout.py"}
        archive = self.out / "input.tar.gz"
        with tarfile.open(archive, "w:gz") as out:
            for name, path in files.items():
                out.add(path, arcname=name)
        save(self.out / "source_hashes.json", {name: hashlib.sha256(path.read_bytes()).hexdigest()
                                              for name, path in files.items()})
        self.client("s3").upload_file(str(archive), self.state["bucket"], "input/input.tar.gz")
        directory = "/opt/" + self.state["name"]
        commands = ["set -eu", f"mkdir -p {directory}", f"cd {directory}",
                    f"aws s3 cp s3://{self.state['bucket']}/input/input.tar.gz input.tar.gz --region {self.region}",
                    "tar -xzf input.tar.gz",
                    f"export AWS_REGION={self.region} AWS_DEFAULT_REGION={self.region}",
                    f"{self.state['python']} -u verify_timeout.py > run.log 2>&1"]
        result = self.client("ssm").send_command(InstanceIds=[self.state["instance_id"]],
            DocumentName="AWS-RunShellScript", Parameters={"commands": commands, "executionTimeout": ["900"]},
            Comment="Code Interpreter 2.6 timeout retest", TimeoutSeconds=60)
        self.state["command_id"] = result["Command"]["CommandId"]
        self.state["remote_directory"] = directory
        self.persist()
        save(self.out / "ssm-request.json", {"commands": commands, "response": result})
        print(self.region, self.state["command_id"], flush=True)

    def status(self):
        result = self.client("ssm").get_command_invocation(
            CommandId=self.state["command_id"], InstanceId=self.state["instance_id"])
        save(self.out / "ssm-status.json", result)
        print(self.region, result["Status"], result.get("StandardErrorContent", "")[-1200:], flush=True)
        for key in ("results/status.json", "results/final_status.json"):
            try:
                response = self.client("s3").get_object(Bucket=self.state["bucket"], Key=key)
                value = json.loads(response["Body"].read())
                response["Body"].close()
                save(self.out / Path(key).name, value)
                print(self.region, json.dumps(value, ensure_ascii=False), flush=True)
            except self.client("s3").exceptions.NoSuchKey:
                pass
        if self.state.get("command_retry_id"):
            result = self.client("ssm").get_command_invocation(
                CommandId=self.state["command_retry_id"], InstanceId=self.state["instance_id"])
            save(self.out / "command-retry/ssm-status.json", result)
            print(self.region, "command-retry", result["Status"], result.get("StandardErrorContent", "")[-1200:], flush=True)
            try:
                response = self.client("s3").get_object(Bucket=self.state["bucket"],
                                                       Key="results/command-retry/final_status.json")
                value = json.loads(response["Body"].read())
                response["Body"].close()
                save(self.out / "command-retry/final_status.json", value)
                print(self.region, "command-retry", json.dumps(value), flush=True)
            except self.client("s3").exceptions.NoSuchKey:
                pass

    def retry_commands(self):
        if self.state.get("command_retry_id"):
            raise RuntimeError("Command retry already submitted")
        folder = self.out / "command-retry"
        cfg = json.loads((self.out / "config.json").read_text())
        cfg["result_prefix"] = "results/command-retry"
        save(folder / "config.json", cfg)
        archive = folder / "input.tar.gz"
        with tarfile.open(archive, "w:gz") as out:
            out.add(ROOT / "verify_timeout.py", arcname="verify_timeout.py")
            out.add(folder / "config.json", arcname="config.json")
        save(folder / "source_hashes.json", {"verify_timeout.py": hashlib.sha256((ROOT / "verify_timeout.py").read_bytes()).hexdigest()})
        self.client("s3").upload_file(str(archive), self.state["bucket"], "input/command-retry.tar.gz")
        directory = self.state["remote_directory"] + "/command-retry"
        commands = ["set -eu", f"mkdir -p {directory}", f"cd {directory}",
                    f"aws s3 cp s3://{self.state['bucket']}/input/command-retry.tar.gz input.tar.gz --region {self.region}",
                    "tar -xzf input.tar.gz",
                    f"export AWS_REGION={self.region} AWS_DEFAULT_REGION={self.region}",
                    f"{self.state['python']} -u verify_timeout.py --command-only > run.log 2>&1"]
        response = self.client("ssm").send_command(InstanceIds=[self.state["instance_id"]],
            DocumentName="AWS-RunShellScript", Parameters={"commands": commands, "executionTimeout": ["300"]},
            Comment="Code Interpreter timeout command control correction")
        self.state["command_retry_id"] = response["Command"]["CommandId"]
        self.persist()
        save(folder / "ssm-request.json", {"commands": commands, "response": response})
        print(self.region, "command retry", self.state["command_retry_id"], flush=True)

    def collect(self):
        archive = self.out / "results.tar.gz"
        self.client("s3").download_file(self.state["bucket"], "results/results.tar.gz", str(archive))
        with tarfile.open(archive, "r:gz") as package:
            package.extractall(self.out, filter="data")
        self.client("s3").download_file(self.state["bucket"], "results/final_status.json",
                                       str(self.out / "final_status.json"))
        self.state["collected_at"] = now()
        if self.state.get("command_retry_id"):
            folder = self.out / "command-retry"
            archive = folder / "results.tar.gz"
            self.client("s3").download_file(self.state["bucket"], "results/command-retry/results.tar.gz", str(archive))
            with tarfile.open(archive, "r:gz") as package:
                package.extractall(folder, filter="data")
            self.state["command_retry_collected_at"] = now()
        self.persist()
        print(self.region, "results collected", flush=True)

    def cleanup(self):
        if not self.state.get("collected_at"):
            raise RuntimeError("Collect evidence before cleanup")
        if self.state.get("command_retry_id") and not self.state.get("command_retry_collected_at"):
            raise RuntimeError("Collect command retry evidence before cleanup")
        result = {}
        result["delete_interpreter"] = self.client("bedrock-agentcore-control").delete_code_interpreter(
            codeInterpreterId=self.state["interpreter"]["codeInterpreterId"])
        result["stop_ec2"] = self.client("ec2").stop_instances(InstanceIds=[self.state["instance_id"]])
        deadline = time.monotonic() + 300
        while time.monotonic() < deadline:
            instance = self.client("ec2").describe_instances(InstanceIds=[self.state["instance_id"]])["Reservations"][0]["Instances"][0]
            if instance["State"]["Name"] == "stopped":
                result["instance"] = {"id": instance["InstanceId"], "state": "stopped"}
                break
            time.sleep(5)
        else:
            raise RuntimeError("EC2 not yet stopped")
        self.client("iam").delete_role_policy(RoleName=self.state["role_name"],
                                              PolicyName="CodeInterpreterTimeoutRetest")
        s3 = self.client("s3")
        for page in s3.get_paginator("list_objects_v2").paginate(Bucket=self.state["bucket"]):
            objects = [{"Key": r["Key"]} for r in page.get("Contents", [])]
            if objects:
                deletion = s3.delete_objects(Bucket=self.state["bucket"], Delete={"Objects": objects})
                assert not deletion.get("Errors"), deletion
        result["delete_bucket"] = s3.delete_bucket(Bucket=self.state["bucket"])
        result["at"] = now()
        save(self.out / "cleanup.json", result)
        print(self.region, "cleaned; EC2 stopped and retained", flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["setup", "run", "retry_commands", "status", "collect", "cleanup"])
    parser.add_argument("--region", choices=list(CLIENTS))
    args = parser.parse_args()
    regions = [args.region] if args.region else list(CLIENTS)
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(regions)) as pool:
        results = [pool.submit(lambda r: getattr(Lab(r), args.action)(), region) for region in regions]
        for future in results:
            future.result()


if __name__ == "__main__":
    main()
