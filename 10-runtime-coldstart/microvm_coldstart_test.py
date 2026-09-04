"""Lambda MicroVMs cold-start benchmark client (companion of coldstart_test.py).

Measures end-to-end cold-start latency of AWS Lambda MicroVMs as a function of
image size and launch concurrency, with the SAME ping-pong agent, size ladder
and probe design as the AgentCore Runtime benchmark so the two can be compared.

A "cold" probe is: RunMicrovm (fresh VM resumed from the image snapshot) ->
CreateMicrovmAuthToken -> POST https://<endpoint>/invocations, retried every
100 ms while the endpoint answers 502/503/connect errors (per the docs, VM
readiness is determined by connecting, GetMicrovm.state is eventually
consistent). cold_ms runs from the RunMicrovm call to the FULL body of the
first HTTP 200 — the analogue of AgentCore's InvokeAgentRuntime with a fresh
runtimeSessionId. Each cold probe is followed by a "warm" re-invoke on the
same VM (same TLS connection), optionally a suspend -> resume -> invoke cycle
(--resume, concurrency 1 only: SuspendMicrovm is 2 TPS), and the VM is
terminated best-effort.

Concurrency-N rounds release all N threads through a threading.Barrier so the
RunMicrovm calls fire simultaneously. botocore retries are DISABLED
(total_max_attempts=1) so throttles surface as counted errors. NOTE the
account quota: RunMicrovm is 5 TPS / burst 5 by default, so c=10 is expected
to show ~5 ThrottlingExceptions — that is a real platform property, recorded
as such (request a quota increase to measure higher fan-out).

Raw result JSON — one file per (size x concurrency) cell, written to
<out>/raw/<UTCts>_<size>_c<N>.json:

    {
      "meta": {
        "platform": "lambda-microvms", "size": str, "image_name": str,
        "image_arn": str, "image_version": str, "target_mb": int,
        "pad_mb": int, "memory_mib": int, "concurrency": int, "rounds": int,
        "region": str, "started_iso": str, "finished_iso": str
      },
      "requests": [
        {
          "size", "concurrency", "round", "request_idx",
          "microvm_id": str|null, "endpoint": str|null,
          "wall_start_iso": str,
          "run_api_ms": float|null,     # RunMicrovm call latency
          "run_state": str|null,        # state returned by RunMicrovm
          "token_ms": float|null,       # CreateMicrovmAuthToken latency
          "cold_ms": float|null,        # e2e: RunMicrovm start -> first 200 body read
          "first_ok_http_ms": float|null,  # duration of the successful HTTP attempt alone
          "attempts": int,              # HTTP attempts until 200
          "attempt_ms": [float],        # duration of each attempt, failed ones included
          "non_ok_statuses": {str: int},   # e.g. {"502": 7, "conn_error": 1}
          "warm_ms": float|null,        # 2nd invoke, same VM + connection
          "resume_ms": float|null,      # suspend -> resume(auto, via ingress) -> 200
          "suspend_api_ms": float|null,
          "success": bool,
          "error_type": null|"throttle"|"timeout"|"other",
          "error_msg": str|null,
          "proc_start_ts": float|null,  # snapshot-time process start (shared by all VMs)
          "run_hook_ts": float|null,    # in-VM: Lambda called /run on this VM
          "request_ts": float|null,     # in-VM: request arrival
          "terminated": bool
        }, ...
      ]
    }

summary.json: {"cells": [{"size", "concurrency", "samples", "success",
"throttles", "other_errors", "cold_p50_ms", "cold_p90_ms", "cold_max_ms",
"cold_mean_ms", "run_api_p50_ms", "attempts_mean", "warm_p50_ms",
"resume_p50_ms", "in_vm_run_to_request_p50_ms"}, ...], "generated_iso": str}
"""

import argparse
import http.client
import json
import ssl
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

HERE = Path(__file__).resolve().parent
THROTTLE_CODES = {
    "ThrottlingException",
    "TooManyRequestsException",
    "ThrottledException",
    "ServiceQuotaExceededException",
}
READY_DEADLINE_S = 180.0      # give up waiting for the first 200 after this
READY_RETRY_SLEEP_S = 0.1
HTTP_TIMEOUT_S = 30
AGENT_PORT = 8080
SIZE_ORDER = {"500mb": 0, "1gb": 1, "2gb": 2}


def utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def load_deployments() -> dict:
    path = HERE / "deployments_microvm.json"
    if not path.exists():
        print(
            f"ERROR: {path} not found — run scripts/deploy_microvm.sh first to create "
            "the S3 bucket, IAM roles and the three coldstart-ping-microvm images.",
            file=sys.stderr,
        )
        sys.exit(2)
    return json.loads(path.read_text())


def make_client(region: str, max_concurrency: int):
    return boto3.client(
        "lambda-microvms",
        region_name=region,
        config=Config(
            retries={"total_max_attempts": 1},
            read_timeout=120,
            connect_timeout=30,
            max_pool_connections=max(64, 2 * max_concurrency),
        ),
    )


def classify_error(exc: Exception) -> tuple[str, str]:
    if isinstance(exc, ClientError):
        code = exc.response.get("Error", {}).get("Code", "")
        if code in THROTTLE_CODES:
            return "throttle", f"{code}: {exc}"
        return "other", f"{code}: {exc}"
    if isinstance(exc, BotoCoreError):
        name = type(exc).__name__
        if "Timeout" in name:
            return "timeout", f"{name}: {exc}"
        return "other", f"{name}: {exc}"
    if isinstance(exc, TimeoutError):
        return "timeout", f"{type(exc).__name__}: {exc}"
    return "other", f"{type(exc).__name__}: {exc}"


class ReadyTimeout(TimeoutError):
    pass


def http_invoke(conn: http.client.HTTPSConnection, token: str) -> tuple[float, int, bytes]:
    """One POST /invocations through the MicroVM proxy; reads the full body.
    Returns (elapsed_ms, status, raw_body). Raises on transport errors."""
    payload = json.dumps({"ping": "coldstart"}).encode()
    t0 = time.perf_counter()
    conn.request("POST", "/invocations", body=payload, headers={
        "X-aws-proxy-auth": token,
        "X-aws-proxy-port": str(AGENT_PORT),
        "Content-Type": "application/json",
        "Accept": "application/json",
    })
    resp = conn.getresponse()
    raw = resp.read()
    return (time.perf_counter() - t0) * 1000.0, resp.status, raw


def new_conn(endpoint: str) -> http.client.HTTPSConnection:
    host = endpoint.replace("https://", "").rstrip("/")
    return http.client.HTTPSConnection(host, timeout=HTTP_TIMEOUT_S,
                                       context=ssl.create_default_context())


def invoke_until_ok(endpoint: str, token: str, deadline_s: float,
                    rec_statuses: dict, attempt_ms: list | None = None,
                    ) -> tuple[float, dict, http.client.HTTPSConnection, int]:
    """Retry POST /invocations until HTTP 200 or deadline. Returns
    (duration_ms_of_ok_attempt, body, open_connection, attempts). Every
    attempt's duration (failed ones included) is appended to attempt_ms."""
    start = time.perf_counter()
    attempts = 0
    conn = new_conn(endpoint)
    while True:
        attempts += 1
        ta = time.perf_counter()
        try:
            ms, status, raw = http_invoke(conn, token)
        except (OSError, http.client.HTTPException) as exc:
            status, raw = None, b""
            ms = (time.perf_counter() - ta) * 1000.0
            rec_statuses["conn_error"] = rec_statuses.get("conn_error", 0) + 1
            rec_statuses.setdefault("last_conn_error", f"{type(exc).__name__}: {exc}"[:120])
            conn.close()
            conn = new_conn(endpoint)
        if attempt_ms is not None:
            attempt_ms.append(round(ms, 1))
        if status == 200:
            try:
                body = json.loads(raw)
            except (json.JSONDecodeError, UnicodeDecodeError):
                body = {}
            return ms, body, conn, attempts
        if status is not None:
            key = str(status)
            rec_statuses[key] = rec_statuses.get(key, 0) + 1
            if status in (401, 403, 404):  # not a readiness condition — stop early
                conn.close()
                raise RuntimeError(f"endpoint returned HTTP {status}: {raw[:200]!r}")
            if status >= 500:              # proxy/app not ready: drop the connection
                conn.close()
                conn = new_conn(endpoint)
        if time.perf_counter() - start > deadline_s:
            conn.close()
            raise ReadyTimeout(
                f"no HTTP 200 within {deadline_s:.0f}s after {attempts} attempts "
                f"(statuses {rec_statuses})")
        time.sleep(READY_RETRY_SLEEP_S)


