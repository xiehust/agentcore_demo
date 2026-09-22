#!/usr/bin/env python3
"""Execute both benchmarks on EC2, record IMDS identity, and upload only test evidence."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import platform
import shutil
import socket
import tarfile
import threading
import time
import traceback
from types import SimpleNamespace
import urllib.request

import boto3
import botocore
from botocore.config import Config

from benchmark_runtime import Benchmark, stats
from verify_code_interpreter import Lab as CodeLab, require_ok

BASE = Path(__file__).resolve().parent
DATA = BASE / "results"


def now():
    return datetime.now(timezone.utc).isoformat()


def save(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(data, indent=2, default=str) + "\n")
    temp.replace(path)


def instance_identity():
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    token = opener.open(urllib.request.Request(
        "http://169.254.169.254/latest/api/token", method="PUT",
        headers={"X-aws-ec2-metadata-token-ttl-seconds": "300"}), timeout=3).read().decode()
    response = opener.open(urllib.request.Request(
        "http://169.254.169.254/latest/dynamic/instance-identity/document",
        headers={"X-aws-ec2-metadata-token": token}), timeout=3)
    return json.loads(response.read())


def cpu_snapshot():
    fields = list(map(int, Path("/proc/stat").read_text().splitlines()[0].split()[1:]))
    # Guest times are already included in user/nice, so total only the first 8 fields.
    memory = {line.split(":")[0]: line.split(":")[1].strip()
              for line in Path("/proc/meminfo").read_text().splitlines()
              if line.startswith(("MemTotal:", "MemAvailable:", "SwapTotal:", "SwapFree:"))}
    return {"at": now(), "unix_s": time.time(), "cpu_total_ticks": sum(fields[:8]),
            "idle_ticks": fields[3] + fields[4], "steal_ticks": fields[7],
            "load_average": os.getloadavg(), "memory": memory}


def ci_serial(lab, count=100):
    rows = []
    for index in range(count):
        sid = None
        label = f"serial-{index:03d}"
        row = {"index": index, "at": now()}
        start = time.monotonic()
        try:
            sid, started = lab.start(label)
            result = lab.code(sid, label + "-execute",
                              f"print('CN_SERIAL_{index}_' + str(6 * 7))")
            end = time.monotonic()
            text = require_ok(result)["stdout"].strip()
            assert text == f"CN_SERIAL_{index}_42", text
            row.update(status="PASS", session_id=sid, start_ms=started["elapsed_s"] * 1000,
                       first_execute_ms=result["elapsed_s"] * 1000,
                       end_to_end_ms=(end - start) * 1000, stdout=text,
                       start_request_id=started["response"]["ResponseMetadata"]["RequestId"],
                       invoke_request_id=result["response"]["ResponseMetadata"]["RequestId"])
        except Exception as exc:
            row.update(status="FAIL", error=str(exc), response=getattr(exc, "response", None))
        finally:
            if sid:
                lab.stop(sid)
            rows.append(row)
            lab.save("serial_rows.json", rows)
        if (index + 1) % 10 == 0:
            print(f"[{now()}] Code Interpreter serial: {index+1}/{count}", flush=True)
    good = [r for r in rows if r["status"] == "PASS"]
    result = {"at": now(), "samples": len(rows), "success": len(good), "failure": len(rows) - len(good),
              "unique_sessions": len({r["session_id"] for r in good}),
              "start": stats([r["start_ms"] for r in good]),
              "first_execute": stats([r["first_execute_ms"] for r in good]),
              "end_to_end": stats([r["end_to_end_ms"] for r in good]),
              "percentile_method": "nearest-rank"}
    lab.save("serial_summary.json", result)
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default=str(BASE / "config.json"))
    args = parser.parse_args()
    settings = json.loads(Path(args.config).read_text())
    DATA.mkdir(exist_ok=True)
    if (DATA / "run_status.json").exists():
        raise RuntimeError("This directory already contains a run; use a fresh directory")
    s3 = boto3.client("s3", region_name=settings["region"],
                      config=Config(retries={"total_max_attempts": 3}))
    identity = instance_identity()
    assert identity["region"] == settings["region"] == "cn-northwest-1", identity
    assert identity["instanceId"] == settings["instance_id"], identity
    assert identity["accountId"] == settings["account"], identity
    env = {"at": now(), "instance_identity_document": identity,
           "platform": platform.platform(), "cpu_count": os.cpu_count(),
           "cpu_info": [line for line in Path("/proc/cpuinfo").read_text().splitlines()
                        if line.startswith(("model name", "processor"))],
           "python": platform.python_version(), "boto3": boto3.__version__, "botocore": botocore.__version__,
           "sts": boto3.client("sts", region_name=settings["region"]).get_caller_identity(),
           "target_endpoint": f"https://bedrock-agentcore.{settings['region']}.amazonaws.com.cn",
           "initial_cpu": cpu_snapshot()}
    save(DATA / "ec2_environment.json", env)
    sources = DATA / "sources"
    sources.mkdir()
    for name in ("run_benchmarks.py", "benchmark_runtime.py", "verify_code_interpreter.py", "config.json", "requirements.txt"):
        shutil.copyfile(BASE / name, sources / name)
    save(DATA / "source_hashes.json", {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
                                      for p in sources.iterdir()})
    done = threading.Event()
    state = {"started_at": now(), "phase": "setup", "state": "running"}
    state_lock = threading.Lock()

    def monitor():
        previous = None
        last_publish = 0
        while not done.is_set():
            sample = cpu_snapshot()
            if previous:
                total = sample["cpu_total_ticks"] - previous["cpu_total_ticks"]
                if total:
                    sample["cpu_busy_percent"] = 100 * (1 - (sample["idle_ticks"] - previous["idle_ticks"]) / total)
                    sample["cpu_steal_percent"] = 100 * (sample["steal_ticks"] - previous["steal_ticks"]) / total
            previous = sample
            with state_lock:
                sample["phase"] = state["phase"]
            with (DATA / "cpu_samples.jsonl").open("a") as out:
                out.write(json.dumps(sample) + "\n")
            if time.monotonic() - last_publish >= 15:
                with state_lock:
                    progress = {**state, "at": now(), "last_cpu": sample}
                log = BASE / "run.log"
                if log.exists():
                    progress["log_tail"] = log.read_text(errors="replace")[-3000:]
                try:
                    s3.put_object(Bucket=settings["bucket"], Key="results/progress.json",
                                  Body=json.dumps(progress).encode(), ContentType="application/json")
                except Exception as exc:
                    print(f"Progress upload: {type(exc).__name__}", flush=True)
                last_publish = time.monotonic()
            done.wait(1)

    watcher = threading.Thread(target=monitor, daemon=True)
    watcher.start()
    results = {}
    runtime = None
    ci = None
    try:
        # Both services are tested sequentially on the same machine.
        with state_lock:
            state["phase"] = "runtime"
        runtime_dir = DATA / "runtime"
        runtime_dir.mkdir()
        os.chdir(runtime_dir)
        runtime = Benchmark(settings["runtime"])
        try:
            runtime.run()
        finally:
            for sid, row in runtime.sessions.items():
                if not row["stop_confirmed"]:
                    runtime.stop(sid)
            results["runtime"] = runtime.summarize()
        with state_lock:
            state["phase"] = "code_interpreter_concurrency"
        ci_dir = DATA / "code_interpreter"
        ci_dir.mkdir()
        os.chdir(ci_dir)
        ci_args = SimpleNamespace(output=str(ci_dir), profile=None, region=settings["region"],
            expected_account=settings["account"], identifier="aws.codeinterpreter.v1",
            observation_seconds=180, concurrency=[1, 10, 50])
        ci = CodeLab(ci_args)
        try:
            ci.environment()
            ci.concurrency()
            with state_lock:
                state["phase"] = "code_interpreter_serial"
            results["ci_serial"] = ci_serial(ci)
            results["ci_concurrency"] = ci.results["2.7"]
        finally:
            ci.cleanup()
        state["state"] = "completed"
    except Exception as exc:
        state.update(state="failed", error=type(exc).__name__, message=str(exc))
        (DATA / "fatal_error.txt").write_text(traceback.format_exc())
        raise
    finally:
        done.set()
        watcher.join(timeout=30)
        os.chdir(BASE)
        state["finished_at"] = now()
        save(DATA / "run_status.json", state)
        save(DATA / "combined_summary.json", results)
        if (BASE / "run.log").exists():
            shutil.copyfile(BASE / "run.log", DATA / "run.log")
        archive = BASE / "results.tar.gz"
        with tarfile.open(archive, "w:gz") as out:
            out.add(DATA, arcname="results")
        s3.upload_file(str(archive), settings["bucket"], "results/results.tar.gz")
        s3.put_object(Bucket=settings["bucket"], Key="results/final_status.json",
                      Body=json.dumps(state).encode(), ContentType="application/json")
        print(json.dumps(state), flush=True)


if __name__ == "__main__":
    main()
