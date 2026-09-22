#!/usr/bin/env python3
"""Retry deletion after AWS releases this experiment's ENI; never force-detach."""
from datetime import datetime, timezone
import json
from pathlib import Path
import time

import boto3
from botocore.config import Config

ROOT = Path(__file__).resolve().parent / "results/20260922"
SG = "sg-049391a8216deb6b8"
ENI = "eni-0951f5e08b0da0589"


def write(status, **extra):
    value = {"at": datetime.now(timezone.utc).isoformat(), "status": status,
             "security_group": SG, "network_interface": ENI, **extra}
    path = ROOT / "network_cleanup_retry.json"
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(value, indent=2, default=str) + "\n")
    tmp.replace(path)
    print(json.dumps(value, default=str), flush=True)


def main():
    aws = boto3.Session(profile_name="agentcore_cn", region_name="cn-northwest-1")
    assert aws.client("sts").get_caller_identity()["Account"] == "447150580482"
    ec2 = aws.client("ec2", config=Config(connect_timeout=10, read_timeout=20,
                                         retries={"total_max_attempts": 1}))
    state = json.loads((ROOT / "resources.json").read_text())
    assert state["security_groups"]["client"] == SG
    deadline = time.monotonic() + 1800
    attempts = 0
    while time.monotonic() < deadline:
        attempts += 1
        try:
            interfaces = ec2.describe_network_interfaces(
                Filters=[{"Name": "group-id", "Values": [SG]}])["NetworkInterfaces"]
            assert all(item["NetworkInterfaceId"] == ENI for item in interfaces), interfaces
            for item in interfaces:
                # Only delete after the provider has detached the known test ENI.
                if item["Status"] == "available" and not item.get("Attachment"):
                    ec2.delete_network_interface(NetworkInterfaceId=ENI)
            ec2.delete_security_group(GroupId=SG)
            write("complete", attempts=attempts)
            return
        except Exception as exc:
            code = getattr(exc, "response", {}).get("Error", {}).get("Code")
            if code == "InvalidGroup.NotFound":
                write("complete", attempts=attempts, already_absent=True)
                return
            if code != "DependencyViolation":
                write("error", attempts=attempts, error=str(exc))
                raise
            write("waiting_for_service_detach", attempts=attempts)
            time.sleep(20)
    write("pending_after_timeout", attempts=attempts)


if __name__ == "__main__":
    main()
