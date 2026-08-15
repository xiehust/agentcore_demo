#!/usr/bin/env python3
"""Two-phase long-task concurrency ramp for one shared microVM session."""

from __future__ import annotations

import argparse
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
from typing import Any

from runtime_session import (
    RuntimeSession,
    atomic_write_json,
    cleanup_session,
    enforce_unique_workspaces,
    finalize_before_session_stop,
    new_session_id,
    parse_levels,
    percentile,
    read_monitor,
    start_monitor,
    utc_iso,
    validate_session_id,
    window_stats,
)

ROOT = Path(__file__).resolve().parent.parent
EXPECTED_FILES = (
    "index.html",
    "about.html",
    "styles.css",
    "app.js",
    "README.md",
    "loadtest.json",
)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Run two resumable web-project phases per user, verify exactly six "
            "files inside the active microVM, and collect /proc metrics."
        )
    )
    parser.add_argument("--config", default=str(ROOT / "runtime.json"))
    parser.add_argument(
        "--levels",
        default=os.environ.get("LEVELS", "1,2,4"),
        help="Comma-separated levels or JSON array (default: 1,2,4)",
    )
    parser.add_argument(
        "--success-floor",
        type=float,
        default=float(os.environ.get("SUCCESS_FLOOR", "0.75")),
    )
    parser.add_argument(
        "--request-timeout",
        type=int,
        default=int(os.environ.get("TASK_READ_TIMEOUT_S", "1800")),
    )
    parser.add_argument(
        "--level-pause",
        type=float,
        default=float(os.environ.get("LEVEL_PAUSE_S", "20")),
    )
    parser.add_argument(
        "--monitor-duration",
        type=int,
        default=int(os.environ.get("MONITOR_DURATION_S", "7200")),
    )
    parser.add_argument("--session-id")
    parser.add_argument("--output", help="Result JSON path (default: timestamped)")
    parser.add_argument(
        "--keep-session",
        action="store_true",
        default=os.environ.get("STOP_SESSION", "1") == "0",
        help="Skip StopRuntimeSession (also selected by STOP_SESSION=0; billable)",
    )
    args = parser.parse_args(argv)
    try:
        args.levels = parse_levels(args.levels)
    except ValueError as exc:
        parser.error(str(exc))
    if not 0 <= args.success_floor <= 1:
        parser.error("--success-floor must be between 0 and 1")
    if args.request_timeout < 1:
        parser.error("--request-timeout must be positive")
    if args.level_pause < 0:
        parser.error("--level-pause cannot be negative")
    if not 5 <= args.monitor_duration <= 28800:
        parser.error("--monitor-duration must be 5..28800 seconds")
    if args.session_id:
        try:
            validate_session_id(args.session_id)
        except ValueError as exc:
            parser.error(str(exc))
    return args


def project_phases(run_token: str) -> list[dict[str, str]]:
    expected_json = json.dumps(list(EXPECTED_FILES))
    return [
        {
            "name": "foundation",
            "marker": "PHASE-1-DONE 4",
            "prompt": f"""Create an offline project under webapp/ for run token {run_token}.
The project is a responsive task board named "Shared Runtime Project Board".
Create exactly these four foundation files:
1. index.html with accessible backlog/doing/done columns, add-task form,
   search, stats, navigation, and links to styles.css and app.js.
2. styles.css with board/card/form/focus styles and explicit 480px and 768px
   responsive breakpoints.
3. app.js with add/move/delete/filter, localStorage persistence, and safe
   textContent rendering. The literal token innerHTML must not appear anywhere
   in app.js, including comments.
4. README.md with features, usage, a file table, and token {run_token}.
Read all four files back and fix omissions. Reply exactly: PHASE-1-DONE 4""",
        },
        {
            "name": "final-qa",
            "marker": "PROJECT-DONE 6",
            "prompt": f"""Resume the existing webapp for run token {run_token}.
Preserve the four foundation files. Add about.html with purpose, features,
keyboard notes, a stylesheet reference, and navigation back to index.html.
Ensure index.html links to about.html. Add exactly <footer>v2.0</footer> before
</body> in both HTML files. Recheck accessibility, references, 480px/768px
breakpoints, localStorage, and textContent safety. Remove every occurrence of
literal token innerHTML from app.js, including comments.
Create loadtest.json as valid JSON with exactly these keys and values:
- "run_token": "{run_token}"
- "status": "complete"
- "expected_files": {expected_json}
The webapp directory must contain exactly these six files and no others:
{", ".join(EXPECTED_FILES)}.
Read both HTML files and loadtest.json before replying exactly: PROJECT-DONE 6""",
        },
    ]


