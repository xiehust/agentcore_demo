#!/usr/bin/env python3
"""Create an isolated EFS mount, verify cross-session persistence, and clean up."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import time
import traceback
import uuid

import boto3
from botocore.config import Config

ROOT = Path(__file__).resolve().parent


def now():
    return datetime.now(timezone.utc).isoformat()


def save(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, default=str) + "\n")
    tmp.replace(path)


class Lab:
    def __init__(self, out, region="cn-northwest-1", subnet_id="subnet-01c597ab731966fe0",
                 profile_name="agentcore_cn"):
        self.out = Path(out).resolve()
        self.out.mkdir(parents=True, exist_ok=True)
        self.state_path = self.out / "resources.json"
        self.region = region
        self.subnet_id = subnet_id
        self.aws = boto3.Session(profile_name=profile_name, region_name=region)
        self.config = Config(connect_timeout=10, read_timeout=180, retries={"total_max_attempts": 1})
        self.state = json.loads(self.state_path.read_text()) if self.state_path.exists() else {
            "name": "cn_ci_efs_" + uuid.uuid4().hex[:10], "created_at": now(), "sessions": []}
        identity = self.client("sts").get_caller_identity()
        assert identity["Account"] == "447150580482"
        self.state["identity"] = identity
        self.persist()

    def client(self, service):
        return self.aws.client(service, config=self.config)

    def persist(self):
        save(self.state_path, self.state)

    def log(self, text):
        print(f"[{now()}] {text}", flush=True)

    def call(self, label, client, method, **request):
        record = {"at": now(), "operation": method, "request": request}
        started = time.monotonic()
        try:
            response = getattr(client, method)(**request)
            if "stream" in response:
                stream = response.pop("stream")
                try:
                    record["events"] = list(stream)
                finally:
                    stream.close()
            record["response"] = response
            return record
        except Exception as exc:
            record["error"] = {"type": type(exc).__name__, "message": str(exc),
                               "response": getattr(exc, "response", None)}
            raise
        finally:
            record["elapsed_s"] = time.monotonic() - started
            save(self.out / "api" / (label + ".json"), record)

    def wait_available(self, label, fetch, select, field, seconds=600):
        deadline = time.monotonic() + seconds
        while True:
            value = fetch()
            save(self.out / (label + ".json"), value)
            status = select(value)[field]
            self.log(f"{label}: {status}")
            if status in ("available", "READY"):
                return select(value)
            if "failed" in status.lower() or time.monotonic() >= deadline:
                raise RuntimeError(json.dumps(value, default=str))
            time.sleep(5)

    def deploy(self):
        ec2, efs, iam = self.client("ec2"), self.client("efs"), self.client("iam")
        name = self.state["name"]
        # Use the configured subnet without changing its routes.
        subnet = ec2.describe_subnets(SubnetIds=[self.subnet_id])["Subnets"][0]
        self.state["subnet"] = subnet
        self.persist()
        if "security_groups" not in self.state:
            self.state["security_groups"] = {}
            self.persist()
        for purpose in ("client", "efs"):
            if purpose not in self.state["security_groups"]:
                response = self.call(f"create-sg-{purpose}", ec2, "create_security_group",
                    GroupName=name + "-" + purpose, Description="Temporary Code Interpreter EFS verification",
                    VpcId=subnet["VpcId"],
                    TagSpecifications=[{"ResourceType": "security-group",
                                        "Tags": [{"Key": "Experiment", "Value": name}]}])["response"]
                self.state["security_groups"][purpose] = response["GroupId"]
                self.persist()
        if not self.state.get("nfs_rule"):
            self.call("authorize-nfs", ec2, "authorize_security_group_ingress",
                      GroupId=self.state["security_groups"]["efs"],
                      IpPermissions=[{"IpProtocol": "tcp", "FromPort": 2049, "ToPort": 2049,
                                      "UserIdGroupPairs": [{"GroupId": self.state["security_groups"]["client"]}]}])
            self.state["nfs_rule"] = True
            self.persist()
        if "file_system" not in self.state:
            response = self.call("create-efs", efs, "create_file_system",
                CreationToken=name, Encrypted=True, PerformanceMode="generalPurpose",
                ThroughputMode="bursting", Tags=[{"Key": "Name", "Value": name}])["response"]
            self.state["file_system"] = response
            self.persist()
        fs = self.state["file_system"]
        self.wait_available("efs-ready", lambda: efs.describe_file_systems(FileSystemId=fs["FileSystemId"]),
                            lambda r: r["FileSystems"][0], "LifeCycleState")
        if "access_point" not in self.state:
            response = self.call("create-access-point", efs, "create_access_point",
                ClientToken=name, FileSystemId=fs["FileSystemId"],
                PosixUser={"Uid": 1000, "Gid": 1000},
                RootDirectory={"Path": "/ci-verification",
                               "CreationInfo": {"OwnerUid": 1000, "OwnerGid": 1000, "Permissions": "0770"}},
                Tags=[{"Key": "Name", "Value": name}])["response"]
            self.state["access_point"] = response
            self.persist()
        if "mount_target" not in self.state:
            response = self.call("create-mount-target", efs, "create_mount_target",
                FileSystemId=fs["FileSystemId"], SubnetId=subnet["SubnetId"],
                SecurityGroups=[self.state["security_groups"]["efs"]])["response"]
            self.state["mount_target"] = response
            self.persist()
        self.wait_available("mount-target-ready",
            lambda: efs.describe_mount_targets(MountTargetId=self.state["mount_target"]["MountTargetId"]),
            lambda r: r["MountTargets"][0], "LifeCycleState")
        self.wait_available("access-point-ready", lambda: efs.describe_access_points(
            AccessPointId=self.state["access_point"]["AccessPointId"]),
            lambda r: r["AccessPoints"][0], "LifeCycleState")
        if "role" not in self.state:
            response = self.call("create-role", iam, "create_role", RoleName=name,
                AssumeRolePolicyDocument=json.dumps({"Version": "2012-10-17", "Statement": [{
                    "Effect": "Allow", "Principal": {"Service": "bedrock-agentcore.amazonaws.com"},
                    "Action": "sts:AssumeRole",
                    "Condition": {"StringEquals": {"aws:SourceAccount": "447150580482"},
                                  "ArnLike": {"aws:SourceArn":
                                      f"arn:aws-cn:bedrock-agentcore:{self.region}:447150580482:*"}}}]}),
                Tags=[{"Key": "Experiment", "Value": name}])["response"]
            self.state["role"] = response["Role"]
            self.persist()
        access = self.state["access_point"]["AccessPointArn"]
        permission = {"Version": "2012-10-17", "Statement": [
            {"Effect": "Allow", "Action": ["elasticfilesystem:ClientMount", "elasticfilesystem:ClientWrite"],
             "Resource": fs["FileSystemArn"],
             "Condition": {"StringEquals": {"elasticfilesystem:AccessPointArn": access}}},
            {"Effect": "Allow", "Action": ["elasticfilesystem:DescribeMountTargets",
                                          "elasticfilesystem:DescribeAccessPoints",
                                          "elasticfilesystem:DescribeFileSystems"],
             "Resource": "*"},
            {"Effect": "Allow", "Action": ["ec2:CreateNetworkInterface", "ec2:DeleteNetworkInterface",
                "ec2:DescribeNetworkInterfaces", "ec2:DescribeSubnets", "ec2:DescribeSecurityGroups",
                "ec2:DescribeVpcs", "ec2:CreateTags", "ec2:AssignPrivateIpAddresses",
                "ec2:UnassignPrivateIpAddresses"],
             "Resource": "*", "Condition": {"StringEquals": {"aws:RequestedRegion": self.region}}},
        ]}
        self.call("put-role-policy", iam, "put_role_policy", RoleName=name,
                  PolicyName="EfsVerification", PolicyDocument=json.dumps(permission))
        time.sleep(10)
        if "interpreter" not in self.state:
            request = {
                "name": name, "executionRoleArn": self.state["role"]["Arn"],
                "networkConfiguration": {"networkMode": "VPC", "vpcConfig": {
                    "subnets": [subnet["SubnetId"]],
                    "securityGroups": [self.state["security_groups"]["client"]]}},
                "filesystemConfigurations": [{"efsConfiguration": {
                    "accessPointArn": access, "fileSystemArn": fs["FileSystemArn"], "mountPath": "/mnt/efs"}}],
                "tags": {"Experiment": name}}
            response = self.call("create-interpreter", self.client("bedrock-agentcore-control"),
                                  "create_code_interpreter", **request)["response"]
            self.state["interpreter"] = response
            self.persist()
        self.wait_available("interpreter-ready", lambda: self.client("bedrock-agentcore-control").get_code_interpreter(
            codeInterpreterId=self.state["interpreter"]["codeInterpreterId"]), lambda r: r, "status")

    def start(self, label):
        response = self.call(label + "-start", self.client("bedrock-agentcore"),
            "start_code_interpreter_session",
            codeInterpreterIdentifier=self.state["interpreter"]["codeInterpreterId"],
            name=self.state["name"] + "-" + label, sessionTimeoutSeconds=600)["response"]
        self.state["sessions"].append({"id": response["sessionId"], "stopped": False, "label": label})
        self.persist()
        return response["sessionId"]

    def stop(self, sid):
        row = next(r for r in self.state["sessions"] if r["id"] == sid)
        if not row["stopped"]:
            self.call(row["label"] + "-stop", self.client("bedrock-agentcore"), "stop_code_interpreter_session",
                      codeInterpreterIdentifier=self.state["interpreter"]["codeInterpreterId"], sessionId=sid)
            row["stopped"] = True
            self.persist()

    def code(self, sid, label, code):
        record = self.call(label, self.client("bedrock-agentcore"), "invoke_code_interpreter",
            codeInterpreterIdentifier=self.state["interpreter"]["codeInterpreterId"],
            sessionId=sid, name="executeCode", arguments={"language": "python", "code": code})
        results = [event["result"] for event in record["events"] if "result" in event]
        assert len(results) == len(record["events"]) and results, record
        assert not any(r.get("isError") for r in results), results
        output = "\n".join(r.get("structuredContent", {}).get("stdout", "") for r in results)
        return json.loads(output.strip().splitlines()[-1])

    def verify(self):
        token = uuid.uuid4().hex
        content = "EFS persistence proof " + token + "\n"
        filename = "/mnt/efs/proof-" + token + ".txt"
        a = self.start("session-a")
        try:
            first = self.code(a, "session-a-write", f"""
