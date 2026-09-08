#!/usr/bin/env python3
"""Client-side load test for the userId -> runtimeSessionId session pool.

Drives N virtual users through the Session Router (ALB URL from pool.json),
polls /v1/pool while running, and checks the architecture's invariants:

  * per-session inflight and valid leases never exceed maxInflight (10);
  * a user's follow-up request (reset=false) resumes its Claude conversation
    (resumed_from != null); with workspaces on the shared S3 Files mount it may
    legitimately run on a different session;
  * the number of sessions used stays within MAX_SESSIONS;
  * with --chaos-mode stop, a StopRuntimeSession in the middle is detected as a
    new generation and the follow-up still resumes from the shared workspace;
  * with --chaos-mode drain, the affected users migrate to other sessions and
    still resume.

Scenarios
  short : round 1 `Reply with exactly: PONG <token>` (reset), round 2+ recall
          the token (resume). ~10-20 s per request.
  long  : the two-phase web-project task from load_test_longrun.py
          (foundation -> final-qa). Minutes per phase.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import os
import random
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
from load_test_longrun import EXPECTED_FILES, project_phases  # noqa: E402
from runtime_session import atomic_write_json, percentile  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent


def utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--config", default=str(ROOT / "pool.json"))
    parser.add_argument("--router-url", help="override routerUrl from pool.json")
    parser.add_argument("--scenario", choices=("short", "long"), default="short")
    parser.add_argument("--users", type=int, default=int(os.environ.get("USERS", "30")))
    parser.add_argument("--rounds", type=int, default=2, help="short scenario: requests per user (>=2 tests resume)")
    parser.add_argument("--arrival", choices=("burst", "ramp"), default="burst")
    parser.add_argument("--ramp-s", type=float, default=30.0, help="spread arrivals over this window (ramp)")
    parser.add_argument("--tenant", default="demo")
    parser.add_argument("--user-prefix", default=None, help="default: <scenario>-<timestamp>")
    parser.add_argument("--request-timeout", type=int, default=int(os.environ.get("REQUEST_TIMEOUT_S", "1800")))
    parser.add_argument("--max-attempts", type=int, default=12, help="retries on 429 backpressure")
    parser.add_argument("--poll-interval", type=float, default=2.0)
    parser.add_argument("--chaos", action="store_true", help="inject a fault on the busiest session between rounds")
    parser.add_argument("--chaos-mode", choices=("stop", "drain"), default="stop",
                        help="stop: StopRuntimeSession behind the router's back (generation change); "
                             "drain: mark the session DRAINING so its users must migrate and resume elsewhere")
    parser.add_argument("--verify-files", action="store_true", help="long: check each user's webapp files in the shared workspace (S3 objects + one in-session listing)")
    parser.add_argument("--output")
    args = parser.parse_args(argv)
    if args.users < 1:
        parser.error("--users must be positive")
    if args.rounds < 1:
        parser.error("--rounds must be positive")
    return args


# ------------------------------------------------------------------ HTTP
class RouterClient:
    def __init__(self, base_url: str, timeout: int) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def _json(self, method: str, path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(
            self.base_url + path, data=data, method=method,
            headers={"Content-Type": "application/json"} if data else {},
        )
        with urllib.request.urlopen(req, timeout=60) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def pool(self) -> dict[str, Any]:
        return self._json("GET", "/v1/pool")

    def chaos_stop(self, session_id: str) -> dict[str, Any]:
        return self._json("POST", f"/v1/admin/sessions/{session_id}/chaos-stop", {})

    def set_status(self, session_id: str, status: str) -> dict[str, Any]:
        return self._json("POST", f"/v1/admin/sessions/{session_id}/status", {"status": status})

    def invoke(self, payload: dict[str, Any]) -> dict[str, Any]:
        """POST /v1/invoke and fold the SSE stream into one record."""
        req = urllib.request.Request(
            self.base_url + "/v1/invoke",
            data=json.dumps(payload).encode("utf-8"),
            method="POST",
            headers={"Content-Type": "application/json", "Accept": "text/event-stream"},
        )
        started = time.perf_counter()
        record: dict[str, Any] = {
            "request_id": payload["request_id"], "http_status": None, "events": [],
            "routed": None, "complete": None, "errors": [], "generation_changed": None,
            "first_byte_ms": None, "tool_calls": 0,
        }
        try:
            resp = urllib.request.urlopen(req, timeout=self.timeout)
        except urllib.error.HTTPError as exc:
            record["http_status"] = exc.code
            try:
                record["error_body"] = json.loads(exc.read().decode("utf-8"))
            except Exception:  # noqa: BLE001
                record["error_body"] = {}
            record["latency_ms"] = round((time.perf_counter() - started) * 1000, 1)
            return record
        record["http_status"] = resp.status
        data_lines: list[str] = []
        with resp:
            for raw in resp:
                if record["first_byte_ms"] is None:
                    record["first_byte_ms"] = round((time.perf_counter() - started) * 1000, 1)
                line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
                if line.startswith("data:"):
                    data_lines.append(line[5:].lstrip())
                    continue
                if line == "" and data_lines:
                    try:
                        event = json.loads("\n".join(data_lines))
                    except json.JSONDecodeError:
                        event = {"event": "malformed"}
                    data_lines = []
                    self._fold(record, event)
        record["latency_ms"] = round((time.perf_counter() - started) * 1000, 1)
        return record

    @staticmethod
    def _fold(record: dict[str, Any], event: dict[str, Any]) -> None:
        kind = event.get("event")
        record["events"].append(kind)
        if kind == "routed":
            record["routed"] = event
        elif kind == "complete":
            record["complete"] = event
        elif kind == "generation_changed":
            record["generation_changed"] = event
        elif kind == "tool":
            record["tool_calls"] += 1
        elif kind == "denied":
            # Path-guard denials are expected defence-in-depth, not request failures.
            record.setdefault("denials", []).append(event.get("reason"))
        elif kind in ("error", "router_error"):
            record["errors"].append({k: v for k, v in event.items() if k != "event"})


# ------------------------------------------------------------- pool poller
class PoolPoller(threading.Thread):
    def __init__(self, client: RouterClient, interval: float) -> None:
        super().__init__(daemon=True)
        self.client = client
        self.interval = interval
        self.snapshots: list[dict[str, Any]] = []
        self.errors = 0
        self._stop = threading.Event()

    def run(self) -> None:
        while not self._stop.is_set():
            try:
                snap = self.client.pool()
                self.snapshots.append(
                    {
                        "t": time.time(),
                        "waiters": snap.get("waiters"),
                        "status_counts": snap.get("status_counts"),
                        "metrics": snap.get("metrics"),
                        "sessions": [
                            {k: s.get(k) for k in ("runtime_session_id", "status", "inflight", "valid_leases", "generation", "assigned_users")}
                            for s in snap.get("sessions", [])
                        ],
                    }
                )
            except Exception:  # noqa: BLE001
                self.errors += 1
            self._stop.wait(self.interval)

    def stop(self) -> None:
        self._stop.set()

    def summary(self) -> dict[str, Any]:
        max_inflight: dict[str, int] = {}
        max_leases: dict[str, int] = {}
        peak_active = 0
        peak_waiters = 0
        for snap in self.snapshots:
            active = 0
            for s in snap["sessions"]:
                sid = s["runtime_session_id"]
                max_inflight[sid] = max(max_inflight.get(sid, 0), int(s.get("inflight") or 0))
                max_leases[sid] = max(max_leases.get(sid, 0), int(s.get("valid_leases") or 0))
                if s.get("status") == "ACTIVE":
                    active += 1
            peak_active = max(peak_active, active)
            peak_waiters = max(peak_waiters, int(snap.get("waiters") or 0))
        return {
            "snapshots": len(self.snapshots),
            "poll_errors": self.errors,
            "max_inflight_per_session": max_inflight,
            "max_valid_leases_per_session": max_leases,
            "peak_active_sessions": peak_active,
            "peak_waiters": peak_waiters,
            "final_metrics": self.snapshots[-1]["metrics"] if self.snapshots else None,
            "final_status_counts": self.snapshots[-1]["status_counts"] if self.snapshots else None,
        }


# ---------------------------------------------------------------- scenarios
def short_rounds(token: str, rounds: int) -> list[dict[str, Any]]:
    out = [{"name": "echo", "prompt": f"Reply with exactly: PONG {token}", "reset": True, "marker": f"PONG {token}"}]
    for i in range(1, rounds):
        out.append(
            {
                "name": f"recall-{i}",
                "prompt": "What exact token did I ask you to echo earlier in this conversation? Reply with exactly that token and nothing else.",
                "reset": False,
                "marker": token,
            }
        )
    return out


def long_rounds(run_token: str) -> list[dict[str, Any]]:
    phases = project_phases(run_token)
    return [
        {"name": phases[0]["name"], "prompt": phases[0]["prompt"], "reset": True, "marker": phases[0]["marker"]},
        {"name": phases[1]["name"], "prompt": phases[1]["prompt"], "reset": False, "marker": phases[1]["marker"]},
    ]


def invoke_with_retries(client: RouterClient, payload: dict[str, Any], max_attempts: int) -> dict[str, Any]:
    attempts: list[dict[str, Any]] = []
    for attempt in range(1, max_attempts + 1):
        record = client.invoke(payload)
        record["attempt"] = attempt
        if record["http_status"] == 429:
            body = record.get("error_body") or {}
            wait = float(body.get("retry_after_s", 5)) + random.uniform(0, 2)
            attempts.append({"attempt": attempt, "status": 429, "waited_ms": body.get("waited_ms"), "sleep_s": round(wait, 1)})
            time.sleep(wait)
            continue
        record["backpressure_retries"] = attempts
        return record
    record = {"request_id": payload["request_id"], "http_status": 429, "backpressure_retries": attempts, "errors": [{"code": "BACKPRESSURE_EXHAUSTED"}], "events": [], "routed": None, "complete": None, "latency_ms": None}
    return record


def run_user(
    client: RouterClient,
    tenant: str,
    user_id: str,
    rounds: list[dict[str, Any]],
    *,
    max_attempts: int,
    start_delay: float,
    barrier: threading.Barrier,
    chaos_gate: tuple[threading.Barrier, threading.Event] | None,
) -> dict[str, Any]:
    barrier.wait()
    if start_delay:
        time.sleep(start_delay)
    result: dict[str, Any] = {"user_id": user_id, "rounds": [], "start_epoch": time.time()}
    first_session: str | None = None
    for index, spec in enumerate(rounds):
        if index == 1 and chaos_gate is not None:
            arrive, release = chaos_gate
            arrive.wait()  # tell the coordinator round 1 is finished
            release.wait()  # wait for the fault injection to complete
        request_id = f"{user_id}-{spec['name']}-{uuid.uuid4().hex[:8]}"
        payload = {"tenant_id": tenant, "user_id": user_id, "request_id": request_id, "prompt": spec["prompt"], "reset": spec["reset"]}
        record = invoke_with_retries(client, payload, max_attempts)
        complete = record.get("complete") or {}
        routed = record.get("routed") or {}
        result_text = complete.get("result") or ""
        session_id = routed.get("runtime_session_id")
        if index == 0:
            first_session = session_id
        entry = {
            "name": spec["name"],
            "reset": spec["reset"],
            "request_id": request_id,
            "http_status": record.get("http_status"),
            "attempt": record.get("attempt"),
            "backpressure_retries": len(record.get("backpressure_retries") or []),
            "latency_ms": record.get("latency_ms"),
            "first_byte_ms": record.get("first_byte_ms"),
            "queue_wait_ms": routed.get("queue_wait_ms"),
            "agent_ms": complete.get("agent_ms"),
            "runtime_session_id": session_id,
            "generation": (complete.get("router") or {}).get("generation", routed.get("generation")),
            "affinity_hit": routed.get("affinity_hit"),
            "remapped": routed.get("remapped"),
            "cold_resume": routed.get("cold_resume"),
            "generation_changed": record.get("generation_changed") is not None,
            "resumed_from": complete.get("resumed_from"),
            "claude_session_id": complete.get("claude_session_id"),
            "tool_calls": record.get("tool_calls"),
            "denied_count": complete.get("denied_count"),
            "denials": record.get("denials") or [],
            "marker_ok": spec["marker"] in result_text if spec["reset"] else result_text.strip().endswith(spec["marker"]) or spec["marker"] in result_text,
            "result_tail": result_text[-160:],
            "errors": record.get("errors"),
            "same_session_as_first": (session_id == first_session) if index else None,
        }
        entry["success"] = bool(
            record.get("http_status") == 200
            and complete
            and not complete.get("is_error")
            and entry["marker_ok"]
            and not record.get("errors")
        )
        if not spec["reset"]:
            # A resume succeeds when the conversation continued; with workspaces
            # on a shared file system it may legitimately run on another session.
            entry["resume_ok"] = bool(entry["success"] and entry["resumed_from"])
        result["rounds"].append(entry)
        if not entry["success"]:
            # Keep the gate protocol intact for the coordinator thread.
            if chaos_gate is not None and index == 0:
                chaos_gate[0].wait()
            break
    result["end_epoch"] = time.time()
    result["success"] = all(r["success"] for r in result["rounds"]) and len(result["rounds"]) == len(rounds)
    return result


# ---------------------------------------------------------------- verification
def verify_files_in_sessions(
    config: dict[str, Any], session_ids: list[str], users_root: str, user_ids: list[str] | None = None
) -> dict[str, Any]:
    """List each user's webapp files inside every session via InvokeAgentRuntimeCommand.

    On a shared file system every session sees every user, so `user_ids`
    restricts the count to this run's users."""
    from runtime_session import RuntimeSession, create_agentcore_client

    slugs: set[str] | None = None
    if user_ids is not None:
        sys.path.insert(0, str(ROOT / "app"))
        from isolation import user_slug  # noqa: E402

        slugs = {user_slug(u) for u in user_ids}
    runtime = {"region": config["region"], "runtimeArn": config["runtimeArn"]}
    client = create_agentcore_client(runtime, read_timeout=300, max_connections=8)
    out: dict[str, Any] = {}
    script = f"""
set -e
cd {users_root} 2>/dev/null || {{ echo "NO_USERS_ROOT"; exit 0; }}
for d in */; do
  d="${{d%/}}"
  if [ -d "$d/webapp" ]; then
    printf '%s\\t%s\\n' "$d" "$(find "$d/webapp" -type f -printf '%f\\n' | sort | tr '\\n' ',')"
  fi
done
"""
    for sid in session_ids:
        try:
            session = RuntimeSession(runtime, sid, client)
            result = session.run_shell_script(script, timeout=120, require_success=False)
            rows = {}
            for line in (result.get("stdout") or "").splitlines():
                if "\t" in line:
                    slug, files = line.split("\t", 1)
                    rows[slug] = sorted(f for f in files.split(",") if f)
            if slugs is not None:
                rows = {k: v for k, v in rows.items() if k in slugs}
            out[sid] = {
                "success": result.get("success"),
                "error": result.get("error"),
                "same_session_id": result.get("runtime_session_id") == sid,
                "workspaces_with_webapp": len(rows),
                "complete_projects": sum(1 for f in rows.values() if f == sorted(EXPECTED_FILES)),
                "files": rows,
            }
        except Exception as exc:  # noqa: BLE001
            out[sid] = {"success": False, "error": f"{type(exc).__name__}: {exc}"[:300]}
    return out