def probe(client, dep: dict, size: str, concurrency: int, rnd: int, idx: int,
          barrier: threading.Barrier | None, with_resume: bool,
          cw_logs: bool) -> dict:
    """One cold probe (+ warm, optional resume) + terminate. Never raises."""
    img = dep["images"][size]
    rec = {
        "size": size, "concurrency": concurrency, "round": rnd, "request_idx": idx,
        "microvm_id": None, "endpoint": None, "wall_start_iso": utc_iso(),
        "run_api_ms": None, "run_state": None, "token_ms": None,
        "cold_ms": None, "first_ok_http_ms": None, "attempts": 0,
        "attempt_ms": [], "non_ok_statuses": {}, "warm_ms": None, "resume_ms": None,
        "suspend_api_ms": None, "success": False, "error_type": None,
        "error_msg": None, "proc_start_ts": None, "run_hook_ts": None,
        "request_ts": None, "terminated": False,
    }
    idle_policy = (
        {"maxIdleDurationSeconds": 120, "suspendedDurationSeconds": 300, "autoResumeEnabled": True}
        if with_resume else
        # safety net: suspend after 60 s idle and terminate immediately
        {"maxIdleDurationSeconds": 60, "suspendedDurationSeconds": 0, "autoResumeEnabled": False}
    )
    run_kwargs = dict(
        imageIdentifier=img["arn"],
        imageVersion=img["version"],
        executionRoleArn=dep["execution_role"],
        idlePolicy=idle_policy,
        maximumDurationInSeconds=900,
        runHookPayload=f"coldstart {size} c{concurrency} r{rnd} i{idx}",
    )
    if not cw_logs:
        run_kwargs["logging"] = {"disabled": {}}
    if barrier is not None:
        barrier.wait()
    conn = None
    try:
        t0 = time.perf_counter()
        run = client.run_microvm(**run_kwargs)
        rec["run_api_ms"] = round((time.perf_counter() - t0) * 1000.0, 1)
        rec["microvm_id"] = run["microvmId"]
        rec["endpoint"] = run["endpoint"]
        rec["run_state"] = run.get("state")

        t1 = time.perf_counter()
        tok = client.create_microvm_auth_token(
            microvmIdentifier=run["microvmId"], expirationInMinutes=15,
            allowedPorts=[{"port": AGENT_PORT}])
        token = tok["authToken"]["X-aws-proxy-auth"]
        rec["token_ms"] = round((time.perf_counter() - t1) * 1000.0, 1)

        ok_ms, body, conn, attempts = invoke_until_ok(
            run["endpoint"], token, READY_DEADLINE_S, rec["non_ok_statuses"], rec["attempt_ms"])
        rec.update(
            cold_ms=round((time.perf_counter() - t0) * 1000.0, 1),
            first_ok_http_ms=round(ok_ms, 1), attempts=attempts, success=True,
            proc_start_ts=body.get("proc_start_ts"),
            run_hook_ts=body.get("run_hook_ts"),
            request_ts=body.get("request_ts"),
        )
        try:
            warm_ms, status, _ = http_invoke(conn, token)
            if status == 200:
                rec["warm_ms"] = round(warm_ms, 1)
            else:
                rec["error_msg"] = f"warm returned HTTP {status}"
        except Exception as exc:  # warm failure doesn't invalidate the cold sample
            rec["error_msg"] = f"warm failed: {classify_error(exc)[1]}"

        if with_resume:
            try:
                ts = time.perf_counter()
                client.suspend_microvm(microvmIdentifier=run["microvmId"])
                rec["suspend_api_ms"] = round((time.perf_counter() - ts) * 1000.0, 1)
                # wait until the VM is actually SUSPENDED (state may lag)
                deadline = time.perf_counter() + 120
                while time.perf_counter() < deadline:
                    st = client.get_microvm(microvmIdentifier=run["microvmId"])["state"]
                    if st == "SUSPENDED":
                        break
                    time.sleep(1.0)
                else:
                    raise ReadyTimeout("VM did not reach SUSPENDED within 120s")
                conn.close()
                tr = time.perf_counter()
                _, rbody, conn, _ = invoke_until_ok(
                    run["endpoint"], token, READY_DEADLINE_S, {})
                rec["resume_ms"] = round((time.perf_counter() - tr) * 1000.0, 1)
                rec["resume_hook_ts"] = rbody.get("resume_hook_ts")
            except Exception as exc:
                rec["error_msg"] = (rec["error_msg"] or "") + f" resume failed: {classify_error(exc)[1][:160]}"
    except Exception as exc:
        rec["error_type"], rec["error_msg"] = classify_error(exc)
    finally:
        if conn is not None:
            conn.close()
        if rec["microvm_id"]:
            try:
                client.terminate_microvm(microvmIdentifier=rec["microvm_id"])
                rec["terminated"] = True
            except Exception as exc:
                print(f"  [warn] terminate_microvm failed for {rec['microvm_id']}: "
                      f"{classify_error(exc)[1][:120]}")
    return rec


