#!/usr/bin/env python3
"""Long-horizon concurrency load test for the shared runtime demo.

Each virtual user builds and reviews a small offline web project over two
sequential agent invocations. Users run concurrently, while each user's later
phases resume the Claude session created by the previous phase.

The test uses one shared AgentCore runtimeSessionId and verifies the generated
files directly on the managed EC2 host through SSM. A unique run id is embedded
in every user id and project manifest so stale workspaces cannot pass.

Output: results/load_test_longrun_<timestamp>.json and a console summary.
"""

from __future__ import annotations

import base64
import concurrent.futures
import json
import os
import shlex
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


def _load_levels() -> list[int]:
    raw = json.loads(os.environ.get("LEVELS", "[2, 4, 8]"))
    if (
        not isinstance(raw, list)
        or not raw
        or any(not isinstance(value, int) or value < 1 for value in raw)
    ):
        raise ValueError("LEVELS must be a non-empty JSON array of positive integers")
    return raw


LEVELS = _load_levels()
SUCCESS_FLOOR = float(os.environ.get("SUCCESS_FLOOR", "0.75"))
LEVEL_PAUSE_S = int(os.environ.get("LEVEL_PAUSE_S", "20"))
MONITOR_DURATION_S = int(os.environ.get("MONITOR_DURATION_S", "3600"))
TASK_READ_TIMEOUT_S = int(os.environ.get("TASK_READ_TIMEOUT_S", "1200"))
HOST_USERS_ROOT = os.environ.get(
    "HOST_USERS_ROOT", "/var/lib/agentcore/volumes/scratch/users"
)
RUN_ID = os.environ.get("RUN_ID", uuid.uuid4().hex[:8])
SHARED_SESSION_ID = os.environ.get(
    "SHARED_SESSION_ID", f"shared-longrun-{RUN_ID}-{uuid.uuid4().hex}"
)

EXPECTED_FILES = (
    "index.html",
    "about.html",
    "styles.css",
    "app.js",
    "README.md",
    "loadtest.json",
)
PHASE_COUNT = 2

WARMUP_PROMPT = "Reply with exactly: LONGRUN-READY"
WARMUP_MARKER = "LONGRUN-READY"

agentcore = boto3.client(
    "bedrock-agentcore",
    region_name=REGION,
    config=Config(
        connect_timeout=30,
        read_timeout=TASK_READ_TIMEOUT_S,
        retries={"total_max_attempts": 1},
        max_pool_connections=max(32, max(LEVELS) + 4),
    ),
)
ssm = boto3.client("ssm", region_name=REGION)
ec2 = boto3.client("ec2", region_name=REGION)
_print_lock = threading.Lock()

MONITOR_SCRIPT = r"""#!/bin/bash
rm -f /tmp/loadmon.csv
echo "epoch,cpu_pct,mem_used_mb,mem_avail_mb,load1,agent_procs" > /tmp/loadmon.csv
END=$(( $(date +%s) + __DURATION__ ))
while [ "$(date +%s)" -lt "$END" ]; do
  IDLE=$(vmstat 1 2 | tail -1 | awk '{print $15}')
  CPU=$((100 - IDLE))
  MEM_USED=$(free -m | awk '/^Mem:/{print $3}')
  MEM_AVAIL=$(free -m | awk '/^Mem:/{print $7}')
  LOAD1=$(cut -d' ' -f1 /proc/loadavg)
  PROCS=$(ps -e -o comm= | grep -c -E '^(node|claude)' || true)
  echo "$(date +%s),$CPU,$MEM_USED,$MEM_AVAIL,$LOAD1,$PROCS" >> /tmp/loadmon.csv
  sleep 2
done
"""

