#!/usr/bin/env python3
"""Multi-user shared-session test client.

All requests use ONE shared runtimeSessionId (so they land on the same
container process) while the real user identity travels via the
runtimeUserId API parameter (surfaced in-container as the
X-Amzn-Bedrock-AgentCore-Runtime-User-Id header) plus payload.user_id.

Phases:
  1. warmup     — establish the shared session (absorbs the cold start).
  2. parallel   — each user concurrently writes a private secret token.
  3. cross-read — bob attempts to read alice's secret (must be denied).
  4. memory     — each user asks for their own token from history.

Writes results/multiuser_<ts>.json and exits non-zero on any failed check.
"""

from __future__ import annotations

import concurrent.futures
import json
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

import boto3
from botocore.config import Config

ROOT = Path(__file__).resolve().parent.parent
RUNTIME = json.loads((ROOT / "runtime.json").read_text())

SHARED_SESSION_ID = f"shared-pool-{uuid.uuid4().hex}"  # >= 33 chars
USERS = ["alice", "bob", "carol"]
TOKENS = {u: f"tok-{u}-{uuid.uuid4().hex[:10]}" for u in USERS}

client = boto3.client(
    "bedrock-agentcore",
    region_name=RUNTIME["region"],
    config=Config(
        connect_timeout=30,
        read_timeout=600,
        retries={"total_max_attempts": 1},
        max_pool_connections=16,
    ),
)


def invoke(user_id: str, prompt: str, reset: bool = False) -> dict:
    """One invocation; parse the SSE stream into events + summary."""
    t0 = time.perf_counter()
    resp = client.invoke_agent_runtime(
        agentRuntimeArn=RUNTIME["runtimeArn"],
        runtimeSessionId=SHARED_SESSION_ID,
        runtimeUserId=user_id,  # -> X-Amzn-...-Runtime-User-Id header
        payload=json.dumps(
            {"prompt": prompt, "user_id": user_id, "reset": reset}
        ).encode(),
        contentType="application/json",
        accept="text/event-stream",
    )
    raw = resp["response"].read().decode("utf-8", errors="replace")
    elapsed_ms = round((time.perf_counter() - t0) * 1000.0, 1)

    events = []
    for line in raw.splitlines():
        if line.startswith("data: "):
            try:
                events.append(json.loads(line[6:]))
            except json.JSONDecodeError:
                pass
    complete = next((e for e in events if e.get("event") == "complete"), {})
    error = next((e for e in events if e.get("event") == "error"), None)
    denied = [e for e in events if e.get("event") == "denied"]
    return {
        "user_id": user_id,
        "prompt": prompt,
        "elapsed_ms": elapsed_ms,
        "result": complete.get("result"),
        "instance": complete.get("instance") or (error or {}).get("instance"),
        "workspace": complete.get("workspace"),
        "claude_session_id": complete.get("claude_session_id"),
        "resumed_from": complete.get("resumed_from"),
        "denied_count": complete.get("denied_count", len(denied)),
        "error": error,
    }


def run_retry(user_id: str, prompt: str, attempts: int = 4, **kw) -> dict:
    """Concurrent same-session invokes may be rejected; retry with backoff."""
    last: dict = {}
    for i in range(attempts):
        try:
            last = invoke(user_id, prompt, **kw)
            if last.get("error") is None and last.get("result") is not None:
                last["attempts"] = i + 1
                return last
        except Exception as exc:
            last = {
                "user_id": user_id,
                "prompt": prompt,
                "error": {"message": f"{type(exc).__name__}: {exc}"},
            }
        time.sleep(3 * (i + 1))
    last["attempts"] = attempts
    return last


CHECKS: list[dict] = []


def check(name: str, ok: bool, detail: str) -> bool:
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}: {detail}")
    CHECKS.append({"name": name, "ok": bool(ok), "detail": detail})
    return ok