def verify_files_in_s3(config: dict[str, Any], user_ids: list[str]) -> dict[str, Any]:
    """The workspace file system is backed by an S3 bucket: list each user's
    webapp objects directly with the S3 API (independent of any microVM)."""
    import boto3

    sys.path.insert(0, str(ROOT / "app"))
    from isolation import user_slug  # noqa: E402

    bucket = config["workspaceBucket"]
    root = config.get("s3FilesRoot", "/users").strip("/")
    s3 = boto3.client("s3", region_name=config["region"])
    rows: dict[str, Any] = {}
    for user_id in user_ids:
        prefix = f"{root}/{user_slug(user_id)}/webapp/"
        names: list[str] = []
        token: str | None = None
        while True:
            kwargs = {"Bucket": bucket, "Prefix": prefix}
            if token:
                kwargs["ContinuationToken"] = token
            page = s3.list_objects_v2(**kwargs)
            names.extend(obj["Key"].rsplit("/", 1)[-1] for obj in page.get("Contents", []) if not obj["Key"].endswith("/"))
            token = page.get("NextContinuationToken")
            if not token:
                break
        rows[user_id] = {"prefix": prefix, "files": sorted(names), "complete": sorted(set(names)) == sorted(EXPECTED_FILES)}
    return {
        "bucket": bucket,
        "users": len(rows),
        "complete_projects": sum(1 for r in rows.values() if r["complete"]),
        "detail": rows,
    }


