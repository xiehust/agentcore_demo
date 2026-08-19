#!/usr/bin/env python3
"""Measure one end-to-end cold invocation of an AgentCore Runtime.

The script verifies the managed Capacity Provider is scaled to zero, invokes
with a fresh runtime session ID, consumes the full streaming response, records
latency to headers/first byte/full body, and stops the runtime session
best-effort after measurement.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import boto3
from botocore.config import Config

DEFAULT_RUNTIME_ARN = (
    "arn:aws:bedrock-agentcore:us-west-2:434444145045:"
    "runtime/my_instances_agent-EAjRkZFB9z"
)
DEFAULT_CAPACITY_PROVIDER_ID = "capacity_provider_arm_kb-FQtDNVGq1t"
DEFAULT_REGION = "us-west-2"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def new_session_id() -> str:
    # AgentCore requires fresh runtime session IDs to be at least 33 chars.
    return f"coldstart-{uuid.uuid4().hex}"


def elapsed_ms(start: float, end: float) -> float:
    return round((end - start) * 1000.0, 1)


def inspect_capacity(region: str, capacity_provider_id: str) -> dict[str, Any]:
    """Capture scale-to-zero evidence without changing capacity."""
    asg_name = f"agentcore-managed-instances-{capacity_provider_id}"
    autoscaling = boto3.client("autoscaling", region_name=region)
    ec2 = boto3.client("ec2", region_name=region)

    groups = autoscaling.describe_auto_scaling_groups(
        AutoScalingGroupNames=[asg_name]
    )["AutoScalingGroups"]
    if not groups:
        return {
            "asg_name": asg_name,
            "error": "Auto Scaling group not found",
            "is_scaled_to_zero": None,
        }

    group = groups[0]
    reservations = ec2.describe_instances(
        Filters=[
            {
                "Name": "tag:bedrock-agentcore:capacity-provider-id",
                "Values": [capacity_provider_id],
            },
            {
                "Name": "instance-state-name",
                "Values": ["pending", "running", "stopping", "stopped"],
            },
        ]
    )["Reservations"]
    instances = [
        {
            "instance_id": instance["InstanceId"],
            "state": instance["State"]["Name"],
            "instance_type": instance["InstanceType"],
        }
        for reservation in reservations
        for instance in reservation["Instances"]
    ]

    desired = group["DesiredCapacity"]
    return {
        "asg_name": asg_name,
        "min_size": group["MinSize"],
        "desired_capacity": desired,
        "asg_instances": [
            {
                "instance_id": instance["InstanceId"],
                "lifecycle_state": instance["LifecycleState"],
                "health_status": instance["HealthStatus"],
            }
            for instance in group["Instances"]
        ],
        "ec2_instances": instances,
        "is_scaled_to_zero": desired == 0 and not group["Instances"] and not instances,
    }


def parse_body(raw: bytes) -> Any:
    text = raw.decode("utf-8", errors="replace")
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return text


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Measure one AgentCore Runtime cold-start invocation."
    )
    parser.add_argument("--runtime-arn", default=DEFAULT_RUNTIME_ARN)
    parser.add_argument(
        "--capacity-provider-id", default=DEFAULT_CAPACITY_PROVIDER_ID
    )
    parser.add_argument("--region", default=DEFAULT_REGION)
    parser.add_argument("--qualifier", default="DEFAULT")
    parser.add_argument(
        "--prompt",
        default="Reply with exactly: cold-start-ok",
        help="Prompt sent as the JSON field 'prompt'.",
    )
    parser.add_argument(
        "--timeout-seconds",
        type=int,
        default=900,
        help="SDK read timeout; capacity cold starts can take several minutes.",
    )
    parser.add_argument(
        "--results-dir",
        type=Path,
        default=Path(__file__).resolve().parent / "results",
    )
    parser.add_argument(
        "--allow-warm-capacity",
        action="store_true",
        help="Invoke even if the Capacity Provider is not scaled to zero.",
    )
    parser.add_argument(
        "--keep-session",
        action="store_true",
        help="Do not call StopRuntimeSession after the measurement.",
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    session_id = new_session_id()
    started_iso = utc_now()

    try:
        before = inspect_capacity(args.region, args.capacity_provider_id)
    except Exception as exc:  # preserve a useful result if evidence lookup fails
        before = {
            "error": f"{type(exc).__name__}: {exc}",
            "is_scaled_to_zero": None,
        }

    print(f"runtime ARN       : {args.runtime_arn}")
    print(f"runtime session   : {session_id}")
    print(f"capacity provider : {args.capacity_provider_id}")
    print(f"scaled to zero    : {before.get('is_scaled_to_zero')}")

    if before.get("is_scaled_to_zero") is False and not args.allow_warm_capacity:
        print(
            "ERROR: capacity is already warm; refusing to label this a cold test. "
            "Use --allow-warm-capacity to override.",
            file=sys.stderr,
        )
        return 2

    client = boto3.client(
        "bedrock-agentcore",
        region_name=args.region,
        config=Config(
            connect_timeout=30,
            read_timeout=args.timeout_seconds,
            retries={"total_max_attempts": 1, "mode": "standard"},
            tcp_keepalive=True,
        ),
    )

    result: dict[str, Any] = {
        "runtime_arn": args.runtime_arn,
        "qualifier": args.qualifier,
        "region": args.region,
        "capacity_provider_id": args.capacity_provider_id,
        "runtime_session_id": session_id,
        "started_at": started_iso,
        "payload": {"prompt": args.prompt},
        "capacity_before": before,
        "measurement": {},
        "success": False,
    }

    t0 = time.perf_counter()
    try:
        response = client.invoke_agent_runtime(
            agentRuntimeArn=args.runtime_arn,
            qualifier=args.qualifier,
            runtimeSessionId=session_id,
            contentType="application/json",
            accept="application/json",
            payload=json.dumps({"prompt": args.prompt}).encode("utf-8"),
        )
        t_headers = time.perf_counter()

        stream = response["response"]
        first = stream.read(1)
        t_first_byte = time.perf_counter()
        raw = first + stream.read()
        t_complete = time.perf_counter()

        status_code = response.get("statusCode") or response.get(
            "ResponseMetadata", {}
        ).get("HTTPStatusCode")
        result.update(
            success=status_code is not None and 200 <= int(status_code) < 300,
            status_code=status_code,
            content_type=response.get("contentType"),
            response_runtime_session_id=response.get("runtimeSessionId"),
            response_body=parse_body(raw),
            response_bytes=len(raw),
        )
        result["measurement"] = {
            "request_to_headers_ms": elapsed_ms(t0, t_headers),
            "request_to_first_body_byte_ms": elapsed_ms(t0, t_first_byte),
            "request_to_full_body_ms": elapsed_ms(t0, t_complete),
            "body_stream_ms": elapsed_ms(t_headers, t_complete),
        }
    except Exception as exc:
        t_failed = time.perf_counter()
        result["error"] = f"{type(exc).__name__}: {exc}"
        result["measurement"]["request_to_error_ms"] = elapsed_ms(t0, t_failed)
    finally:
        try:
            result["capacity_after"] = inspect_capacity(
                args.region, args.capacity_provider_id
            )
        except Exception as exc:
            result["capacity_after"] = {
                "error": f"{type(exc).__name__}: {exc}"
            }

        result["session_stop_requested"] = False
        if not args.keep_session:
            try:
                client.stop_runtime_session(
                    agentRuntimeArn=args.runtime_arn,
                    qualifier=args.qualifier,
                    runtimeSessionId=session_id,
                )
                result["session_stop_requested"] = True
            except Exception as exc:
                result["session_stop_error"] = f"{type(exc).__name__}: {exc}"

    result["finished_at"] = utc_now()
    args.results_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    output_path = args.results_dir / f"cold_start_{stamp}.json"
    output_path.write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n")

    metrics = result["measurement"]
    if result["success"]:
        print(f"HTTP status       : {result['status_code']}")
        print(f"headers latency   : {metrics['request_to_headers_ms']:.1f} ms")
        print(f"first-byte latency: {metrics['request_to_first_body_byte_ms']:.1f} ms")
        print(f"full response     : {metrics['request_to_full_body_ms']:.1f} ms")
        print(f"response bytes    : {result['response_bytes']}")
        print(f"response body     : {result['response_body']}")
    else:
        print(f"ERROR: {result.get('error', 'non-2xx response')}", file=sys.stderr)
    print(f"result file       : {output_path}")
    print(f"session stopped   : {result['session_stop_requested']}")
    return 0 if result["success"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
