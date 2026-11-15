"""Create or update one AgentCore runtime from a JSON request on stdin.

deploy.sh uses this instead of the AWS CLI because `hooksConfiguration`
(lifecycle hooks) needs botocore >= the version pinned in uv.lock, newer than
the AWS CLI this demo was first written against.

    uv run python scripts/put_runtime.py create --region us-east-1 < request.json
    uv run python scripts/put_runtime.py update --region us-east-1 --id <runtimeId> < request.json

Prints the response's agentRuntimeId on success. On an API error prints
"<ErrorCode>: <message>" to stderr and exits 1, so callers can grep the code.
"""
import argparse
import json
import sys

import boto3
from botocore.exceptions import ClientError


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("op", choices=("create", "update"))
    p.add_argument("--region", required=True)
    p.add_argument("--id", help="agentRuntimeId (update only)")
    args = p.parse_args()
    request = json.load(sys.stdin)
    client = boto3.client("bedrock-agentcore-control", region_name=args.region)
    try:
        if args.op == "create":
            out = client.create_agent_runtime(**request)
        else:
            if not args.id:
                p.error("update needs --id")
            out = client.update_agent_runtime(agentRuntimeId=args.id, **request)
    except ClientError as err:
        info = err.response.get("Error", {})
        print(f"{info.get('Code', 'ClientError')}: {info.get('Message', err)}", file=sys.stderr)
        return 1
    print(out["agentRuntimeId"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
