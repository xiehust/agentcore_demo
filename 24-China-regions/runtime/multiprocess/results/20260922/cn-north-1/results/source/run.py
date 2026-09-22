"""Run only on the specified regional eight-vCPU EC2 and upload evidence."""
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import platform
import shutil
import sys
import tarfile
import time
import traceback
import urllib.request

import boto3
import botocore
from botocore.config import Config

import analyze
import client

ROOT = Path(__file__).resolve().parent


def main():
    cfg = json.loads((ROOT / "config.json").read_text())
    out = ROOT / "results"
    out.mkdir(exist_ok=False)
    session = boto3.Session(region_name=cfg["region"])
    s3 = session.client("s3")
    status = {"started_at": client.now(), "region": cfg["region"], "completed_cells": []}

    def publish():
        client.save(out / "progress.json", status)
        s3.put_object(Bucket=cfg["bucket"], Key="results/progress.json", Body=json.dumps(status).encode())
        print(json.dumps(status), flush=True)

    rc = 1
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        token = opener.open(urllib.request.Request("http://169.254.169.254/latest/api/token",
            method="PUT", headers={"X-aws-ec2-metadata-token-ttl-seconds": "60"}), timeout=3).read().decode()
        identity = json.loads(opener.open(urllib.request.Request(
            "http://169.254.169.254/latest/dynamic/instance-identity/document",
            headers={"X-aws-ec2-metadata-token": token}), timeout=3).read())
        assert identity["region"] == cfg["region"] and identity["instanceId"] == cfg["instance_id"]
        assert identity["instanceType"] == "c6g.2xlarge" and identity["architecture"] == "arm64"
        assert os.cpu_count() == 8 and len(os.sched_getaffinity(0)) == 8
        caller = session.client("sts", config=Config(retries={"total_max_attempts": 1})).get_caller_identity()
        assert caller["Account"] == cfg["account"] and cfg["instance_id"] in caller["Arn"]
        metadata = {"started_at": client.now(), "identity": identity, "caller": caller,
            "cpu_count": os.cpu_count(), "affinity": sorted(os.sched_getaffinity(0)),
            "python": sys.version, "platform": platform.platform(),
            "boto3": boto3.__version__, "botocore": botocore.__version__, "config": cfg,
            "source_sha256": {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in ROOT.glob("*.py")}}
        client.save(out / "run.json", metadata)
        for path in ROOT.glob("*.py"):
            (out / "source").mkdir(exist_ok=True)
            shutil.copy2(path, out / "source" / path.name)
        for key, runtime in cfg["runtimes"].items():
            status["current_cell"] = key
            publish()
            settle = max(0, runtime["ready_observed_unix"] + cfg["settle_seconds"] - time.time())
            if settle:
                time.sleep(settle)
            raw = client.run_burst(cfg["region"], runtime["agentRuntimeArn"], runtime["concurrency"],
                out / "cells" / key, hold=runtime["hold_seconds"], process_count=cfg["process_count"])
            summary = analyze.summarize(raw)
            client.save(out / "cells" / key / "summary.json", summary)
            status["completed_cells"].append({"cell": key, "all_checks_pass": summary["all_checks_pass"],
                "cold_failures": summary["cold_failures"], "cold_p99_ms": summary["cold_ms"]["p99"],
                "before_send_spread_ms": summary["before_send_spread_ms"]})
            publish()
            if raw["failure"]:
                raise RuntimeError("Load client failed: " + raw["failure"])
        combined = analyze.analyze(out)
        status["all_checks_pass"] = combined["all_checks_pass"]
        rc = 0 if combined["all_checks_pass"] else 2
    except BaseException as exc:
        status.update(error=f"{type(exc).__name__}: {exc}", traceback=traceback.format_exc())
        raise
    finally:
        status.update(finished_at=client.now(), exit_code=rc)
        client.save(out / "final.json", status)
        for name in ("benchmark.log", "client_checks.log", "build.log", "build_config.json", "config.json"):
            if (ROOT / name).exists():
                shutil.copy2(ROOT / name, out / name)
        archive = ROOT / "results.tar.gz"
        with tarfile.open(archive, "w:gz") as stream:
            stream.add(out, arcname="results")
        s3.upload_file(str(archive), cfg["bucket"], "results/results.tar.gz")
        s3.put_object(Bucket=cfg["bucket"], Key="results/final.json", Body=json.dumps(status).encode())
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
