#!/usr/bin/env python3
"""Concurrency load test for the shared-runtime-session demo.

Ramps concurrent users against ONE shared runtimeSessionId while a background
sampler (started via SSM on the managed EC2 instance) records CPU, memory,
load average and node (Claude CLI) process count every ~3 seconds.

Phases:
  0. warmup      — one request; boots capacity / absorbs cold start.
  1. locate      — find the managed EC2 instance and start the SSM sampler.
  2. ramp        — for each level in LEVELS fire N simultaneous user requests
                   (all sharing the session); stop early when the success
                   rate drops below SUCCESS_FLOOR.
  3. collect     — fetch the sampler CSV, correlate per-level time windows.

Output: results/load_test_<ts>.json + console summary table.
"""

from __future__ import annotations

import base64
import concurrent.futures
import json
import os
import sys
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

import boto3
from botocore.config import Config

ROOT = Path(__file__).resolve().parent.parent
RUNTIME = json.loads((ROOT / "runtime.json").read_text())
REGION = RUNTIME["region"]
CAPACITY_PROVIDER_ID = RUNTIME["capacityProviderArn"].split("/")[-1]

LEVELS = json.loads(os.environ.get("LEVELS", "[2, 4, 6, 8, 12, 16, 20]"))
SUCCESS_FLOOR = float(os.environ.get("SUCCESS_FLOOR", "0.8"))
LEVEL_PAUSE_S = int(os.environ.get("LEVEL_PAUSE_S", "10"))
MONITOR_DURATION_S = int(os.environ.get("MONITOR_DURATION_S", "2100"))
TASK_READ_TIMEOUT_S = int(os.environ.get("TASK_READ_TIMEOUT_S", "900"))
SERVER_MAX_PARALLEL_AGENTS = int(
    os.environ.get("SERVER_MAX_PARALLEL_AGENTS", "0")
) or None
PROMPT = "Reply with exactly: pong"

RUN_ID = uuid.uuid4().hex[:8]
SHARED_SESSION_ID = f"shared-load-{RUN_ID}-{uuid.uuid4().hex}"

agentcore = boto3.client(
    "bedrock-agentcore",
    region_name=REGION,
    config=Config(
        connect_timeout=30,
        read_timeout=TASK_READ_TIMEOUT_S,
        retries={"total_max_attempts": 1},
        max_pool_connections=64,
    ),
)
ssm = boto3.client("ssm", region_name=REGION)
ec2 = boto3.client("ec2", region_name=REGION)

MONITOR_SCRIPT = r"""#!/bin/bash
# LOADMON sampler — one line every ~3s.
rm -f /tmp/loadmon.csv
echo "epoch,cpu_pct,mem_used_mb,mem_avail_mb,load1,node_procs" > /tmp/loadmon.csv
END=$(( $(date +%s) + __DURATION__ ))
while [ "$(date +%s)" -lt "$END" ]; do
  IDLE=$(vmstat 1 2 | tail -1 | awk '{print $15}')
  CPU=$((100 - IDLE))
  MEM_USED=$(free -m | awk '/^Mem:/{print $3}')
  MEM_AVAIL=$(free -m | awk '/^Mem:/{print $7}')
  LOAD1=$(cut -d' ' -f1 /proc/loadavg)
  NODES=$(ps -e -o comm= | grep -c -E '^(node|claude)' || true)
  echo "$(date +%s),$CPU,$MEM_USED,$MEM_AVAIL,$LOAD1,$NODES" >> /tmp/loadmon.csv
  sleep 2
done
"""


def utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def percentile(vals: list[float], pct: float) -> float | None:
    if not vals:
        return None
    s = sorted(vals)
    k = (len(s) - 1) * pct / 100.0
    lo, hi = int(k), min(int(k) + 1, len(s) - 1)
    return round(s[lo] + (s[hi] - s[lo]) * (k - lo), 1)


