#!/usr/bin/env python3
"""Short-task concurrency ramp in one shared AgentCore microVM session."""

from __future__ import annotations

import argparse
import concurrent.futures
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


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Ramp concurrent short agent requests against one shared microVM "
            "Runtime session while sampling /proc through InvokeAgentRuntimeCommand."
        )
    )
    parser.add_argument("--config", default=str(ROOT / "runtime.json"))
    parser.add_argument(
        "--levels",
        default=os.environ.get("LEVELS", "2,4,8"),
        help="Comma-separated levels or JSON array (default: 2,4,8)",
    )
    parser.add_argument(
        "--success-floor",
        type=float,
        default=float(os.environ.get("SUCCESS_FLOOR", "0.8")),
    )
    parser.add_argument(
        "--request-timeout",
        type=int,
        default=int(os.environ.get("TASK_READ_TIMEOUT_S", "900")),
    )
    parser.add_argument(
        "--level-pause",
        type=float,
        default=float(os.environ.get("LEVEL_PAUSE_S", "10")),
    )
    parser.add_argument(
        "--monitor-duration",
        type=int,
        default=int(os.environ.get("MONITOR_DURATION_S", "3600")),
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


def invoke_short(
    session: RuntimeSession,
    user_id: str,
    marker: str,
    barrier: threading.Barrier | None,
    expected_instance: dict[str, Any] | None,
) -> dict[str, Any]:
    if barrier is not None:
        barrier.wait()
    started_epoch = time.time()
    started = time.perf_counter()
    try:
        record = session.invoke(user_id, f"Reply with exactly: {marker}", reset=True)
    except Exception as exc:
        record = {
            "success": False,
            "error": f"{type(exc).__name__}: {exc}"[:500],
            "latency_ms": round((time.perf_counter() - started) * 1000.0, 1),
        }
    record.pop("events", None)
    fingerprint = record.get("instance") or {}
    record.update(
        user_id=user_id,
        expected_marker=marker,
        marker_ok=(record.get("result") or "").strip() == marker,
        fresh_session=bool(record.get("claude_session_id"))
        and record.get("resumed_from") is None,
        expected_process=(bool(expected_instance) and fingerprint == expected_instance),
        start_epoch=started_epoch,
        end_epoch=time.time(),
    )
    record["contract_success"] = bool(
        record.get("success")
        and record["marker_ok"]
        and record["fresh_session"]
        and record["expected_process"]
        and record.get("workspace")
    )
    return record


def run_level(
    session: RuntimeSession,
    level: int,
    run_id: str,
    expected_instance: dict[str, Any] | None,
) -> dict[str, Any]:
    barrier = threading.Barrier(level)
    window_start = time.time()
    with concurrent.futures.ThreadPoolExecutor(max_workers=level) as pool:
        futures = []
        for index in range(level):
            user_id = f"short-{run_id}-l{level}-u{index:03d}"
            marker = f"PONG-{run_id}-L{level}-U{index:03d}"
            futures.append(
                pool.submit(
                    invoke_short,
                    session,
                    user_id,
                    marker,
                    barrier,
                    expected_instance,
                )
            )
        requests = [future.result() for future in futures]
    window_end = time.time()
    distinct_workspaces = enforce_unique_workspaces(requests, "contract_success")
    successful = [item for item in requests if item["contract_success"]]
    latencies = [item["latency_ms"] for item in successful]
    fingerprints = {
        str(sorted((item.get("instance") or {}).items()))
        for item in requests
        if item.get("instance")
    }
    errors = [
        {
            "user_id": item["user_id"],
            "error": item.get("error") or "short-task contract check failed",
        }
        for item in requests
        if not item["contract_success"]
    ]
    summary = {
        "level": level,
        "window": [window_start, window_end],
        "success": len(successful),
        "failed": level - len(successful),
        "success_rate": round(len(successful) / level, 3),
        "latency_p50_ms": percentile(latencies, 50),
        "latency_p90_ms": percentile(latencies, 90),
        "latency_max_ms": percentile(latencies, 100),
        "distinct_server_processes": len(fingerprints),
        "single_server_process": len(fingerprints) == 1,
        "distinct_workspaces": distinct_workspaces,
        "errors": errors[:10],
        "requests": requests,
        "monitor_available": False,
        "resources": {},
    }
    print(
        f"  level {level:>3}: ok={summary['success']}/{level} "
        f"p50={summary['latency_p50_ms']}ms p90={summary['latency_p90_ms']}ms "
        f"max={summary['latency_max_ms']}ms "
        f"processes={summary['distinct_server_processes']}"
    )
    return summary


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    run_id = uuid.uuid4().hex[:10]
    session_id = args.session_id or new_session_id(f"shared-short-{run_id}")
    if args.output:
        result_path = Path(args.output)
    else:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        result_path = ROOT / "results" / f"load_test_{stamp}.json"

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
                },
                "shared_session_id": session_id,
                "prompt_contract": "unique exact PONG marker",
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

        print("== phase 0: warmup ==")
        warmup = invoke_short(
            session,
            f"short-{run_id}-warmup",
            f"READY-{run_id}",
            None,
            None,
        )
        # The first call defines the process; it cannot compare itself beforehand.
        warmup["expected_process"] = True
        warmup["contract_success"] = bool(
            warmup.get("success")
            and warmup.get("marker_ok")
            and warmup.get("fresh_session")
            and warmup.get("workspace")
            and (warmup.get("instance") or {}).get("server_run_id")
        )
        print(f"  ok={warmup['contract_success']} latency={warmup.get('latency_ms')}ms")
        if not warmup["contract_success"]:
            raise RuntimeError(f"warmup failed: {warmup.get('error')}")
        expected_instance = warmup["instance"]

        print("\n== phase 1: start command-channel monitor ==")
        start_monitor(session, run_id, duration=args.monitor_duration)
        monitor_started = True
        print("  monitor started")
        checkpoint()

        print("\n== phase 2: concurrency ramp ==")
        for index, level in enumerate(args.levels):
            summary = run_level(session, level, run_id, expected_instance)
            levels.append(summary)
            # Preserve request evidence before any monitor command can fail.
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
                print(f"  monitor unavailable: {error}", file=sys.stderr)
            checkpoint()
            if summary["success_rate"] < args.success_floor:
                print(
                    f"  stopping ramp: {summary['success_rate']} < {args.success_floor}"
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
        and all(level.get("monitor_available") for level in levels)
        and cleanup["monitor"].get("success")
        and (cleanup["session"].get("success") or args.keep_session)
    )
    print(f"\nresults: {result_path}")
    return 0 if operational_success else 1


if __name__ == "__main__":
    sys.exit(main())
