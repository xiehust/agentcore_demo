"""Measure the same stages on Lambda MicroVMs as bench.py does on AgentCore V2.

Per session: RunMicrovm (a fresh VM resumed from the image snapshot) ->
CreateMicrovmAuthToken -> POST /invocations through the MicroVM proxy, retried
while the proxy answers 502/503 or the connection fails (the documented way to
detect readiness) -> turns 2..T on the same VM -> TerminateMicrovm.

Records keep bench.py's shape so stages.compute() applies unchanged. Turn 1's
send_ns is the RunMicrovm call, the analogue of InvokeAgentRuntime with a new
runtimeSessionId. Writes ../results/<out>/{records.jsonl,meta.json,summary.json}.

    ../../.venv/bin/python bench_microvm.py --sessions 10 --out microvm_serial_c1
    ../../.venv/bin/python bench_microvm.py --sessions 10 --concurrency 5 --out microvm_burst_c5
"""

from __future__ import annotations

import argparse
import http.client
import json
import ssl
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))
import bench  # noqa: E402
import stages  # noqa: E402

AGENT_PORT = 8080
READY_DEADLINE_S = 180
RETRY_SLEEP_S = 0.1


def new_conn(endpoint: str) -> http.client.HTTPSConnection:
    host = endpoint.removeprefix("https://").rstrip("/")
    return http.client.HTTPSConnection(host, timeout=300, context=ssl.create_default_context())


def post_stream(conn: http.client.HTTPSConnection, token: str, prompt: str, c: dict) -> tuple[int, dict | None, bytes]:
    """POST /invocations and read the SSE stream as it arrives, timestamping into c."""
    conn.request("POST", "/invocations", body=json.dumps({"prompt": prompt}).encode(), headers={
        "X-aws-proxy-auth": token, "X-aws-proxy-port": str(AGENT_PORT),
        "Content-Type": "application/json", "Accept": "text/event-stream"})
    resp = conn.getresponse()
    c["headers_ns"] = time.time_ns()
    if resp.status != 200:
        return resp.status, None, resp.read()
    done, buffer = None, b""
    while chunk := resp.read1(65536):
        now = time.time_ns()
        c.setdefault("first_byte_ns", now)
        events, buffer = bench.parse_sse(buffer + chunk)
        for event in events:
            if event.get("event") == "delta":
                c.setdefault("first_token_ns", now)
            elif event.get("event") == "done":
                done = event
    c["done_ns"] = time.time_ns()
    return 200, done, b""


def first_turn(endpoint: str, token: str, prompt: str, c: dict, rec: dict) -> http.client.HTTPSConnection:
    """Retry turn 1 until the proxy reaches the agent; keep every attempt's outcome."""
    conn = new_conn(endpoint)
    deadline = time.monotonic() + READY_DEADLINE_S
    while True:
        attempt = {"send_ns": time.time_ns()}
        try:
            status, done, body = post_stream(conn, token, prompt, c)
        except (OSError, http.client.HTTPException) as e:
            status, done, body = None, None, f"{type(e).__name__}: {e}".encode()
        attempt.update(end_ns=time.time_ns(), status=status)
        rec["attempt_log"].append(attempt)
        if status == 200:
            rec["done"] = done
            return conn
        conn.close()
        if status in (400, 401, 403, 404) or time.monotonic() > deadline:
            raise RuntimeError(f"turn 1 failed: HTTP {status} {body[:200]!r}")
        for key in ("headers_ns", "first_byte_ns", "first_token_ns", "done_ns"):
            c.pop(key, None)
        time.sleep(RETRY_SLEEP_S)
        conn = new_conn(endpoint)


