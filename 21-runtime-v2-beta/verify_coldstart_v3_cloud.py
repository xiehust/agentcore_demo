"""Read-only AWS postcheck: actual Session models and owned Runtime absence.

Writes a new postcheck.json without replacing measurements or prior evidence.
"""
import argparse
import gzip
import hashlib
import json
import os
from pathlib import Path

import boto3
import botocore
from botocore.config import Config
from botocore.exceptions import ClientError

from coldstart_v3 import public_sdk_evidence
from coldstart_v2 import bench

def postcheck(out):
    state = json.loads((out / "matrix.json").read_text())
    assert state["report_version"] == 3 and state["cleanup_complete"]
    assert boto3.__version__ == state["boto3"] and botocore.__version__ == state["botocore"]
    evidence = public_sdk_evidence()
    session = boto3.Session(region_name=state["region"])
    # Inspect the actual Session loader, including profile data_path overrides.
    loader = session._session.get_component("data_loader")
    result = {"checked_iso": bench.utc_iso(), "region": state["region"],
              "boto3": boto3.__version__, "botocore": botocore.__version__,
              "aws_data_path": os.environ.get("AWS_DATA_PATH"),
              "configured_data_path": session._session.get_config_variable("data_path"),
              "actual_boto3_loader_paths": loader.search_paths, "models": {}, "runtimes": []}
    for service, model in evidence["models"].items():
        assert model == state["sdk"]["models"][service]
        path = Path(botocore.__file__).parent.parent / model["wheel_member"]
        data = path.read_bytes()
        if path.suffix == ".gz":
            data = gzip.decompress(data)
        assert hashlib.sha256(data).hexdigest() == model["json_sha256"]
        assert loader.load_service_model(service, "service-2") == json.loads(data)
        result["models"][service] = {"matches_public_wheel": True, "json_sha256": model["json_sha256"]}
    cfg = Config(connect_timeout=10, read_timeout=20, retries={"total_max_attempts": 1})
    assert session.client("sts", config=cfg).get_caller_identity()["Account"] == state["account"]
    ctl = session.client("bedrock-agentcore-control", config=cfg)
    for cell in state["cells"].values():
        runtime = cell["runtime"]
        assert not cell["reused"] and runtime["id"].startswith("ga_v3_")
        try:
            response = ctl.get_agent_runtime(agentRuntimeId=runtime["id"])
            record = {"id": runtime["id"], "absent": False, "status": response["status"]}
        except ClientError as exc:
            record = {"id": runtime["id"], "error_code": exc.response["Error"]["Code"],
                      "absent": exc.response["Error"]["Code"] == "ResourceNotFoundException",
                      "request_id": exc.response["ResponseMetadata"]["RequestId"]}
        result["runtimes"].append(record)
    result["all_absent"] = len(result["runtimes"]) == 15 and all(r["absent"] for r in result["runtimes"])
    result["finished_iso"] = bench.utc_iso()
    os.umask(0o077)
    with (out / "postcheck.json").open("x") as stream:
        json.dump(result, stream, indent=2)
        stream.write("\n")
    assert result["all_absent"], "Some owned runtimes still exist; see postcheck.json"
    print("PASS: actual boto3 Session control/data models match public wheel; 15 runtimes independently absent")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("out", type=Path)
    postcheck(parser.parse_args().out.resolve())