import os, pathlib, json, hashlib
p = pathlib.Path({filename!r})
p.write_text({content!r})
with p.open("rb") as f: os.fsync(f.fileno())
mounts = [line for line in pathlib.Path("/proc/mounts").read_text().splitlines() if "/mnt/efs" in line]
print(json.dumps({{"exists":p.exists(),"sha256":hashlib.sha256(p.read_bytes()).hexdigest(),
                  "mounts":mounts,"is_mount":os.path.ismount("/mnt/efs"),"stat":{{"uid":p.stat().st_uid,"gid":p.stat().st_gid}}}}))
""")
            assert first["sha256"] == hashlib.sha256(content.encode()).hexdigest()
            assert first["is_mount"] and any("nfs" in line for line in first["mounts"]), first
        finally:
            self.stop(a)
        b = self.start("session-b")
        try:
            second = self.code(b, "session-b-read-append", f"""
import os, pathlib, json, hashlib
p = pathlib.Path({filename!r})
previous = p.read_text()
with p.open("a") as f:
    f.write("APPENDED_BY_SESSION_B\\n"); f.flush(); os.fsync(f.fileno())
print(json.dumps({{"previous":previous,"sha256":hashlib.sha256(p.read_bytes()).hexdigest()}}))
""")
            assert second["previous"] == content
        finally:
            self.stop(b)
        c = self.start("session-c")
        try:
            third = self.code(c, "session-c-read", f"""
