"""Delete what deploy_microvm.py created: the three images, both roles, the artifact bucket.

    ../../.venv/bin/python cleanup_microvm.py          # dry run
    ../../.venv/bin/python cleanup_microvm.py --yes
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import boto3
from botocore.exceptions import ClientError

HERE = Path(__file__).resolve().parent


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--yes", action="store_true", help="actually delete")
    args = ap.parse_args()
    dep = json.loads((HERE / "microvm_deployment.json").read_text())
    session = boto3.Session(region_name=dep["region"])
    roles = [dep["build_role"]["name"], dep["execution_role"]["name"]]
    print(f"images: {[i['name'] for i in dep['images'].values()]}\nroles: {roles}\nbucket: {dep['bucket']}")
    if not args.yes:
        print("dry run; pass --yes to delete")
        return

    mvm = session.client("lambda-microvms")
    running = [v["microvmId"] for v in mvm.list_microvms().get("items", [])
               if v.get("state") not in ("TERMINATED", "TERMINATING")
               and v.get("imageArn") in {i["arn"] for i in dep["images"].values()}]
    for microvm_id in running:
        mvm.terminate_microvm(microvmIdentifier=microvm_id)
        print(f"terminated {microvm_id}")
    for img in dep["images"].values():
        try:
            mvm.delete_microvm_image(imageIdentifier=img["arn"])
            print(f"delete requested: {img['name']}")
        except mvm.exceptions.ResourceNotFoundException:
            print(f"already gone: {img['name']}")

    iam = session.client("iam")
    for role in roles:
        try:
            for name in iam.list_role_policies(RoleName=role)["PolicyNames"]:
                iam.delete_role_policy(RoleName=role, PolicyName=name)
            iam.delete_role(RoleName=role)
            print(f"deleted role {role}")
        except iam.exceptions.NoSuchEntityException:
            print(f"role already gone: {role}")

    bucket = session.resource("s3").Bucket(dep["bucket"])
    try:
        bucket.object_versions.delete()
        bucket.delete()
        print(f"deleted bucket {dep['bucket']}")
    except ClientError as e:
        if e.response["Error"]["Code"] != "NoSuchBucket":
            raise
        print(f"bucket already gone: {dep['bucket']}")


if __name__ == "__main__":
    main()
