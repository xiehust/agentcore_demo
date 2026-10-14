"""Delete everything deploy.py created: the runtimes, the IAM role, the ECR repository.

    ../.venv/bin/python cleanup.py          # dry run: print what would be deleted
    ../.venv/bin/python cleanup.py --yes
"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

import boto3
from botocore.exceptions import ClientError

HERE = Path(__file__).resolve().parent


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--yes", action="store_true", help="actually delete")
    args = ap.parse_args()
    dep = json.loads((HERE / "deployment.json").read_text())
    session = boto3.Session(region_name=dep["region"])
    print(f"runtimes: {[r['id'] for r in dep['runtimes'].values()]}\n"
          f"role: {dep['role_name']}\necr repository: {dep['repository']} ({dep['region']})")
    if not args.yes:
        print("dry run; pass --yes to delete")
        return

    ctl = session.client("bedrock-agentcore-control")
    for rt in dep["runtimes"].values():
        try:
            ctl.delete_agent_runtime(agentRuntimeId=rt["id"])
            print(f"delete requested: {rt['id']}")
        except ctl.exceptions.ResourceNotFoundException:
            print(f"already gone: {rt['id']}")
    for rt in dep["runtimes"].values():
        for _ in range(60):
            try:
                ctl.get_agent_runtime(agentRuntimeId=rt["id"])
            except ctl.exceptions.ResourceNotFoundException:
                print(f"confirmed deleted: {rt['id']}")
                break
            time.sleep(5)
        else:
            raise SystemExit(f"{rt['id']} still exists after 5 minutes")

    iam = session.client("iam")
    try:
        for name in iam.list_role_policies(RoleName=dep["role_name"])["PolicyNames"]:
            iam.delete_role_policy(RoleName=dep["role_name"], PolicyName=name)
        iam.delete_role(RoleName=dep["role_name"])
        print(f"deleted role {dep['role_name']}")
    except iam.exceptions.NoSuchEntityException:
        print(f"role already gone: {dep['role_name']}")

    try:
        session.client("ecr").delete_repository(repositoryName=dep["repository"], force=True)
        print(f"deleted ECR repository {dep['repository']}")
    except ClientError as e:
        if e.response["Error"]["Code"] != "RepositoryNotFoundException":
            raise
        print(f"repository already gone: {dep['repository']}")


if __name__ == "__main__":
    main()