import pathlib, json, hashlib
p = pathlib.Path({filename!r})
print(json.dumps({{"content":p.read_text(),"sha256":hashlib.sha256(p.read_bytes()).hexdigest()}}))
""")
            assert third["content"] == content + "APPENDED_BY_SESSION_B\n"
            assert third["sha256"] == second["sha256"]
        finally:
            self.stop(c)
        result = {"status": "PASS", "at": now(), "region": self.region,
                  "session_ids": [a, b, c], "session_a": first, "session_b": second, "session_c": third}
        save(self.out / "result.json", result)
        self.log("EFS mount and cross-session persistence: PASS")

    def cleanup(self):
        ec2, efs, iam, control = (self.client(n) for n in
                                 ("ec2", "efs", "iam", "bedrock-agentcore-control"))
        events = []
        def run(action, identifier, fn, absent):
            try:
                response = fn()
                item = {"action": action, "resource": identifier, "status": "OK", "response": response}
            except Exception as exc:
                code = getattr(exc, "response", {}).get("Error", {}).get("Code")
                item = {"action": action, "resource": identifier,
                        "status": "ABSENT" if code in absent else "ERROR",
                        "error": getattr(exc, "response", {"message": str(exc)})}
            events.append(item)
            save(self.out / "cleanup.json", {"at": now(), "actions": events})
            return item
        for row in self.state["sessions"]:
            if not row["stopped"]:
                run("stop-session", row["id"], lambda r=row: self.stop(r["id"]), ())
        if "interpreter" in self.state:
            cid = self.state["interpreter"]["codeInterpreterId"]
            run("delete-interpreter", cid, lambda: control.delete_code_interpreter(codeInterpreterId=cid),
                ("ResourceNotFoundException",))
            deadline = time.monotonic() + 300
            while time.monotonic() < deadline:
                try:
                    response = control.get_code_interpreter(codeInterpreterId=cid)
                    save(self.out / "interpreter-final.json", response)
                    if response["status"] == "DELETED":
                        break
                except control.exceptions.ResourceNotFoundException as exc:
                    save(self.out / "interpreter-final.json", {"deleted": True, "response": exc.response})
                    break
                time.sleep(5)
            else:
                raise RuntimeError("Interpreter deletion not confirmed")
        if "mount_target" in self.state:
            mid = self.state["mount_target"]["MountTargetId"]
            run("delete-mount-target", mid, lambda: efs.delete_mount_target(MountTargetId=mid),
                ("MountTargetNotFound",))
            deadline = time.monotonic() + 300
            while time.monotonic() < deadline:
                try:
                    efs.describe_mount_targets(MountTargetId=mid)
                except efs.exceptions.MountTargetNotFound:
                    break
                time.sleep(5)
            else:
                raise RuntimeError("Mount target deletion not confirmed")
        if "access_point" in self.state:
            aid = self.state["access_point"]["AccessPointId"]
            run("delete-access-point", aid, lambda: efs.delete_access_point(AccessPointId=aid),
                ("AccessPointNotFound",))
            deadline = time.monotonic() + 180
            while time.monotonic() < deadline:
                try:
                    efs.describe_access_points(AccessPointId=aid)
                except efs.exceptions.AccessPointNotFound:
                    break
                time.sleep(3)
            else:
                raise RuntimeError("Access point deletion not confirmed")
        if "file_system" in self.state:
            fid = self.state["file_system"]["FileSystemId"]
            run("delete-file-system", fid, lambda: efs.delete_file_system(FileSystemId=fid),
                ("FileSystemNotFound",))
            deadline = time.monotonic() + 180
            while time.monotonic() < deadline:
                try:
                    response = efs.describe_file_systems(FileSystemId=fid)
                    save(self.out / "efs-final.json", response)
                except efs.exceptions.FileSystemNotFound as exc:
                    save(self.out / "efs-final.json", {"deleted": True, "response": exc.response})
                    break
                time.sleep(3)
            else:
                raise RuntimeError("EFS file system deletion not confirmed")
        # Wait for service-managed ENIs to release before removing their SGs.
        for kind in ("efs", "client"):
            sg = self.state.get("security_groups", {}).get(kind)
            if not sg:
                continue
            deadline = time.monotonic() + getattr(self, "network_cleanup_wait_seconds", 300)
            while True:
                try:
                    response = ec2.delete_security_group(GroupId=sg)
                    events.append({"action": "delete-sg", "resource": sg, "status": "OK", "response": response})
                    break
                except Exception as exc:
                    code = getattr(exc, "response", {}).get("Error", {}).get("Code")
                    if code == "InvalidGroup.NotFound":
                        break
                    if code == "DependencyViolation" and time.monotonic() < deadline:
                        self.log(f"Waiting for ENIs to release {sg}")
                        time.sleep(10)
                        continue
                    events.append({"action": "delete-sg", "resource": sg, "status": "ERROR",
                                   "error": getattr(exc, "response", {"message": str(exc)})})
                    break
            save(self.out / "cleanup.json", {"at": now(), "actions": events})
        if "role" in self.state and not any(e["status"] == "ERROR" and e["action"] == "delete-sg"
                                            for e in events):
            role = self.state["role"]["RoleName"]
            run("delete-policy", role, lambda: iam.delete_role_policy(RoleName=role, PolicyName="EfsVerification"),
                ("NoSuchEntity",))
            run("delete-role", role, lambda: iam.delete_role(RoleName=role), ("NoSuchEntity",))
        self.state["cleanup_finished_at"] = now()
        self.persist()
        if any(e["status"] == "ERROR" for e in events):
            raise RuntimeError("Cleanup needs attention; see cleanup.json")
        self.log("Test resources cleaned up")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["all", "deploy", "verify", "cleanup"], default="all", nargs="?")
    parser.add_argument("--output", default=str(ROOT / "results/20260922"))
    parser.add_argument("--region", default="cn-northwest-1")
    parser.add_argument("--subnet-id", default="subnet-01c597ab731966fe0")
    args = parser.parse_args()
    lab = Lab(args.output, region=args.region, subnet_id=args.subnet_id)
    try:
        if args.action in ("all", "deploy"):
            lab.deploy()
        if args.action in ("all", "verify"):
            lab.verify()
    except Exception as exc:
        save(lab.out / "failure.json", {"at": now(), "error": str(exc),
                                       "response": getattr(exc, "response", None),
                                       "traceback": traceback.format_exc()})
        raise
    finally:
        if args.action in ("all", "cleanup"):
            lab.cleanup()


if __name__ == "__main__":
    main()