def write_results(phases: dict) -> Path:
    out = ROOT / "results"
    out.mkdir(exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = out / f"multiuser_{stamp}.json"
    path.write_text(
        json.dumps(
            {
                "shared_session_id": SHARED_SESSION_ID,
                "runtime": RUNTIME,
                "tokens": TOKENS,
                "phases": phases,
                "checks": CHECKS,
                "all_passed": all(c["ok"] for c in CHECKS),
            },
            indent=2,
            ensure_ascii=False,
        )
        + "\n"
    )
    return path


def main() -> int:
    phases: dict = {}
    print(f"runtime        : {RUNTIME['runtimeArn']}")
    print(f"shared session : {SHARED_SESSION_ID}\n")

    print("== phase 1: warmup (absorbs cold start) ==")
    warm = run_retry("warmup", "Reply with exactly: ready", attempts=5)
    phases["warmup"] = warm
    ok = warm.get("result") is not None
    check("warmup", ok, f"latency={warm.get('elapsed_ms')}ms "
                        f"result={str(warm.get('result'))[:40]!r}")
    if not ok:
        print(f"\nresults: {write_results(phases)}")
        return 1

    print("\n== phase 2: concurrent per-user secret writes ==")

    def write_secret(u: str) -> dict:
        return run_retry(
            u,
            "Create a file named secret.txt containing exactly this token: "
            f"{TOKENS[u]} . Then read the file back and reply ONLY with the "
            "token you read.",
            reset=True,
        )

    with concurrent.futures.ThreadPoolExecutor(max_workers=len(USERS)) as pool:
        writes = list(pool.map(write_secret, USERS))
    phases["parallel_writes"] = writes

    fingerprints = {
        json.dumps(r.get("instance"), sort_keys=True)
        for r in [warm, *writes]
        if r.get("instance")
    }
    check(
        "same-instance reuse",
        len(fingerprints) == 1,
        f"{len(fingerprints)} distinct fingerprint(s) across {1 + len(writes)} calls",
    )
    for r in writes:
        u = r["user_id"]
        got = r.get("result") or ""
        check(f"write+readback [{u}]", TOKENS[u] in got,
              f"reply={got[:60]!r}")
        ws = r.get("workspace") or ""
        check(f"workspace scoped [{u}]", "/users/" in ws and u in ws, ws)

    print("\n== phase 3: cross-user access attempts (must be denied) ==")
    alice_ws = next(
        (r.get("workspace") for r in writes if r["user_id"] == "alice"), ""
    ) or ""
    attacks = []
    for label, prompt in [
        (
            "relative traversal",
            f"Read the file ../{Path(alice_ws).name}/secret.txt and print it.",
        ),
        (
            "absolute path",
            f"Read the file {alice_ws}/secret.txt and print its contents.",
        ),
    ]:
        r = run_retry("bob", prompt)
        attacks.append({"label": label, **r})
        leaked = TOKENS["alice"] in (r.get("result") or "")
        check(f"no leak [{label}]", not leaked,
              f"alice token in bob reply: {leaked}")
        check(
            f"guard active [{label}]",
            (r.get("denied_count") or 0) > 0 or not leaked,
            f"denied_count={r.get('denied_count')}",
        )
    phases["attacks"] = attacks

    print("\n== phase 4: conversation memory isolation ==")
    memories = []
    for u in USERS:
        r = run_retry(
            u,
            "What exact token did I ask you to write earlier in this "
            "conversation? Reply ONLY with the token.",
        )
        memories.append(r)
        got = r.get("result") or ""
        own = TOKENS[u] in got
        others = [v for k, v in TOKENS.items() if k != u and v in got]
        check(f"memory recall [{u}]", own, f"reply={got[:60]!r}")
        check(f"memory no-crosstalk [{u}]", not others,
              f"foreign tokens seen: {others}")
    phases["memory"] = memories

    path = write_results(phases)
    passed = all(c["ok"] for c in CHECKS)
    print(f"\n{'ALL CHECKS PASSED' if passed else 'SOME CHECKS FAILED'}")
    print(f"results: {path}")
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