def run_cell(client, dep: dict, size: str, concurrency: int, rounds: int,
             pause: float, out_dir: Path, with_resume: bool, cw_logs: bool) -> dict:
    img = dep["images"][size]
    started = utc_iso()
    requests: list[dict] = []
    resume_here = with_resume and concurrency == 1
    print(f"== cell size={size} c={concurrency} rounds={rounds} ({img['name']} v{img['version']})"
          f"{' +resume' if resume_here else ''} ==")
    try:
        for rnd in range(1, rounds + 1):
            barrier = threading.Barrier(concurrency) if concurrency > 1 else None
            results: list[dict | None] = [None] * concurrency
            threads = [
                threading.Thread(
                    target=lambda i=i: results.__setitem__(
                        i, probe(client, dep, size, concurrency, rnd, i, barrier,
                                 resume_here, cw_logs)),
                    daemon=True,
                )
                for i in range(concurrency)
            ]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
            round_recs = [r for r in results if r is not None]
            requests.extend(round_recs)
            colds = [r["cold_ms"] for r in round_recs if r["success"]]
            thr = sum(1 for r in round_recs if r["error_type"] == "throttle")
            print(f"  round {rnd}/{rounds}: ok={len(colds)}/{concurrency} thr={thr} "
                  f"cold_ms={sorted(colds) if len(colds) <= 4 else f'p50={percentile(colds, 50):.0f} max={max(colds):.0f}'}")
            if rnd < rounds:
                time.sleep(pause)
    finally:
        cell_file = out_dir / "raw" / (
            f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}_{size}_c{concurrency}.json")
        cell_file.parent.mkdir(parents=True, exist_ok=True)
        cell_file.write_text(json.dumps({
            "meta": {
                "platform": "lambda-microvms", "size": size,
                "image_name": img["name"], "image_arn": img["arn"],
                "image_version": img["version"], "target_mb": img["target_mb"],
                "pad_mb": img["pad_mb"], "memory_mib": dep["memory_mib"],
                "concurrency": concurrency, "rounds": rounds,
                "region": dep["region"], "started_iso": started,
                "finished_iso": utc_iso(),
            },
            "requests": requests,
        }, indent=2))
        print(f"  wrote {cell_file}")
    return summarize_cell(size, concurrency, requests)


def percentile(values: list[float], pct: float) -> float:
    """Linear-interpolated percentile; works for small n."""
    if not values:
        return float("nan")
    s = sorted(values)
    if len(s) == 1:
        return s[0]
    k = (len(s) - 1) * pct / 100.0
    lo = int(k)
    hi = min(lo + 1, len(s) - 1)
    return s[lo] + (s[hi] - s[lo]) * (k - lo)


def in_vm_run_to_request_ms(r: dict) -> float | None:
    """In-VM time from Lambda's /run hook to request arrival — the part of the
    cold start spent AFTER the snapshot was restored, as seen by the guest."""
    if not r["success"] or r["run_hook_ts"] is None or r["request_ts"] is None:
        return None
    return (r["request_ts"] - r["run_hook_ts"]) * 1000.0


