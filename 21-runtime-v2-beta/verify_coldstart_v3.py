"""Offline independent acceptance of report-v3 evidence; never invokes AWS."""
import argparse
import hashlib
import json
from pathlib import Path

from verify_coldstart_v2_matrix import check_cell, HERE, SIZES, LEVELS

def verify(out):
    state = json.loads((out / "matrix.json").read_text())
    expected = {f"{size}_c{c}" for size in SIZES for c in LEVELS}
    assert state["report_version"] == 3 and state["historical_samples_reused"] == 0
    assert set(state["cells"]) == set(state["requests"]) == expected
    assert state["measurement_complete"] and state["cleanup_complete"] and not state.get("error")
    assert not state.get("create_inflight") and not state.get("reconciliation_pending")
    assert state["region"] == "us-west-2" and state["sizes"] == list(SIZES) and state["levels"] == list(LEVELS)
    sdk = json.loads((out / "sdk.json").read_text())
    assert state["sdk"] == sdk and state["boto3"] == sdk["boto3"] == sdk["pypi_latest_boto3"]
    assert state["botocore"] == sdk["botocore"]
    assert set(sdk["models"]) == {"bedrock-agentcore-control", "bedrock-agentcore"}
    assert all(m["matches_public_wheel"] for m in sdk["models"].values())
    assert state["sdk_platform_fields"] == {op: "string" for op in
            ("CreateAgentRuntime", "UpdateAgentRuntime", "GetAgentRuntime")}
    for name, digest in state["source_sha256"].items():
        assert hashlib.sha256((HERE.parent / name).read_bytes()).hexdigest() == digest, name
    postcheck = json.loads((out / "postcheck.json").read_text())
    assert postcheck["region"] == state["region"] and postcheck["all_absent"]
    assert postcheck["boto3"] == state["boto3"] and postcheck["botocore"] == state["botocore"]
    assert set(postcheck["models"]) == set(sdk["models"])
    for service, model in postcheck["models"].items():
        assert model["matches_public_wheel"] and model["json_sha256"] == sdk["models"][service]["json_sha256"]
    assert len(postcheck["runtimes"]) == 15
    assert all(r["absent"] and r["error_code"] == "ResourceNotFoundException" for r in postcheck["runtimes"])
    assert {r["id"] for r in postcheck["runtimes"]} == {c["runtime"]["id"] for c in state["cells"].values()}
    output, sessions, runtimes = [], [], []
    for size in SIZES:
        for concurrency in LEVELS:
            key = f"{size}_c{concurrency}"
            cell, request = state["cells"][key], state["requests"][key]
            assert cell["size"] == size and cell["concurrency"] == concurrency and not cell["reused"]
            assert (HERE / cell["folder"]).parent == out
            assert request["platformVersion"] == "V2" and request["agentRuntimeName"].startswith("ga_v3_")
            ready = cell["runtime"]["ready_response"]
            for field in ("agentRuntimeArtifact", "roleArn", "networkConfiguration",
                          "protocolConfiguration", "lifecycleConfiguration", "platformVersion", "agentRuntimeName"):
                assert request[field] == ready[field], (key, field)
            assert ready["agentRuntimeId"] == cell["runtime"]["id"]
            assert ready["agentRuntimeArn"] == cell["runtime"]["arn"]
            row, ids = check_cell(cell, state["images"][size])
            output.append(row)
            sessions.extend(ids)
            runtimes.append(cell["runtime"]["id"])
    assert len(runtimes) == len(set(runtimes)) == 15
    assert len(sessions) == len(set(sessions)) == 1098
    assert sum(r["samples"] for r in output) == 1083
    successful = all(r["success"] == r["warm_success"] == r["samples"] for r in output)
    assert state["all_invocations_successful"] == successful
    assert state["exit_code"] == (0 if successful and all(r["stop_success"] == r["samples"] for r in output) else 1)
    print("PASS: 15 fresh cells, 1083 first attempts + 15 smoke; public SDK, source hashes, raw timings, stops and deletions")
    print(json.dumps(output, indent=2))
    return output


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("out", type=Path)
    verify(parser.parse_args().out.resolve())
