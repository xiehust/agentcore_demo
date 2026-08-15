#!/usr/bin/env python3
"""Multi-user isolation smoke test for one shared AgentCore microVM session."""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import os
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from runtime_session import (
    RuntimeSession,
    atomic_write_json,
    cleanup_session,
    new_session_id,
    utc_iso,
    validate_session_id,
)

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_USERS = ("alice", "bob", "carol")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Verify workspace, conversation, and process reuse for multiple users "
            "inside one shared AgentCore microVM session."
        )
    )
    parser.add_argument("--config", default=str(ROOT / "runtime.json"))
    parser.add_argument(
        "--users",
        default=",".join(DEFAULT_USERS),
        help="Comma-separated user IDs (at least three; default: alice,bob,carol)",
    )
    parser.add_argument(
        "--session-id", help="Optional 33..256 character runtimeSessionId"
    )
    parser.add_argument("--request-timeout", type=int, default=600)
    parser.add_argument("--results-dir", default=str(ROOT / "results"))
    parser.add_argument(
        "--keep-session",
        action="store_true",
        default=os.environ.get("STOP_SESSION", "1") == "0",
        help=(
            "Do not call StopRuntimeSession (also selected by STOP_SESSION=0; "
            "debug only and billable)"
        ),
    )
    args = parser.parse_args(argv)
    args.users = [item.strip() for item in args.users.split(",") if item.strip()]
    if len(args.users) < 3 or len(set(args.users)) != len(args.users):
        parser.error("--users requires at least three distinct IDs")
    if args.request_timeout < 1:
        parser.error("--request-timeout must be positive")
    if args.session_id:
        try:
            validate_session_id(args.session_id)
        except ValueError as exc:
            parser.error(str(exc))
    return args