def run_session(mvm, dep: dict, variant: str, turns: int, barrier: threading.Barrier | None) -> list[dict]:
    img = dep["images"][variant]
    if barrier is not None:
        barrier.wait()
    records, microvm_id, conn = [], None, None
    c: dict = {"send_ns": time.time_ns()}
    rec = {"platform": "lambda-microvm", "mode": variant, "turn": 1, "client": c, "done": None,
           "error": None, "attempt_log": []}
    try:
        t0 = time.perf_counter_ns()
        run = mvm.run_microvm(imageIdentifier=img["arn"], imageVersion=img["version"],
                              executionRoleArn=dep["execution_role"]["arn"], maximumDurationInSeconds=900,
                              idlePolicy={"maxIdleDurationSeconds": 60, "suspendedDurationSeconds": 0,
                                          "autoResumeEnabled": False},
                              runHookPayload=json.dumps({"variant": variant}))
        rec["run_api_ms"] = (time.perf_counter_ns() - t0) / 1e6
        microvm_id = rec["session_id"] = run["microvmId"]
        t1 = time.perf_counter_ns()
        token = mvm.create_microvm_auth_token(microvmIdentifier=microvm_id, expirationInMinutes=15,
                                              allowedPorts=[{"port": AGENT_PORT}])["authToken"]["X-aws-proxy-auth"]
        rec["token_ms"] = (time.perf_counter_ns() - t1) / 1e6
        conn = first_turn(run["endpoint"], token, bench.PROMPTS[0], c, rec)
        rec["client_perf_total_ms"] = (time.perf_counter_ns() - t0) / 1e6
        records.append(rec)
        for turn in range(2, turns + 1):
            c2: dict = {"send_ns": time.time_ns()}
            t2 = time.perf_counter_ns()
            warm = {"platform": "lambda-microvm", "mode": variant, "turn": turn, "session_id": microvm_id,
                    "client": c2, "done": None, "error": None}
            try:
                status, warm["done"], body = post_stream(conn, token, bench.PROMPTS[(turn - 1) % len(bench.PROMPTS)], c2)
                if status != 200:
                    warm["error"] = f"HTTP {status} {body[:200]!r}"
            except (OSError, http.client.HTTPException) as e:
                c2.setdefault("done_ns", time.time_ns())
                warm["error"] = f"{type(e).__name__}: {e}"
            warm["client_perf_total_ms"] = (time.perf_counter_ns() - t2) / 1e6
            records.append(warm)
            if warm["error"]:
                break
    except Exception as e:  # noqa: BLE001 - recorded, the run continues
        c.setdefault("done_ns", time.time_ns())
        rec["error"] = f"{type(e).__name__}: {e}"
        rec.setdefault("client_perf_total_ms", None)
        if rec not in records:
            records.insert(0, rec)
    finally:
        if conn is not None:
            conn.close()
        stopped = None
        if microvm_id:
            try:
                mvm.terminate_microvm(microvmIdentifier=microvm_id)
                stopped = True
            except ClientError as e:
                stopped = e.response["Error"]["Code"]
        for r in records:
            r["stopped"] = stopped
    return records


def clock_offset_ms(warm: list[dict]) -> tuple[float, float] | None:
    """Bounds on (VM clock - client clock) from warm turns of one session.

    For each warm turn the true arrival lies between client send and client
    first byte, so the VM's offset is within [received - first_byte, received - send].
    """
    lo, hi = float("-inf"), float("inf")
    for r in warm:
        p, c = (r.get("done") or {}).get("probes") or {}, r["client"]
        if p.get("request_received_ns") and c.get("first_byte_ns"):
            lo = max(lo, (p["request_received_ns"] - c["first_byte_ns"]) / 1e6)
            hi = min(hi, (p["request_received_ns"] - c["send_ns"]) / 1e6)
    return (lo, hi) if lo <= hi < float("inf") else None


def microvm_stages(rec: dict, offset: tuple[float, float] | None = None) -> dict:
    """Platform-specific spans that stages.compute() has no field for (ms).

    ``*_corrected`` subtract the midpoint of the session's clock-offset bounds.
    """
    c, d = rec["client"], rec.get("done") or {}
    run_hook = (d.get("server") or {}).get("run_hook_ns")
    received = (d.get("probes") or {}).get("request_received_ns")
    attempts = rec.get("attempt_log") or []
    ok = attempts[-1] if attempts else {}
    ms = lambda a, b: None if a is None or b is None else (a - b) / 1e6  # noqa: E731
    out = {
        "run_api": rec.get("run_api_ms"),
        "token_api": rec.get("token_ms"),
        "send_to_run_hook": ms(run_hook, c["send_ns"]),
        "run_hook_to_received": ms(received, run_hook),
        "ok_attempt_send_to_received": ms(received, ok.get("send_ns")),
        "attempts": len(attempts) or None,
        "failed_attempts_ms": sum((a["end_ns"] - a["send_ns"]) / 1e6 for a in attempts[:-1]) if attempts else None,
        "clock_offset_mid": None if offset is None else (offset[0] + offset[1]) / 2,
        "clock_offset_width": None if offset is None else offset[1] - offset[0],
    }
    if offset is not None:
        mid = (offset[0] + offset[1]) / 2
        out["send_to_run_hook_corrected"] = None if out["send_to_run_hook"] is None else out["send_to_run_hook"] - mid
        raw = ms(received, c["send_ns"])
        out["send_to_received_corrected"] = None if raw is None else raw - mid
    return out