def invoke_once(user_id: str, barrier: threading.Barrier | None) -> dict:
    if barrier is not None:
        barrier.wait()
    t0 = time.perf_counter()
    rec: dict = {"user_id": user_id, "start_epoch": time.time()}
    try:
        resp = agentcore.invoke_agent_runtime(
            agentRuntimeArn=RUNTIME["runtimeArn"],
            runtimeSessionId=SHARED_SESSION_ID,
            runtimeUserId=user_id,
            payload=json.dumps(
                {"prompt": PROMPT, "user_id": user_id, "reset": True}
            ).encode(),
            contentType="application/json",
            accept="text/event-stream",
        )
        raw = resp["response"].read().decode("utf-8", errors="replace")
        events = []
        for line in raw.splitlines():
            if line.startswith("data: "):
                try:
                    events.append(json.loads(line[6:]))
                except json.JSONDecodeError:
                    pass
        complete = next((e for e in events if e.get("event") == "complete"), {})
        error = next((e for e in events if e.get("event") == "error"), None)
        rec.update(
            success=bool(complete.get("result")) and error is None,
            result=(complete.get("result") or "")[:60],
            error=(error or {}).get("message"),
            fingerprint=(complete.get("instance") or {}).get("server_run_id"),
            hostname=(complete.get("instance") or {}).get("hostname"),
        )
        if not rec["success"] and not rec.get("error"):
            rec["error"] = "complete event or result missing"
    except Exception as exc:
        rec.update(success=False, error=f"{type(exc).__name__}: {exc}"[:300],
                   fingerprint=None)
    rec["latency_ms"] = round((time.perf_counter() - t0) * 1000.0, 1)
    rec["end_epoch"] = time.time()
    return rec


def run_level(level: int) -> dict:
    barrier = threading.Barrier(level)
    started = time.time()
    with concurrent.futures.ThreadPoolExecutor(max_workers=level) as pool:
        futures = [
            pool.submit(
                invoke_once,
                f"load-{RUN_ID}-l{level}-u{i:02d}",
                barrier,
            )
            for i in range(level)
        ]
        requests = [f.result() for f in futures]
    finished = time.time()
    lat_ok = [r["latency_ms"] for r in requests if r["success"]]
    errors = [r["error"] for r in requests if not r["success"]]
    fingerprints = {r["fingerprint"] for r in requests if r.get("fingerprint")}
    summary = {
        "level": level,
        "window": [started, finished],
        "success": len(lat_ok),
        "failed": level - len(lat_ok),
        "success_rate": round(len(lat_ok) / level, 3),
        "latency_p50_ms": percentile(lat_ok, 50),
        "latency_p90_ms": percentile(lat_ok, 90),
        "latency_max_ms": percentile(lat_ok, 100),
        "distinct_instances": len(fingerprints),
        "errors": errors[:5],
        "requests": requests,
    }
    print(
        f"  level {level:>2}: ok={summary['success']}/{level} "
        f"p50={summary['latency_p50_ms']} p90={summary['latency_p90_ms']} "
        f"max={summary['latency_max_ms']} ms "
        f"instances={summary['distinct_instances']}"
    )
    return summary


def find_instance(container_hostname: str | None) -> str | None:
    """Match the server-reported hostname to the ASG instance hosting it.

    AgentCore may keep several warm instances in the ASG; the shared session
    is pinned to exactly one of them, identified by its private DNS name.
    """
    autoscaling = boto3.client("autoscaling", region_name=REGION)
    groups = autoscaling.describe_auto_scaling_groups(
        AutoScalingGroupNames=[
            f"agentcore-managed-instances-{CAPACITY_PROVIDER_ID}"
        ]
    )["AutoScalingGroups"]
    ids = [
        inst["InstanceId"]
        for group in groups
        for inst in group["Instances"]
        if inst["LifecycleState"] == "InService"
    ]
    if not ids:
        return None
    if not container_hostname:
        return ids[0]
    reservations = ec2.describe_instances(InstanceIds=ids)["Reservations"]
    for r in reservations:
        for inst in r["Instances"]:
            if inst.get("PrivateDnsName", "").startswith(container_hostname):
                return inst["InstanceId"]
    return None