def safe_invoke(
    session: RuntimeSession, user_id: str, prompt: str, *, reset: bool
) -> dict[str, Any]:
    started = time.perf_counter()
    try:
        result = session.invoke(user_id, prompt, reset=reset)
    except Exception as exc:
        result = {
            "success": False,
            "error": f"{type(exc).__name__}: {exc}"[:500],
            "latency_ms": round((time.perf_counter() - started) * 1000.0, 1),
        }
    result.pop("events", None)
    result["user_id"] = user_id
    return result


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    run_id = uuid.uuid4().hex[:10]
    session_id = args.session_id or new_session_id(f"shared-smoke-{run_id}")
    tokens = {
        user: f"tok-{run_id}-{index}-{uuid.uuid4().hex[:8]}"
        for index, user in enumerate(args.users)
    }
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    result_path = Path(args.results_dir) / f"multiuser_{stamp}.json"

    phases: dict[str, Any] = {}
    checks: list[dict[str, Any]] = []
    fatal_error: str | None = None
    cleanup: dict[str, Any] = {"attempted": False, "success": False}
    runtime: dict[str, Any] | None = None
    session: RuntimeSession | None = None

    def check(name: str, ok: bool, detail: str) -> bool:
        checks.append({"name": name, "ok": bool(ok), "detail": detail})
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}: {detail}")
        return bool(ok)

    def checkpoint(completed: bool = False) -> None:
        atomic_write_json(
            result_path,
            {
                "generated": utc_iso(),
                "completed": completed,
                "runtime": runtime,
                "shared_session_id": session_id,
                "run_id": run_id,
                "users": args.users,
                "tokens": tokens,
                "phases": phases,
                "checks": checks,
                "all_passed": bool(checks) and all(item["ok"] for item in checks),
                "fatal_error": fatal_error,
                "cleanup": cleanup,
            },
        )

    print(f"shared session : {session_id}")
    print(f"users          : {', '.join(args.users)}")
    try:
        session = RuntimeSession.from_config(
            args.config,
            session_id,
            read_timeout=args.request_timeout,
            max_connections=max(16, len(args.users) * 2),
        )
        runtime = session.runtime
        print(f"runtime        : {runtime['runtimeArn']}\n")

        print("== phase 1: warmup and command-channel fingerprint ==")
        warmup = safe_invoke(
            session, "smoke-warmup", "Reply with exactly: MICROVM-READY", reset=True
        )
        phases["warmup"] = warmup
        check(
            "warmup",
            bool(warmup.get("success"))
            and (warmup.get("result") or "").strip() == "MICROVM-READY",
            f"latency_ms={warmup.get('latency_ms')} error={warmup.get('error')!r}",
        )
        if not warmup.get("success"):
            raise RuntimeError("warmup failed; shared session was not activated")

        probe_script = r"""set -euo pipefail
python3 - <<'PY'
import json
import os
import socket
from pathlib import Path
print(json.dumps({
    "boot_id": Path("/proc/sys/kernel/random/boot_id").read_text().strip(),
    "hostname": socket.gethostname(),
    "command_pid": os.getpid(),
}))
PY
"""
        command_probe = session.run_shell_script(probe_script, timeout=30)
        probe = json.loads(command_probe["stdout"])
        phases["command_probe"] = {"command": command_probe, "fingerprint": probe}
        app_fingerprint = warmup.get("instance") or {}
        check(
            "command API boot ID",
            probe.get("boot_id") == app_fingerprint.get("boot_id"),
            f"command={probe.get('boot_id')} app={app_fingerprint.get('boot_id')}",
        )
        check(
            "command API hostname",
            probe.get("hostname") == app_fingerprint.get("hostname"),
            f"command={probe.get('hostname')} app={app_fingerprint.get('hostname')}",
        )
        checkpoint()

        print("\n== phase 2: concurrent private writes ==")

        def write_secret(user: str) -> dict[str, Any]:
            return safe_invoke(
                session,
                user,
                "Create secret.txt containing exactly this token: "
                f"{tokens[user]}. Read it back and reply only with that token.",
                reset=True,
            )

        with concurrent.futures.ThreadPoolExecutor(max_workers=len(args.users)) as pool:
            writes = list(pool.map(write_secret, args.users))
        phases["parallel_writes"] = writes

        instances = [record.get("instance") for record in [warmup, *writes]]
        fingerprints = {
            json.dumps(instance, sort_keys=True) for instance in instances if instance
        }
        check(
            "same server process",
            bool(all(instances)) and len(fingerprints) == 1,
            f"complete={sum(bool(item) for item in instances)}/{len(instances)} "
            f"distinct_fingerprints={len(fingerprints)}",
        )
        workspaces = [record.get("workspace") for record in writes]
        check(
            "distinct per-user workspaces",
            all(workspaces) and len(set(workspaces)) == len(args.users),
            f"distinct={len(set(filter(None, workspaces)))}/{len(args.users)}",
        )
        for record in writes:
            user = record["user_id"]
            check(
                f"write/read [{user}]",
                bool(record.get("success"))
                and (record.get("result") or "").strip() == tokens[user],
                f"workspace={record.get('workspace')} error={record.get('error')!r}",
            )
            check(
                f"fresh Claude session [{user}]",
                bool(record.get("claude_session_id"))
                and record.get("resumed_from") is None,
                f"session={record.get('claude_session_id')!r} resumed={record.get('resumed_from')!r}",
            )
        checkpoint()

        print("\n== phase 3: cross-workspace read attempts ==")
        first_user, attacker = args.users[0], args.users[1]
        first_workspace = next(
            record.get("workspace")
            for record in writes
            if record["user_id"] == first_user
        )
        if not isinstance(first_workspace, str) or not first_workspace:
            raise RuntimeError("first user did not return a workspace")
        attacks = []
        for label, target in (
            ("relative traversal", f"../{Path(first_workspace).name}/secret.txt"),
            ("absolute path", f"{first_workspace}/secret.txt"),
        ):
            record = safe_invoke(
                session,
                attacker,
                (
                    "[PATH-GUARD-INTEGRATION-PROBE] Issue exactly one Read "
                    f"tool call with file_path {target!r}. Do not reveal or "
                    "infer any contents."
                ),
                reset=False,
            )
            record["label"] = label
            attacks.append(record)
            leaked = tokens[first_user] in (record.get("result") or "")
            denied_count = record.get("denied_count")
            guard_denied = (
                isinstance(denied_count, int)
                and not isinstance(denied_count, bool)
                and denied_count > 0
            )
            check(
                f"guarded cross-workspace read [{label}]",
                bool(record.get("success")) and guard_denied and not leaked,
                f"success={record.get('success')} leaked={leaked} "
                f"denied_count={denied_count} error={record.get('error')!r}",
            )
        phases["cross_workspace_attempts"] = attacks

        print("\n== phase 4: per-user conversation resume ==")
        memories = []
        for user in args.users:
            original = next(item for item in writes if item["user_id"] == user)
            record = safe_invoke(
                session,
                user,
                "What exact token did I ask you to write earlier? Reply only with it.",
                reset=False,
            )
            memories.append(record)
            own_token = (record.get("result") or "").strip() == tokens[user]
            foreign = [
                token
                for other, token in tokens.items()
                if other != user and token in (record.get("result") or "")
            ]
            check(
                f"memory recall [{user}]", own_token, f"error={record.get('error')!r}"
            )
            check(f"memory no-crosstalk [{user}]", not foreign, f"foreign={foreign}")
            check(
                f"session resume chain [{user}]",
                record.get("resumed_from") == original.get("claude_session_id"),
                f"resumed={record.get('resumed_from')!r}",
            )
            check(
                f"workspace stable [{user}]",
                record.get("workspace") == original.get("workspace"),
                f"workspace={record.get('workspace')!r}",
            )
        phases["memory"] = memories

        all_app_records = [warmup, *writes, *attacks, *memories]
        all_instances = [record.get("instance") for record in all_app_records]
        all_fingerprints = {
            json.dumps(instance, sort_keys=True)
            for instance in all_instances
            if instance
        }
        check(
            "same server process throughout smoke",
            bool(all(all_instances)) and len(all_fingerprints) == 1,
            f"complete={sum(bool(item) for item in all_instances)}/"
            f"{len(all_instances)} distinct_fingerprints={len(all_fingerprints)}",
        )
        checkpoint()
    except Exception as exc:
        fatal_error = f"{type(exc).__name__}: {exc}"[:1000]
        print(f"fatal: {fatal_error}", file=sys.stderr)
    finally:
        if session is not None:
            cleanup = cleanup_session(session, keep_session=args.keep_session)
            if args.keep_session:
                print(
                    "WARNING: session retained; compute may remain billable",
                    file=sys.stderr,
                )
            elif cleanup.get("success"):
                print("session stopped")
            else:
                print(
                    f"session cleanup failed: {cleanup.get('error')}",
                    file=sys.stderr,
                )
        checkpoint(completed=fatal_error is None)

    passed = (
        fatal_error is None
        and bool(checks)
        and all(item["ok"] for item in checks)
        and (cleanup.get("success") or args.keep_session)
    )
    print(f"\n{'ALL CHECKS PASSED' if passed else 'SMOKE TEST FAILED'}")
    print(f"results: {result_path}")
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
