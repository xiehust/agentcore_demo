"""Independent evidence/ownership audit after collect; cloud calls are read-only."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path

import boto3
from botocore.config import Config

from client import save


def audit(root):
    root = Path(root)
    output = {"checked_at": datetime.now(timezone.utc).isoformat(), "regions": {}}
    for region in ("cn-northwest-1", "cn-north-1"):
        folder = root / region
        state = json.loads((folder / "ec2/resources.json").read_text())
        rt = json.loads((folder / "runtime/resources.json").read_text())
        cfg = json.loads((folder / "config.json").read_text())
        built = json.loads((folder / "build_result.json").read_text())
        run = json.loads((folder / "results/run.json").read_text())
        input_hashes = json.loads((folder / "benchmark-sha256.json").read_text())
        checks = {}
        checks["eight_cpu_regional_client"] = (
            run["identity"]["region"] == region and run["cpu_count"] == 8
            and run["identity"]["instanceType"] == "c6g.2xlarge"
            and run["identity"]["instanceId"] == state["instance"]["InstanceId"])
        checks["submitted_sources_match_executed"] = all(
            run["source_sha256"][name] == digest
            and hashlib.sha256((folder / "results/source" / name).read_bytes()).hexdigest() == digest
            for name, digest in input_hashes.items() if name.endswith(".py"))
        levels = cfg.get("levels", [1, 10, 50, 100, 200])
        expected_cells = {f"c{level}-r{r}" for level in levels for r in (1, 2, 3)} | {"scale-c50"}
        expected_cold = sum(r["concurrency"] for r in cfg["runtimes"].values())
        checks["complete_matrix"] = set(cfg["runtimes"]) == expected_cells <= set(rt["runtimes"])
        checks["independent_runtimes"] = len({r["agentRuntimeArn"] for r in rt["runtimes"].values()}) == len(rt["runtimes"])
        checks["build_executed_in_region"] = built["identity"]["region"] == region
        sids, instances, raw_hashes = [], [], {}
        cold_count = warm_count = stop_count = 0
        all_ready = all_correct_arn = all_metadata = True
        for path in sorted((folder / "results/cells").glob("*/raw.json")):
            raw_hashes[path.parent.name] = hashlib.sha256(path.read_bytes()).hexdigest()
            raw = json.loads(path.read_text())
            runtime = cfg["runtimes"][path.parent.name]
            all_correct_arn &= raw["plan"]["runtime_arn"] == runtime["agentRuntimeArn"]
            for report in raw["reports"]:
                for e in report["events"]:
                    if e["phase"] == "cold":
                        cold_count += 1
                        sids.append(e["session_id"])
                        if e["valid"]:
                            instances.append(e["body"]["instance_id"])
                        all_ready &= e["started_unix"] >= runtime["ready_observed_unix"] + cfg["settle_seconds"]
                    warm_count += e["phase"] == "warm"
                    stop_count += e["phase"] == "stop"
                    all_metadata &= bool(e.get("metadata", {}).get("RequestId"))
        checks.update(all_ready_at_least_60s=all_ready, runtime_assignment_correct=all_correct_arn,
            all_request_ids_present=all_metadata, expected_cold_count=cold_count == expected_cold,
            expected_warm_count=warm_count == len(instances), expected_stop_count=stop_count == expected_cold,
            globally_unique_sessions=len(set(sids)) == len(sids) == expected_cold,
            globally_unique_instance_markers=len(set(instances)) == len(instances) > 0)
        isolated = folder / "isolated-results"
        if isolated.exists():
            isolated_run = json.loads((isolated / "run.json").read_text())
            isolated_hashes = json.loads((folder / "isolated-sha256.json").read_text())
            checks["isolated_sources_match"] = all(
                isolated_run["source_sha256"][name] == digest and
                hashlib.sha256((isolated / "source" / name).read_bytes()).hexdigest() == digest
                for name, digest in isolated_hashes.items() if name.endswith(".py"))
            isolated_rows = []
            isolated_ids = []
            for cell in isolated_run["config"]["runtimes"]:
                raw = json.loads((isolated / "cells" / cell / "raw.json").read_text())
                cold = [e for r in raw["reports"] for e in r["events"] if e["phase"] == "cold"]
                isolated_ids.extend(e["session_id"] for e in cold)
                start = min(e["started_unix"] for e in cold)
                isolated_rows.append({"cell": cell, "start": start, "finished": raw["finished_at"]})
            isolated_runtimes = isolated_run["config"]["runtimes"]
            checks["isolated_cells_complete"] = (len(isolated_rows) == len(isolated_runtimes)
                and len(isolated_ids) == sum(r["concurrency"] for r in isolated_runtimes.values()))
            checks["isolated_sessions_unique"] = len(set(isolated_ids + sids)) == len(isolated_ids) + len(sids)
            checks["isolated_cooldowns_at_least_180s"] = all(
                following["start"] - datetime.fromisoformat(previous["finished"]).timestamp() >= 180
                for previous, following in zip(isolated_rows, isolated_rows[1:]))
        aws = boto3.Session(profile_name="agentcore_cn", region_name=region)
        config = Config(retries={"total_max_attempts": 1}, connect_timeout=10, read_timeout=60)
        ec2, control = aws.client("ec2", config=config), aws.client("bedrock-agentcore-control", config=config)
        iid = state["instance"]["InstanceId"]
        instance = ec2.describe_instances(InstanceIds=[iid])["Reservations"][0]["Instances"][0]
        cleanup = {"instance_id": iid, "instance_state": instance["State"]["Name"], "runtimes": {}}
        for key, runtime in rt["runtimes"].items():
            try:
                response = control.get_agent_runtime(agentRuntimeId=runtime["agentRuntimeId"])
                cleanup["runtimes"][key] = {"state": response["status"]}
            except control.exceptions.ResourceNotFoundException as exc:
                cleanup["runtimes"][key] = {"state": "absent", "metadata": exc.response["ResponseMetadata"]}
        for kind, service, operation, arguments in (
            ("repository", "ecr", "describe_repositories", {"repositoryNames": [rt["repository"]["repositoryName"]]}),
            ("runtime_role", "iam", "get_role", {"RoleName": rt["roles"]["runtime"]["RoleName"]}),
            ("transfer_bucket", "s3", "head_bucket", {"Bucket": state["bucket"]})):
            try:
                getattr(aws.client(service, config=config), operation)(**arguments)
                cleanup[kind] = "present"
            except Exception as exc:
                cleanup[kind] = getattr(exc, "response", {}).get("Error", {}).get("Code", str(exc))
        cleanup["instance_inline_policies"] = aws.client("iam", config=config).list_role_policies(
            RoleName=state["role"]["RoleName"])["PolicyNames"]
        old_small_id = ("i-0a685957c9b9355d7" if region == "cn-northwest-1" else "i-0ab621bda76e7f0af")
        old_small = ec2.describe_instances(InstanceIds=[old_small_id])["Reservations"][0]["Instances"][0]
        cleanup["previous_small_instance"] = {"id": old_small_id, "state": old_small["State"]["Name"]}
        original_runtimes = json.loads((folder / "existing-runtimes.json").read_text())["agentRuntimes"]
        current_runtimes = control.list_agent_runtimes(maxResults=100)["agentRuntimes"]
        current_ids = {r["agentRuntimeId"] for r in current_runtimes}
        checks["preexisting_runtimes_still_present"] = all(r["agentRuntimeId"] in current_ids for r in original_runtimes)
        save(folder / "runtimes-after.json", current_runtimes)
        checks["previous_small_instance_still_stopped"] = old_small["State"]["Name"] == "stopped"
        ssm = aws.client("ssm", config=config)
        for command in state["commands"]:
            value = ssm.get_command_invocation(CommandId=command["id"], InstanceId=iid)
            save(folder / "ec2" / f"ssm-{command['label']}-final.json", value)
        checks["cleanup_complete"] = (instance["State"]["Name"] == "stopped"
            and all(v["state"] in ("absent", "DELETED") for v in cleanup["runtimes"].values())
            and cleanup["repository"] == "RepositoryNotFoundException"
            and cleanup["runtime_role"] == "NoSuchEntity"
            and cleanup["transfer_bucket"] in ("404", "NoSuchBucket")
            and not cleanup["instance_inline_policies"])
        output["regions"][region] = {"checks": checks, "all_checks_pass": all(checks.values()),
            "counts": {"cold": cold_count, "warm": warm_count, "stop": stop_count},
            "cleanup": cleanup, "raw_sha256": raw_hashes}
    save(root / "audit.json", output)
    return output


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("directory", type=Path)
    args = parser.parse_args()
    print(json.dumps(audit(args.directory), indent=2))
