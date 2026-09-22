#!/usr/bin/env python3
"""Live China-region Code Interpreter checks; creates and stops billable sessions."""

import argparse
import base64
import concurrent.futures
import hashlib
import json
import math
from pathlib import Path
import platform
import shlex
import statistics
import sys
import tempfile
import textwrap
import threading
import time
import traceback
import uuid
from datetime import datetime, timezone

import boto3
import botocore
from botocore.config import Config


ROOT = Path(__file__).resolve().parent
PRIORITY = {"2.1": "P0", "2.2": "P0", "2.3": "P0", "2.4": "P1",
            "2.5": "P1", "2.6": "P0", "2.7": "P0", "2.9": "P1", "2.10": "P0"}


def utc():
    return datetime.now(timezone.utc).isoformat()


def encode(value):
    if isinstance(value, bytes):
        return {"encoding": "base64", "value": base64.b64encode(value).decode(),
                "bytes": len(value), "sha256": hashlib.sha256(value).hexdigest()}
    if isinstance(value, datetime):
        return value.isoformat()
    raise TypeError(type(value).__name__)


def dump(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=encode) + "\n")
    temporary.replace(path)


def error_info(exc):
    return {"type": type(exc).__name__, "message": str(exc),
            "response": getattr(exc, "response", None)}


def structured(record):
    results = [e["result"] for e in record.get("events", []) if "result" in e]
    return {
        "stdout": "\n".join(r.get("structuredContent", {}).get("stdout", "") for r in results),
        "stderr": "\n".join(r.get("structuredContent", {}).get("stderr", "") for r in results),
        "isError": any(r.get("isError", False) for r in results),
        "results": results,
        "exceptions": [e for e in record.get("events", []) if "result" not in e],
    }


def require_ok(record):
    result = structured(record)
    if result["exceptions"] or result["isError"] or not result["results"]:
        raise AssertionError(json.dumps(result, default=encode))
    codes = [r.get("structuredContent", {}).get("exitCode") for r in result["results"]]
    if any(c not in (None, 0) for c in codes):
        raise AssertionError(f"Nonzero exit codes: {codes}; {result}")
    return result


def json_stdout(record):
    output = require_ok(record)["stdout"]
    return json.loads(output.strip().splitlines()[-1])


def percentile(values, percent):
    values = sorted(values)
    index = (len(values) - 1) * percent / 100
    low, high = math.floor(index), math.ceil(index)
    return values[low] + (values[high] - values[low]) * (index - low)


def distribution(values):
    if not values:
        return {}
    return {**{f"p{p}_s": percentile(values, p) for p in (50, 90, 95)},
            "min_s": min(values), "max_s": max(values), "mean_s": statistics.mean(values)}