def summarize_cell(size: str, concurrency: int, requests: list[dict]) -> dict:
    ok = [r for r in requests if r["success"]]
    colds = [r["cold_ms"] for r in ok]
    warms = [r["warm_ms"] for r in ok if r["warm_ms"] is not None]
    resumes = [r["resume_ms"] for r in ok if r.get("resume_ms") is not None]
    run_api = [r["run_api_ms"] for r in requests if r["run_api_ms"] is not None]
    in_vm = [v for v in (in_vm_run_to_request_ms(r) for r in ok) if v is not None]
    return {
        "size": size,
        "concurrency": concurrency,
        "samples": len(requests),
        "success": len(ok),
        "throttles": sum(1 for r in requests if r["error_type"] == "throttle"),
        "other_errors": sum(1 for r in requests if r["error_type"] in ("timeout", "other")),
        "cold_p50_ms": round(percentile(colds, 50), 1) if colds else None,
        "cold_p90_ms": round(percentile(colds, 90), 1) if colds else None,
        "cold_max_ms": round(max(colds), 1) if colds else None,
        "cold_mean_ms": round(sum(colds) / len(colds), 1) if colds else None,
        "run_api_p50_ms": round(percentile(run_api, 50), 1) if run_api else None,
        "attempts_mean": round(sum(r["attempts"] for r in ok) / len(ok), 1) if ok else None,
        "warm_p50_ms": round(percentile(warms, 50), 1) if warms else None,
        "resume_p50_ms": round(percentile(resumes, 50), 1) if resumes else None,
        "in_vm_run_to_request_p50_ms": round(percentile(in_vm, 50), 1) if in_vm else None,
    }


def print_summary_table(cells: list[dict]) -> None:
    hdr = (f"{'size':<7} {'conc':>4} {'n':>3} {'ok':>3} {'thr':>3} {'err':>3} "
           f"{'p50_ms':>9} {'p90_ms':>9} {'max_ms':>9} {'runapi50':>9} {'att':>5} "
           f"{'warm_p50':>9} {'resume50':>9}")
    print("\n" + hdr)
    print("-" * len(hdr))
    for c in cells:
        print(f"{c['size']:<7} {c['concurrency']:>4} {c['samples']:>3} {c['success']:>3} "
              f"{c['throttles']:>3} {c['other_errors']:>3} "
              f"{c['cold_p50_ms'] or '—':>9} {c['cold_p90_ms'] or '—':>9} "
              f"{c['cold_max_ms'] or '—':>9} {c['run_api_p50_ms'] or '—':>9} "
              f"{c['attempts_mean'] or '—':>5} {c['warm_p50_ms'] or '—':>9} "
              f"{c['resume_p50_ms'] or '—':>9}")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=(__doc__ or "").split("\n")[0])
    mode = p.add_mutually_exclusive_group(required=True)
    mode.add_argument("--smoke", action="store_true",
                      help="single cold+warm(+resume) probe against the 500mb image")
    mode.add_argument("--full", action="store_true",
                      help="run the full sizes x concurrency matrix")
    p.add_argument("--sizes", default="500mb,1gb,2gb",
                   help="comma-separated image-size labels (default: 500mb,1gb,2gb)")
    p.add_argument("--concurrency", default="1,5,10",
                   help="comma-separated launch concurrency levels (default: 1,5,10; "
                        "RunMicrovm quota is 5 TPS/burst 5 so >5 records throttles)")
    p.add_argument("--rounds-c1", type=int, default=10, help="rounds for concurrency=1 (default 10)")
    p.add_argument("--rounds-c5", type=int, default=4, help="rounds for concurrency=5 (default 4)")
    p.add_argument("--rounds-c10", type=int, default=2, help="rounds for concurrency=10 (default 2)")
    p.add_argument("--rounds-c50", type=int, default=1, help="rounds for concurrency=50 (default 1)")
    p.add_argument("--out", default="results/microvm",
                   help="output directory (default: results/microvm/)")
    p.add_argument("--pause", type=float, default=5.0,
                   help="seconds between rounds (default 5; also lets the RunMicrovm token bucket refill)")
    p.add_argument("--resume", action="store_true",
                   help="also measure suspend -> auto-resume -> invoke on concurrency-1 probes")
    p.add_argument("--cw-logs", action="store_true",
                   help="ship MicroVM stdout to CloudWatch (default: logging disabled)")
    args = p.parse_args(argv)

    args.size_list = [s.strip() for s in args.sizes.split(",") if s.strip()]
    try:
        args.conc_list = [int(c) for c in args.concurrency.split(",") if c.strip()]
    except ValueError:
        p.error(f"--concurrency must be comma-separated integers, got {args.concurrency!r}")
    if any(c < 1 for c in args.conc_list):
        p.error("--concurrency values must be >= 1")
    return args