def invoke_phase(
    session: RuntimeSession,
    user_id: str,
    phase: dict[str, str],
    *,
    reset: bool,
) -> dict[str, Any]:
    started_epoch = time.time()
    started = time.perf_counter()
    try:
        record = session.invoke(user_id, phase["prompt"], reset=reset)
    except Exception as exc:
        record = {
            "success": False,
            "error": f"{type(exc).__name__}: {exc}"[:500],
            "latency_ms": round((time.perf_counter() - started) * 1000.0, 1),
        }
    record.update(
        name=phase["name"],
        expected_marker=phase["marker"],
        marker_ok=phase["marker"] in (record.get("result") or ""),
        result_tail=(record.get("result") or "")[-200:],
        start_epoch=started_epoch,
        end_epoch=time.time(),
    )
    record["phase_success"] = bool(record.get("success") and record["marker_ok"])
    # Full text and verbose event deltas are unnecessary in load-test artifacts.
    record.pop("result", None)
    record.pop("events", None)
    return record


def invoke_project(
    session: RuntimeSession,
    user_id: str,
    run_token: str,
    barrier: threading.Barrier,
    expected_instance: dict[str, Any],
) -> dict[str, Any]:
    barrier.wait()
    started = time.perf_counter()
    record: dict[str, Any] = {
        "user_id": user_id,
        "run_token": run_token,
        "start_epoch": time.time(),
        "phases": [],
    }
    for index, phase in enumerate(project_phases(run_token)):
        phase_result = invoke_phase(session, user_id, phase, reset=index == 0)
        record["phases"].append(phase_result)
        print(
            f"    [{user_id}] {phase['name']}: "
            f"ok={phase_result['phase_success']} "
            f"time={phase_result.get('latency_ms', 0) / 1000:.1f}s "
            f"tools={phase_result.get('tool_call_count', 0)}",
            flush=True,
        )
        if not phase_result["phase_success"]:
            break

    phases = record["phases"]
    workspaces = {phase.get("workspace") for phase in phases if phase.get("workspace")}
    instances = [phase.get("instance") for phase in phases]
    session_chain_ok = len(phases) == 2 and (
        phases[1].get("resumed_from") == phases[0].get("claude_session_id")
    )
    workspace_consistent = len(phases) == 2 and len(workspaces) == 1
    process_consistent = len(phases) == 2 and all(
        instance == expected_instance for instance in instances
    )
    agent_success = bool(
        len(phases) == 2
        and all(phase["phase_success"] for phase in phases)
        and session_chain_ok
        and workspace_consistent
        and process_consistent
    )
    record.update(
        agent_success=agent_success,
        session_chain_ok=session_chain_ok,
        workspace_consistent=workspace_consistent,
        process_consistent=process_consistent,
        workspace=next(iter(workspaces), None),
        instance_fingerprint=instances[0] if instances else None,
        server_run_id=(
            (instances[0] or {}).get("server_run_id") if instances else None
        ),
        tool_call_count=sum(phase.get("tool_call_count", 0) for phase in phases),
        error=next(
            (phase.get("error") for phase in phases if phase.get("error")), None
        ),
        end_epoch=time.time(),
        latency_ms=round((time.perf_counter() - started) * 1000.0, 1),
    )
    return record


