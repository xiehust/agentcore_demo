#!/usr/bin/env python3
"""Runs on each regional EC2. Presigned witness URLs never enter saved evidence."""
import concurrent.futures
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shlex
import tarfile
import threading
import time
import traceback
import urllib.request

import boto3
import botocore
from botocore.config import Config

ROOT = Path(__file__).resolve().parent
OUT = ROOT / "evidence"

TTL_WORKER = '''
import json, os, sys, time, urllib.request
cfg = json.load(open(sys.argv[1]))
started = time.monotonic()
sequence = 0
def put(url, value):
    request = urllib.request.Request(url, method="PUT",
        data=json.dumps(value).encode(), headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=4) as response:
        if response.status != 200:
            raise RuntimeError("witness write unsuccessful")
while time.monotonic() - started < cfg["work_seconds"]:
    put(cfg["heartbeat_url"], {"sequence": sequence, "elapsed_s": time.monotonic()-started,
                               "pid": os.getpid(), "unix_s": time.time()})
    sequence += 1
    time.sleep(1)
put(cfg["done_url"], {"completed": True, "elapsed_s": time.monotonic()-started,
                     "sequence": sequence, "unix_s": time.time()})
print("WORKER_FINISHED")
'''

PROCESS_WORKER = '''
import json, os, pathlib, signal, subprocess, sys, time
role, mode, folder, duration = sys.argv[1], sys.argv[2], pathlib.Path(sys.argv[3]), float(sys.argv[4])
folder.mkdir(parents=True, exist_ok=True)
if mode == "ignore":
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
info = {"pid": os.getpid(), "ppid": os.getppid(), "pgid": os.getpgrp(),
        "start_ticks": pathlib.Path("/proc/self/stat").read_text().split()[21]}
(folder / (role + "-pid.json")).write_text(json.dumps(info))
if role == "parent":
    subprocess.Popen([sys.executable, __file__, "child", mode, str(folder), str(duration)])
started = time.monotonic()
counter = 0
while time.monotonic()-started < duration:
    (folder / (role+"-heartbeat.txt")).write_text(str(counter))
    counter += 1
    time.sleep(0.25)
(folder / (role+"-done.txt")).write_text("COMPLETED")
'''


def utc():
    return datetime.now(timezone.utc).isoformat()


