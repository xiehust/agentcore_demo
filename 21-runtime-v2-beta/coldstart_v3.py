"""Report-v3 verification of Runtime V2 using the public SDK: all 15 cells fresh.

Reuse the frozen matrix lifecycle and client, replacing only its beta/reuse
preflight. No historical measurements are included in the new run.
"""
import gzip
import hashlib
import io
import json
import os
from pathlib import Path
import sys
import urllib.request
import uuid
import zipfile

import boto3
import botocore
from botocore.config import Config
from botocore.loaders import Loader
from botocore.validate import validate_parameters

import coldstart_v2_matrix as matrix

HERE = Path(__file__).resolve().parent

def public_sdk_evidence():
    """Compare loaded control/data models with the published botocore wheel."""
    def fetch(url):
        with urllib.request.urlopen(url, timeout=60) as response:
            return response.read()

    latest = json.loads(fetch("https://pypi.org/pypi/boto3/json"))
    if boto3.__version__ != latest["info"]["version"]:
        raise RuntimeError("Upgrade boto3 to the current public PyPI release first")
    package = json.loads(fetch(f"https://pypi.org/pypi/botocore/{botocore.__version__}/json"))
    wheel = next(f for f in package["urls"] if f["filename"].endswith(".whl"))
    content = fetch(wheel["url"])
    assert hashlib.sha256(content).hexdigest() == wheel["digests"]["sha256"]
    loader = Loader()
    models = {}
    with zipfile.ZipFile(io.BytesIO(content)) as archive:
        for service in ("bedrock-agentcore-control", "bedrock-agentcore"):
            names = [n for n in archive.namelist() if n.startswith(f"botocore/data/{service}/")
                     and n.endswith(("/service-2.json", "/service-2.json.gz"))]
            name = sorted(names)[-1]
            data = archive.read(name)
            if name.endswith(".gz"):
                data = gzip.decompress(data)
            published = json.loads(data)
            assert loader.load_service_model(service, "service-2") == published, service
            models[service] = {"wheel_member": name, "json_sha256": hashlib.sha256(data).hexdigest(),
                               "matches_public_wheel": True}
    return {"checked_iso": matrix.client.base.shared.bench.utc_iso(),
            "boto3": boto3.__version__, "botocore": botocore.__version__,
            "pypi_latest_boto3": latest["info"]["version"], "wheel_url": wheel["url"],
            "wheel_sha256": wheel["digests"]["sha256"], "models": models,
            "aws_data_path": os.environ.get("AWS_DATA_PATH"), "model_search_paths": loader.search_paths}


def cell_plan(out):
    return {f"{size}_c{c}": {"size": size, "concurrency": c, "reused": False,
            "folder": str((out / f"{size}_c{c}").relative_to(HERE))}
            for size in matrix.SIZES for c in matrix.LEVELS}


def preflight(out):
    sdk = public_sdk_evidence()
    matrix.save(out / "sdk.json", sdk)
    templates = json.loads((matrix.TEMPLATES / "create_requests.json").read_text())
    old_images = json.loads((matrix.TEMPLATES / "images.json").read_text())
    prior = json.loads((matrix.TEMPLATES / "run.json").read_text())
    region, account = prior["region"], prior["account"]
    assert region == "us-west-2"
    session = boto3.Session(region_name=region)
    cfg = Config(connect_timeout=15, read_timeout=60, retries={"total_max_attempts": 1})
    assert session.client("sts", config=cfg).get_caller_identity()["Account"] == account
    ctl, ecr = session.client("bedrock-agentcore-control", config=cfg), session.client("ecr", config=cfg)
    fields = {}
    for operation in ("CreateAgentRuntime", "UpdateAgentRuntime", "GetAgentRuntime"):
        model = ctl.meta.service_model.operation_model(operation)
        shape = model.output_shape if operation == "GetAgentRuntime" else model.input_shape
        assert "platformVersion" in shape.members
        fields[operation] = shape.members["platformVersion"].type_name
    images = {}
    for size in matrix.SIZES:
        uri = templates[size]["agentRuntimeArtifact"]["containerConfiguration"]["containerUri"]
        image = ecr.describe_images(repositoryName=uri.split("/", 1)[1].split("@")[0],
            imageIds=[{"imageDigest": uri.split("@")[1]}])["imageDetails"][0]
        assert all(image[k] == old_images[size][k] for k in ("imageDigest", "imageSizeInBytes"))
        images[size] = image
    cells, requests = cell_plan(out), {}
    for key, cell in cells.items():
        request = {**templates[cell["size"]], "agentRuntimeName": f"ga_v3_{key}_{uuid.uuid4().hex[:8]}",
                   "clientToken": str(uuid.uuid4()), "description": "Temporary public SDK V2 coldstart report v3"}
        assert request["platformVersion"] == "V2"
        validate_parameters(request, ctl.meta.service_model.operation_model("CreateAgentRuntime").input_shape)
        requests[key] = request
    sources = [Path(__file__), Path(matrix.__file__), Path(matrix.client.__file__),
               Path(matrix.client.base.__file__), Path(matrix.previous.__file__),
               Path(matrix.client.base.shared.__file__), HERE / "coldstart_v2.py", HERE / "check_v2.py",
               matrix.client.base.shared.original.BASELINE / "coldstart_test.py",
               matrix.TEMPLATES / "create_requests.json", matrix.TEMPLATES / "images.json"]
    state = {"report_version": 3, "started_iso": matrix.client.base.shared.bench.utc_iso(),
             "region": region, "account": account, "cells": cells, "requests": requests,
             "images": images, "sizes": list(matrix.SIZES), "levels": list(matrix.LEVELS),
             "process_rule": "min(8, concurrency)", "settle_min_seconds": 180,
             "source_sha256": {str(p.relative_to(HERE.parent)): matrix.digest(p) for p in sources},
             "quotas": matrix.client.base.shared.read_quotas(session), "sdk": sdk,
             "sdk_platform_fields": fields, "boto3": boto3.__version__, "botocore": botocore.__version__,
             "python": sys.version, "cpu_affinity": sorted(os.sched_getaffinity(0)),
             "control_endpoint": ctl.meta.endpoint_url, "historical_samples_reused": 0}
    matrix.save(out / "matrix.json", state)
    print("PREFLIGHT", json.dumps({"boto3": state["boto3"], "botocore": state["botocore"],
          "public_models_verified": True, "fresh_cells": len(cells), "quotas": state["quotas"]}), flush=True)
    return state, ctl


if __name__ == "__main__":
    # The frozen runner calls this once before it creates any cloud resource.
    matrix.preflight = preflight
    matrix.__doc__ = __doc__
    sys.exit(matrix.main())
