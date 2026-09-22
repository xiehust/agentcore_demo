#!/usr/bin/env python3
"""Deploy and clean up only this experiment's China-region resources."""
import argparse
import base64
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import time
import uuid

import boto3
from botocore.config import Config

ROOT = Path(__file__).resolve().parent


def now():
    return datetime.now(timezone.utc).isoformat()


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, indent=2, ensure_ascii=False, default=str) + "\n")
    temporary.replace(path)


def policy(statements):
    return json.dumps({"Version": "2012-10-17", "Statement": statements})


def allow(actions, resource):
    return {"Effect": "Allow", "Action": actions, "Resource": resource}


class Lab:
    def __init__(self, args):
        self.args = args
        self.out = Path(args.output).resolve()
        self.out.mkdir(parents=True, exist_ok=True)
        self.path = self.out / "resources.json"
        self.session = boto3.Session(profile_name=args.profile, region_name=args.region)
        self.config = Config(connect_timeout=10, read_timeout=300,
                             retries={"total_max_attempts": 1}, max_pool_connections=60)
        self.state = json.loads(self.path.read_text()) if self.path.exists() else {
            "created_at": now(), "name": "cn-runtime24-" + uuid.uuid4().hex[:10],
            "profile": args.profile, "region": args.region, "runtimes": {}, "roles": {}}
        if self.state["profile"] != args.profile or self.state["region"] != args.region:
            raise ValueError("State/profile/region mismatch")
        identity = self.client("sts").get_caller_identity()
        if identity["Account"] != "447150580482":
            raise ValueError("Unexpected account")
        self.state["identity"] = identity
        self.persist()

    def client(self, service):
        return self.session.client(service, config=self.config)

    def persist(self):
        save(self.path, self.state)

    def record(self, name, value):
        save(self.out / name, value)

    def log(self, text):
        print(f"[{now()}] {text}", flush=True)

    def wait(self, fetch, key, accepted, label, seconds=600):
        deadline = time.monotonic() + seconds
        while True:
            response = fetch()
            self.record(f"{label}.json", response)
            status = response[key]
            self.log(f"{label}: {status}")
            if status in accepted:
                return response
            if "FAILED" in status or time.monotonic() >= deadline:
                raise RuntimeError(f"{label}: {response}")
            time.sleep(5)

    def role(self, kind, statements):
        iam = self.client("iam")
        name = self.state["name"] + "-" + kind
        if kind not in self.state["roles"]:
            self.state.setdefault("role_intents", {})[kind] = name
            self.persist()
            trust = policy([{"Effect": "Allow", "Principal": {
                "Service": "bedrock-agentcore.amazonaws.com"}, "Action": "sts:AssumeRole",
                "Condition": {"StringEquals": {"aws:SourceAccount": "447150580482"},
                              "ArnLike": {"aws:SourceArn":
                                  f"arn:aws-cn:bedrock-agentcore:{self.args.region}:447150580482:*"}}}])
            try:
                response = iam.create_role(RoleName=name, AssumeRolePolicyDocument=trust,
                                           Tags=[{"Key": "Experiment", "Value": self.state["name"]}])
            except iam.exceptions.EntityAlreadyExistsException:
                response = iam.get_role(RoleName=name)
            self.state["roles"][kind] = response["Role"]
            self.persist()
        iam.put_role_policy(RoleName=name, PolicyName="RuntimeValidation",
                            PolicyDocument=policy(statements))
        self.record(f"{kind}-policy.json", {"policy": json.loads(policy(statements))})
        return self.state["roles"][kind]["Arn"]

    def deploy(self):
        ecr = self.client("ecr")
        name, region = self.state["name"], self.args.region
        if "repository" not in self.state:
            self.state["repository_intent"] = name
            self.persist()
            try:
                repo = ecr.create_repository(repositoryName=name, imageTagMutability="IMMUTABLE",
                    tags=[{"Key": "Experiment", "Value": name}])["repository"]
            except ecr.exceptions.RepositoryAlreadyExistsException:
                repo = ecr.describe_repositories(repositoryNames=[name])["repositories"][0]
            self.state["repository"] = repo
            self.persist()
        digest = hashlib.sha256((ROOT / "app.py").read_bytes() + (ROOT / "Dockerfile").read_bytes()).hexdigest()[:16]
        image = self.state["repository"]["repositoryUri"] + ":" + digest
        if "image" not in self.state:
            self.log("Building ARM64 image")
            subprocess.run(["docker", "build", "--platform", "linux/arm64", "-t", image, str(ROOT)],
                           check=True)
            inspected = json.loads(subprocess.check_output(["docker", "image", "inspect", image]))[0]
            if inspected["Architecture"] != "arm64":
                raise RuntimeError("Image must be arm64")
            # Validate the exact container before uploading it.
            cid = subprocess.check_output(["docker", "run", "-d", "--rm", "-p", "127.0.0.1::8080", image],
                                          text=True).strip()
            try:
                mapping = subprocess.check_output(["docker", "port", cid, "8080/tcp"], text=True).strip()
                import urllib.request
                url = "http://" + mapping
                for _ in range(30):
                    try:
                        ping = json.loads(urllib.request.urlopen(url + "/ping", timeout=1).read())
                        break
                    except OSError:
                        time.sleep(0.2)
                else:
                    raise RuntimeError("Local container did not start")
                replies = []
                for i in range(3):
                    request = urllib.request.Request(url + "/invocations",
                        data=json.dumps({"nonce": str(i)}).encode(),
                        headers={"Content-Type": "application/json"})
                    replies.append(json.loads(urllib.request.urlopen(request, timeout=2).read()))
                assert ping["status"] == "Healthy"
                assert [r["request_index"] for r in replies] == [1, 2, 3]
                assert len({r["process_id"] for r in replies}) == 1
                assert len({r["instance_id"] for r in replies}) == 1
                self.record("local-contract.json", {"ping": ping, "replies": replies})
            finally:
                subprocess.run(["docker", "stop", cid], check=True, stdout=subprocess.DEVNULL)
            auth = ecr.get_authorization_token()["authorizationData"][0]
            user, password = base64.b64decode(auth["authorizationToken"]).decode().split(":", 1)
            cfg = self.out / "docker-auth"
            cfg.mkdir(mode=0o700, exist_ok=True)
            try:
                subprocess.run(["docker", "--config", str(cfg), "login", "--username", user,
                                "--password-stdin", auth["proxyEndpoint"]],
                               input=password, text=True, check=True)
                subprocess.run(["docker", "--config", str(cfg), "push", image], check=True)
            finally:
                subprocess.run(["docker", "--config", str(cfg), "logout", auth["proxyEndpoint"]],
                               stdout=subprocess.DEVNULL)
                shutil.rmtree(cfg)
            description = ecr.describe_images(repositoryName=name, imageIds=[{"imageTag": digest}])["imageDetails"][0]
            self.state["image"] = {"uri": image, "immutable_uri": self.state["repository"]["repositoryUri"]
                                   + "@" + description["imageDigest"], "ecr": description,
                                   "local_size_bytes": inspected["Size"], "architecture": "arm64"}
            self.persist()
        role = self.role("runtime", [
            allow(["ecr:GetAuthorizationToken"], "*"),
            allow(["ecr:BatchGetImage", "ecr:GetDownloadUrlForLayer"], self.state["repository"]["repositoryArn"]),
            allow(["logs:DescribeLogGroups"], f"arn:aws-cn:logs:{region}:447150580482:log-group:*"),
            allow(["logs:CreateLogGroup", "logs:CreateLogStream", "logs:DescribeLogStreams", "logs:PutLogEvents"],
                  f"arn:aws-cn:logs:{region}:447150580482:log-group:/aws/bedrock-agentcore/runtimes/{name.replace('-', '_')}*"),
        ])
        control = self.client("bedrock-agentcore-control")
        time.sleep(10)
        for kind in ("baseline", "scale"):
            if kind not in self.state["runtimes"]:
                request = {
                    "agentRuntimeName": (name + "_" + kind).replace("-", "_"),
                    "agentRuntimeArtifact": {"containerConfiguration": {
                        "containerUri": self.state["image"]["immutable_uri"]}},
                    "roleArn": role, "networkConfiguration": {"networkMode": "PUBLIC"},
                    "protocolConfiguration": {"serverProtocol": "HTTP"},
                    "lifecycleConfiguration": {"idleRuntimeSessionTimeout": 60, "maxLifetime": 1800},
                    "clientToken": str(uuid.uuid4()), "tags": {"Experiment": name}}
                self.record(f"create-runtime-{kind}-request.json", request)
                try:
                    result = control.create_agent_runtime(**request)
                except Exception as exc:
                    self.record(f"create-runtime-{kind}-error.json", getattr(exc, "response", {"error": str(exc)}))
                    raise
                self.state["runtimes"][kind] = result
                self.persist()
        for kind, runtime in self.state["runtimes"].items():
            ready = self.wait(lambda r=runtime: control.get_agent_runtime(agentRuntimeId=r["agentRuntimeId"]),
                              "status", {"READY"}, f"runtime-{kind}-ready")
            runtime["configuration"] = ready
            self.persist()
        if getattr(self.args, "runtime_only", False):
            self.log("Runtime deployment READY; load client managed separately")
            return
        resource_arns = [v["agentRuntimeArn"] for v in self.state["runtimes"].values()]
        load_role = self.role("load", [
            allow(["bedrock-agentcore:InvokeAgentRuntime", "bedrock-agentcore:StopRuntimeSession"],
                  resource_arns + [arn + "/*" for arn in resource_arns])])
        time.sleep(10)
        if "code_interpreter" not in self.state:
            request = {"name": name.replace("-", "_") + "_load",
                       "networkConfiguration": {"networkMode": "PUBLIC"},
                       "executionRoleArn": load_role}
            self.record("create-load-client-request.json", request)
            self.state["code_interpreter"] = control.create_code_interpreter(**request)
            self.persist()
        self.wait(lambda: control.get_code_interpreter(
            codeInterpreterId=self.state["code_interpreter"]["codeInterpreterId"]),
            "status", {"READY"}, "load-client-ready")
        self.log("Deployment READY")

    def ci(self, label, name, arguments):
        response = self.client("bedrock-agentcore").invoke_code_interpreter(
            codeInterpreterIdentifier=self.state["code_interpreter"]["codeInterpreterId"],
            sessionId=self.state["load_session"]["sessionId"], name=name, arguments=arguments)
        events = list(response["stream"])
        response["stream"].close()
        result = {"at": now(), "name": name, "events": events,
                  "metadata": response["ResponseMetadata"]}
        # Binary responses are written separately, not coerced to a string.
        if name != "readFiles":
            self.record(f"client-api/{label}.json", result)
        for event in events:
            if "result" not in event or (event["result"].get("isError") and name != "getTask"):
                raise RuntimeError(json.dumps(result, default=str))
        return result

    def run(self):
        if "load_task" in self.state:
            raise RuntimeError("Task already started; use collect, not run")
        data = self.client("bedrock-agentcore")
        if "load_session" not in self.state:
            self.state["load_session_intent"] = self.state["name"] + "-load"
            self.persist()
            self.state["load_session"] = data.start_code_interpreter_session(
                codeInterpreterIdentifier=self.state["code_interpreter"]["codeInterpreterId"],
                name=self.state["load_session_intent"], sessionTimeoutSeconds=1800)
            self.persist()
        # Only outputs identity metadata, never credentials.
        preflight = self.ci("preflight", "executeCommand", {"command":
            "python3 -c 'import boto3,botocore,json,sys; "
            "print(json.dumps({\"python\":sys.version,\"boto3\":boto3.__version__,"
            "\"botocore\":botocore.__version__,"
            "\"identity\":boto3.client(\"sts\",region_name=\"cn-northwest-1\").get_caller_identity()},default=str))'"})
        self.log("China load generator role preflight succeeded")
        settings = {"region": self.args.region, "account": "447150580482",
                    "runtimes": {k: v["agentRuntimeArn"] for k, v in self.state["runtimes"].items()},
                    "cold_samples": 100, "warm_samples": 500, "concurrency": 50,
                    "load_role_name": self.state["roles"]["load"]["RoleName"],
                    "load_client": {"kind": "CodeInterpreter", "region": self.args.region,
                                    "identifier": self.state["code_interpreter"]["codeInterpreterId"],
                                    "session_id": self.state["load_session"]["sessionId"]}}
        self.record("benchmark_config.json", settings)
        source = (ROOT / "benchmark.py").read_text()
        self.record("benchmark-source.json", {"sha256": hashlib.sha256(source.encode()).hexdigest()})
        self.ci("upload", "writeFiles", {"content": [
            {"path": "benchmark.py", "text": source},
            {"path": "benchmark_config.json", "text": json.dumps(settings)},
        ]})
        result = self.ci("start-task", "startCommandExecution", {
            "command": "python3 -u benchmark.py --config benchmark_config.json > benchmark.log 2>&1"})
        task = result["events"][-1]["result"]["structuredContent"]["taskId"]
        self.state["load_task"] = {"taskId": task, "started_at": now()}
        self.persist()
        self.log(f"Benchmark started: {task}")

    def collect(self):
        task = self.state["load_task"]["taskId"]
        response = self.ci("task-latest", "getTask", {"taskId": task})
        status = response["events"][-1]["result"]["structuredContent"].get("taskStatus")
        self.log(f"Benchmark status: {status}")
        filenames = ["benchmark.log", "benchmark_environment.json",
                     "runtime_sessions.json", "requests.jsonl"]
        if status in ("completed", "failed", "canceled"):
            filenames += ["benchmark_summary.json", "benchmark_results.json", "benchmark.py"]
        for name in filenames:
            try:
                result = self.ci("download-" + name, "readFiles", {"paths": [name]})
                found = False
                for event in result["events"]:
                    for item in event["result"].get("content", []):
                        resource = item.get("resource", {})
                        if "text" in resource:
                            content = resource["text"].encode()
                        elif "blob" in resource:
                            content = resource["blob"]
                        else:
                            continue
                        (self.out / name).write_bytes(content)
                        found = True
                if not found:
                    raise RuntimeError("No file content returned")
            except Exception as exc:
                self.record("download-error-" + name + ".json", {"error": str(exc)})
        self.state["load_task"].update(last_status=status, checked_at=now())
        self.persist()
        if (self.out / "benchmark.log").exists():
            self.log((self.out / "benchmark.log").read_text()[-1800:])
        return status

    def cleanup(self):
        control, data = self.client("bedrock-agentcore-control"), self.client("bedrock-agentcore")
        results = []

        def record(action, resource, fn, missing=()):
            try:
                response = fn()
                result = {"action": action, "resource": resource, "status": "OK", "response": response}
            except Exception as exc:
                code = getattr(exc, "response", {}).get("Error", {}).get("Code")
                result = {"action": action, "resource": resource,
                          "status": "ALREADY_ABSENT" if code in missing else "ERROR",
                          "error": getattr(exc, "response", {"message": str(exc)})}
            results.append(result)
            self.record("cleanup.json", {"at": now(), "actions": results})
            return result

        if ("load_task" in self.state and
                self.state["load_task"].get("last_status") not in ("completed", "failed", "canceled")):
            raise RuntimeError("Benchmark is still running; collect first")
        sessions = self.out / "runtime_sessions.json"
        if sessions.exists():
            for sid, session in json.loads(sessions.read_text()).items():
                if not session["stop_confirmed"]:
                    record("stop-runtime-session", sid, lambda s=sid, r=session:
                           data.stop_runtime_session(agentRuntimeArn=r["runtime_arn"],
                                                     runtimeSessionId=s, qualifier="DEFAULT"))
        if "load_session" in self.state:
            record("stop-load-session", self.state["load_session"]["sessionId"],
                   lambda: data.stop_code_interpreter_session(
                       codeInterpreterIdentifier=self.state["code_interpreter"]["codeInterpreterId"],
                       sessionId=self.state["load_session"]["sessionId"]),
                   ("ResourceNotFoundException",))
        if "code_interpreter" in self.state:
            cid = self.state["code_interpreter"]["codeInterpreterId"]
            record("delete-load-interpreter", cid,
                   lambda: control.delete_code_interpreter(codeInterpreterId=cid),
                   ("ResourceNotFoundException",))
        for runtime in self.state["runtimes"].values():
            rid = runtime["agentRuntimeId"]
            record("delete-runtime", rid, lambda r=rid: control.delete_agent_runtime(agentRuntimeId=r),
                   ("ResourceNotFoundException",))
        pending = [(r["agentRuntimeId"], "runtime") for r in self.state["runtimes"].values()]
        if "code_interpreter" in self.state:
            pending.append((self.state["code_interpreter"]["codeInterpreterId"], "interpreter"))
        deadline = time.monotonic() + 600
        while pending and time.monotonic() < deadline:
            next_pending = []
            for identifier, kind in pending:
                try:
                    response = (control.get_agent_runtime(agentRuntimeId=identifier) if kind == "runtime"
                                else control.get_code_interpreter(codeInterpreterId=identifier))
                    self.record(f"cleanup-final-{identifier}.json", response)
                    if response.get("status") != "DELETED":
                        next_pending.append((identifier, kind))
                except control.exceptions.ResourceNotFoundException as exc:
                    self.record(f"cleanup-final-{identifier}.json",
                                {"at": now(), "deleted": True, "response": exc.response})
            pending = next_pending
            if pending:
                self.log(f"Waiting for {len(pending)} resource deletions")
                time.sleep(5)
        if pending:
            raise RuntimeError("Resource deletions not confirmed; keep ECR and roles for retry")
        logs = self.client("logs")
        for runtime in self.state["runtimes"].values():
            prefix = "/aws/bedrock-agentcore/runtimes/" + runtime["agentRuntimeId"] + "-"
            for page in logs.get_paginator("describe_log_groups").paginate(logGroupNamePrefix=prefix):
                for group in page.get("logGroups", []):
                    log_name = group["logGroupName"]
                    record("delete-test-log-group", log_name,
                           lambda n=log_name: logs.delete_log_group(logGroupName=n))
        if "repository_intent" in self.state:
            repo = self.state["repository_intent"]
            record("delete-repository", repo, lambda: self.client("ecr").delete_repository(
                repositoryName=repo, force=True), ("RepositoryNotFoundException",))
        iam = self.client("iam")
        for role_name in self.state.get("role_intents", {}).values():
            record("delete-inline-policy", role_name, lambda n=role_name: iam.delete_role_policy(
                RoleName=n, PolicyName="RuntimeValidation"), ("NoSuchEntity",))
            record("delete-role", role_name, lambda n=role_name: iam.delete_role(RoleName=n), ("NoSuchEntity",))
        self.state["cleanup_completed_at"] = now()
        self.persist()
        if any(row["status"] == "ERROR" for row in results):
            raise RuntimeError("Cleanup errors; inspect cleanup.json")
        self.log("All experiment resources deleted")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["deploy", "run", "collect", "cleanup"])
    parser.add_argument("--profile", default="agentcore_cn")
    parser.add_argument("--region", default="cn-northwest-1")
    parser.add_argument("--output", required=True)
    parser.add_argument("--runtime-only", action="store_true",
                        help="Deploy the runtimes without a Code Interpreter load generator.")
    args = parser.parse_args()
    lab = Lab(args)
    getattr(lab, args.action)()


if __name__ == "__main__":
    main()