# ---------------------------------------------------------------------- main
def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    config = json.loads(Path(args.config).read_text(encoding="utf-8")) if Path(args.config).exists() else {}
    router_url = args.router_url or config.get("routerUrl")
    if not router_url:
        print("router URL missing: pass --router-url or deploy first (pool.json)", file=sys.stderr)
        return 2
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    prefix = args.user_prefix or f"{args.scenario}-{stamp[-6:]}"
    output = Path(args.output or ROOT / "results" / f"pool_{args.scenario}_{stamp}.json")
    output.parent.mkdir(parents=True, exist_ok=True)

    client = RouterClient(router_url, args.request_timeout)
    try:
        initial_pool = client.pool()
    except Exception as exc:  # noqa: BLE001
        print(f"router unreachable at {router_url}: {exc}", file=sys.stderr)
        return 2
    max_sessions = int(initial_pool.get("settings", {}).get("max_sessions", 0))
    max_inflight = int(initial_pool.get("settings", {}).get("max_inflight", 10))
    print(f"router {router_url} ok: settings={initial_pool.get('settings')} status={initial_pool.get('status_counts')}")

    users = [f"{prefix}-u{index:03d}" for index in range(args.users)]
    if args.scenario == "short":
        rounds_for = lambda u: short_rounds(f"{u}-{uuid.uuid4().hex[:6]}", args.rounds)  # noqa: E731
    else:
        rounds_for = lambda u: long_rounds(f"{u}-{uuid.uuid4().hex[:6]}")  # noqa: E731
    round_count = args.rounds if args.scenario == "short" else 2

    poller = PoolPoller(client, args.poll_interval)
    poller.start()
    barrier = threading.Barrier(args.users)
    chaos_gate = (threading.Barrier(args.users + 1), threading.Event()) if args.chaos and round_count > 1 else None
    delays = [0.0] * args.users if args.arrival == "burst" else sorted(random.uniform(0, args.ramp_s) for _ in users)

    started = time.time()
    print(f"{utc_iso()} starting {args.users} users x {round_count} rounds ({args.scenario}, {args.arrival}) chaos={args.chaos}")
    chaos_record: dict[str, Any] | None = None
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.users) as pool:
        futures = [
            pool.submit(run_user, client, args.tenant, u, rounds_for(u), max_attempts=args.max_attempts,
                        start_delay=delays[i], barrier=barrier, chaos_gate=chaos_gate)
            for i, u in enumerate(users)
        ]
        if chaos_gate is not None:
            arrive, release = chaos_gate
            arrive.wait()  # every user finished round 1
            snap = client.pool()
            candidates = [s for s in snap.get("sessions", []) if s.get("status") == "ACTIVE"]
            candidates.sort(key=lambda s: -int(s.get("assigned_users") or 0))
            if candidates:
                victim = candidates[0]["runtime_session_id"]
                if args.chaos_mode == "stop":
                    print(f"{utc_iso()} CHAOS(stop): StopRuntimeSession on {victim} (assigned_users={candidates[0].get('assigned_users')}, generation={candidates[0].get('generation')})")
                    chaos_record = {"mode": "stop", "victim": victim, "before": candidates[0], "stop": client.chaos_stop(victim), "at": time.time()}
                    time.sleep(20)  # let the teardown finish so the next invoke provisions a new microVM
                else:
                    print(f"{utc_iso()} CHAOS(drain): marking {victim} DRAINING (assigned_users={candidates[0].get('assigned_users')}); its users must migrate")
                    chaos_record = {"mode": "drain", "victim": victim, "before": candidates[0], "drain": client.set_status(victim, "DRAINING"), "at": time.time()}
            else:
                chaos_record = {"victim": None, "reason": "no ACTIVE session"}
            release.set()
        user_results = [f.result() for f in futures]
    finished = time.time()
    time.sleep(args.poll_interval * 2)
    poller.stop()
    poller.join(timeout=10)

    # ---------------------------------------------------------- evaluation
    rounds_flat = [r for u in user_results for r in u["rounds"]]
    by_round: dict[str, list[dict[str, Any]]] = {}
    for r in rounds_flat:
        by_round.setdefault(r["name"], []).append(r)
    sessions_used = sorted({r["runtime_session_id"] for r in rounds_flat if r.get("runtime_session_id")})
    pool_summary = poller.summary()
    follow_ups = [r for r in rounds_flat if not r["reset"]]
    chaos_users = []
    if chaos_record and chaos_record.get("victim"):
        victim = chaos_record["victim"]
        first_by_user = {u["user_id"]: u["rounds"][0].get("runtime_session_id") for u in user_results if u["rounds"]}
        chaos_users = [u for u, sid in first_by_user.items() if sid == victim]
    chaos_follow_ups = [r for u in user_results if u["user_id"] in chaos_users for r in u["rounds"][1:2]]

    invariants = {
        "all_requests_succeeded": all(u["success"] for u in user_results),
        "inflight_never_exceeded_cap": all(v <= max_inflight for v in pool_summary["max_inflight_per_session"].values()),
        "leases_never_exceeded_cap": all(v <= max_inflight for v in pool_summary["max_valid_leases_per_session"].values()),
        "sessions_within_max": len(sessions_used) <= max_sessions if max_sessions else True,
        "active_sessions_within_max": pool_summary["peak_active_sessions"] <= max_sessions if max_sessions else True,
        "follow_ups_resumed": all(r.get("resume_ok") for r in follow_ups) if follow_ups else None,
        "no_incomplete_or_upstream_errors": not any(r.get("errors") for r in rounds_flat),
    }
    if chaos_record and chaos_record.get("victim"):
        victim_after = next((s for s in client.pool().get("sessions", []) if s.get("runtime_session_id") == chaos_record["victim"]), {})
        chaos_record["after"] = victim_after
        invariants["chaos_users_still_resumed"] = bool(chaos_follow_ups) and all(r.get("resume_ok") for r in chaos_follow_ups)
        if chaos_record.get("mode") == "drain":
            # Externalized context: the drained session's users must land elsewhere and still resume.
            invariants["chaos_users_migrated"] = bool(chaos_follow_ups) and all(
                r.get("runtime_session_id") and r.get("runtime_session_id") != chaos_record["victim"] for r in chaos_follow_ups
            )
        else:
            # The new generation is recorded either by the pre-admission probe (router
            # log + /v1/pool) or, without an idle window, on a user's own request
            # (`generation_changed` event). Accept both.
            gen_before = int((chaos_record.get("before") or {}).get("generation") or 0)
            gen_after = int(victim_after.get("generation") or 0)
            invariants["chaos_generation_detected"] = gen_after > gen_before or any(r.get("generation_changed") for r in chaos_follow_ups)

    def stats(values: list[float | None]) -> dict[str, Any]:
        clean = [float(v) for v in values if v is not None]
        return {"n": len(clean), "p50": percentile(clean, 50), "p90": percentile(clean, 90), "max": max(clean) if clean else None}

    report = {
        "scenario": args.scenario,
        "started_at": datetime.fromtimestamp(started, timezone.utc).isoformat(timespec="seconds"),
        "finished_at": datetime.fromtimestamp(finished, timezone.utc).isoformat(timespec="seconds"),
        "wall_s": round(finished - started, 1),
        "router_url": router_url,
        "users": args.users,
        "rounds": round_count,
        "arrival": args.arrival,
        "chaos": chaos_record,
        "chaos_affected_users": chaos_users,
        "settings": initial_pool.get("settings"),
        "sessions_used": sessions_used,
        "per_round": {
            name: {
                "n": len(rs),
                "success": sum(1 for r in rs if r["success"]),
                "affinity_hits": sum(1 for r in rs if r.get("affinity_hit")),
                "remapped": sum(1 for r in rs if r.get("remapped")),
                "cold_resumes": sum(1 for r in rs if r.get("cold_resume")),
                "generation_changed": sum(1 for r in rs if r.get("generation_changed")),
                "path_guard_denials": sum(int(r.get("denied_count") or 0) for r in rs),
                "requests_with_denials": sum(1 for r in rs if r.get("denied_count")),
                "backpressure_retries": sum(int(r.get("backpressure_retries") or 0) for r in rs),
                "latency_ms": stats([r.get("latency_ms") for r in rs]),
                "queue_wait_ms": stats([r.get("queue_wait_ms") for r in rs]),
                "agent_ms": stats([r.get("agent_ms") for r in rs]),
                "sessions": sorted({str(r.get("runtime_session_id")) for r in rs if r.get("runtime_session_id")}),
            }
            for name, rs in by_round.items()
        },
        "pool": pool_summary,
        "pool_timeline": poller.snapshots,
        "invariants": invariants,
        "users_detail": user_results,
    }
    if args.verify_files and args.scenario == "long" and config.get("runtimeArn"):
        if config.get("workspaceBucket"):
            # Shared file system: one in-session listing suffices (every session sees
            # the same tree), plus an S3 API listing that needs no microVM at all.
            probe_sessions = sessions_used[:1]
            print(f"{utc_iso()} verifying workspace files: S3 bucket {config['workspaceBucket']} + 1 in-session listing ...")
            # S3 Files exports writes to the bucket asynchronously (~60 s); poll.
            s3_deadline = time.time() + 150
            while True:
                report["file_verification_s3"] = verify_files_in_s3(config, users)
                if report["file_verification_s3"]["complete_projects"] == args.users or time.time() >= s3_deadline:
                    break
                time.sleep(10)
            invariants["all_projects_complete_in_s3"] = report["file_verification_s3"]["complete_projects"] == args.users
        else:
            probe_sessions = sessions_used
            print(f"{utc_iso()} verifying workspace files inside {len(sessions_used)} sessions ...")
        report["file_verification"] = verify_files_in_sessions(config, probe_sessions, config.get("usersRoot", "/mnt/users"), users)
        total_complete = sum(v.get("complete_projects", 0) for v in report["file_verification"].values())
        invariants["all_projects_complete_in_workspace_fs"] = total_complete == args.users
    atomic_write_json(output, report)

    print(f"\n=== {args.scenario} scenario: {args.users} users, {round_count} rounds, wall {report['wall_s']}s ===")
    for name, agg in report["per_round"].items():
        print(f"  {name:<12} ok {agg['success']}/{agg['n']}  affinity {agg['affinity_hits']}  remap {agg['remapped']}  "
              f"cold {agg['cold_resumes']}  gen+ {agg['generation_changed']}  429-retries {agg['backpressure_retries']}  "
              f"guard-denials {agg['path_guard_denials']}  "
              f"latency p50/p90/max {agg['latency_ms']['p50']}/{agg['latency_ms']['p90']}/{agg['latency_ms']['max']} ms  "
              f"queue p90 {agg['queue_wait_ms']['p90']} ms  sessions {len(agg['sessions'])}")
    print(f"  sessions used: {len(sessions_used)} (max {max_sessions}); peak ACTIVE {pool_summary['peak_active_sessions']}; peak waiters {pool_summary['peak_waiters']}")
    print(f"  max inflight per session: {pool_summary['max_inflight_per_session']}")
    if chaos_record:
        print(f"  chaos({chaos_record.get('mode')}): victim {chaos_record.get('victim')} generation {(chaos_record.get('before') or {}).get('generation')} -> {(chaos_record.get('after') or {}).get('generation')}; "
              f"affected users {len(chaos_users)}; follow-ups on other sessions: {sum(1 for r in chaos_follow_ups if r.get('runtime_session_id') != chaos_record.get('victim'))}")
    if report.get("file_verification_s3"):
        fv = report["file_verification_s3"]
        print(f"  S3 {fv['bucket']}: {fv['complete_projects']}/{fv['users']} users have exactly the 6 expected webapp objects")
    print("  invariants:")
    for key, value in invariants.items():
        print(f"    {'PASS' if value else ('n/a ' if value is None else 'FAIL')} {key}")
    print(f"  report: {output}")
    return 0 if all(v is not False for v in invariants.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
