#!/usr/bin/env python3
"""Beijing-region management. Builds and all test invocations execute on EC2."""
import argparse
import hashlib
import importlib.util
import json
from pathlib import Path
import tarfile
import time
from types import SimpleNamespace
import uuid

ROOT = Path(__file__).resolve().parent
CHINA = ROOT.parent
OUT = ROOT / "results/20260922"
REGION = "cn-north-1"
ACCOUNT = "447150580482"


def module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    return value


runtime_module = module("beijing_runtime_lab", CHINA / "runtime/lab.py")
ec2_module = module("beijing_ec2_lab", CHINA / "ec2_benchmark/ec2_lab.py")
efs_module = module("beijing_efs_lab", CHINA / "code_interpreter/efs/verify_efs.py")
save = runtime_module.save


class Lab:
    def __init__(self):
        self.runtime = runtime_module.Lab(SimpleNamespace(
            profile="agentcore_cn", region=REGION, output=str(OUT / "runtime"), runtime_only=True))
        self.ec2_args = SimpleNamespace(output=str(OUT / "ec2"), runtime_output=str(OUT / "runtime"),
                                        region=REGION, instance_type="t4g.small")

    def ec2(self):
        return ec2_module.Lab(self.ec2_args)

    def setup(self):
        rt = self.runtime
        name = rt.state["name"]
        ecr = rt.client("ecr")
        if "repository" not in rt.state:
            rt.state["repository_intent"] = name
            rt.persist()
            rt.state["repository"] = ecr.create_repository(repositoryName=name,
                imageTagMutability="IMMUTABLE", tags=[{"Key": "Experiment", "Value": name}])["repository"]
            rt.persist()
        prefix = name.replace("-", "_")
        rt.role("runtime", [
            runtime_module.allow(["ecr:GetAuthorizationToken"], "*"),
            runtime_module.allow(["ecr:BatchGetImage", "ecr:GetDownloadUrlForLayer"],
                                 rt.state["repository"]["repositoryArn"]),
            runtime_module.allow(["logs:DescribeLogGroups"], f"arn:aws-cn:logs:{REGION}:{ACCOUNT}:log-group:*"),
            runtime_module.allow(["logs:CreateLogGroup", "logs:CreateLogStream", "logs:DescribeLogStreams",
                                   "logs:PutLogEvents"],
                                 f"arn:aws-cn:logs:{REGION}:{ACCOUNT}:log-group:/aws/bedrock-agentcore/runtimes/{prefix}*"),
        ])
        self.ec2().deploy()

    def send_archive(self, name, files):
        ec = self.ec2()
        archive = OUT / "ec2" / (name + ".tar.gz")
        with tarfile.open(archive, "w:gz") as stream:
            for label, path in files.items():
                stream.add(path, arcname=label)
        save(OUT / "ec2" / (name + "-source-hashes.json"),
             {label: hashlib.sha256(Path(path).read_bytes()).hexdigest() for label, path in files.items()})
        ec.client("s3").upload_file(str(archive), ec.state["bucket"], "input/" + name + ".tar.gz")
        return ec

    def build(self):
        ec = self.ec2()
        cfg = {"region": REGION, "account": ACCOUNT, "instance_id": ec.state["instance"]["InstanceId"],
               "repository": self.runtime.state["repository"], "bucket": ec.state["bucket"]}
        if (OUT / "ec2/offline_base.json").exists():
            cfg["offline_base"] = json.loads((OUT / "ec2/offline_base.json").read_text())
        save(OUT / "ec2/build_config.json", cfg)
        ec = self.send_archive("build", {
            "build_runtime.py": ROOT / "build_runtime.py",
            "build_config.json": OUT / "ec2/build_config.json",
            "app.py": CHINA / "runtime/app.py", "Dockerfile": CHINA / "runtime/Dockerfile",
            ".dockerignore": CHINA / "runtime/.dockerignore",
            "requirements.txt": CHINA / "runtime/requirements.txt",
        })
        build_count = sum(item["label"].startswith("build") for item in ec.state["commands"])
        ec.command("build" if not build_count else f"build-{build_count+1}", [
            "set -eu", "cloud-init status --wait",
            "dnf install -y python3.12 python3.12-pip docker",
            "systemctl enable --now docker",
            "mkdir -p /opt/beijing-agentcore",
            "cd /opt/beijing-agentcore",
            f"aws s3 cp s3://{ec.state['bucket']}/input/build.tar.gz build.tar.gz --region {REGION}",
            "tar -xzf build.tar.gz",
            "python3.12 -m venv venv",
            "venv/bin/pip install --disable-pip-version-check --index-url https://pypi.tuna.tsinghua.edu.cn/simple -r requirements.txt",
            f"export AWS_REGION={REGION} AWS_DEFAULT_REGION={REGION}",
            "venv/bin/python -u build_runtime.py > build.log 2>&1",
        ], timeout=1800)

    def efs_setup(self):
        marker = OUT / "efs-setup-state.json"
        save(marker, {"status": "running", "at": runtime_module.now()})
        ec = self.ec2()
        efs = efs_module.Lab(OUT / "efs", region=REGION,
                             subnet_id=ec.state["network"]["subnet"]["SubnetId"])
        try:
            efs.deploy()
            save(marker, {"status": "ready", "at": runtime_module.now()})
        except Exception as exc:
            save(marker, {"status": "failed", "at": runtime_module.now(), "error": str(exc)})
            raise

    def status(self):
        ec = self.ec2()
        last = ec.state["commands"][-1]
        response = ec.client("ssm").get_command_invocation(
            CommandId=last["id"], InstanceId=ec.state["instance"]["InstanceId"])
        save(OUT / "ec2" / f"ssm-{last['label']}-result.json", response)
        print(last["label"], response["Status"], flush=True)
        print(response.get("StandardOutputContent", "")[-1500:], flush=True)
        print(response.get("StandardErrorContent", "")[-1500:], flush=True)
        keys = (["results/build_status.json"] if last["label"].startswith("build") else []) + [
            "results/progress.json", "results/final_status.json"]
        for key in keys:
            try:
                result = ec.client("s3").get_object(Bucket=ec.state["bucket"], Key=key)
                body = json.loads(result["Body"].read())
                result["Body"].close()
                save(OUT / "ec2" / Path(key).name, body)
                shown = dict(body)
                if "log_tail" in shown:
                    shown["log_tail"] = shown["log_tail"][-1800:]
                print(key, json.dumps(shown, default=str), flush=True)
            except ec.client("s3").exceptions.NoSuchKey:
                pass

    def resources(self):
        rt, ec = self.runtime, self.ec2()
        response = ec.client("s3").get_object(Bucket=ec.state["bucket"], Key="results/build_result.json")
        built = json.loads(response["Body"].read())
        response["Body"].close()
        save(OUT / "ec2/build_result.json", built)
        assert built["identity"]["region"] == REGION
        rt.state["image"] = built["image"]
        rt.persist()
        control = rt.client("bedrock-agentcore-control")
        if not (OUT / "existing_runtimes.json").exists():
            save(OUT / "existing_runtimes.json", control.list_agent_runtimes(maxResults=100))
        for kind in ("baseline", "scale"):
            if kind not in rt.state["runtimes"]:
                request = {
                    "agentRuntimeName": (rt.state["name"] + "_" + kind).replace("-", "_"),
                    "agentRuntimeArtifact": {"containerConfiguration": {
                        "containerUri": built["image"]["immutable_uri"]}},
                    "roleArn": rt.state["roles"]["runtime"]["Arn"],
                    "networkConfiguration": {"networkMode": "PUBLIC"},
                    "protocolConfiguration": {"serverProtocol": "HTTP"},
                    "lifecycleConfiguration": {"idleRuntimeSessionTimeout": 60, "maxLifetime": 1800},
                    "clientToken": str(uuid.uuid4()), "tags": {"Experiment": rt.state["name"]}}
                save(OUT / "runtime" / f"create-{kind}-request.json", request)
                rt.state["runtimes"][kind] = control.create_agent_runtime(**request)
                rt.persist()
        extra_path = OUT / "control.json"
        extra = json.loads(extra_path.read_text()) if extra_path.exists() else {}
        if "public_interpreter" not in extra:
            extra["public_interpreter"] = control.create_code_interpreter(
                name=rt.state["name"].replace("-", "_") + "_public",
                networkConfiguration={"networkMode": "PUBLIC"})
            save(extra_path, extra)
        subnet = ec.state["network"]["subnet"]["SubnetId"]
        marker = OUT / "efs-setup-state.json"
        if marker.exists():
            deadline = time.monotonic() + 900
            while json.loads(marker.read_text())["status"] == "running":
                if time.monotonic() >= deadline:
                    raise TimeoutError("EFS setup did not finish")
                print("Waiting for independently provisioning EFS interpreter", flush=True)
                time.sleep(10)
            if json.loads(marker.read_text())["status"] != "ready":
                raise RuntimeError(marker.read_text())
            efs = efs_module.Lab(OUT / "efs", region=REGION, subnet_id=subnet)
        else:
            efs = efs_module.Lab(OUT / "efs", region=REGION, subnet_id=subnet)
            efs.deploy()
        for kind, runtime in rt.state["runtimes"].items():
            runtime["configuration"] = rt.wait(lambda r=runtime: control.get_agent_runtime(
                agentRuntimeId=r["agentRuntimeId"]), "status", {"READY"}, "runtime-" + kind + "-ready")
            rt.persist()
        extra["efs_interpreter"] = efs.state["interpreter"]
        extra["efs_subnet_id"] = subnet
        save(extra_path, extra)
        policy = json.loads((OUT / "ec2/instance-policy.json").read_text())
        arns = []
        for item in (extra["public_interpreter"], extra["efs_interpreter"]):
            arns += [item["codeInterpreterArn"],
                     f"arn:aws-cn:bedrock-agentcore:{REGION}:{ACCOUNT}:code-interpreter/{item['codeInterpreterId']}"]
        policy["Statement"].append({"Effect": "Allow", "Resource": arns,
            "Action": ["bedrock-agentcore:StartCodeInterpreterSession", "bedrock-agentcore:InvokeCodeInterpreter",
                       "bedrock-agentcore:StopCodeInterpreterSession", "bedrock-agentcore:GetCodeInterpreterSession",
                       "bedrock-agentcore:ListCodeInterpreterSessions", "bedrock-agentcore:GetCodeInterpreter"]})
        ec.client("iam").put_role_policy(RoleName=ec.state["role"]["RoleName"], PolicyName="ChinaBenchmarks",
                                        PolicyDocument=json.dumps(policy))
        save(OUT / "ec2/instance-policy-final.json", policy)
        print("Beijing runtime, PUBLIC interpreter and EFS interpreter READY", flush=True)

    def run(self):
        ec = self.ec2()
        if any(item["label"] == "benchmark" for item in ec.state["commands"]):
            raise RuntimeError("Benchmark already submitted")
        extra = json.loads((OUT / "control.json").read_text())
        cfg = {"region": REGION, "account": ACCOUNT, "bucket": ec.state["bucket"],
               "instance_id": ec.state["instance"]["InstanceId"],
               "run_functional": True,
               "public_interpreter_id": extra["public_interpreter"]["codeInterpreterId"],
               "efs": {"subnet_id": extra["efs_subnet_id"]},
               "runtime": {"region": REGION, "account": ACCOUNT,
                   "load_role_name": ec.state["role"]["RoleName"],
                   "cold_samples": 100, "warm_samples": 500, "concurrency": 50,
                   "runtimes": {k: v["agentRuntimeArn"] for k, v in self.runtime.state["runtimes"].items()},
                   "load_client": {"kind": "EC2", "region": REGION, "instance_type": "t4g.small",
                                   "instance_id": ec.state["instance"]["InstanceId"]}}}
        save(OUT / "ec2/config.json", cfg)
        ec = self.send_archive("benchmark", {
            "config.json": OUT / "ec2/config.json",
            "run_benchmarks.py": CHINA / "ec2_benchmark/run_benchmarks.py",
            "benchmark_runtime.py": CHINA / "runtime/benchmark.py",
            "verify_code_interpreter.py": CHINA / "code_interpreter/verify_code_interpreter.py",
            "verify_efs.py": CHINA / "code_interpreter/efs/verify_efs.py",
            "efs_resources.json": OUT / "efs/resources.json",
            "requirements.txt": CHINA / "ec2_benchmark/requirements.txt",
        })
        ec.command("benchmark", [
            "set -eu", "cd /opt/beijing-agentcore", "test ! -e results/run_status.json",
            f"aws s3 cp s3://{ec.state['bucket']}/input/benchmark.tar.gz benchmark.tar.gz --region {REGION}",
            "tar -xzf benchmark.tar.gz",
            f"export AWS_REGION={REGION} AWS_DEFAULT_REGION={REGION}",
            "venv/bin/python -u run_benchmarks.py --config config.json > run.log 2>&1",
        ], timeout=2400)

    def collect(self):
        import shutil
        ec = self.ec2()
        response = ec.client("s3").get_object(Bucket=ec.state["bucket"], Key="results/final_status.json")
        status = json.loads(response["Body"].read())
        response["Body"].close()
        save(OUT / "ec2/final_status.json", status)
        archive_path = OUT / "ec2/results.tar.gz"
        ec.client("s3").download_file(ec.state["bucket"], "results/results.tar.gz", str(archive_path))
        extracted = OUT / "ec2/collected"
        extracted.mkdir(exist_ok=True)
        with tarfile.open(archive_path, "r:gz") as archive:
            archive.extractall(extracted, filter="data")
        for kind in ("runtime", "code_interpreter", "code_interpreter_public", "efs"):
            source = extracted / "results" / kind
            if source.exists():
                shutil.copytree(source, OUT / kind, dirs_exist_ok=True)
        ec.client("s3").download_file(ec.state["bucket"], "results/build.log", str(OUT / "ec2/build.log"))
        ec.state["results_collected_at"] = runtime_module.now()
        ec.state["results_status"] = status
        ec.persist()
        print(json.dumps(status), flush=True)

    def cleanup(self):
        ec = self.ec2()
        if not ec.state.get("results_collected_at"):
            raise RuntimeError("Collect evidence before cleanup")
        self.runtime.cleanup()
        control = self.runtime.client("bedrock-agentcore-control")
        extra = json.loads((OUT / "control.json").read_text())
        cid = extra["public_interpreter"]["codeInterpreterId"]
        response = control.delete_code_interpreter(codeInterpreterId=cid)
        save(OUT / "public-delete.json", response)
        ec.stop()
        efs = efs_module.Lab(OUT / "efs", region=REGION, subnet_id=extra["efs_subnet_id"])
        efs.network_cleanup_wait_seconds = 30
        try:
            efs.cleanup()
        except RuntimeError as exc:
            save(OUT / "efs/cleanup-pending.json", {"at": runtime_module.now(), "error": str(exc)})
            print("EFS network cleanup pending; see efs/cleanup-pending.json", flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["setup", "build", "efs_setup", "status", "resources", "run", "collect", "cleanup"])
    args = parser.parse_args()
    getattr(Lab(), args.action)()


if __name__ == "__main__":
    main()
