#!/usr/bin/env python3
"""Compare the default sandbox with a temporary PUBLIC Code Interpreter."""

import argparse
import json
from pathlib import Path
import sys
import time
import uuid

from verify_code_interpreter import Lab, error_info, utc


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", default="agentcore_cn")
    parser.add_argument("--region", default="cn-northwest-1")
    parser.add_argument("--expected-account", default="447150580482")
    parser.add_argument("--output", required=True)
    parser.add_argument("--network", choices=["default", "PUBLIC"], default="default")
    parser.add_argument("--cleanup-resource", action="store_true",
                        help="Delete only the custom resource recorded in this output directory.")
    args = parser.parse_args()
    args.identifier = "aws.codeinterpreter.v1"
    args.observation_seconds = 180
    lab = Lab(args)
    control = lab.aws.client("bedrock-agentcore-control", config=lab.config)
    resource = None
    try:
        lab.environment()
        if args.cleanup_resource:
            resource = json.loads((Path(args.output) / "custom-resource.json").read_text())
            args.identifier = resource["codeInterpreterId"]
            lab.cleanup()
            return 0
        if args.network == "PUBLIC":
            response = control.create_code_interpreter(
                name="cn_ci_validation_" + uuid.uuid4().hex[:12],
                description="Temporary China Code Interpreter dependency validation",
                networkConfiguration={"networkMode": "PUBLIC"})
            resource = response
            lab.save("custom-resource.json", response)
            args.identifier = response["codeInterpreterId"]
            deadline = time.monotonic() + 120
            while True:
                status = control.get_code_interpreter(codeInterpreterId=args.identifier)
                lab.save("custom-resource-ready.json", status)
                if status["status"] == "READY":
                    break
                if status["status"] in ("CREATE_FAILED", "DELETED") or time.monotonic() > deadline:
                    raise RuntimeError(f"Custom resource not ready: {status}")
                time.sleep(2)
            lab.environment()
        lab.dependencies()
    except Exception as exc:
        lab.save("diagnostic-error.json", {"at": utc(), "error": error_info(exc)})
        raise
    finally:
        lab.cleanup()
        if resource:
            try:
                deleted = control.delete_code_interpreter(codeInterpreterId=resource["codeInterpreterId"])
                lab.save("custom-resource-delete.json", deleted)
                deadline = time.monotonic() + 60
                while True:
                    try:
                        status = control.get_code_interpreter(codeInterpreterId=resource["codeInterpreterId"])
                        lab.save("custom-resource-final.json", status)
                        if status["status"] == "DELETED":
                            break
                    except control.exceptions.ResourceNotFoundException as exc:
                        lab.save("custom-resource-final.json",
                                 {"at": utc(), "deleted": True, "response": exc.response})
                        break
                    if time.monotonic() > deadline:
                        raise RuntimeError("Resource deletion not confirmed within 60 seconds")
                    time.sleep(2)
            except Exception as exc:
                lab.save("custom-resource-cleanup-error.json", {"at": utc(), "error": error_info(exc)})
                raise
    failed = (any(row["status"] != "PASS" for row in lab.results.values())
              or any(not row.get("stopped") for row in lab.ledger.values()))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
