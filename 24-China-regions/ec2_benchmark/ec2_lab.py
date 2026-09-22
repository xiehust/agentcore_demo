#!/usr/bin/env python3
"""Manage this experiment's small Ningxia EC2 and private transfer bucket."""
import argparse
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
CHINA = ROOT.parent


def now():
    return datetime.now(timezone.utc).isoformat()


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(value, indent=2, default=str) + "\n")
    tmp.replace(path)


class Lab:
    def __init__(self, args):
        self.args = args
        self.out = Path(args.output).resolve()
        self.out.mkdir(parents=True, exist_ok=True)
        self.path = self.out / "resources.json"
        region = getattr(args, "region", "cn-northwest-1")
        self.aws = boto3.Session(profile_name="agentcore_cn", region_name=region)
        self.cfg = Config(connect_timeout=10, read_timeout=120, retries={"total_max_attempts": 1})
        identity = self.client("sts").get_caller_identity()
        if identity["Account"] != "447150580482":
            raise RuntimeError("Unexpected AWS account")
        self.state = json.loads(self.path.read_text()) if self.path.exists() else {
            "created_at": now(), "name": "cn-ec2-bench-" + uuid.uuid4().hex[:10],
            "region": region, "account": identity["Account"], "identity": identity,
            "commands": []}
        if self.state["region"] != region:
            raise ValueError("Saved resources belong to a different region")
        self.persist()

    def client(self, service):
        return self.aws.client(service, config=self.cfg)

    def persist(self):
        save(self.path, self.state)

    def log(self, message):
        print(f"[{now()}] {message}", flush=True)

    def deploy(self):
        ec2, iam, s3 = self.client("ec2"), self.client("iam"), self.client("s3")
        name = self.state["name"]
        region = self.state["region"]
        if "bucket" not in self.state:
            bucket = name + "-447150580482"
            self.state["bucket_intent"] = bucket
            self.persist()
            s3.create_bucket(Bucket=bucket, CreateBucketConfiguration={"LocationConstraint": region})
            self.state["bucket"] = bucket
            self.persist()
            s3.put_public_access_block(Bucket=bucket, PublicAccessBlockConfiguration={
                "BlockPublicAcls": True, "IgnorePublicAcls": True,
                "BlockPublicPolicy": True, "RestrictPublicBuckets": True})
            s3.put_bucket_encryption(Bucket=bucket, ServerSideEncryptionConfiguration={
                "Rules": [{"ApplyServerSideEncryptionByDefault": {"SSEAlgorithm": "AES256"}}]})
            s3.put_bucket_tagging(Bucket=bucket, Tagging={"TagSet": [{"Key": "Experiment", "Value": name}]})
        if "role" not in self.state:
            self.state["role_intent"] = name
            self.persist()
            self.state["role"] = iam.create_role(RoleName=name,
                AssumeRolePolicyDocument=json.dumps({"Version": "2012-10-17", "Statement": [{
                    "Effect": "Allow", "Principal": {"Service": "ec2.amazonaws.com.cn"},
                    "Action": "sts:AssumeRole"}]}),
                Tags=[{"Key": "Experiment", "Value": name}])["Role"]
            self.persist()
        managed_policy = "arn:aws-cn:iam::aws:policy/AmazonSSMManagedInstanceCore"
        iam.attach_role_policy(RoleName=name, PolicyArn=managed_policy)
        self.state["managed_policy"] = managed_policy
        runtime_state = json.loads(Path(self.args.runtime_output, "resources.json").read_text())
        runtime_prefix = runtime_state["name"].replace("-", "_")
        runtime_arn = f"arn:aws-cn:bedrock-agentcore:{region}:447150580482:runtime/{runtime_prefix}_*"
        ci_arns = [f"arn:aws-cn:bedrock-agentcore:{region}:{account}:code-interpreter/aws.codeinterpreter.v1"
                   for account in ("aws", "447150580482")]
        statements = [
            {"Effect": "Allow", "Action": ["s3:GetObject"],
             "Resource": f"arn:aws-cn:s3:::{self.state['bucket']}/input/*"},
            {"Effect": "Allow", "Action": ["s3:PutObject"],
             "Resource": f"arn:aws-cn:s3:::{self.state['bucket']}/results/*"},
            {"Effect": "Allow", "Action": ["bedrock-agentcore:InvokeAgentRuntime",
                                           "bedrock-agentcore:StopRuntimeSession"], "Resource": runtime_arn},
            {"Effect": "Allow", "Action": [
                "bedrock-agentcore:StartCodeInterpreterSession", "bedrock-agentcore:InvokeCodeInterpreter",
                "bedrock-agentcore:StopCodeInterpreterSession", "bedrock-agentcore:GetCodeInterpreterSession",
                "bedrock-agentcore:ListCodeInterpreterSessions", "bedrock-agentcore:GetCodeInterpreter"],
             "Resource": ci_arns},
        ]
        if "repository" in runtime_state:
            statements += [
                {"Effect": "Allow", "Action": ["ecr:GetAuthorizationToken"], "Resource": "*"},
                {"Effect": "Allow", "Action": ["ecr:BatchCheckLayerAvailability", "ecr:InitiateLayerUpload",
                    "ecr:UploadLayerPart", "ecr:CompleteLayerUpload", "ecr:PutImage",
                    "ecr:BatchGetImage", "ecr:GetDownloadUrlForLayer", "ecr:DescribeImages"],
                 "Resource": runtime_state["repository"]["repositoryArn"]},
            ]
        inline = {"Version": "2012-10-17", "Statement": statements}
        iam.put_role_policy(RoleName=name, PolicyName="ChinaBenchmarks", PolicyDocument=json.dumps(inline))
        save(self.out / "instance-policy.json", inline)
        if "profile" not in self.state:
            self.state["profile_intent"] = name
            self.persist()
            profile = iam.create_instance_profile(InstanceProfileName=name)["InstanceProfile"]
            self.state["profile"] = profile
            self.persist()
            iam.add_role_to_instance_profile(InstanceProfileName=name, RoleName=name)
        if "network" not in self.state:
            vpc = ec2.describe_vpcs(Filters=[{"Name": "isDefault", "Values": ["true"]}])["Vpcs"][0]
            tables = ec2.describe_route_tables(Filters=[{"Name": "vpc-id", "Values": [vpc["VpcId"]]}])["RouteTables"]
            subnets = ec2.describe_subnets(Filters=[{"Name": "vpc-id", "Values": [vpc["VpcId"]]}])["Subnets"]
            selected = None
            for subnet in sorted(subnets, key=lambda r: r["AvailabilityZone"]):
                explicit = [t for t in tables if any(a.get("SubnetId") == subnet["SubnetId"] for a in t["Associations"])]
                applicable = explicit or [t for t in tables if any(a.get("Main") for a in t["Associations"])]
                if any(route.get("DestinationCidrBlock") == "0.0.0.0/0"
                       and route.get("GatewayId", "").startswith("igw-") and route.get("State") == "active"
                       for table in applicable for route in table["Routes"]):
                    selected = subnet
                    break
            if not selected:
                raise RuntimeError("No default VPC subnet with an active public internet route")
            self.state["network"] = {"vpc": vpc["VpcId"], "subnet": selected, "route_tables": applicable}
            self.persist()
        if "security_group" not in self.state:
            self.state["sg_intent"] = name
            self.persist()
            sg = ec2.create_security_group(GroupName=name, Description="SSM-only China region benchmark; no inbound",
                VpcId=self.state["network"]["vpc"],
                TagSpecifications=[{"ResourceType": "security-group", "Tags": [{"Key": "Name", "Value": name}]}])
            self.state["security_group"] = sg["GroupId"]
            self.persist()
        if "instance" not in self.state:
            instance_type = getattr(self.args, "instance_type", "t3.small")
            type_info = ec2.describe_instance_types(InstanceTypes=[instance_type])["InstanceTypes"][0]
            architecture = type_info["ProcessorInfo"]["SupportedArchitectures"][0]
            self.state["instance_type_info"] = type_info
            ami_parameter = self.client("ssm").get_parameter(
                Name="/aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-" + architecture)["Parameter"]
            ami = ec2.describe_images(ImageIds=[ami_parameter["Value"]])["Images"][0]
            self.state["ami"] = {"parameter": ami_parameter, "image": ami}
            self.state["instance_client_token"] = self.state.get("instance_client_token", str(uuid.uuid4()))
            self.persist()
            time.sleep(12)
            response = ec2.run_instances(
                ImageId=ami["ImageId"], InstanceType=instance_type, MinCount=1, MaxCount=1,
                ClientToken=self.state["instance_client_token"],
                IamInstanceProfile={"Name": name},
                NetworkInterfaces=[{"DeviceIndex": 0, "SubnetId": self.state["network"]["subnet"]["SubnetId"],
                                    "Groups": [self.state["security_group"]], "AssociatePublicIpAddress": True,
                                    "DeleteOnTermination": True}],
                MetadataOptions={"HttpTokens": "required", "HttpEndpoint": "enabled", "HttpPutResponseHopLimit": 1},
                BlockDeviceMappings=[{"DeviceName": ami["RootDeviceName"],
                                      "Ebs": {"VolumeSize": 8, "VolumeType": "gp3", "Encrypted": True,
                                              "DeleteOnTermination": True}}],
                **({"CreditSpecification": {"CpuCredits": "unlimited"}}
                   if type_info.get("BurstablePerformanceSupported") else {}),
                InstanceInitiatedShutdownBehavior="stop",
                UserData="#!/bin/bash\nset -eu\ndnf install -y python3-pip awscli\nsystemctl enable --now amazon-ssm-agent\n",
                TagSpecifications=[{"ResourceType": resource, "Tags": [
                    {"Key": "Name", "Value": name}, {"Key": "Experiment", "Value": name}]}
                    for resource in ("instance", "volume")])
            self.state["instance"] = response["Instances"][0]
            self.persist()
        instance_id = self.state["instance"]["InstanceId"]
        deadline = time.monotonic() + 600
        while time.monotonic() < deadline:
            response = self.client("ssm").describe_instance_information(
                Filters=[{"Key": "InstanceIds", "Values": [instance_id]}])
            save(self.out / "ssm-instance.json", response)
            if any(r.get("PingStatus") == "Online" for r in response["InstanceInformationList"]):
                self.log(f"EC2 {instance_id} is SSM Online")
                break
            self.log(f"Waiting for EC2 {instance_id} / SSM")
            time.sleep(10)
        else:
            raise RuntimeError("EC2 did not become SSM Online")
        desc = ec2.describe_instances(InstanceIds=[instance_id])["Reservations"][0]["Instances"][0]
        self.state["instance"] = desc
        self.persist()
        save(self.out / "security-group.json", ec2.describe_security_groups(GroupIds=[self.state["security_group"]]))

    def command(self, label, commands, timeout=1800):
        response = self.client("ssm").send_command(
            InstanceIds=[self.state["instance"]["InstanceId"]], DocumentName="AWS-RunShellScript",
            Parameters={"commands": commands, "executionTimeout": [str(timeout)]},
            Comment="China same-region benchmark: " + label, TimeoutSeconds=60)
        cid = response["Command"]["CommandId"]
        self.state["commands"].append({"label": label, "id": cid, "at": now()})
        self.persist()
        save(self.out / f"ssm-{label}-request.json", {"commands": commands, "response": response})
        self.log(f"SSM {label}: {cid}")
        return cid

    def status(self):
        for item in self.state["commands"][-1:]:
            response = self.client("ssm").get_command_invocation(
                CommandId=item["id"], InstanceId=self.state["instance"]["InstanceId"])
            save(self.out / f"ssm-{item['label']}-result.json", response)
            self.log(f"{item['label']}: {response['Status']}")
            if response.get("StandardOutputContent"):
                print(response["StandardOutputContent"][-3500:], flush=True)
            if response.get("StandardErrorContent"):
                print(response["StandardErrorContent"][-2500:], flush=True)
        for key in ("results/progress.json", "results/final_status.json"):
            try:
                response = self.client("s3").get_object(Bucket=self.state["bucket"], Key=key)
                value = json.loads(response["Body"].read())
                response["Body"].close()
                save(self.out / Path(key).name, value)
                self.log(json.dumps(value))
            except self.client("s3").exceptions.NoSuchKey:
                pass

    def run(self):
        attempts = [item for item in self.state["commands"] if item["label"].startswith("benchmark")]
        if attempts:
            previous = self.client("ssm").get_command_invocation(
                CommandId=attempts[-1]["id"], InstanceId=self.state["instance"]["InstanceId"])
            save(self.out / f"ssm-{attempts[-1]['label']}-result.json", previous)
            if previous["Status"] != "Failed":
                raise RuntimeError("Benchmark already submitted; do not start a duplicate run")
        runtime_state = json.loads(Path(self.args.runtime_output, "resources.json").read_text())
        if any(row.get("configuration", {}).get("status") != "READY"
               for row in runtime_state["runtimes"].values()):
            raise RuntimeError("Runtime deployment is not yet ready")
        settings = {
            "region": self.state["region"], "account": self.state["account"],
            "bucket": self.state["bucket"], "instance_id": self.state["instance"]["InstanceId"],
            "runtime": {"region": self.state["region"], "account": self.state["account"],
                        "load_role_name": self.state["role"]["RoleName"],
                        "cold_samples": 100, "warm_samples": 500, "concurrency": 50,
                        "runtimes": {k: v["agentRuntimeArn"] for k, v in runtime_state["runtimes"].items()},
                        "load_client": {"kind": "EC2", "instance_id": self.state["instance"]["InstanceId"],
                                        "region": self.state["region"], "instance_type": "t3.small"}}}
        save(self.out / "config.json", settings)
        files = {
            "config.json": self.out / "config.json",
            "run_benchmarks.py": ROOT / "run_benchmarks.py",
            "requirements.txt": ROOT / "requirements.txt",
            "benchmark_runtime.py": CHINA / "runtime" / "benchmark.py",
            "verify_code_interpreter.py": CHINA / "code_interpreter" / "verify_code_interpreter.py",
        }
        archive = self.out / "input.tar.gz"
        with tarfile.open(archive, "w:gz") as out:
            for name, path in files.items():
                out.add(path, arcname=name)
        save(self.out / "input_hashes.json", {name: hashlib.sha256(path.read_bytes()).hexdigest()
                                             for name, path in files.items()})
        self.client("s3").upload_file(str(archive), self.state["bucket"], "input/bench.tar.gz")
        region = self.state["region"]
        bucket = self.state["bucket"]
        commands = [
            "set -eu",
            "cloud-init status --wait",
            "mkdir -p /opt/cn-agentcore-benchmark",
            "cd /opt/cn-agentcore-benchmark",
            "test ! -e results/run_status.json",
            "test ! -e results/runtime/requests.jsonl",
            "dnf install -y python3.12 python3.12-pip",
            f"aws s3 cp s3://{bucket}/input/bench.tar.gz input.tar.gz --region {region}",
            "tar -xzf input.tar.gz",
            "python3.12 -m venv --clear venv",
            "venv/bin/pip install --disable-pip-version-check --index-url https://pypi.tuna.tsinghua.edu.cn/simple -r requirements.txt",
            f"export AWS_REGION={region} AWS_DEFAULT_REGION={region} AWS_EC2_METADATA_DISABLED=false",
            "venv/bin/python -u run_benchmarks.py --config config.json > run.log 2>&1",
        ]
        self.command("benchmark" if not attempts else f"benchmark-{len(attempts)+1}", commands, timeout=2400)

    def collect(self):
        s3 = self.client("s3")
        final = s3.get_object(Bucket=self.state["bucket"], Key="results/final_status.json")
        value = json.loads(final["Body"].read())
        final["Body"].close()
        save(self.out / "final_status.json", value)
        target = self.out / "results.tar.gz"
        s3.download_file(self.state["bucket"], "results/results.tar.gz", str(target))
        extracted = self.out / "collected"
        extracted.mkdir(exist_ok=True)
        with tarfile.open(target, "r:gz") as archive:
            archive.extractall(extracted, filter="data")
        data = extracted / "results"
        for source_name, target_dir in [
            ("runtime", Path(self.args.runtime_output)),
            ("code_interpreter", CHINA / "code_interpreter" / "results" / "20260922-ec2")]:
            source = data / source_name
            if source.exists():
                import shutil
                shutil.copytree(source, target_dir, dirs_exist_ok=True)
        self.state["results_collected_at"] = now()
        self.state["results_status"] = value
        self.persist()
        self.log(f"Downloaded results: {value}")

    def stop(self):
        if "results_collected_at" not in self.state:
            raise RuntimeError("Collect result archive before stopping the EC2")
        ec2 = self.client("ec2")
        instance_id = self.state["instance"]["InstanceId"]
        response = ec2.stop_instances(InstanceIds=[instance_id])
        save(self.out / "stop-instance.json", response)
        deadline = time.monotonic() + 300
        while time.monotonic() < deadline:
            instance = ec2.describe_instances(InstanceIds=[instance_id])["Reservations"][0]["Instances"][0]
            self.log(f"EC2 {instance_id}: {instance['State']['Name']}")
            save(self.out / "instance-final.json", instance)
            if instance["State"]["Name"] == "stopped":
                break
            time.sleep(5)
        else:
            raise RuntimeError("Instance stop not confirmed")
        self.state["instance"] = instance
        self.state["stopped_at"] = now()
        self.persist()
        # Remove the now-unneeded test data-plane permissions, retaining SSM access for reuse.
        self.client("iam").delete_role_policy(RoleName=self.state["role"]["RoleName"],
                                               PolicyName="ChinaBenchmarks")
        bucket = self.state["bucket"]
        s3 = self.client("s3")
        for page in s3.get_paginator("list_objects_v2").paginate(Bucket=bucket):
            objects = [{"Key": row["Key"]} for row in page.get("Contents", [])]
            if objects:
                deleted = s3.delete_objects(Bucket=bucket, Delete={"Objects": objects})
                if deleted.get("Errors"):
                    raise RuntimeError(str(deleted["Errors"]))
        deleted = s3.delete_bucket(Bucket=bucket)
        save(self.out / "transfer-bucket-deleted.json", deleted)
        self.state["transfer_bucket_deleted_at"] = now()
        self.state["retained"] = {"instance": instance_id, "instance_state": "stopped",
            "role": self.state["role"]["RoleName"], "profile": self.state["profile"]["InstanceProfileName"],
            "security_group": self.state["security_group"],
            "volumes": [r["Ebs"]["VolumeId"] for r in instance["BlockDeviceMappings"]]}
        self.persist()
        self.log("EC2 stopped and retained; transfer bucket deleted; SSM role retained")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["deploy", "run", "status", "collect", "stop"])
    parser.add_argument("--output", default=str(ROOT / "results" / "20260922"))
    parser.add_argument("--region", default="cn-northwest-1")
    parser.add_argument("--instance-type", default="t3.small")
    parser.add_argument("--runtime-output", default=str(CHINA / "runtime" / "results" / "20260922-ec2"))
    args = parser.parse_args()
    getattr(Lab(args), args.action)()


if __name__ == "__main__":
    main()