def run_level(
    session: RuntimeSession,
    level: int,
    run_id: str,
    expected_instance: dict[str, Any],
) -> dict[str, Any]:
    barrier = threading.Barrier(level)
    window_start = time.time()
    with concurrent.futures.ThreadPoolExecutor(max_workers=level) as pool:
        futures = []
        for index in range(level):
            user_id = f"long-{run_id}-l{level}-u{index:03d}"
            run_token = f"{run_id}-l{level}-u{index:03d}"
            futures.append(
                pool.submit(
                    invoke_project,
                    session,
                    user_id,
                    run_token,
                    barrier,
                    expected_instance,
                )
            )
        requests = [future.result() for future in futures]
    window_end = time.time()
    distinct_workspaces = enforce_unique_workspaces(requests, "agent_success")
    successful = [item for item in requests if item["agent_success"]]
    latencies = [item["latency_ms"] for item in successful]
    summary = {
        "level": level,
        "window": [window_start, window_end],
        "agent_success": len(successful),
        "agent_failed": level - len(successful),
        "agent_success_rate": round(len(successful) / level, 3),
        "task_p50_s": (
            round((percentile(latencies, 50) or 0) / 1000.0, 1) if latencies else None
        ),
        "task_p90_s": (
            round((percentile(latencies, 90) or 0) / 1000.0, 1) if latencies else None
        ),
        "task_max_s": (
            round((percentile(latencies, 100) or 0) / 1000.0, 1) if latencies else None
        ),
        "tool_calls_avg": round(
            sum(item["tool_call_count"] for item in requests) / level, 1
        ),
        "distinct_workspaces": distinct_workspaces,
        "distinct_server_processes": len(
            {
                json.dumps(item.get("instance_fingerprint"), sort_keys=True)
                for item in requests
                if item.get("instance_fingerprint")
            }
        ),
        "errors": [
            {
                "user_id": item["user_id"],
                "error": item.get("error") or "agent contract failed",
            }
            for item in requests
            if not item["agent_success"]
        ][:10],
        "requests": requests,
        "verification_available": False,
        "success": None,
        "failed": None,
        "success_rate": None,
        "monitor_available": False,
        "resources": {},
    }
    print(
        f"  level {level:>3}: agent_ok={len(successful)}/{level} "
        f"p50={summary['task_p50_s']}s p90={summary['task_p90_s']}s "
        f"max={summary['task_max_s']}s",
        flush=True,
    )
    return summary


