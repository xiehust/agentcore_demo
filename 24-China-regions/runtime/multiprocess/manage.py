"""Manage regional 8-vCPU clients; invoke measurements only through regional EC2."""
import argparse
import hashlib
import importlib.util
import json
from pathlib import Path
import shlex
import tarfile
import time
from types import SimpleNamespace
import uuid

ROOT = Path(__file__).resolve().parent
CHINA = ROOT.parents[1]
# User narrowed future runs to at most 100 concurrent requests.
LEVELS = (1, 10, 50, 100)


def module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


rtmod = module("multiprocess_runtime_lab", CHINA / "runtime/lab.py")
ecmod = module("multiprocess_ec2_lab", CHINA / "ec2_benchmark/ec2_lab.py")
save = rtmod.save


class Lab:
    def __init__(self, args):
        self.region = args.region
        self.out = Path(args.output).resolve() / self.region
        self.out.mkdir(parents=True, exist_ok=True)
        self.rt = rtmod.Lab(SimpleNamespace(profile="agentcore_cn", region=self.region,
            output=str(self.out / "runtime"), runtime_only=True))
        self.ec = ecmod.Lab(SimpleNamespace(region=self.region, output=str(self.out / "ec2"),
            runtime_output=str(self.out / "runtime"), instance_type="c6g.2xlarge"))

    def setup(self):
        rt = self.rt
        ecr = rt.client("ecr")
        name = rt.state["name"]
        if "repository" not in rt.state:
            rt.state["repository_intent"] = name
            rt.persist()
            rt.state["repository"] = ecr.create_repository(repositoryName=name,
                imageTagMutability="IMMUTABLE", tags=[{"Key": "Experiment", "Value": name}])["repository"]
            rt.persist()
        rt.role("runtime", [
            rtmod.allow(["ecr:GetAuthorizationToken"], "*"),
            rtmod.allow(["ecr:BatchGetImage", "ecr:GetDownloadUrlForLayer"],
                        rt.state["repository"]["repositoryArn"]),
            rtmod.allow(["logs:DescribeLogGroups"], f"arn:aws-cn:logs:{self.region}:447150580482:log-group:*"),
            rtmod.allow(["logs:CreateLogGroup", "logs:CreateLogStream", "logs:DescribeLogStreams",
                         "logs:PutLogEvents"],
                f"arn:aws-cn:logs:{self.region}:447150580482:log-group:/aws/bedrock-agentcore/runtimes/{name.replace('-', '_')}*"),
        ])
        self.ec.deploy()
        info = self.ec.state["instance_type_info"]
        assert info["VCpuInfo"]["DefaultVCpus"] == 8
        assert not info.get("BurstablePerformanceSupported")
        save(self.out / "existing-runtimes.json",
             rt.client("bedrock-agentcore-control").list_agent_runtimes(maxResults=100))

    def archive(self, name, files):
        path = self.out / (name + ".tar.gz")
        with tarfile.open(path, "w:gz") as stream:
            for label, source in files.items():
                stream.add(source, arcname=label)
        save(self.out / (name + "-sha256.json"),
             {label: hashlib.sha256(Path(source).read_bytes()).hexdigest() for label, source in files.items()})
        self.ec.client("s3").upload_file(str(path), self.ec.state["bucket"], "input/" + path.name)
        return path.name

    def build(self):
        previous = CHINA / "cn-north-1/results/20260922/ec2"
        base = json.loads((previous / "offline_base.json").read_text())
        path = previous / "offline-base.tar.gz"
        assert hashlib.sha256(path.read_bytes()).hexdigest() == base["archive_sha256"]
        self.ec.client("s3").upload_file(str(path), self.ec.state["bucket"], base["s3_key"])
        config = {"region": self.region, "account": "447150580482",
            "instance_id": self.ec.state["instance"]["InstanceId"], "bucket": self.ec.state["bucket"],
            "repository": self.rt.state["repository"], "offline_base": base}
        save(self.out / "build_config.json", config)
        name = self.archive("build", {
            "build_runtime.py": CHINA / "cn-north-1/build_runtime.py",
            "build_config.json": self.out / "build_config.json",
            "app.py": CHINA / "runtime/app.py", "Dockerfile": CHINA / "runtime/Dockerfile",
            ".dockerignore": CHINA / "runtime/.dockerignore",
            "requirements.txt": CHINA / "runtime/requirements.txt"})
        self.ec.command("build", ["set -eu", "cloud-init status --wait",
            "dnf install -y python3.12 python3.12-pip docker", "systemctl enable --now docker",
            "mkdir -p /opt/china-multiprocess", "cd /opt/china-multiprocess",
            f"aws s3 cp s3://{self.ec.state['bucket']}/input/{name} input.tar.gz --region {self.region}",
            "tar -xzf input.tar.gz", "python3.12 -m venv venv",
            "venv/bin/pip install --disable-pip-version-check --index-url https://pypi.tuna.tsinghua.edu.cn/simple -r requirements.txt",
            f"export AWS_REGION={self.region} AWS_DEFAULT_REGION={self.region}",
            "venv/bin/python -u build_runtime.py > build.log 2>&1"], timeout=1800)

    def object(self, key):
        response = self.ec.client("s3").get_object(Bucket=self.ec.state["bucket"], Key=key)
        try:
            return json.loads(response["Body"].read())
        finally:
            response["Body"].close()

    def status(self):
        command = self.ec.state["commands"][-1]
        response = self.ec.client("ssm").get_command_invocation(CommandId=command["id"],
            InstanceId=self.ec.state["instance"]["InstanceId"])
        save(self.out / "ec2" / f"ssm-{command['label']}-result.json", response)
        print(self.region, command["label"], response["Status"], flush=True)
        print(response.get("StandardErrorContent", "")[-1000:], flush=True)
        prefix = "isolated" if command["label"].startswith("isolated") else "results"
        for key in ("results/build_status.json", prefix + "/progress.json", prefix + "/final.json"):
            try:
                value = self.object(key)
                local_name = ("isolated-" if key.startswith("isolated/") else "") + Path(key).name
                save(self.out / local_name, value)
                print(key, json.dumps(value, default=str)[-2500:], flush=True)
            except self.ec.client("s3").exceptions.NoSuchKey:
                pass

    def resources(self, isolated=False):
        rt = self.rt
        built = self.object("results/build_result.json")
        save(self.out / "build_result.json", built)
        rt.state["image"] = built["image"]
        rt.persist()
        control = rt.client("bedrock-agentcore-control")
        if isolated:
            cells = [("isolated-scale-c50", 50, 1, 5),
                     ("isolated-c100", 100, 1, 0)]
        else:
            cells = [(f"c{level}-r{repeat}", level, repeat, 0)
                     for level in LEVELS for repeat in range(1, 4)]
            cells.append(("scale-c50", 50, 1, 5))
        for key, level, repeat, hold in cells:
            if key in rt.state["runtimes"]:
                continue
            request_file = rt.out / f"create-{key}.json"
            request = json.loads(request_file.read_text()) if request_file.exists() else {
                "agentRuntimeName": (rt.state["name"] + "_" + key).replace("-", "_"),
                "agentRuntimeArtifact": {"containerConfiguration": {
                    "containerUri": built["image"]["immutable_uri"]}},
                "roleArn": rt.state["roles"]["runtime"]["Arn"],
                "networkConfiguration": {"networkMode": "PUBLIC"},
                "protocolConfiguration": {"serverProtocol": "HTTP"},
                "lifecycleConfiguration": {"idleRuntimeSessionTimeout": 300, "maxLifetime": 1800},
                "clientToken": str(uuid.uuid4()), "tags": {"Experiment": rt.state["name"]}}
            save(request_file, request)
            created = control.create_agent_runtime(**request)
            created.update(cell=key, concurrency=level, repeat=repeat, hold_seconds=hold)
            rt.state["runtimes"][key] = created
            rt.persist()
            print(self.region, "CREATED", key, flush=True)
        for key, runtime in rt.state["runtimes"].items():
            if key not in {cell[0] for cell in cells}:
                continue
            runtime["configuration"] = rt.wait(lambda r=runtime: control.get_agent_runtime(
                agentRuntimeId=r["agentRuntimeId"]), "status", {"READY"}, f"{key}-ready")
            # First observed READY timestamp; waiting longer on resume is safe.
            runtime.setdefault("ready_observed_unix", time.time())
            rt.persist()

    def run(self, isolated=False):
        label = "isolated" if isolated else "benchmark"
        if any(c["label"] == label for c in self.ec.state["commands"]):
            raise RuntimeError("Benchmark already submitted; collect its evidence before considering a rerun")
        if isolated:
            self.isolated_permissions()
        runtimes = {k: v for k, v in self.rt.state["runtimes"].items()
                    if k.startswith("isolated-") == isolated}
        assert len(runtimes) == (2 if isolated else len(LEVELS) * 3 + 1)
        cfg = {"region": self.region, "account": "447150580482",
            "instance_id": self.ec.state["instance"]["InstanceId"], "bucket": self.ec.state["bucket"],
            "runtimes": runtimes, "levels": list(LEVELS), "process_count": 8, "settle_seconds": 60}
        config_name = "isolated-config.json" if isolated else "config.json"
        save(self.out / config_name, cfg)
        files = {name: ROOT / name for name in ("client.py", "run.py", "analyze.py", "test_client.py")}
        files[config_name] = self.out / config_name
        name = self.archive(label, files)
        result_dir = "isolated-results" if isolated else "results"
        invocation = ("venv/bin/python -u run.py --config isolated-config.json "
                      "--output isolated-results --key-prefix isolated --interval-seconds 180"
                      if isolated else "venv/bin/python -u run.py")
        commands = ["set -eu", "cd /opt/china-multiprocess",
            f"test ! -e {result_dir}/run.json",
            f"aws s3 cp s3://{self.ec.state['bucket']}/input/{name} benchmark.tar.gz --region {self.region}",
            "tar -xzf benchmark.tar.gz",
            f"export AWS_REGION={self.region} AWS_DEFAULT_REGION={self.region}",
            "venv/bin/python -m unittest -v test_client > client_checks.log 2>&1",
            invocation + f" > {label}.log 2>&1"]
        self.ec.command(label, commands, timeout=3600)

    def collect(self, isolated=False):
        prefix = "isolated" if isolated else "results"
        status = self.object(prefix + "/final.json")
        save(self.out / ("isolated-final.json" if isolated else "final.json"), status)
        archive = self.out / (prefix + ".tar.gz")
        self.ec.client("s3").download_file(self.ec.state["bucket"], prefix + "/results.tar.gz", str(archive))
        with tarfile.open(archive) as stream:
            stream.extractall(self.out, filter="data")
        self.ec.state["results_collected_at"] = rtmod.now()
        self.ec.persist()
        print(self.region, status, flush=True)

    def isolated_resources(self):
        self.resources(isolated=True)

    def isolated_run(self):
        self.run(isolated=True)

    def isolated_collect(self):
        self.collect(isolated=True)

    def isolated_permissions(self):
        iam = self.ec.client("iam")
        role = self.ec.state["role"]["RoleName"]
        document = iam.get_role_policy(RoleName=role, PolicyName="ChinaBenchmarks")["PolicyDocument"]
        resource = f"arn:aws-cn:s3:::{self.ec.state['bucket']}/isolated/*"
        if not any(s.get("Resource") == resource for s in document["Statement"]):
            document["Statement"].append({"Effect": "Allow", "Action": ["s3:PutObject"], "Resource": resource})
        iam.put_role_policy(RoleName=role, PolicyName="ChinaBenchmarks", PolicyDocument=json.dumps(document))
        save(self.out / "ec2/isolated-instance-policy.json", document)

    def isolated_retry_preparation(self):
        """Specific recovery for the initial S3-prefix failure; never replay measured traffic."""
        prior = next(c for c in self.ec.state["commands"] if c["label"] == "isolated")
        status = self.ec.client("ssm").get_command_invocation(CommandId=prior["id"],
            InstanceId=self.ec.state["instance"]["InstanceId"])
        assert status["Status"] == "Failed"
        assert not any(c["label"] == "isolated-retry" for c in self.ec.state["commands"])
        self.isolated_permissions()
        bucket = self.ec.state["bucket"]
        commands = ["set -eu", "cd /opt/china-multiprocess",
            "test ! -e isolated-results/cells",
            "venv/bin/python -c " + shlex.quote(
                "import json; d=json.load(open('isolated-results/final.json')); "
                "assert not d['completed_cells'] and 'AccessDenied' in d['error'] and 'PutObject' in d['error']"),
            f"aws s3 cp isolated-results.tar.gz s3://{bucket}/results/isolated-preparation-failure.tar.gz --region {self.region}",
            f"aws s3 cp isolated.log s3://{bucket}/results/isolated-preparation-failure.log --region {self.region}",
            "mv isolated-results isolated-preparation-failure",
            "mv isolated-results.tar.gz isolated-preparation-failure.tar.gz",
            "sleep 10",
            f"export AWS_REGION={self.region} AWS_DEFAULT_REGION={self.region}",
            "venv/bin/python -u run.py --config isolated-config.json --output isolated-results "
            "--key-prefix isolated --interval-seconds 180 > isolated.log 2>&1"]
        self.ec.command("isolated-retry", commands, timeout=1800)

    def cleanup(self):
        assert self.ec.state.get("results_collected_at")
        if any(k.startswith("isolated-") for k in self.rt.state["runtimes"]):
            assert (self.out / "isolated-final.json").exists(), "Collect isolated run before cleanup"
        self.rt.cleanup()
        self.ec.stop()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["setup", "build", "resources", "run", "status", "collect",
                                         "isolated_resources", "isolated_run", "isolated_collect",
                                         "isolated_retry_preparation", "cleanup"])
    parser.add_argument("--region", required=True, choices=["cn-north-1", "cn-northwest-1"])
    parser.add_argument("--output", default=str(ROOT / "results/20260922"))
    args = parser.parse_args()
    getattr(Lab(args), args.action)()