HOST_VERIFY_SCRIPT = r"""
import json
import sys
from pathlib import Path

root = Path(sys.argv[1])
specs = json.loads(sys.argv[2])
expected = {
    "index.html", "about.html", "styles.css", "app.js", "README.md",
    "loadtest.json",
}
minimum_sizes = {
    "index.html": 500,
    "about.html": 250,
    "styles.css": 600,
    "app.js": 700,
    "README.md": 300,
    "loadtest.json": 100,
}

results = {}
for spec in specs:
    user_id = spec["user_id"]
    workspace = root / spec["workspace_slug"] / "webapp"
    errors = []
    sizes = {}

    for name in sorted(expected):
        path = workspace / name
        if path.is_symlink():
            errors.append(f"{name}: symlinks are not allowed")
        elif not path.is_file():
            errors.append(f"{name}: missing")
        else:
            sizes[name] = path.stat().st_size
            if sizes[name] < minimum_sizes[name]:
                errors.append(
                    f"{name}: too small ({sizes[name]} < {minimum_sizes[name]} bytes)"
                )

    def text(name):
        try:
            return (workspace / name).read_text(errors="replace")
        except OSError:
            return ""

    index = text("index.html")
    readme = text("README.md")
    styles = text("styles.css")
    app = text("app.js")

    for needle in (
        "Shared Runtime Project Board",
        "styles.css",
        "app.js",
    ):
        if needle not in index:
            errors.append(f"index.html: missing {needle!r}")
    for name in ("index.html", "about.html"):
        if text(name).count("<footer>v2.0</footer>") != 1:
            errors.append(f"{name}: expected one v2.0 footer")
    for breakpoint in ("480px", "768px"):
        if breakpoint not in styles:
            errors.append(f"styles.css: missing {breakpoint} breakpoint")
    if "localStorage" not in app:
        errors.append("app.js: localStorage not found")
    if "textContent" not in app:
        errors.append("app.js: textContent-based rendering not found")
    if spec["run_token"] not in readme:
        errors.append("README.md: run token not found")

    manifest = {}
    try:
        manifest = json.loads(text("loadtest.json"))
    except json.JSONDecodeError as exc:
        errors.append(f"loadtest.json: invalid JSON ({exc})")
    if not isinstance(manifest, dict):
        errors.append("loadtest.json: top-level value must be an object")
        manifest = {}
    if manifest.get("run_token") != spec["run_token"]:
        errors.append("loadtest.json: wrong run_token")
    if manifest.get("status") != "complete":
        errors.append("loadtest.json: status is not complete")
    manifest_files = manifest.get("expected_files")
    if not isinstance(manifest_files, list) or set(manifest_files) != expected:
        errors.append("loadtest.json: expected_files mismatch")

    results[user_id] = {
        "success": not errors,
        "workspace": str(workspace),
        "file_sizes": sizes,
        "errors": errors,
    }

print(json.dumps(results, separators=(",", ":")))
"""


def log(message: str) -> None:
    with _print_lock:
        print(message, flush=True)


def percentile(values: list[float], pct: float) -> float | None:
    if not values:
        return None
    sorted_values = sorted(values)
    position = (len(sorted_values) - 1) * pct / 100.0
    low = int(position)
    high = min(low + 1, len(sorted_values) - 1)
    return round(
        sorted_values[low]
        + (sorted_values[high] - sorted_values[low]) * (position - low),
        1,
    )