_ARTIFACT_VERIFIER = r"""#!/usr/bin/env python3
import json
import sys
from pathlib import Path

SPECS = json.loads(Path(sys.argv[1]).read_text())
USERS_ROOT = Path("/tmp/agentcore-users").resolve()
EXPECTED_ORDER = [
    "index.html",
    "about.html",
    "styles.css",
    "app.js",
    "README.md",
    "loadtest.json",
]
EXPECTED = set(EXPECTED_ORDER)
MINIMUM = {
    "index.html": 500,
    "about.html": 250,
    "styles.css": 600,
    "app.js": 700,
    "README.md": 300,
    "loadtest.json": 100,
}
results = {}

for spec in SPECS:
    errors = []
    sizes = {}
    workspace_raw = Path(spec["workspace"])
    if workspace_raw.is_symlink():
        errors.append("workspace cannot be a symlink")
    try:
        workspace = workspace_raw.resolve(strict=True)
    except OSError as exc:
        workspace = workspace_raw.resolve()
        errors.append(f"workspace unavailable: {exc}")
    if USERS_ROOT not in workspace.parents:
        errors.append("workspace is outside /tmp/agentcore-users")
    project = workspace / "webapp"
    if not project.is_dir() or project.is_symlink():
        errors.append("webapp must be a real directory")
        actual = set()
    else:
        actual = {entry.name for entry in project.iterdir()}
        if actual != EXPECTED:
            errors.append(
                "webapp entries mismatch: expected=" + repr(sorted(EXPECTED))
                + " actual=" + repr(sorted(actual))
            )

    contents = {}
    for name in sorted(EXPECTED):
        path = project / name
        if path.is_symlink():
            errors.append(f"{name}: symlinks are not allowed")
        elif not path.is_file():
            errors.append(f"{name}: missing")
        else:
            size = path.stat().st_size
            sizes[name] = size
            if size < MINIMUM[name]:
                errors.append(f"{name}: too small ({size} < {MINIMUM[name]})")
            if size > 2_000_000:
                errors.append(f"{name}: unexpectedly large ({size})")
            else:
                contents[name] = path.read_text(errors="replace")

    index = contents.get("index.html", "")
    about = contents.get("about.html", "")
    styles = contents.get("styles.css", "")
    app = contents.get("app.js", "")
    readme = contents.get("README.md", "")
    for needle in ("Shared Runtime Project Board", "styles.css", "app.js", "about.html"):
        if needle not in index:
            errors.append(f"index.html: missing {needle!r}")
    for name, html, backlink in (
        ("index.html", index, "about.html"),
        ("about.html", about, "index.html"),
    ):
        if html.count("<footer>v2.0</footer>") != 1:
            errors.append(f"{name}: expected exactly one v2.0 footer")
        if "styles.css" not in html:
            errors.append(f"{name}: missing styles.css reference")
        if backlink not in html:
            errors.append(f"{name}: missing {backlink} navigation")
    for breakpoint in ("480px", "768px"):
        if breakpoint not in styles:
            errors.append(f"styles.css: missing {breakpoint} breakpoint")
    if "localStorage" not in app:
        errors.append("app.js: localStorage not found")
    if "textContent" not in app:
        errors.append("app.js: textContent not found")
    if "innerHTML" in app:
        errors.append("app.js: innerHTML is forbidden by this workload")
    if spec["run_token"] not in readme:
        errors.append("README.md: run token not found")

    manifest = {}
    try:
        manifest = json.loads(contents.get("loadtest.json", ""))
    except json.JSONDecodeError as exc:
        errors.append(f"loadtest.json: invalid JSON ({exc})")
    if not isinstance(manifest, dict):
        errors.append("loadtest.json: top-level value must be an object")
        manifest = {}
    if set(manifest) != {"run_token", "status", "expected_files"}:
        errors.append("loadtest.json: keys mismatch")
    if manifest.get("run_token") != spec["run_token"]:
        errors.append("loadtest.json: wrong run_token")
    if manifest.get("status") != "complete":
        errors.append("loadtest.json: status must be complete")
    files = manifest.get("expected_files")
    if files != EXPECTED_ORDER:
        errors.append("loadtest.json: expected_files mismatch")

    results[spec["user_id"]] = {
        "success": not errors,
        "workspace": str(project),
        "file_sizes": sizes,
        "errors": errors,
    }

print(json.dumps(results, separators=(",", ":")))
"""


def verify_artifacts(
    session: RuntimeSession, requests: list[dict[str, Any]]
) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    specs = [
        {
            "user_id": request["user_id"],
            "workspace": request["workspace"],
            "run_token": request["run_token"],
        }
        for request in requests
        if request.get("workspace")
    ]
    verifier = base64.b64encode(_ARTIFACT_VERIFIER.encode("utf-8")).decode("ascii")
    encoded_specs = base64.b64encode(
        json.dumps(specs, separators=(",", ":")).encode("utf-8")
    ).decode("ascii")
    script = f"""set -euo pipefail
run_dir='/tmp/agentcore-loadtest/artifact-verifier-{uuid.uuid4().hex[:12]}'
umask 077
mkdir -p "$run_dir"
printf '%s' '{verifier}' | base64 -d > "$run_dir/verify.py"
printf '%s' '{encoded_specs}' | base64 -d > "$run_dir/specs.json"
python3 "$run_dir/verify.py" "$run_dir/specs.json"
rm -rf "$run_dir"
"""
    command = session.run_shell_script(script, timeout=180)
    value = json.loads(command["stdout"])
    if not isinstance(value, dict):
        raise ValueError("artifact verifier output must be a JSON object")
    return value, command