MICROVM_LABELS = {
    "run_api": "RunMicrovm API call",
    "token_api": "CreateMicrovmAuthToken API call",
    "send_to_run_hook": "RunMicrovm start → /run hook in VM *",
    "run_hook_to_received": "/run hook → /invocations received",
    "ok_attempt_send_to_received": "successful attempt sent → received *",
    "attempts": "HTTP attempts until 200",
    "failed_attempts_ms": "time spent in failed attempts (502 etc.)",
    "clock_offset_mid": "VM clock − client clock, midpoint (ms)",
    "clock_offset_width": "width of that offset bound (ms)",
    "send_to_run_hook_corrected": "RunMicrovm start → /run hook, offset-corrected",
    "send_to_received_corrected": "client send → received, offset-corrected",
}


def write_and_print(out: Path, meta: dict, records: list[dict]) -> int:
    rc = bench.write_summary(out, meta, records)
    summary_path = out / "summary.json"
    summary = json.loads(summary_path.read_text())
    extra = {}
    for variant in sorted({r["mode"] for r in records}):
        warm_by_session: dict[str, list[dict]] = {}
        for r in records:
            if r["mode"] == variant and r["turn"] > 1:
                warm_by_session.setdefault(r.get("session_id"), []).append(r)
        cold = [microvm_stages(r, clock_offset_ms(warm_by_session.get(r.get("session_id"), [])))
                for r in records if r["mode"] == variant and r["turn"] == 1 and r["done"]]
        extra[variant] = {k: stages.summarize([x[k] for x in cold]) for k in MICROVM_LABELS}
        errors = [r["error"] for r in records if r["mode"] == variant and r["error"]]
        statuses = {}
        for r in records:
            if r["mode"] == variant:
                for a in (r.get("attempt_log") or [])[:-1]:
                    statuses[str(a["status"])] = statuses.get(str(a["status"]), 0) + 1
        extra[variant]["failed_attempt_statuses"] = statuses
        extra[variant]["errors"] = errors
        print(f"\n== {variant}/microvm-cold")
        for k, label in MICROVM_LABELS.items():
            stat = extra[variant][k]
            if stat:
                print(f"   {label:<52} {stat['n']:>3} {stat['p50']:>9.1f} {stat['p90']:>9.1f}"
                      f" {stat['min']:>9.1f} {stat['max']:>9.1f}")
        print(f"   failed attempt statuses: {statuses}  errors: {errors[:3]}")
    summary["microvm"] = extra
    summary_path.write_text(json.dumps(summary, indent=2, default=str) + "\n")
    return rc


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--variants", default="lazy,lazy-validated,eager")
    ap.add_argument("--sessions", type=int, default=10)
    ap.add_argument("--turns", type=int, default=3)
    ap.add_argument("--concurrency", type=int, default=1)
    ap.add_argument("--out", required=True, help="directory name under ../results")
    ap.add_argument("--report", action="store_true", help="only rebuild the summary from records.jsonl")
    args = ap.parse_args()
    out = ROOT / "results" / args.out
    if args.report:
        records = [json.loads(line) for line in (out / "records.jsonl").read_text().splitlines()]
        return write_and_print(out, json.loads((out / "meta.json").read_text()), records)

    dep = json.loads((HERE / "microvm_deployment.json").read_text())
    out.mkdir(parents=True, exist_ok=False)
    mvm = boto3.client("lambda-microvms", region_name=dep["region"], config=Config(
        connect_timeout=10, read_timeout=120, retries={"total_max_attempts": 1},
        max_pool_connections=max(10, 2 * args.concurrency)))
    variants = args.variants.split(",")
    meta = {"started_iso": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "args": vars(args),
            "deployment": dep, "client_clock": bench.clock_status(), "platform": "lambda-microvm"}
    records: list[dict] = []
    for variant in variants:
        remaining = args.sessions
        while remaining:
            batch = min(args.concurrency, remaining)
            barrier = threading.Barrier(batch) if batch > 1 else None
            with ThreadPoolExecutor(batch) as pool:
                futures = [pool.submit(run_session, mvm, dep, variant, args.turns, barrier) for _ in range(batch)]
                for f in futures:
                    for rec in f.result():
                        records.append(rec)
                        flag = rec["error"] or (rec["done"] or {}).get("error") or "ok"
                        total = rec.get("client_perf_total_ms")
                        print(f"{variant} turn{rec['turn']} {total if total is None else round(total, 1)} ms"
                              f"  attempts={len(rec.get('attempt_log') or []) or '-'}  {flag}", flush=True)
            remaining -= batch
            if remaining and args.concurrency > 1:
                time.sleep(5)  # let the RunMicrovm token bucket (5 TPS) refill between bursts
    meta["finished_iso"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    (out / "meta.json").write_text(json.dumps(meta, indent=2, default=str) + "\n")
    with (out / "records.jsonl").open("w") as f:
        for rec in records:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    return write_and_print(out, meta, records)


if __name__ == "__main__":
    raise SystemExit(main())