class Lab:
    def __init__(self, args):
        self.args = args
        self.out = Path(args.output).resolve()
        self.out.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.aws = boto3.Session(profile_name=args.profile, region_name=args.region)
        self.config = Config(connect_timeout=10, read_timeout=args.observation_seconds + 120,
                             retries={"total_max_attempts": 1}, max_pool_connections=80)
        self.client = self.aws.client("bedrock-agentcore", config=self.config)
        self.ledger_path = self.out / "sessions.json"
        self.ledger = json.loads(self.ledger_path.read_text()) if self.ledger_path.exists() else {}
        self.results_path = self.out / "summary.json"
        self.results = json.loads(self.results_path.read_text()) if self.results_path.exists() else {}

    def log(self, text):
        print(f"[{utc()}] {text}", flush=True)

    def save(self, name, value):
        dump(self.out / name, value)

    def api(self, label, method, **params):
        started = time.monotonic()
        record = {"at": utc(), "operation": method, "request": params}
        try:
            response = getattr(self.client, method)(**params)
            stream = response.pop("stream", None)
            record["response"] = response
            if stream is not None:
                record["events"] = []
                try:
                    for event in stream:
                        record["events"].append(event)
                finally:
                    stream.close()
            return record
        except Exception as exc:
            record["error"] = error_info(exc)
            raise
        finally:
            record["elapsed_s"] = time.monotonic() - started
            self.save(f"api/{label}.json", record)

    def start(self, label, ttl=900):
        request = {"codeInterpreterIdentifier": self.args.identifier,
                   "name": f"cn-ci-{label}-{uuid.uuid4().hex[:8]}",
                   "sessionTimeoutSeconds": ttl, "clientToken": str(uuid.uuid4())}
        # Persist intent first so an interrupted Start can be reconciled by name.
        with self.lock:
            pending = self.out / "start_intents" / f"{label}.json"
            dump(pending, {"at": utc(), "request": request})
        record = self.api(f"{label}-start", "start_code_interpreter_session", **request)
        sid = record["response"]["sessionId"]
        with self.lock:
            self.ledger[sid] = {"label": label, "identifier": self.args.identifier,
                                "created_at": utc(), "stopped": False}
            dump(self.ledger_path, self.ledger)
        return sid, record

    def stop(self, sid):
        with self.lock:
            item = self.ledger[sid]
            if item.get("stopped"):
                return
        try:
            record = self.api(f"{item['label']}-stop", "stop_code_interpreter_session",
                              codeInterpreterIdentifier=item["identifier"], sessionId=sid)
            with self.lock:
                item.update(stopped=True, stopped_at=utc(), stop_response=record["response"])
                item.pop("stop_error", None)
                dump(self.ledger_path, self.ledger)
        except Exception as exc:
            with self.lock:
                item["stop_error"] = error_info(exc)
                dump(self.ledger_path, self.ledger)
            self.log(f"STOP ERROR {sid}: {exc}")

    def invoke(self, sid, label, name, arguments):
        return self.api(label, "invoke_code_interpreter",
                        codeInterpreterIdentifier=self.args.identifier,
                        sessionId=sid, name=name, arguments=arguments)

    def code(self, sid, label, code):
        return self.invoke(sid, label, "executeCode",
                           {"language": "python", "code": textwrap.dedent(code),
                            "clearContext": False})

    def result(self, key, status, details):
        self.results[key] = {"status": status, "priority": PRIORITY.get(key),
                             "at": utc(), "details": details}
        dump(self.results_path, self.results)
        self.log(f"{key}: {status}")

    def check(self, key, fn):
        try:
            details = fn()
            self.result(key, "PASS", details)
        except Exception as exc:
            self.result(key, "FAIL", {"error": error_info(exc), "traceback": traceback.format_exc()})

    def environment(self):
        identity = self.aws.client("sts", config=self.config).get_caller_identity()
        if identity["Account"] != self.args.expected_account:
            raise RuntimeError(f"Unexpected account: {identity['Account']}")
        arguments = self.client.meta.service_model.operation_model(
            "InvokeCodeInterpreter").input_shape.members["arguments"]
        data = {"at": utc(), "profile": self.args.profile, "region": self.args.region,
                "identity": identity, "endpoint": self.client.meta.endpoint_url,
                "identifier": self.args.identifier, "boto3": boto3.__version__,
                "botocore": botocore.__version__, "python": platform.python_version(),
                "client_platform": platform.platform(), "sdk_total_max_attempts": 1,
                "connect_timeout_s": 10, "read_timeout_s": self.config.read_timeout,
                "invoke_argument_fields": list(arguments.members), "command": sys.argv}
        control = self.aws.client("bedrock-agentcore-control", config=self.config)
        try:
            data["resource"] = control.get_code_interpreter(codeInterpreterId=self.args.identifier)
        except Exception as exc:
            data["resource_error"] = error_info(exc)
        filename = ("environment.json" if not (self.out / "environment.json").exists()
                    else f"environment-checks/{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')}.json")
        self.save(filename, data)

    def functional(self):
        a, _ = self.start("functional-a")
        b = None
        try:
            b, _ = self.start("functional-b")
            self.check("2.1", lambda: self.basic(a))
            self.check("2.2", lambda: self.isolation(a, b))
            self.check("2.3", lambda: self.output(a))
            self.check("2.4", lambda: self.libraries(a))
            self.check("2.5", lambda: self.files(a))
            self.check("2.9", lambda: self.state(a, b))
            self.check("2.10", lambda: self.packages(a))
        finally:
            self.stop(a)
            if b:
                self.stop(b)

    def basic(self, sid):
        data = json_stdout(self.code(sid, "2.1-basic", """
            import json, math, datetime, statistics, hashlib
            print(json.dumps({
                "arithmetic": (7 + 5) * 3, "factorial": math.factorial(10),
                "sqrt": math.sqrt(81), "mean": statistics.mean([2, 4, 6]),
                "date": (datetime.date(2026, 9, 21) + datetime.timedelta(days=9)).isoformat(),
                "json_roundtrip": json.loads(json.dumps({"中文": [1, 2]})),
                "sha256": hashlib.sha256(b"agentcore-cn").hexdigest()
            }, ensure_ascii=False))
        """))
        assert data == {"arithmetic": 36, "factorial": 3628800, "sqrt": 9.0,
                        "mean": 4, "date": "2026-09-30", "json_roundtrip": {"中文": [1, 2]},
                        "sha256": hashlib.sha256(b"agentcore-cn").hexdigest()}, data
        return data

    def isolation(self, a, b):
        marker = uuid.uuid4().hex
        sandbox_path = f"/tmp/ci-private-{marker}.txt"
        with tempfile.TemporaryDirectory(prefix="ci-host-marker-") as host:
            host_path = Path(host) / "host-only.txt"
            host_path.write_text(marker)
            assert host_path.read_text() == marker
            probe = f"""
                import pathlib, json, os
                def check_private_path(path):
                    try:
                        with open(path, "rb") as f:
                            return {{"accessible": True, "bytes": len(f.read(128))}}
                    except OSError as e:
                        return {{"accessible": False, "error": type(e).__name__, "errno": e.errno}}
                print(json.dumps({{"host_marker": check_private_path({str(host_path)!r}),
                                  "pid1_root_marker": check_private_path({('/proc/1/root' + str(host_path))!r}),
                                  "cwd": os.getcwd(), "uid": os.getuid()}}))
            """
            host_check = json_stdout(self.code(a, "2.2-host-isolation", probe))
            assert not host_check["host_marker"]["accessible"], host_check
            assert not host_check["pid1_root_marker"]["accessible"], host_check
            own = json_stdout(self.code(a, "2.2-own-file", f"""
                import pathlib, json
                p = pathlib.Path({sandbox_path!r}); p.write_text({marker!r})
                print(json.dumps({{"own_file_readable": p.read_text() == {marker!r}}}))
            """))
            peer = json_stdout(self.code(b, "2.2-peer-file", f"""
                import pathlib, json
                print(json.dumps({{"peer_file_exists": pathlib.Path({sandbox_path!r}).exists()}}))
            """))
            assert own["own_file_readable"] and not peer["peer_file_exists"], (own, peer)
        return {"host_file_existed_locally": True, "host_marker_removed": not host_path.exists(),
                "host_probe": host_check, "own": own, "peer": peer,
                "scope": "Client host marker and cross-session files; no provider-host escape audit."}

    def output(self, sid):
        # This is the Agent-side tool adapter: consumes all SDK events, preserving both channels.
        response = self.code(sid, "2.3-stdout-stderr", """
            import sys
            print("CN_STDOUT_中文_123")
            print("CN_STDERR_错误_456", file=sys.stderr)
        """)
        result = require_ok(response)
        assert result["stdout"].strip() == "CN_STDOUT_中文_123", result
        assert result["stderr"].strip() == "CN_STDERR_错误_456", result
        failure = structured(self.code(sid, "2.3-exception", """
            print("BEFORE_CONTROLLED_ERROR")
            raise ValueError("CN_EXPECTED_EXCEPTION")
        """))
        assert failure["isError"], failure
        assert "BEFORE_CONTROLLED_ERROR" in failure["stdout"], failure
        assert "ValueError" in failure["stderr"] and "CN_EXPECTED_EXCEPTION" in failure["stderr"], failure
        delivered = {"success": result, "controlled_exception": failure,
                     "integration_scope": "Python Agent-side SDK tool adapter; no LLM/framework invocation"}
        self.save("agent_received.json", delivered)
        return delivered

    def libraries(self, sid):
        result = json_stdout(self.code(sid, "2.4-libraries", """
            import json, pathlib, numpy as np, pandas as pd, matplotlib
            matplotlib.use("Agg")
            import matplotlib.pyplot as plt
            matrix = np.array([[1, 2], [3, 4]])
            df = pd.DataFrame({"group": ["a", "a", "b"], "value": [10, 20, 40]})
            grouped = df.groupby("group")["value"].sum().to_dict()
            plt.figure(figsize=(4, 3))
            plt.bar(list(grouped), list(grouped.values()))
            plt.title("China Code Interpreter")
            plt.tight_layout(); plt.savefig("analysis.png"); plt.close()
            image = pathlib.Path("analysis.png").read_bytes()
            print(json.dumps({"versions": {"numpy": np.__version__, "pandas": pd.__version__,
                              "matplotlib": matplotlib.__version__},
                              "matrix_square": (matrix @ matrix).tolist(), "grouped": grouped,
                              "png_bytes": len(image), "png_signature_hex": image[:8].hex()}))
        """))
        assert result["matrix_square"] == [[7, 10], [15, 22]], result
        assert result["grouped"] == {"a": 30, "b": 40}, result
        assert result["png_bytes"] > 100 and result["png_signature_hex"] == "89504e470d0a1a0a", result
        return result

    @staticmethod
    def extract_files(record):
        files = {}
        for result in require_ok(record)["results"]:
            for item in result.get("content", []):
                resource = item.get("resource", {})
                uri = resource.get("uri", item.get("uri", item.get("name", "")))
                if resource:
                    value = resource.get("blob")
                    if value is None and "text" in resource:
                        value = resource["text"].encode()
                else:
                    value = item.get("data")
                if value is not None:
                    files[uri.split("/")[-1]] = value
        return files

    def files(self, sid):
        text = "name,value\n中国区,42\nAgentCore,7\n"
        binary = bytes(range(256)) * 16
        require_ok(self.invoke(sid, "2.5-upload", "writeFiles", {"content": [
            {"path": "input.csv", "text": text},
            {"path": "input.bin", "blob": binary},
        ]}))
        result = json_stdout(self.code(sid, "2.5-file-hashes", """
            import hashlib, json, pathlib
            csv = pathlib.Path("input.csv").read_bytes()
            binary = pathlib.Path("input.bin").read_bytes()
            pathlib.Path("output.csv").write_bytes(csv + "processed,49\\n".encode())
            pathlib.Path("output.bin").write_bytes(binary[::-1])
            print(json.dumps({"input_csv_sha256": hashlib.sha256(csv).hexdigest(),
                              "input_bin_sha256": hashlib.sha256(binary).hexdigest()}))
        """))
        assert result["input_csv_sha256"] == hashlib.sha256(text.encode()).hexdigest()
        assert result["input_bin_sha256"] == hashlib.sha256(binary).hexdigest()
        response = self.invoke(sid, "2.5-download", "readFiles",
                               {"paths": ["output.csv", "output.bin", "analysis.png"]})
        downloaded = self.extract_files(response)
        assert downloaded["output.csv"] == (text + "processed,49\n").encode()
        assert downloaded["output.bin"] == binary[::-1]
        assert downloaded["analysis.png"].startswith(b"\x89PNG\r\n\x1a\n")
        for name, content in downloaded.items():
            path = self.out / "downloads" / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
        result["downloaded"] = {name: {"bytes": len(value),
                                       "sha256": hashlib.sha256(value).hexdigest()}
                                for name, value in downloaded.items()}
        return result

    def state(self, a, b):
        initial = json_stdout(self.code(a, "2.9-turn1", """
            import json
            cn_state = {"counter": 40, "history": ["first"]}
            print(json.dumps(cn_state))
        """))
        updated = json_stdout(self.code(a, "2.9-turn2", """
            cn_state["counter"] += 2
            cn_state["history"].append("second")
            print(json.dumps(cn_state))
        """))
        final = json_stdout(self.code(a, "2.9-turn3", "print(json.dumps(cn_state))"))
        peer = json_stdout(self.code(b, "2.9-peer-state", """
            import json
            print(json.dumps({"exists": "cn_state" in globals()}))
        """))
        assert initial == {"counter": 40, "history": ["first"]}
        assert updated == final == {"counter": 42, "history": ["first", "second"]}
        assert not peer["exists"]
        return {"initial": initial, "updated": updated, "final": final, "peer": peer}

    def packages(self, sid):
        package = "pytimeparse==1.1.8"
        mirror = "https://pypi.tuna.tsinghua.edu.cn/simple"
        prep = json_stdout(self.code(sid, "2.10-package-before", """
            import importlib.util, json, sys
            print(json.dumps({"package_preinstalled": importlib.util.find_spec("pytimeparse") is not None,
                              "python_executable": sys.executable}))
        """))
        results = {}
        for source, index in [("china_mirror", mirror), ("default", None)]:
            target = f"/tmp/ci-pip-{source}-{uuid.uuid4().hex}"
            argv = [prep["python_executable"], "-m", "pip", "install", "--no-cache-dir",
                    "--disable-pip-version-check", "--no-input", "--retries", "0",
                    "--timeout", "20", "--no-deps", "--target", target]
            if index:
                argv += ["--index-url", index]
            argv.append(package)
            response = self.invoke(sid, f"2.10-install-{source}", "executeCommand",
                                   {"command": shlex.join(argv)})
            parsed = structured(response)
            imported = None
            if not parsed["isError"] and not parsed["exceptions"]:
                imported = json_stdout(self.code(sid, f"2.10-import-{source}", f"""
                    import sys, json, pathlib
                    sys.path.insert(0, {target!r})
                    for key in list(sys.modules):
                        if key == "pytimeparse" or key.startswith("pytimeparse."):
                            del sys.modules[key]
                    from pytimeparse.timeparse import timeparse
                    import pytimeparse
                    print(json.dumps({{"seconds": timeparse("1h 20m"), "module_file": pytimeparse.__file__,
                                      "target_exists": pathlib.Path({target!r}).exists()}}))
                """))
            results[source] = {"index_url": index, "elapsed_s": response["elapsed_s"],
                               "output": parsed, "import": imported}
        self.save("package_sources.json", results)
        mirror_result = results["china_mirror"]
        assert not mirror_result["output"]["isError"], mirror_result
        assert mirror_result["import"]["seconds"] == 4800, mirror_result
        assert "/tmp/ci-pip-china_mirror-" in mirror_result["import"]["module_file"], mirror_result
        assert not prep["package_preinstalled"], prep
        return {"package": package, "before": prep, "sources": results}

    def network_probe(self, sid):
        return json_stdout(self.code(sid, "network-probe", """
            import json, urllib.request, urllib.error, socket, time, os
            urls = [
                "https://pypi.tuna.tsinghua.edu.cn/simple/pytimeparse/",
                "https://mirrors.aliyun.com/pypi/simple/pytimeparse/",
                "https://pypi.org/simple/pytimeparse/",
            ]
            results = []
            for url in urls:
                start = time.monotonic()
                result = {"url": url}
                try:
                    with urllib.request.urlopen(url, timeout=8) as response:
                        result.update(status=response.status,
                                      content_type=response.headers.get("Content-Type"),
                                      sample=response.read(512).decode("utf-8", errors="replace"))
                except urllib.error.HTTPError as e:
                    result.update(status=e.code, error=str(e),
                                  sample=e.read(512).decode("utf-8", errors="replace"))
                except Exception as e:
                    result.update(error=str(e), error_type=type(e).__name__)
                result["elapsed_s"] = time.monotonic() - start
                results.append(result)
            print(json.dumps({"requests": results,
                              "network_env_names": sorted(k for k in os.environ
                                  if "proxy" in k.lower() or k.startswith("PIP_"))}))
        """))

    def dependencies(self):
        sid, _ = self.start("dependencies")
        try:
            self.save("network.json", self.network_probe(sid))
            self.check("2.10", lambda: self.packages(sid))
        finally:
            self.stop(sid)

    def timeout(self):
        sid, _ = self.start("timeout", ttl=max(900, self.args.observation_seconds + 300))
        try:
            self._timeout(sid)
        except Exception as exc:
            self.result("2.6", "FAIL", {"error": error_info(exc), "traceback": traceback.format_exc()})
        finally:
            self.stop(sid)

    def _timeout(self, sid):
        seconds = self.args.observation_seconds
        self.log(f"2.6: observing native executeCode for up to {seconds}s")
        code = f"""
            import time, pathlib
            for i in range({seconds}):
                pathlib.Path("/tmp/native-heartbeat.txt").write_text(str(i))
                time.sleep(1)
            pathlib.Path("/tmp/native-done.txt").write_text("COMPLETED")
            print("NATIVE_LONG_TASK_COMPLETED")
        """
        native = self.code(sid, "2.6-native-long-execution", code)
        native_output = structured(native)
        native_checks = []
        for n in range(2):
            if n:
                time.sleep(5)
            native_checks.append(json_stdout(self.code(sid, f"2.6-native-check-{n}", """
                import pathlib, json
                p = pathlib.Path("/tmp/native-heartbeat.txt")
                print(json.dumps({"heartbeat": p.read_text() if p.exists() else None,
                                  "done": pathlib.Path("/tmp/native-done.txt").exists(),
                                  "recovery": 6 * 7}))
            """)))
        self.log("2.6: checking caller deadline + stopTask")
        worker = """
import time, pathlib
for i in range(120):
    pathlib.Path("/tmp/cancel-heartbeat.txt").write_text(str(i))
    time.sleep(1)
pathlib.Path("/tmp/cancel-done.txt").write_text("COMPLETED")
"""
        command = "exec python3 -u -c " + shlex.quote(worker)
        begun = self.invoke(sid, "2.6-async-start", "startCommandExecution", {"command": command})
        start_data = require_ok(begun)["results"][-1]["structuredContent"]
        task_id = start_data["taskId"]
        time.sleep(5)
        before = self.invoke(sid, "2.6-async-before", "getTask", {"taskId": task_id})
        stopped = self.invoke(sid, "2.6-async-stop", "stopTask", {"taskId": task_id})
        require_ok(stopped)
        checks = []
        for n, pause in enumerate([2, 5]):
            time.sleep(pause)
            checks.append(json_stdout(self.code(sid, f"2.6-cancel-check-{n}", """
                import pathlib, json
                p = pathlib.Path("/tmp/cancel-heartbeat.txt")
                print(json.dumps({"heartbeat": p.read_text() if p.exists() else None,
                                  "done": pathlib.Path("/tmp/cancel-done.txt").exists(),
                                  "recovery": 6 * 7}))
            """)))
        after = self.invoke(sid, "2.6-async-after", "getTask", {"taskId": task_id})
        after_result = require_ok(after)
        after_status = after_result["results"][-1].get("structuredContent", {}).get("taskStatus")
        cancel_ok = (checks[0]["heartbeat"] is not None
                     and checks[0]["heartbeat"] == checks[1]["heartbeat"]
                     and not any(c["done"] for c in checks)
                     and all(c["recovery"] == 42 for c in checks)
                     and after_status == "canceled")
        error_text = json.dumps(native_output).lower().replace(" ", "")
        native_timed_out = (native_output["isError"]
                            and any(term in error_text for term in ("timeout", "timedout", "deadlineexceeded"))
                            and not any(c["done"] for c in native_checks)
                            and native_checks[0]["heartbeat"] == native_checks[1]["heartbeat"])
        details = {"native_observation_seconds": seconds, "native_elapsed_s": native["elapsed_s"],
                   "native_output": native_output, "native_checks": native_checks,
                   "native_timeout_proven": native_timed_out,
                   "caller_deadline_seconds": 5, "cancel_checks": checks,
                   "cancel_termination_proven": cancel_ok, "task_id": task_id,
                   "before": structured(before), "stop": structured(stopped),
                   "after": structured(after),
                   "scope": "No per-execution timeout argument in installed service model. "
                            "Caller deadline plus stopTask is tested separately from native automatic timeout."}
        self.result("2.6", "PASS" if native_timed_out and cancel_ok else
                    ("PARTIAL" if cancel_ok else "FAIL"), details)

    def concurrency(self):
        batches = []
        for count in self.args.concurrency:
            self.log(f"2.7: starting {count} concurrent new sessions")
            barrier = threading.Barrier(count + 1)
            held_sessions = []
            held_lock = threading.Lock()
            released_at = {}

            def worker(index):
                label = f"concurrency-{count}-{index:02d}"
                barrier.wait(timeout=60)
                begin = time.monotonic()
                row = {"index": index, "at": utc(),
                       "launch_offset_s": begin - released_at["time"]}
                try:
                    sid, start = self.start(label)
                    row.update(session_id=sid, start_s=start["elapsed_s"],
                               start_request_id=start["response"]["ResponseMetadata"]["RequestId"])
                    with held_lock:
                        held_sessions.append(sid)
                    response = self.code(sid, label + "-first-execution",
                                         f"print('CN_COLD_{count}_{index}_' + str(6 * 7))")
                    output = require_ok(response)["stdout"].strip()
                    row.update(first_execute_s=response["elapsed_s"],
                               end_to_end_s=time.monotonic() - begin,
                               execute_request_id=response["response"]["ResponseMetadata"]["RequestId"],
                               stdout=output)
                    assert output == f"CN_COLD_{count}_{index}_42", output
                    row["status"] = "PASS"
                except Exception as exc:
                    row.update(status="FAIL", error=error_info(exc),
                               elapsed_until_failure_s=time.monotonic() - begin)
                finally:
                    self.save(f"concurrency/{label}.json", row)
                return row

            with concurrent.futures.ThreadPoolExecutor(max_workers=count) as pool:
                futures = [pool.submit(worker, i) for i in range(count)]
                released_at["time"] = time.monotonic()
                barrier.wait(timeout=60)
                rows = [f.result() for f in concurrent.futures.as_completed(futures)]
            # Hold every successful session until all first executions finish; no churn lowers concurrency.
            successful = [r for r in rows if r["status"] == "PASS"]
            batch = {"concurrency": count, "success": len(successful),
                     "failure": count - len(successful), "rows": sorted(rows, key=lambda r: r["index"]),
                     "start": distribution([r["start_s"] for r in rows if "start_s" in r]),
                     "first_execute": distribution([r["first_execute_s"] for r in successful]),
                     "end_to_end": distribution([r["end_to_end_s"] for r in successful]),
                     "max_launch_skew_s": max(r["launch_offset_s"] for r in rows),
                     "sessions_held_until_batch_complete": len(held_sessions)}
            batches.append(batch)
            self.save("concurrency.json", batches)
            self.log(f"2.7: {count} concurrency: {len(successful)}/{count} passed; stopping sessions")
            with concurrent.futures.ThreadPoolExecutor(max_workers=min(count, 10)) as pool:
                list(pool.map(self.stop, held_sessions))
        self.result("2.7", "PASS" if all(b["failure"] == 0 for b in batches) else "FAIL",
                    {"batches": batches, "retries": 0, "latency_sla": None,
                     "scope": "New sessions and first execution; provider microVM allocation is unobservable."})

    def cleanup(self):
        remaining = [sid for sid, row in self.ledger.items() if not row.get("stopped")]
        if remaining:
            with concurrent.futures.ThreadPoolExecutor(max_workers=10) as pool:
                list(pool.map(self.stop, remaining))
        # Reconcile only exact names from this run, including interrupted Start calls.
        intents = {json.loads(p.read_text())["request"]["name"]
                   for p in (self.out / "start_intents").glob("*.json")}
        listed = []
        params = {"codeInterpreterIdentifier": self.args.identifier, "maxResults": 100}
        try:
            while True:
                response = self.client.list_code_interpreter_sessions(**params)
                for row in response.get("items", []):
                    if row.get("name") in intents:
                        listed.append(row)
                        sid = row["sessionId"]
                        if sid not in self.ledger:
                            self.ledger[sid] = {"label": f"recovered-{sid}",
                                                "identifier": self.args.identifier,
                                                "stopped": row.get("status") == "TERMINATED"}
                            dump(self.ledger_path, self.ledger)
                        if row.get("status") != "TERMINATED":
                            self.ledger[sid]["stopped"] = False
                            self.stop(sid)
                if not response.get("nextToken"):
                    break
                params["nextToken"] = response["nextToken"]
            self.save("cleanup-list.json", {"at": utc(), "matching_sessions": listed})
        except Exception as exc:
            self.save("cleanup-list.json", {"at": utc(), "error": error_info(exc)})
        active = [sid for sid, row in self.ledger.items() if not row.get("stopped")]
        self.save("cleanup.json", {"at": utc(), "tracked": len(self.ledger),
                                  "stopped": len(self.ledger) - len(active),
                                  "not_confirmed_stopped": active, "custom_resources_created": []})
        self.log(f"Cleanup: {len(self.ledger)-len(active)}/{len(self.ledger)} stopped")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", default="agentcore_cn")
    parser.add_argument("--region", default="cn-northwest-1")
    parser.add_argument("--expected-account", default="447150580482")
    parser.add_argument("--identifier", default="aws.codeinterpreter.v1")
    parser.add_argument("--phase", choices=["all", "functional", "dependencies", "timeout", "concurrency", "cleanup"],
                        default="all")
    parser.add_argument("--output", default=str(ROOT / "results" / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")))
    parser.add_argument("--concurrency", nargs="+", type=int, default=[1, 10, 50])
    parser.add_argument("--observation-seconds", type=int, default=180)
    args = parser.parse_args()
    if any(n < 1 or n > 50 for n in args.concurrency):
        parser.error("Concurrency must be between 1 and 50.")
    if not 1 <= args.observation_seconds <= 600:
        parser.error("Observation must be between 1 and 600 seconds.")
    lab = Lab(args)
    lab.log(f"Output: {lab.out}")
    try:
        lab.environment()
        if args.phase in ("all", "functional"):
            lab.functional()
        if args.phase == "dependencies":
            lab.dependencies()
        if args.phase in ("all", "timeout"):
            lab.timeout()
        if args.phase in ("all", "concurrency"):
            lab.concurrency()
    except Exception as exc:
        lab.save("fatal-error.json", {"at": utc(), "error": error_info(exc),
                                      "traceback": traceback.format_exc()})
        raise
    finally:
        lab.cleanup()
    failed = (any(r["status"] in ("FAIL", "BLOCKED", "PARTIAL") for r in lab.results.values())
              or any(not row.get("stopped") for row in lab.ledger.values()))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