def project_phases(run_token: str) -> list[dict[str, str]]:
    expected_json = json.dumps(list(EXPECTED_FILES))
    return [
        {
            "name": "foundation",
            "marker": "PHASE-1-DONE 4",
            "prompt": f"""Create the foundation of an offline web project under webapp/.
The project is a responsive task board named "Shared Runtime Project Board".
This load-test run token is {run_token}; include it in webapp/README.md.

Create these four files:
1. index.html - accessible board UI with backlog/doing/done columns, add-task
   form, search field, stats, and links to styles.css and app.js.
2. styles.css - board/card/form/navigation styles, focus states, and explicit
   480px and 768px responsive breakpoints.
3. app.js - add, move, delete, filter and persist tasks with localStorage.
   Render user-controlled text with textContent, never innerHTML.
4. README.md - features, file table, usage, and the run token.

Read all four files back once and fix incomplete markup, broken references, or
missing requirements. Reply with exactly: PHASE-1-DONE 4""",
        },
        {
            "name": "final-qa",
            "marker": f"PROJECT-DONE {len(EXPECTED_FILES)}",
            "prompt": f"""Continue the existing webapp for run token {run_token}.
Preserve the four foundation files and add:
1. about.html - project purpose, feature list, keyboard notes and navigation.

Then perform final QA across all five files. Fix broken references, unsafe
user-text rendering, inaccessible controls, missing responsive rules, storage
errors, and documentation drift. Add exactly <footer>v2.0</footer> before
</body> in index.html and about.html. Confirm index.html references
styles.css and app.js, styles.css has explicit 480px and 768px breakpoints,
and app.js contains localStorage and textContent-based rendering.

Finally create webapp/loadtest.json as valid JSON with exactly these fields:
- "run_token": "{run_token}"
- "status": "complete"
- "expected_files": {expected_json}

Read loadtest.json and the two HTML files back once more. Do not claim success
until every requirement is present. Reply with exactly:
PROJECT-DONE {len(EXPECTED_FILES)}""",
        },
    ]


def parse_sse(raw: str) -> list[dict]:
    events = []
    for line in raw.splitlines():
        if not line.startswith("data:"):
            continue
        try:
            events.append(json.loads(line[5:].strip()))
        except json.JSONDecodeError:
            continue
    return events


def invoke_prompt(
    user_id: str,
    prompt: str,
    *,
    reset: bool,
    expected_marker: str,
) -> dict:
    started = time.perf_counter()
    record: dict = {
        "start_epoch": time.time(),
        "expected_marker": expected_marker,
    }
    try:
        response = agentcore.invoke_agent_runtime(
            agentRuntimeArn=RUNTIME["runtimeArn"],
            runtimeSessionId=SHARED_SESSION_ID,
            runtimeUserId=user_id,
            payload=json.dumps(
                {"prompt": prompt, "user_id": user_id, "reset": reset}
            ).encode(),
            contentType="application/json",
            accept="text/event-stream",
        )
        raw = response["response"].read().decode("utf-8", errors="replace")
        events = parse_sse(raw)
        complete = next((e for e in events if e.get("event") == "complete"), {})
        error_event = next((e for e in events if e.get("event") == "error"), None)
        result = complete.get("result") or ""
        marker_found = expected_marker in result
        record.update(
            success=marker_found
            and error_event is None
            and not complete.get("is_error"),
            marker_found=marker_found,
            result_tail=result[-160:],
            error=(error_event or {}).get("message"),
            is_error=complete.get("is_error"),
            tool_call_count=sum(e.get("event") == "tool" for e in events),
            denied_count=complete.get("denied_count"),
            workspace=complete.get("workspace"),
            claude_session_id=complete.get("claude_session_id"),
            resumed_from=complete.get("resumed_from"),
            fingerprint=(complete.get("instance") or {}).get("server_run_id"),
            hostname=(complete.get("instance") or {}).get("hostname"),
        )
        if not record["success"] and not record.get("error"):
            record["error"] = (
                f"expected marker {expected_marker!r} was not returned"
                if not marker_found
                else "agent returned an error result"
            )
    except Exception as exc:
        record.update(
            success=False,
            marker_found=False,
            error=f"{type(exc).__name__}: {exc}"[:500],
        )
    record["latency_ms"] = round((time.perf_counter() - started) * 1000.0, 1)
    record["end_epoch"] = time.time()
    return record


