#!/usr/bin/env python3
"""Validate complete cloud benchmark matrices and record read-only deployment evidence."""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import itertools
import json
import math
import sys
from datetime import datetime, timezone
from pathlib import Path

import boto3

ROOT = Path(__file__).resolve().parents[1]

def validate(record: dict) -> dict:
    assert record["success"] is True and record.get("finished_at")
    assert not record.get("error") and not record.get("failed_response")
    assert record["schema"] == "s5cmd-workspace-v1" and record["engine"] == "s5cmd"
    assert type(record["repetitions"]) is int and 1 <= record["repetitions"] <= 3
    assert record["measurements"]
    baselines = {item["kind"]: item for item in record["local_workspace_baselines"]}
    assert set(baselines) == {"git-clone", "unzip"}
    for item in baselines.values():
        assert item["success"] and item["fixture"]["revision"] == "75c4403f07b8ad25893f7832dbe8fc6814b53b2d"
    rows = record["measurements"]
    workers = record["workers"]
    assert workers in (8, 32, 64, 128, 256)
    expected = set(itertools.product(range(record["repetitions"]), baselines,
        ("s3", "juicefs"), ("persist-small-files", "cold-first-pass", "warm-repeat")))
    actual = [(r["repetition"], r["workload"], r["backend"], r["label"]) for r in rows]
    assert len(actual) == len(expected) and set(actual) == expected
    for row in rows:
        baseline = baselines[row["workload"]]
        assert row["schema"] == "s5cmd-workspace-v1" and row["engine"] == "s5cmd"
        assert row["s5cmd_version"] == "v2.3.0" and row["workers"] == workers
        assert row["success"] and not row["deadline_exceeded"] and row["error"] is None
        assert row["completed_files"] == row["files"] == baseline["files"]
        assert row["total_bytes"] == baseline["total_bytes"]
        status = row["transfer_status"]
        assert status["exit_code"] == 0 and status["success"] and status["expected_copies_matched"]
        assert not status["timed_out"] and status["error_records"] == 0
        assert row["part_concurrency"] == 1 and row["retry_count"] == 2
        for key in ("manifest_sha256", "directories", "symlinks", "small_files_le_16k"):
            assert row[key] == baseline[key]
        write = row["label"] == "persist-small-files"
        assert row["phase"] == ("write" if write else "restore")
        assert row["manifest_published"] == write
        for key in ("wall_seconds", "file_batch_seconds", "manifest_seconds", "validation_seconds"):
            assert math.isfinite(row[key]) and row[key] > 0
        assert row["wall_seconds"] >= row["file_batch_seconds"]
        assert math.isclose(row["file_batch_mib_per_second"], row["total_bytes"] / 1024**2 / row["file_batch_seconds"])
    assert len(record["cache_resets"]) == 2 * record["repetitions"]
    assert all(item["status"] == "Success" for item in record["cache_resets"])
    assert len({item["stdout"] for item in record["cache_resets"]}) == len(record["cache_resets"])
    assert len(record["isolation"]) == 4
    assert {(r["tenant"], r["backend"]) for r in record["isolation"]} == set(itertools.product(("tenant-a", "tenant-b"), ("s3", "juicefs")))
    for item in record["isolation"]:
        assert item["success"] and len(item["checks"]) == 6
        assert all(check["passed"] and check["access_denied"] and check["exit_code"] != 0 for check in item["checks"].values())
    assert record["cross_session_token_rejected"]
    assert len({r["session_id"] for r in record["sessions"].values()}) == 2
    assert len({r["process_id"] for r in record["sessions"].values()}) == 2
    assert set(record["session_cleanup"]) == {"tenant-a", "tenant-b"}
    for tenant, item in record["session_cleanup"].items():
        assert item["attempted"] and item["success"] and item["status_code"] == 200
        assert item["runtime_session_id"] == record["sessions"][tenant]["session_id"]
    return {"valid": True, "engine": "s5cmd", "rows": len(rows), "workers": workers,
            "repetitions": record["repetitions"], "file_copies": sum(row["completed_files"] for row in rows),
            "application_bytes": sum(row["total_bytes"] for row in rows),
            "isolation_checks": 24, "sessions_stopped": 2}


def evidence(state: dict) -> dict:
    region, output = state["region"], state["outputs"]
    ec2 = boto3.client("ec2", region_name=region)
    instance = ec2.describe_instances(InstanceIds=[output["GatewayInstanceId"]])["Reservations"][0]["Instances"][0]
    volumes = ec2.describe_volumes(VolumeIds=[v["Ebs"]["VolumeId"] for v in instance["BlockDeviceMappings"]])["Volumes"]
    runtime = boto3.client("bedrock-agentcore-control", region_name=region).get_agent_runtime(agentRuntimeId=state["runtime"]["id"])
    tag = state["image_uri"].rsplit(":", 1)[1]
    image = boto3.client("ecr", region_name=region).describe_images(
        repositoryName=output["RepositoryUri"].split("/", 1)[1], imageIds=[{"imageTag": tag}])["imageDetails"][0]
    sys.path.insert(0, str(ROOT / "scripts"))
    spec = importlib.util.spec_from_file_location("benchmark_controller", ROOT / "scripts/05-juicefs-demo.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    probe = module.ssm_command(module.api("ssm", region), output["GatewayInstanceId"],
        "set -eu\n/opt/juicefs-demo/juicefs version\n/opt/juicefs-demo/mc --version\n"
        "systemctl show juicefs-demo --property=ActiveState,SubState,ExecStart\n"
        "df -h /opt/juicefs-demo\nuname -a\n", 120)
    return {"recorded_at": datetime.now(timezone.utc).isoformat(), "region": region,
        "stack_id": state["stack_id"], "outputs": output,
        "runtime": {key: runtime.get(key) for key in ("agentRuntimeArn", "agentRuntimeVersion", "status", "networkConfiguration", "roleArn")},
        "gateway": {key: instance.get(key) for key in ("InstanceId", "InstanceType", "ImageId", "Placement", "PrivateIpAddress", "State")},
        "volumes": [{key: volume.get(key) for key in ("VolumeId", "Size", "VolumeType", "Iops", "Throughput", "Encrypted")} for volume in volumes],
        "image": {key: image.get(key) for key in ("imageDigest", "imageSizeInBytes", "imagePushedAt")},
        "gateway_probe": probe,
        "source_sha256": {str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest()
                          for p in (ROOT / "demo/juicefs").glob("*.py")},
        "infrastructure_retained": True}

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("reports", type=Path, nargs="+")
    parser.add_argument("--state", type=Path, default=ROOT / "build/juicefs-state.json")
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--collect-cloud-evidence", action="store_true")
    args = parser.parse_args()
    if not __debug__:
        raise RuntimeError("do not run this assertion-based verifier with python -O")
    if args.out.exists():
        raise ValueError("output exists; do not overwrite evidence")
    result = {"checked_at": datetime.now(timezone.utc).isoformat(), "reports": {}}
    for path in args.reports:
        raw = path.read_bytes()
        result["reports"][str(path)] = {"sha256": hashlib.sha256(raw).hexdigest(), **validate(json.loads(raw))}
    if args.collect_cloud_evidence:
        result["deployment"] = evidence(json.loads(args.state.read_text()))
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(result, indent=2, default=str) + "\n")
    print(json.dumps(result["reports"], indent=2))
    return 0

if __name__ == "__main__":
    sys.exit(main())