def attach_verification(
    summary: dict[str, Any], verification: dict[str, dict[str, Any]]
) -> None:
    artifact_success = 0
    verified_success = 0
    errors = []
    for request in summary["requests"]:
        result = verification.get(
            request["user_id"],
            {"success": False, "errors": ["workspace was not returned or checked"]},
        )
        request["artifact_verification"] = result
        request["artifact_success"] = bool(result.get("success"))
        request["verified_success"] = bool(
            request["agent_success"] and request["artifact_success"]
        )
        artifact_success += int(request["artifact_success"])
        verified_success += int(request["verified_success"])
        if not request["artifact_success"]:
            errors.append(
                {"user_id": request["user_id"], "errors": result.get("errors", [])}
            )
    level = summary["level"]
    summary.update(
        verification_available=True,
        artifact_success=artifact_success,
        artifact_failed=level - artifact_success,
        verified_success=verified_success,
        verified_failed=level - verified_success,
        success=verified_success,
        failed=level - verified_success,
        success_rate=round(verified_success / level, 3),
        artifact_errors=errors[:10],
        artifact_verification={
            "users_checked": len(verification),
            "per_user": verification,
        },
    )


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    run_id = uuid.uuid4().hex[:10]
    session_id = args.session_id or new_session_id(f"shared-long-{run_id}")
    if args.output:
        result_path = Path(args.output)
    else:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        result_path = ROOT / "results" / f"load_test_longrun_{stamp}.json"

    wall_started = time.perf_counter()
    session: RuntimeSession | None = None
    runtime: dict[str, Any] | None = None
    warmup: dict[str, Any] | None = None
    levels: list[dict[str, Any]] = []
    monitor_samples: list[dict[str, Any]] = []
    monitor_errors: list[dict[str, str]] = []
    monitor_started = False
    fatal_error: str | None = None
    cleanup: dict[str, Any] = {
        "monitor": {"attempted": False, "success": False},
        "session": {"attempted": False, "success": False},
    }

    def checkpoint(completed: bool = False) -> None:
        atomic_write_json(
            result_path,
            {
                "generated": utc_iso(),
                "completed": completed,
                "runtime": runtime,
                "config": {
                    "run_id": run_id,
                    "levels": args.levels,
                    "success_floor": args.success_floor,
                    "request_timeout_s": args.request_timeout,
                    "level_pause_s": args.level_pause,
                    "monitor_duration_s": args.monitor_duration,
                    "phases_per_user": 2,
                    "expected_files": list(EXPECTED_FILES),
                },
                "shared_session_id": session_id,
                "warmup": warmup,
                "levels": levels,
                "monitor_samples": monitor_samples,
                "monitor_errors": monitor_errors,
                "monitor_error": monitor_errors[-1]["error"]
                if monitor_errors
                else None,
                "fatal_error": fatal_error,
                "cleanup": cleanup,
                "total_wall_s": round(time.perf_counter() - wall_started, 1),
            },
        )

    print(f"shared session : {session_id}")
    print(f"levels         : {args.levels}")
    try:
        session = RuntimeSession.from_config(
            args.config,
            session_id,
            read_timeout=args.request_timeout,
            max_connections=max(32, max(args.levels) + 8),
        )
        runtime = session.runtime
        print(f"runtime        : {runtime['runtimeArn']}\n")

        print("== phase 0: warmup ==", flush=True)
        warmup = invoke_phase(
            session,
            f"long-{run_id}-warmup",
            {
                "name": "warmup",
                "marker": f"LONG-READY-{run_id}",
                "prompt": f"Reply exactly: LONG-READY-{run_id}",
            },
            reset=True,
        )
        if not warmup["phase_success"]:
            raise RuntimeError(f"warmup failed: {warmup.get('error')}")
        expected_instance = warmup.get("instance")
        if not isinstance(expected_instance, dict) or not expected_instance.get(
            "server_run_id"
        ):
            raise RuntimeError("warmup did not return a server process fingerprint")

        print("\n== phase 1: start command-channel monitor ==", flush=True)
        start_monitor(session, run_id, duration=args.monitor_duration)
        monitor_started = True
        checkpoint()

        print("\n== phase 2: two-phase concurrency ramp ==", flush=True)
        for index, level in enumerate(args.levels):
            summary = run_level(session, level, run_id, expected_instance)
            levels.append(summary)
            # Agent evidence is durable before command-based verification starts.
            checkpoint()
            try:
                verification, command = verify_artifacts(session, summary["requests"])
                attach_verification(summary, verification)
                summary["artifact_command"] = {
                    "api_status": command["api_status"],
                    "content_stop": command["content_stop"],
                    "stream_exceptions": command["stream_exceptions"],
                }
                print(
                    f"    artifacts={summary['artifact_success']}/{level} "
                    f"end_to_end={summary['verified_success']}/{level}",
                    flush=True,
                )
            except Exception as exc:
                summary["verification_available"] = False
                summary["verification_error"] = f"{type(exc).__name__}: {exc}"[:500]
                summary["success"] = None
                summary["failed"] = None
                summary["success_rate"] = None
                checkpoint()
                raise RuntimeError(
                    "artifact verification unavailable; agent results were preserved"
                ) from exc
            checkpoint()

            try:
                monitor_samples, _ = read_monitor(session, run_id, stop=False)
                for completed_level in levels:
                    completed_level["resources"] = window_stats(
                        monitor_samples, *completed_level["window"]
                    )
                    completed_level["monitor_available"] = bool(
                        completed_level["resources"]
                    )
            except Exception as exc:
                error = f"{type(exc).__name__}: {exc}"[:500]
                summary["monitor_error"] = error
                monitor_errors.append(
                    {"phase": f"level-{level}", "at": utc_iso(), "error": error}
                )
                print(f"    monitor unavailable: {error}", file=sys.stderr)
            checkpoint()

            if summary["success_rate"] < args.success_floor:
                print(
                    f"  stopping ramp: {summary['success_rate']} < "
                    f"{args.success_floor}",
                    flush=True,
                )
                break
            if index + 1 < len(args.levels):
                time.sleep(args.level_pause)
    except Exception as exc:
        fatal_error = f"{type(exc).__name__}: {exc}"[:1000]
        print(f"fatal: {fatal_error}", file=sys.stderr)
    finally:
        if session is not None:

            def finish_monitor() -> None:
                nonlocal monitor_samples
                if not monitor_started:
                    return
                cleanup["monitor"]["attempted"] = True
                try:
                    monitor_samples, _ = read_monitor(session, run_id, stop=True)
                    cleanup["monitor"].update(
                        success=True, samples=len(monitor_samples)
                    )
                    for summary in levels:
                        summary["resources"] = window_stats(
                            monitor_samples, *summary["window"]
                        )
                        summary["monitor_available"] = bool(summary["resources"])
                except Exception as exc:
                    error = f"{type(exc).__name__}: {exc}"[:500]
                    cleanup["monitor"].update(success=False, error=error)
                    monitor_errors.append(
                        {
                            "phase": "final-collection",
                            "at": utc_iso(),
                            "error": error,
                        }
                    )
                    print(
                        f"final monitor collection failed: {error}",
                        file=sys.stderr,
                    )

            def finish_session() -> None:
                cleanup["session"] = cleanup_session(
                    session, keep_session=args.keep_session
                )
                if args.keep_session:
                    print(
                        "WARNING: session retained; compute may remain billable",
                        file=sys.stderr,
                    )
                elif cleanup["session"].get("success"):
                    print("session stopped")
                else:
                    print(
                        f"session cleanup failed: {cleanup['session'].get('error')}",
                        file=sys.stderr,
                    )

            finalize_before_session_stop(finish_monitor, finish_session)
        checkpoint(completed=fatal_error is None)

    operational_success = (
        fatal_error is None
        and bool(levels)
        and all(level.get("verification_available") for level in levels)
        and all(level.get("monitor_available") for level in levels)
        and cleanup["monitor"].get("success")
        and (cleanup["session"].get("success") or args.keep_session)
    )
    print(f"\nresults: {result_path}")
    return 0 if operational_success else 1


if __name__ == "__main__":
    sys.exit(main())