def invoke_project(
    user_id: str,
    run_token: str,
    barrier: threading.Barrier,
) -> dict:
    barrier.wait()
    started = time.perf_counter()
    record: dict = {
        "user_id": user_id,
        "run_token": run_token,
        "start_epoch": time.time(),
        "phases": [],
    }

    for index, phase in enumerate(project_phases(run_token)):
        phase_result = invoke_prompt(
            user_id,
            phase["prompt"],
            reset=index == 0,
            expected_marker=phase["marker"],
        )
        phase_result["name"] = phase["name"]
        record["phases"].append(phase_result)
        log(
            f"    [{user_id}] {index + 1}/{PHASE_COUNT} {phase['name']}: "
            f"ok={phase_result['success']} "
            f"time={phase_result['latency_ms'] / 1000:.1f}s "
            f"tools={phase_result.get('tool_call_count', 0)}"
        )
        if not phase_result["success"]:
            break

    phases = record["phases"]
    workspaces = {p.get("workspace") for p in phases if p.get("workspace")}
    fingerprints = {p.get("fingerprint") for p in phases if p.get("fingerprint")}
    session_chain_ok = len(phases) == PHASE_COUNT and all(
        phases[index].get("resumed_from")
        == phases[index - 1].get("claude_session_id")
        for index in range(1, len(phases))
    )
    workspace_consistent = len(workspaces) == 1 and len(phases) == PHASE_COUNT
    fingerprint_consistent = (
        len(fingerprints) == 1 and len(phases) == PHASE_COUNT
    )
    agent_success = (
        len(phases) == PHASE_COUNT
        and all(p["success"] for p in phases)
        and session_chain_ok
        and workspace_consistent
        and fingerprint_consistent
    )

    record.update(
        agent_success=agent_success,
        success=agent_success,
        session_chain_ok=session_chain_ok,
        workspace_consistent=workspace_consistent,
        fingerprint_consistent=fingerprint_consistent,
        workspace=next(iter(workspaces), None),
        fingerprint=next(iter(fingerprints), None),
        hostname=next((p.get("hostname") for p in phases if p.get("hostname")), None),
        tool_call_count=sum(p.get("tool_call_count", 0) for p in phases),
        error=next((p.get("error") for p in phases if p.get("error")), None),
    )
    record["latency_ms"] = round((time.perf_counter() - started) * 1000.0, 1)
    record["end_epoch"] = time.time()
    return record


def run_level(level: int) -> dict:
    barrier = threading.Barrier(level)
    started = time.time()
    with concurrent.futures.ThreadPoolExecutor(max_workers=level) as pool:
        futures = [
            pool.submit(
                invoke_project,
                f"long-{RUN_ID}-l{level}-u{index:02d}",
                f"{RUN_ID}-l{level}-u{index:02d}",
                barrier,
            )
            for index in range(level)
        ]
        requests = [future.result() for future in futures]
    finished = time.time()

    successful_latencies = [
        request["latency_ms"] for request in requests if request["agent_success"]
    ]
    agent_success_count = len(successful_latencies)
    summary = {
        "level": level,
        "window": [started, finished],
        "agent_success": agent_success_count,
        "agent_failed": level - agent_success_count,
        "agent_success_rate": round(agent_success_count / level, 3),
        "success": agent_success_count,
        "failed": level - agent_success_count,
        "success_rate": round(agent_success_count / level, 3),
        "task_p50_s": round((percentile(successful_latencies, 50) or 0) / 1000, 1),
        "task_p90_s": round((percentile(successful_latencies, 90) or 0) / 1000, 1),
        "task_max_s": round((percentile(successful_latencies, 100) or 0) / 1000, 1),
        "tool_calls_avg": round(
            sum(request["tool_call_count"] for request in requests) / level, 1
        ),
        "distinct_instances": len(
            {
                request.get("fingerprint")
                for request in requests
                if request.get("fingerprint")
            }
        ),
        "errors": [
            request["error"]
            for request in requests
            if not request["agent_success"] and request.get("error")
        ][:5],
        "requests": requests,
    }
    log(
        f"  level {level:>2}: agent_ok={agent_success_count}/{level} "
        f"p50={summary['task_p50_s']}s p90={summary['task_p90_s']}s "
        f"max={summary['task_max_s']}s tools_avg={summary['tool_calls_avg']} "
        f"instances={summary['distinct_instances']}"
    )
    return summary