def rounds_for(args: argparse.Namespace, concurrency: int) -> int:
    return {1: args.rounds_c1, 5: args.rounds_c5, 10: args.rounds_c10,
            50: args.rounds_c50}.get(concurrency, 1)


def rebuild_summary(out_dir: Path, interrupted: bool = False) -> list[dict]:
    """Aggregate the LATEST raw file per (size, concurrency) cell under
    out_dir/raw into summary.json (same accumulation semantics as
    coldstart_test.py)."""
    latest: dict[tuple, Path] = {}
    for f in sorted((out_dir / "raw").glob("*.json")):
        if f.name.startswith("smoke"):
            continue
        meta = json.loads(f.read_text()).get("meta", {})
        if "size" in meta and "concurrency" in meta:
            latest[(meta["size"], meta["concurrency"])] = f  # later ts wins
    cells = [
        summarize_cell(size, conc, json.loads(f.read_text())["requests"])
        for (size, conc), f in sorted(
            latest.items(), key=lambda kv: (SIZE_ORDER.get(kv[0][0], 99), kv[0][1]))
    ]
    (out_dir / "summary.json").write_text(json.dumps(
        {"platform": "lambda-microvms", "cells": cells, "generated_iso": utc_iso(),
         "interrupted": interrupted},
        indent=2))
    return cells


def main() -> int:
    args = parse_args()
    dep = load_deployments()
    for size in args.size_list:
        if size not in dep["images"]:
            print(f"ERROR: size {size!r} not in deployments_microvm.json "
                  f"(have: {', '.join(dep['images'])})", file=sys.stderr)
            return 2
    out_dir = HERE / args.out if not Path(args.out).is_absolute() else Path(args.out)
    client = make_client(dep["region"], max(args.conc_list))

    if args.smoke:
        rec = probe(client, dep, "500mb", 1, 1, 0, None, args.resume, args.cw_logs)
        smoke_file = out_dir / "raw" / (
            f"smoke_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.json")
        smoke_file.parent.mkdir(parents=True, exist_ok=True)
        smoke_file.write_text(json.dumps(
            {"meta": {"mode": "smoke", "platform": "lambda-microvms"}, "requests": [rec]}, indent=2))
        print(f"smoke: run_api_ms={rec['run_api_ms']} token_ms={rec['token_ms']} "
              f"cold_ms={rec['cold_ms']} attempts={rec['attempts']} "
              f"non_ok={rec['non_ok_statuses']} warm_ms={rec['warm_ms']} "
              f"resume_ms={rec['resume_ms']} success={rec['success']} "
              f"terminated={rec['terminated']}")
        if rec["error_msg"]:
            print(f"smoke: error_type={rec['error_type']} error_msg={rec['error_msg'][:300]}")
        print(f"wrote {smoke_file}")
        return 0 if rec["success"] else 1

    ran_any = False
    interrupted = False
    try:
        for size in args.size_list:
            for conc in args.conc_list:
                run_cell(client, dep, size, conc, rounds_for(args, conc), args.pause,
                         out_dir, args.resume, args.cw_logs)
                ran_any = True
    except KeyboardInterrupt:
        interrupted = True
        print("\nInterrupted — flushing partial summary.", file=sys.stderr)
    finally:
        if ran_any or interrupted:
            cells = rebuild_summary(out_dir, interrupted)
            print_summary_table(cells)
            print(f"\nwrote {out_dir / 'summary.json'} ({len(cells)} cells, "
                  "aggregated from all raw cell files)")
    return 130 if interrupted else 0


if __name__ == "__main__":
    sys.exit(main())