def ssm_run(instance_id: str, commands: list[str], timeout: int = 60) -> str:
    cmd = ssm.send_command(
        InstanceIds=[instance_id],
        DocumentName="AWS-RunShellScript",
        Parameters={"commands": commands, "executionTimeout": [str(timeout)]},
    )["Command"]["CommandId"]
    for _ in range(timeout // 2 + 5):
        time.sleep(2)
        inv = ssm.get_command_invocation(CommandId=cmd, InstanceId=instance_id)
        if inv["Status"] in ("Success", "Failed", "Cancelled", "TimedOut"):
            if inv["Status"] != "Success":
                raise RuntimeError(f"SSM {inv['Status']}: {inv.get('StandardErrorContent','')[:200]}")
            return inv["StandardOutputContent"]
    raise TimeoutError("SSM command did not finish")


def start_monitor(instance_id: str) -> None:
    script = MONITOR_SCRIPT.replace("__DURATION__", str(MONITOR_DURATION_S))
    b64 = base64.b64encode(script.encode()).decode()
    ssm_run(
        instance_id,
        [
            "pkill -f /tmp/loadmon.sh 2>/dev/null || true",
            f"echo {b64} | base64 -d > /tmp/loadmon.sh",
            "chmod +x /tmp/loadmon.sh",
            "nohup /tmp/loadmon.sh </dev/null >/tmp/loadmon.log 2>&1 & echo sampler_pid=$!",
        ],
    )


def fetch_monitor(instance_id: str, *, stop: bool = True) -> list[dict]:
    commands = []
    if stop:
        commands.append("pkill -f /tmp/loadmon.sh 2>/dev/null || true")
    commands.append("cat /tmp/loadmon.csv")
    out = ssm_run(instance_id, commands, timeout=60)
    samples = []
    for line in out.splitlines():
        parts = line.strip().split(",")
        if len(parts) == 6 and parts[0].isdigit():
            samples.append(
                {
                    "epoch": int(parts[0]),
                    "cpu_pct": float(parts[1]),
                    "mem_used_mb": float(parts[2]),
                    "mem_avail_mb": float(parts[3]),
                    "load1": float(parts[4]),
                    "node_procs": int(parts[5]),
                }
            )
    return samples


def window_stats(samples: list[dict], t0: float, t1: float) -> dict:
    win = [s for s in samples if t0 - 1 <= s["epoch"] <= t1 + 1]
    if not win:
        return {}
    return {
        "samples": len(win),
        "cpu_avg_pct": round(sum(s["cpu_pct"] for s in win) / len(win), 1),
        "cpu_max_pct": max(s["cpu_pct"] for s in win),
        "mem_used_max_mb": max(s["mem_used_mb"] for s in win),
        "mem_avail_min_mb": min(s["mem_avail_mb"] for s in win),
        "load1_max": max(s["load1"] for s in win),
        "node_procs_max": max(s["node_procs"] for s in win),
    }


def write_results(
    path: Path,
    *,
    instance_id: str,
    instance_type: str,
    warmup: dict,
    levels: list[dict],
    monitor_samples: list[dict],
    monitor_error: str | None,
    completed: bool,
    wall_started: float,
) -> None:
    payload = {
        "generated": utc_iso(),
        "completed": completed,
        "runtime": RUNTIME,
        "config": {
            "run_id": RUN_ID,
            "levels": LEVELS,
            "success_floor": SUCCESS_FLOOR,
            "level_pause_s": LEVEL_PAUSE_S,
            "monitor_duration_s": MONITOR_DURATION_S,
            "task_read_timeout_s": TASK_READ_TIMEOUT_S,
            "server_max_parallel_agents": SERVER_MAX_PARALLEL_AGENTS,
        },
        "shared_session_id": SHARED_SESSION_ID,
        "instance_id": instance_id,
        "instance_type": instance_type,
        "prompt": PROMPT,
        "warmup": warmup,
        "levels": levels,
        "monitor_samples": monitor_samples,
        "monitor_error": monitor_error,
        "total_wall_s": round(time.perf_counter() - wall_started, 1),
    }
    temporary = path.with_suffix(f"{path.suffix}.tmp")
    temporary.write_text(json.dumps(payload, indent=2) + "\n")
    temporary.replace(path)


def main() -> int:
    print(f"runtime        : {RUNTIME['runtimeArn']}")
    print(f"shared session : {SHARED_SESSION_ID}")
    print(f"run id         : {RUN_ID}")
    print(f"levels         : {LEVELS}\n")

    wall_started = time.perf_counter()
    print("== phase 0: warmup ==")
    warm = invoke_once(f"load-{RUN_ID}-warmup", None)
    print(f"  warmup ok={warm['success']} latency={warm['latency_ms']}ms")
    if not warm["success"]:
        print(f"  warmup failed: {warm.get('error')}", file=sys.stderr)
        return 1

    print("\n== phase 1: locate instance & start sampler ==")
    instance_id = find_instance(warm.get("hostname"))
    if not instance_id:
        print("  no running managed instance found", file=sys.stderr)
        return 1
    itype = ec2.describe_instances(InstanceIds=[instance_id])["Reservations"][0][
        "Instances"][0]["InstanceType"]
    print(f"  instance {instance_id} ({itype})")
    start_monitor(instance_id)
    print("  sampler started (/tmp/loadmon.csv)")
    time.sleep(5)

    out = ROOT / "results"
    out.mkdir(exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = out / f"load_test_{stamp}.json"

    print("\n== phase 2: concurrency ramp ==")
    levels: list[dict] = []
    latest_samples: list[dict] = []
    latest_monitor_error: str | None = None
    for level_index, level in enumerate(LEVELS):
        summary = run_level(level)
        levels.append(summary)
        write_results(
            path,
            instance_id=instance_id,
            instance_type=itype,
            warmup=warm,
            levels=levels,
            monitor_samples=latest_samples,
            monitor_error=latest_monitor_error,
            completed=False,
            wall_started=wall_started,
        )
        print(f"    agent checkpoint: {path}")
        try:
            latest_samples = fetch_monitor(instance_id, stop=False)
            latest_monitor_error = None
            for completed_summary in levels:
                completed_summary["resources"] = window_stats(
                    latest_samples, *completed_summary["window"]
                )
        except Exception as exc:
            latest_monitor_error = f"{type(exc).__name__}: {exc}"[:500]
            print(
                f"    monitor checkpoint unavailable: {latest_monitor_error}",
                file=sys.stderr,
            )
        write_results(
            path,
            instance_id=instance_id,
            instance_type=itype,
            warmup=warm,
            levels=levels,
            monitor_samples=latest_samples,
            monitor_error=latest_monitor_error,
            completed=False,
            wall_started=wall_started,
        )
        print(f"    verified checkpoint: {path}")
        if summary["success_rate"] < SUCCESS_FLOOR:
            print(f"  stopping ramp: success rate {summary['success_rate']} "
                  f"< {SUCCESS_FLOOR}")
            break
        if level_index < len(LEVELS) - 1:
            time.sleep(LEVEL_PAUSE_S)

    print("\n== phase 3: collect monitor data ==")
    monitor_error = latest_monitor_error
    try:
        samples = fetch_monitor(instance_id)
        monitor_error = None
    except Exception as exc:
        samples = latest_samples
        monitor_error = f"{type(exc).__name__}: {exc}"[:500]
        print(
            f"  final monitor collection unavailable: {monitor_error}",
            file=sys.stderr,
        )
    print(f"  {len(samples)} samples collected")
    for summary in levels:
        summary["resources"] = window_stats(samples, *summary["window"])

    write_results(
        path,
        instance_id=instance_id,
        instance_type=itype,
        warmup=warm,
        levels=levels,
        monitor_samples=samples,
        monitor_error=monitor_error,
        completed=True,
        wall_started=wall_started,
    )

    print(f"\n{'level':>5} {'ok':>5} {'p50 ms':>8} {'p90 ms':>8} {'max ms':>8} "
          f"{'cpu avg':>8} {'cpu max':>8} {'mem min avail':>14} {'nodes':>6}")
    for s in levels:
        r = s.get("resources", {})
        print(f"{s['level']:>5} {s['success']:>3}/{s['level']:<2} "
              f"{s['latency_p50_ms'] or '-':>8} {s['latency_p90_ms'] or '-':>8} "
              f"{s['latency_max_ms'] or '-':>8} "
              f"{r.get('cpu_avg_pct','-'):>7}% {r.get('cpu_max_pct','-'):>7}% "
              f"{r.get('mem_avail_min_mb','-'):>11} MB {r.get('node_procs_max','-'):>6}")
    print(f"\nresults: {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