def find_instance(container_hostname: str | None) -> str | None:
    autoscaling = boto3.client("autoscaling", region_name=REGION)
    groups = autoscaling.describe_auto_scaling_groups(
        AutoScalingGroupNames=[
            f"agentcore-managed-instances-{CAPACITY_PROVIDER_ID}"
        ]
    )["AutoScalingGroups"]
    instance_ids = [
        instance["InstanceId"]
        for group in groups
        for instance in group["Instances"]
        if instance["LifecycleState"] == "InService"
    ]
    if not instance_ids:
        return None
    if not container_hostname:
        return instance_ids[0]

    for reservation in ec2.describe_instances(InstanceIds=instance_ids)[
        "Reservations"
    ]:
        for instance in reservation["Instances"]:
            if instance.get("PrivateDnsName") == container_hostname:
                return instance["InstanceId"]
    return None


def ssm_run(instance_id: str, commands: list[str], timeout: int = 120) -> str:
    command_id = ssm.send_command(
        InstanceIds=[instance_id],
        DocumentName="AWS-RunShellScript",
        Parameters={"commands": commands, "executionTimeout": [str(timeout)]},
    )["Command"]["CommandId"]
    for _ in range(timeout // 2 + 10):
        time.sleep(2)
        invocation = ssm.get_command_invocation(
            CommandId=command_id, InstanceId=instance_id
        )
        if invocation["Status"] in ("Success", "Failed", "Cancelled", "TimedOut"):
            if invocation["Status"] != "Success":
                raise RuntimeError(
                    f"SSM {invocation['Status']}: "
                    f"{invocation.get('StandardErrorContent', '')[:500]}"
                )
            return invocation["StandardOutputContent"]
    raise TimeoutError("SSM command did not finish")


def start_monitor(instance_id: str) -> None:
    script = MONITOR_SCRIPT.replace("__DURATION__", str(MONITOR_DURATION_S))
    encoded = base64.b64encode(script.encode()).decode()
    ssm_run(
        instance_id,
        [
            "pkill -f /tmp/loadmon.sh 2>/dev/null || true",
            f"echo {encoded} | base64 -d > /tmp/loadmon.sh",
            "chmod +x /tmp/loadmon.sh",
            "nohup /tmp/loadmon.sh </dev/null >/tmp/loadmon.log 2>&1 & echo ok",
        ],
    )


def fetch_monitor(instance_id: str, *, stop: bool = True) -> list[dict]:
    commands = []
    if stop:
        commands.append("pkill -f /tmp/loadmon.sh 2>/dev/null || true")
    commands.append("cat /tmp/loadmon.csv")
    output = ssm_run(instance_id, commands)
    samples = []
    for line in output.splitlines():
        parts = line.strip().split(",")
        if len(parts) == 6 and parts[0].isdigit():
            samples.append(
                {
                    "epoch": int(parts[0]),
                    "cpu_pct": float(parts[1]),
                    "mem_used_mb": float(parts[2]),
                    "mem_avail_mb": float(parts[3]),
                    "load1": float(parts[4]),
                    "agent_procs": int(parts[5]),
                }
            )
    return samples


def verify_on_host(instance_id: str, requests: list[dict]) -> dict[str, dict]:
    specs = []
    for request in requests:
        workspace = request.get("workspace")
        if workspace:
            specs.append(
                {
                    "user_id": request["user_id"],
                    "workspace_slug": Path(workspace).name,
                    "run_token": request["run_token"],
                }
            )
    if not specs:
        return {}
    command = (
        f"python3 -c {shlex.quote(HOST_VERIFY_SCRIPT)} "
        f"{shlex.quote(HOST_USERS_ROOT)} "
        f"{shlex.quote(json.dumps(specs, separators=(',', ':')))}"
    )
    output = ssm_run(instance_id, [command], timeout=180)
    return json.loads(output)


def attach_host_verification(summary: dict, verification: dict[str, dict]) -> None:
    artifact_success_count = 0
    verified_success_count = 0
    artifact_errors = []

    for request in summary["requests"]:
        result = verification.get(
            request["user_id"],
            {
                "success": False,
                "errors": ["workspace was not returned or verified"],
            },
        )
        request["artifact_verification"] = result
        request["artifact_success"] = bool(result["success"])
        request["success"] = request["agent_success"] and request["artifact_success"]
        artifact_success_count += int(request["artifact_success"])
        verified_success_count += int(request["success"])
        if not request["artifact_success"]:
            artifact_errors.append(
                {
                    "user_id": request["user_id"],
                    "errors": result.get("errors", []),
                }
            )

    level = summary["level"]
    summary.update(
        verification_available=True,
        artifact_success=artifact_success_count,
        artifact_failed=level - artifact_success_count,
        verified_success=verified_success_count,
        verified_failed=level - verified_success_count,
        verified_success_rate=round(verified_success_count / level, 3),
        success=verified_success_count,
        failed=level - verified_success_count,
        success_rate=round(verified_success_count / level, 3),
        artifact_errors=artifact_errors[:5],
        host_verification={
            "users_checked": len(verification),
            "users_artifact_valid": artifact_success_count,
            "per_user": verification,
        },
    )


def window_stats(samples: list[dict], start: float, end: float) -> dict:
    window = [
        sample for sample in samples if start - 1 <= sample["epoch"] <= end + 1
    ]
    if not window:
        return {}
    return {
        "samples": len(window),
        "cpu_avg_pct": round(
            sum(sample["cpu_pct"] for sample in window) / len(window), 1
        ),
        "cpu_max_pct": max(sample["cpu_pct"] for sample in window),
        "mem_used_max_mb": max(sample["mem_used_mb"] for sample in window),
        "mem_avail_min_mb": min(sample["mem_avail_mb"] for sample in window),
        "load1_max": max(sample["load1"] for sample in window),
        "agent_procs_max": max(sample["agent_procs"] for sample in window),
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
        "generated": datetime.now(timezone.utc).isoformat(),
        "completed": completed,
        "runtime": RUNTIME,
        "config": {
            "run_id": RUN_ID,
            "levels": LEVELS,
            "success_floor": SUCCESS_FLOOR,
            "level_pause_s": LEVEL_PAUSE_S,
            "monitor_duration_s": MONITOR_DURATION_S,
            "task_read_timeout_s": TASK_READ_TIMEOUT_S,
            "phase_count": PHASE_COUNT,
            "expected_duration_minutes": [5, 10],
            "expected_files": list(EXPECTED_FILES),
        },
        "shared_session_id": SHARED_SESSION_ID,
        "instance_id": instance_id,
        "instance_type": instance_type,
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
    print(f"runtime        : {RUNTIME['runtimeArn']}", flush=True)
    print(f"shared session : {SHARED_SESSION_ID}", flush=True)
    print(f"run id         : {RUN_ID}", flush=True)
    print(f"levels         : {LEVELS}", flush=True)
    print(
        f"workload       : {PHASE_COUNT} phases, expected 5-10 minutes per user\n",
        flush=True,
    )

    wall_started = time.perf_counter()
    print("== phase 0: lightweight warmup ==", flush=True)
    warmup = invoke_prompt(
        f"long-{RUN_ID}-warmup",
        WARMUP_PROMPT,
        reset=True,
        expected_marker=WARMUP_MARKER,
    )
    print(
        f"  warmup ok={warmup['success']} "
        f"latency={warmup['latency_ms'] / 1000:.1f}s",
        flush=True,
    )
    if not warmup["success"]:
        print(f"  warmup failed: {warmup.get('error')}", file=sys.stderr)
        return 1

    print("\n== phase 1: locate instance and start sampler ==", flush=True)
    instance_id = find_instance(warmup.get("hostname"))
    if not instance_id:
        print(
            f"  could not map runtime hostname {warmup.get('hostname')!r} "
            "to a managed EC2 instance",
            file=sys.stderr,
        )
        return 1
    instance_type = ec2.describe_instances(InstanceIds=[instance_id])[
        "Reservations"
    ][0]["Instances"][0]["InstanceType"]
    print(f"  instance {instance_id} ({instance_type})", flush=True)
    start_monitor(instance_id)
    time.sleep(5)

    output_dir = ROOT / "results"
    output_dir.mkdir(exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = output_dir / f"load_test_longrun_{stamp}.json"

    print("\n== phase 2: long-task concurrency ramp ==", flush=True)
    levels: list[dict] = []
    latest_samples: list[dict] = []
    latest_monitor_error: str | None = None
    for level_index, level in enumerate(LEVELS):
        summary = run_level(level)
        levels.append(summary)
        write_results(
            path,
            instance_id=instance_id,
            instance_type=instance_type,
            warmup=warmup,
            levels=levels,
            monitor_samples=latest_samples,
            monitor_error=latest_monitor_error,
            completed=False,
            wall_started=wall_started,
        )
        print(f"    agent checkpoint: {path}", flush=True)
        try:
            verification = verify_on_host(instance_id, summary["requests"])
            attach_host_verification(summary, verification)
            print(
                f"    artifact check: {summary['artifact_success']}/{level} valid; "
                f"end-to-end: {summary['verified_success']}/{level}",
                flush=True,
            )
        except Exception as exc:
            summary["verification_available"] = False
            summary["host_verification"] = {"error": str(exc)[:500]}
            print(f"    artifact check unavailable: {exc}", file=sys.stderr, flush=True)
        write_results(
            path,
            instance_id=instance_id,
            instance_type=instance_type,
            warmup=warmup,
            levels=levels,
            monitor_samples=latest_samples,
            monitor_error=latest_monitor_error,
            completed=False,
            wall_started=wall_started,
        )
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
                flush=True,
            )
        write_results(
            path,
            instance_id=instance_id,
            instance_type=instance_type,
            warmup=warmup,
            levels=levels,
            monitor_samples=latest_samples,
            monitor_error=latest_monitor_error,
            completed=False,
            wall_started=wall_started,
        )
        print(f"    verified checkpoint: {path}", flush=True)
        if summary["success_rate"] < SUCCESS_FLOOR:
            print(
                f"  stopping ramp: success rate {summary['success_rate']} "
                f"< {SUCCESS_FLOOR}",
                flush=True,
            )
            break
        if level_index < len(LEVELS) - 1:
            time.sleep(LEVEL_PAUSE_S)

    print("\n== phase 3: collect monitor data ==", flush=True)
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
            flush=True,
        )
    print(f"  {len(samples)} samples", flush=True)
    for summary in levels:
        summary["resources"] = window_stats(samples, *summary["window"])

    write_results(
        path,
        instance_id=instance_id,
        instance_type=instance_type,
        warmup=warmup,
        levels=levels,
        monitor_samples=samples,
        monitor_error=monitor_error,
        completed=True,
        wall_started=wall_started,
    )

    print(
        f"\n{'level':>5} {'agent':>8} {'verified':>9} {'p50 s':>7} "
        f"{'p90 s':>7} {'max s':>7} {'cpu avg':>8} {'mem min':>8} {'procs':>6}"
    )
    for summary in levels:
        resources = summary.get("resources", {})
        verified = (
            f"{summary['verified_success']}/{summary['level']}"
            if summary.get("verification_available")
            else "n/a"
        )
        print(
            f"{summary['level']:>5} "
            f"{summary['agent_success']:>3}/{summary['level']:<4} "
            f"{verified:>9} "
            f"{summary['task_p50_s']:>7} {summary['task_p90_s']:>7} "
            f"{summary['task_max_s']:>7} "
            f"{resources.get('cpu_avg_pct', '-'):>7}% "
            f"{resources.get('mem_avail_min_mb', '-'):>8} "
            f"{resources.get('agent_procs_max', '-'):>6}"
        )
    print(f"\nresults: {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