def safe(value):
    if isinstance(value, dict):
        return {str(k): safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [safe(v) for v in value]
    if isinstance(value, str) and ("X-Amz-Signature=" in value or "X-Amz-Security-Token=" in value):
        return "[REDACTED_SIGNED_CAPABILITY]"
    if isinstance(value, datetime):
        return value.isoformat()
    return value


def save(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(safe(data), indent=2, ensure_ascii=False, default=str) + "\n")
    temporary.replace(path)


def parse_result(record):
    results = [r["result"] for r in record.get("events", []) if "result" in r]
    return {"stdout": "\n".join(r.get("structuredContent", {}).get("stdout", "") for r in results),
            "stderr": "\n".join(r.get("structuredContent", {}).get("stderr", "") for r in results),
            "is_error": any(r.get("isError", False) for r in results),
            "exit_codes": [r.get("structuredContent", {}).get("exitCode") for r in results],
            "results": results}


class Lab:
    def __init__(self, cfg):
        self.cfg = cfg
        self.lock = threading.RLock()
        self.sessions = {}
        self.aws = boto3.Session(region_name=cfg["region"])
        self.data = self.aws.client("bedrock-agentcore", config=Config(
            connect_timeout=10, read_timeout=260, retries={"total_max_attempts": 1}, max_pool_connections=30))
        self.s3 = self.aws.client("s3", config=Config(signature_version="s3v4",
            connect_timeout=5, read_timeout=10, retries={"total_max_attempts": 1}, max_pool_connections=20))
        self.identity = self.aws.client("sts").get_caller_identity()
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        token = opener.open(urllib.request.Request("http://169.254.169.254/latest/api/token",
            method="PUT", headers={"X-aws-ec2-metadata-token-ttl-seconds": "60"}), timeout=3).read().decode()
        document = json.loads(opener.open(urllib.request.Request(
            "http://169.254.169.254/latest/dynamic/instance-identity/document",
            headers={"X-aws-ec2-metadata-token": token}), timeout=3).read())
        assert document["region"] == cfg["region"] and document["instanceId"] == cfg["instance_id"]
        assert self.identity["Account"] == cfg["account"]
        timeout_shape = self.data.meta.service_model.operation_model(
            "StartCodeInterpreterSession").input_shape.members["sessionTimeoutSeconds"]
        save(OUT / "environment.json", {"at": utc(), "imds_identity": document, "sts": self.identity,
            "boto3": boto3.__version__, "botocore": botocore.__version__,
            "endpoint": self.data.meta.endpoint_url, "client_read_timeout_s": 260,
            "session_timeout_documentation": timeout_shape.documentation,
            "invoke_argument_fields": list(self.data.meta.service_model.operation_model(
                "InvokeCodeInterpreter").input_shape.members["arguments"].members)})

    def api(self, label, method, **kwargs):
        # Record source digests instead of source bodies: some files contain short-lived URLs.
        request = {k: v for k, v in kwargs.items() if k != "arguments"}
        if "arguments" in kwargs:
            args = kwargs["arguments"]
            request["arguments"] = {k: v for k, v in args.items() if k not in ("code", "command", "content")}
            for field in ("code", "command"):
                if field in args:
                    request["arguments"][field + "_sha256"] = hashlib.sha256(args[field].encode()).hexdigest()
            if "content" in args:
                request["arguments"]["uploaded_paths"] = [r["path"] for r in args["content"]]
        record = {"at": utc(), "operation": method, "request": request}
        started = time.monotonic()
        try:
            response = getattr(self.data, method)(**kwargs)
            stream = response.pop("stream", None)
            record["response"] = response
            if stream is not None:
                record["events"] = []
                try:
                    for event in stream:
                        record["events"].append(event)
                finally:
                    stream.close()
        except Exception as exc:
            record["error"] = {"type": type(exc).__name__, "message": str(exc),
                               "response": getattr(exc, "response", None)}
        record["elapsed_s"] = time.monotonic() - started
        record = safe(record)
        save(OUT / "api" / (label + ".json"), record)
        return record

    def invoke(self, sid, label, name, args):
        return self.api(label, "invoke_code_interpreter",
            codeInterpreterIdentifier=self.sessions[sid]["interpreter"], sessionId=sid,
            name=name, arguments=args)

    def start(self, label, identifier, ttl):
        record = self.api(label + "-start", "start_code_interpreter_session",
            codeInterpreterIdentifier=identifier, name="timeout-retest-" + label,
            sessionTimeoutSeconds=ttl)
        if "error" in record:
            raise RuntimeError(json.dumps(record["error"]))
        response = record["response"]
        sid = response["sessionId"]
        with self.lock:
            self.sessions[sid] = {"label": label, "interpreter": identifier, "ttl": ttl,
                                  "created_at": response["createdAt"], "stop_acknowledged": False}
            save(OUT / "sessions.json", self.sessions)
        return sid

    def stop(self, sid):
        row = self.sessions[sid]
        result = self.api(row["label"] + "-cleanup-stop", "stop_code_interpreter_session",
            codeInterpreterIdentifier=row["interpreter"], sessionId=sid)
        row["cleanup_at"] = utc()
        row["stop_acknowledged"] = "error" not in result
        row["cleanup_result"] = result
        with self.lock:
            save(OUT / "sessions.json", self.sessions)

    def get_witness(self, key):
        try:
            r = self.s3.get_object(Bucket=self.cfg["bucket"], Key=key)
            body = json.loads(r["Body"].read())
            r["Body"].close()
            return {"body": body, "last_modified": r["LastModified"].isoformat(), "etag": r["ETag"]}
        except self.s3.exceptions.NoSuchKey:
            return None

    def native_ttl(self, mode):
        label = "ttl-" + mode
        sid = self.start(label, self.cfg["interpreter"], self.cfg["ttl_seconds"])
        created = datetime.fromisoformat(self.sessions[sid]["created_at"]).timestamp()
        keys = {name: f"witness/{label}/{name}.json" for name in ("heartbeat", "done", "probe")}
        urls = {name: self.s3.generate_presigned_url("put_object",
            Params={"Bucket": self.cfg["bucket"], "Key": key, "ContentType": "application/json"},
            ExpiresIn=900) for name, key in keys.items()}
        try:
            setup = self.invoke(sid, label + "-cwd", "executeCode",
                                {"language": "python", "code": "import os\nprint(os.getcwd())"})
            cwd = parse_result(setup)["stdout"].strip()
            assert cwd.startswith("/")
            upload = self.invoke(sid, label + "-upload", "writeFiles", {"content": [
                {"path": "ttl-worker.py", "text": TTL_WORKER},
                {"path": "ttl-config.json", "text": json.dumps({
                    "heartbeat_url": urls["heartbeat"], "done_url": urls["done"],
                    "work_seconds": self.cfg["work_seconds"]})},
            ]})
            if "error" in upload or parse_result(upload)["is_error"]:
                raise RuntimeError("Worker upload failed")
            samples = []
            first_terminal = None
            task_id = None
            invoke_future = None
            pool = None
            launch = time.monotonic()
            if mode == "executeCode":
                code = "import runpy, sys\nsys.argv=['ttl-worker.py','ttl-config.json']\n_ttl_result=runpy.run_path('ttl-worker.py',run_name='__main__')"
                pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
                invoke_future = pool.submit(self.invoke, sid, label + "-execute", "executeCode",
                                             {"language": "python", "code": code})
            else:
                record = self.invoke(sid, label + "-execute", "startCommandExecution",
                                     {"command": "python3 " + shlex.quote(cwd + "/ttl-worker.py")
                                      + " " + shlex.quote(cwd + "/ttl-config.json")})
                task_id = parse_result(record)["results"][-1]["structuredContent"]["taskId"]
            authorization_probe = None
            last_activity = 0
            while time.monotonic() - launch < self.cfg["observe_seconds"]:
                elapsed = time.time() - created
                state = self.api(label + f"-get-session-{len(samples):03d}",
                    "get_code_interpreter_session", codeInterpreterIdentifier=self.cfg["interpreter"], sessionId=sid)
                status = state.get("response", {}).get("status")
                error_code = state.get("error", {}).get("response", {}).get("Error", {}).get("Code")
                terminal = status == "TERMINATED" or error_code == "ResourceNotFoundException"
                if terminal and first_terminal is None:
                    first_terminal = elapsed
                heartbeat = self.get_witness(keys["heartbeat"])
                done = self.get_witness(keys["done"])
                samples.append({"at": utc(), "elapsed_since_creation_s": elapsed,
                                "session_status": status, "session_error": error_code,
                                "heartbeat": heartbeat, "done": done})
                save(OUT / (label + "-observations.json"), samples)
                if mode == "async" and not terminal and elapsed - last_activity >= 10:
                    self.invoke(sid, label + f"-active-get-task-{len(samples):03d}", "getTask", {"taskId": task_id})
                    last_activity = elapsed
                if elapsed >= self.cfg["ttl_seconds"] + 20 and authorization_probe is None:
                    # A same-expiry URL signed by the same EC2 credentials still works after session termination.
                    request = urllib.request.Request(urls["probe"], method="PUT",
                        data=json.dumps({"at": utc(), "after_ttl": True}).encode(),
                        headers={"Content-Type": "application/json"})
                    with urllib.request.urlopen(request, timeout=5) as response:
                        authorization_probe = {"at": utc(), "http_status": response.status}
                time.sleep(5)
            invocation = invoke_future.result(timeout=60) if invoke_future else record
            if pool:
                pool.shutdown()
            present = [r for r in samples if r["heartbeat"]]
            post = [r for r in samples if r["elapsed_since_creation_s"] >= self.cfg["ttl_seconds"] + 20]
            sequences = {r["heartbeat"]["body"]["sequence"] for r in post if r["heartbeat"]}
            passed = (first_terminal is not None and self.cfg["ttl_seconds"] - 2 <= first_terminal <= self.cfg["ttl_seconds"] + 30
                      and len(present) >= 3 and len(post) >= 3 and len(sequences) == 1
                      and not any(r["done"] for r in samples)
                      and authorization_probe is not None and authorization_probe["http_status"] == 200
                      and samples[-1]["elapsed_since_creation_s"] > self.cfg["work_seconds"] + 15)
            result = {"status": "PASS" if passed else "FAIL", "mode": mode, "session_id": sid,
                "ttl_s": self.cfg["ttl_seconds"], "planned_work_s": self.cfg["work_seconds"],
                "first_terminal_s": first_terminal, "last_observation_s": samples[-1]["elapsed_since_creation_s"],
                "heartbeat_updates_observed": len({r["heartbeat"]["body"]["sequence"] for r in present}),
                "post_ttl_heartbeat_sequences": sorted(sequences), "completion_marker_seen": any(r["done"] for r in samples),
                "presigned_authorization_probe": authorization_probe,
                "last_heartbeat": present[-1]["heartbeat"] if present else None,
                "invocation": invocation, "manual_stop_before_observation_end": False,
                "signed_urls_saved": False}
            save(OUT / (label + "-result.json"), result)
            print(label, result["status"], "terminal at", first_terminal, flush=True)
            return result
        finally:
            self.stop(sid)

    def inspect_processes(self, sid, label, folder):
        code = f'''
import json, pathlib, os
folder = pathlib.Path({folder!r})
result = {{}}
for role in ("parent","child"):
    path = folder/(role+"-pid.json")
    info = json.loads(path.read_text()) if path.exists() else None
    alive = False
    state = None
    if info:
        try:
            fields = pathlib.Path("/proc/"+str(info["pid"])+"/stat").read_text().split()
            state = fields[2]
            alive = fields[21] == info["start_ticks"] and state != "Z"
        except FileNotFoundError:
            pass
    heartbeat = folder/(role+"-heartbeat.txt")
    result[role] = {{"process":info,"alive":alive,"state":state,
                     "heartbeat":heartbeat.read_text() if heartbeat.exists() else None,
                     "done":(folder/(role+"-done.txt")).exists()}}
print(json.dumps(result))
'''
        record = self.invoke(sid, label, "executeCode", {"language": "python", "code": code})
        result = parse_result(record)
        assert not result["is_error"], result
        return json.loads(result["stdout"].strip().splitlines()[-1])

    def command_deadline(self):
        sid = self.start("command-deadlines", "aws.codeinterpreter.v1", 300)
        try:
            self.invoke(sid, "command-worker-upload", "writeFiles",
                        {"content": [{"path": "process-worker.py", "text": PROCESS_WORKER}]})
            setup = self.invoke(sid, "command-setup", "executeCode", {"language": "python",
                "code": "import json, pathlib, shutil\nci_timeout_marker=42\nprint(json.dumps({'timeout_binary':shutil.which('timeout'),'cwd':str(pathlib.Path.cwd())}))"})
            environment = json.loads(parse_result(setup)["stdout"].strip().splitlines()[-1])
            assert environment["timeout_binary"]
            worker = environment["cwd"] + "/process-worker.py"
            control_dir = "/tmp/ci-timeout-control"
            control = self.invoke(sid, "command-control", "executeCommand", {"command":
                f"python3 {shlex.quote(worker)} parent normal {control_dir} 10"})
            control_snapshot = self.inspect_processes(sid, "command-control-inspect", control_dir)
            assert parse_result(control)["exit_codes"] == [0]
            assert all(r["done"] for r in control_snapshot.values()), control_snapshot
            cases = []
            latest_finish = time.monotonic()
            for mode, expected in [("normal", 124), ("ignore", 137)]:
                directory = "/tmp/ci-timeout-" + mode
                started = time.monotonic()
                command = f"timeout --signal=TERM --kill-after=2s 5s python3 {shlex.quote(worker)} parent {mode} {directory} 40"
                response = self.invoke(sid, "command-" + mode, "executeCommand", {"command": command})
                first = self.inspect_processes(sid, "command-" + mode + "-first", directory)
                cases.append({"mode": mode, "command": command, "expected_exit": expected,
                              "response": response, "first": first, "directory": directory})
                latest_finish = max(latest_finish, started + 45)
            time.sleep(max(0, latest_finish - time.monotonic()))
            for case in cases:
                final = self.inspect_processes(sid, "command-" + case["mode"] + "-final", case["directory"])
                case["final"] = final
                channel = parse_result(case["response"])
                elapsed = case["response"]["elapsed_s"]
                target = 5 if case["mode"] == "normal" else 7
                case["status"] = "PASS" if (
                    channel["exit_codes"] == [case["expected_exit"]] and target-1 <= elapsed <= target+4
                    and all(case["first"][r]["heartbeat"] is not None for r in ("parent","child"))
                    and all(not final[r]["alive"] and not final[r]["done"]
                            and final[r]["heartbeat"] == case["first"][r]["heartbeat"] for r in ("parent","child"))
                ) else "FAIL"
            recovery = self.invoke(sid, "command-recovery", "executeCode", {
                "language": "python", "code": "print(ci_timeout_marker)"})
            assert parse_result(recovery)["stdout"].strip() == "42"
            result = {"status": "PASS" if all(c["status"] == "PASS" for c in cases) else "FAIL",
                      "session_id": sid, "environment": environment,
                      "control": {"response": control, "snapshot": control_snapshot},
                      "cases": cases, "same_session_recovery": True,
                      "scope": "GNU timeout inside sandbox; not a per-invocation API timeout field"}
            save(OUT / "command-deadlines-result.json", result)
            print("command-deadlines", result["status"], flush=True)
            return result
        finally:
            self.stop(sid)


def main():
    OUT.mkdir(exist_ok=True)
    assert not (OUT / "final.json").exists(), "Use a fresh run directory"
    cfg = json.loads((ROOT / "config.json").read_text())
    lab = Lab(cfg)
    save(OUT / "config.json", cfg)
    save(OUT / "source.json", {"sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest()})
    completed = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
        futures = {pool.submit(lab.native_ttl, mode): "ttl-" + mode for mode in ("executeCode", "async")}
        futures[pool.submit(lab.command_deadline)] = "command-deadlines"
        try:
            while futures:
                done, _ = concurrent.futures.wait(futures, timeout=15, return_when=concurrent.futures.FIRST_COMPLETED)
                for future in done:
                    name = futures.pop(future)
                    try:
                        completed[name] = future.result()
                    except Exception as exc:
                        completed[name] = {"status": "ERROR", "error": str(exc), "traceback": traceback.format_exc()}
                        save(OUT / (name + "-error.json"), completed[name])
                progress = {"at": utc(), "region": cfg["region"], "completed": {
                    k: v["status"] for k, v in completed.items()}, "running": list(futures.values())}
                lab.s3.put_object(Bucket=cfg["bucket"], Key="results/status.json",
                                  Body=json.dumps(progress).encode())
        finally:
            for sid, row in list(lab.sessions.items()):
                if not row.get("cleanup_at"):
                    lab.stop(sid)
    result = {"at": utc(), "region": cfg["region"], "cases": completed,
              "status": "PASS" if all(r["status"] == "PASS" for r in completed.values()) else "REVIEW"}
    save(OUT / "final.json", result)
    # URLs were only in process memory and the now-terminated test sessions.
    for path in OUT.rglob("*"):
        if path.is_file():
            text = path.read_text(errors="replace")
            assert "X-Amz-Signature=" not in text and "X-Amz-Security-Token=" not in text, path.name
    archive = ROOT / "results.tar.gz"
    with tarfile.open(archive, "w:gz") as out:
        out.add(OUT, arcname="evidence")
    lab.s3.upload_file(str(archive), cfg["bucket"], "results/results.tar.gz")
    lab.s3.put_object(Bucket=cfg["bucket"], Key="results/final_status.json",
                      Body=json.dumps({"at": utc(), "status": result["status"],
                                       "cases": {k:v["status"] for k,v in completed.items()}}).encode())
    print(cfg["region"], result["status"], flush=True)


if __name__ == "__main__":
    main()
