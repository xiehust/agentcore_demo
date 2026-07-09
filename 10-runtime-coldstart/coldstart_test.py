"""AgentCore Runtime cold-start benchmark client.

Measures end-to-end cold-start latency of AgentCore Runtime as a function of
container image size and invocation concurrency. A "cold" probe invokes the
runtime with a fresh >=33-char runtimeSessionId (forcing a new microVM) and
times the InvokeAgentRuntime call from API call to full response-body read.
Each cold probe is followed by a "warm" re-invoke on the same session
(baseline), then the session is stopped best-effort to save cost.

Concurrency-N rounds release all N request threads through a threading.Barrier
so requests fire simultaneously. botocore retries are DISABLED
(total_max_attempts=1) so throttles surface as counted errors instead of
silently inflating latencies.

Raw result JSON schema — one file per (size x concurrency) cell,
written to <out>/raw/<UTCts>_<size>_c<N>.json:

    {
      "meta": {
        "size": "500mb",            # image size label
        "runtime_name": str,
        "runtime_arn": str,
        "docker_size_bytes": int,   # uncompressed image size
        "ecr_size_bytes": int,      # compressed (ECR) image size
        "concurrency": int,
        "rounds": int,
        "region": str,
        "started_iso": str,         # UTC ISO8601
        "finished_iso": str
      },
      "requests": [                 # one entry per cold probe, ALL attempts
        {
          "size": str, "concurrency": int, "round": int, "request_idx": int,
          "session_id": str,        # 40-char fresh id per cold probe
          "wall_start_iso": str,
          "cold_ms": float|null,    # e2e: API call -> full body read
          "warm_ms": float|null,    # 2nd invoke, same session
          "success": bool,
          "error_type": null|"throttle"|"timeout"|"other",
          "error_msg": str|null,
          "status_code": int|null,
          "proc_start_ts": float|null,  # agent process start (in-VM)
          "request_ts": float|null,     # request arrival at agent (in-VM)
          "stopped": bool           # stop_runtime_session issued OK
        }, ...
      ]
    }

summary.json: {"cells": [{"size", "concurrency", "samples", "success",
"throttles", "other_errors", "fresh_boots", "cold_p50_ms", "cold_p90_ms",
"cold_max_ms", "cold_mean_ms", "warm_p50_ms"}, ...], "generated_iso": str}

"fresh_boots" counts probes whose agent process started DURING the request
(request_ts - proc_start_ts < cold_ms): a genuine microVM boot. Other
successful probes landed on instances AgentCore pre-warmed before the request.
"""

import argparse
import json
import sys
import threading
import time
import uuid
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


def utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def load_deployments() -> dict:
    path = HERE / "deployments.json"
    if not path.exists():
        print(
            f"ERROR: {path} not found — run scripts/deploy.sh first to create "
            "the ECR repo, IAM role and the three coldstart_ping runtimes.",
            file=sys.stderr,
        )
        sys.exit(2)
    return json.loads(path.read_text())


def make_client(region: str, max_concurrency: int):
    return boto3.client(
        "bedrock-agentcore",
        region_name=region,
        config=Config(
            retries={"total_max_attempts": 1},
            read_timeout=300,
            connect_timeout=30,
            max_pool_connections=max(64, 2 * max_concurrency),
        ),
    )


def new_session_id() -> str:
    return uuid.uuid4().hex + uuid.uuid4().hex[:8]  # 40 chars (>=33 required)


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
    return "other", f"{type(exc).__name__}: {exc}"


def timed_invoke(client, arn: str, session_id: str) -> tuple[float, int, dict]:
    """Invoke and read the FULL body. Returns (elapsed_ms, status_code, body)."""
    payload = json.dumps({"ping": "coldstart"}).encode()
    t0 = time.perf_counter()
    resp = client.invoke_agent_runtime(
        agentRuntimeArn=arn,
        runtimeSessionId=session_id,
        qualifier="DEFAULT",
        contentType="application/json",
        accept="application/json",
        payload=payload,
    )
    raw = resp["response"].read()
    elapsed_ms = (time.perf_counter() - t0) * 1000.0
    status = resp.get("statusCode") or resp["ResponseMetadata"]["HTTPStatusCode"]
    try:
        body = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError):
        body = {}
    return elapsed_ms, status, body


def probe(client, arn: str, size: str, concurrency: int, rnd: int, idx: int,
          barrier: threading.Barrier | None) -> dict:
    """One cold probe + warm follow-up + session stop. Never raises."""
    session_id = new_session_id()
    rec = {
        "size": size, "concurrency": concurrency, "round": rnd,
        "request_idx": idx, "session_id": session_id,
        "wall_start_iso": utc_iso(),
        "cold_ms": None, "warm_ms": None, "success": False,
        "error_type": None, "error_msg": None, "status_code": None,
        "proc_start_ts": None, "request_ts": None, "stopped": False,
    }
    if barrier is not None:
        barrier.wait()
    try:
        cold_ms, status, body = timed_invoke(client, arn, session_id)
        rec.update(
            cold_ms=round(cold_ms, 1), status_code=status, success=True,
            proc_start_ts=body.get("proc_start_ts"),
            request_ts=body.get("request_ts"),
        )
        try:
            warm_ms, _, _ = timed_invoke(client, arn, session_id)
            rec["warm_ms"] = round(warm_ms, 1)
        except Exception as exc:  # warm failure doesn't invalidate the cold sample
            rec["error_msg"] = f"warm failed: {classify_error(exc)[1]}"
    except Exception as exc:
        rec["error_type"], rec["error_msg"] = classify_error(exc)
    finally:
        try:
            client.stop_runtime_session(agentRuntimeArn=arn, runtimeSessionId=session_id)
            rec["stopped"] = True
        except Exception as exc:
            print(f"  [warn] stop_runtime_session failed for {session_id[:8]}…: "
                  f"{classify_error(exc)[1][:120]}")
    return rec


def run_cell(client, dep: dict, size: str, concurrency: int, rounds: int,
             pause: float, out_dir: Path) -> dict:
    rt = dep["runtimes"][size]
    arn = rt["arn"]
    started = utc_iso()
    requests: list[dict] = []
    print(f"== cell size={size} c={concurrency} rounds={rounds} ({rt['name']}) ==")
    try:
        for rnd in range(1, rounds + 1):
            barrier = threading.Barrier(concurrency) if concurrency > 1 else None
            results: list[dict | None] = [None] * concurrency
            threads = [
                threading.Thread(
                    target=lambda i=i: results.__setitem__(
                        i, probe(client, arn, size, concurrency, rnd, i, barrier)),
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
            print(f"  round {rnd}/{rounds}: ok={len(colds)}/{concurrency} "
                  f"cold_ms={sorted(colds) if len(colds) <= 4 else f'p50={percentile(colds, 50):.0f} max={max(colds):.0f}'}")
            if rnd < rounds:
                time.sleep(pause)
    finally:
        cell_file = out_dir / "raw" / (
            f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}_{size}_c{concurrency}.json")
        cell_file.parent.mkdir(parents=True, exist_ok=True)
        cell_file.write_text(json.dumps({
            "meta": {
                "size": size, "runtime_name": rt["name"], "runtime_arn": arn,
                "docker_size_bytes": rt["docker_size_bytes"],
                "ecr_size_bytes": rt["ecr_size_bytes"],
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


def is_fresh_boot(r: dict) -> bool:
    """True when the agent process started during this request (a genuine
    boot), not on a pre-warmed instance provisioned before the request."""
    if not r["success"] or r["proc_start_ts"] is None or r["request_ts"] is None:
        return False
    uptime_at_request = r["request_ts"] - r["proc_start_ts"]
    return uptime_at_request * 1000.0 < (r["cold_ms"] or 0)


def summarize_cell(size: str, concurrency: int, requests: list[dict]) -> dict:
    colds = [r["cold_ms"] for r in requests if r["success"]]
    warms = [r["warm_ms"] for r in requests if r["warm_ms"] is not None]
    return {
        "size": size,
        "concurrency": concurrency,
        "samples": len(requests),
        "success": len(colds),
        "throttles": sum(1 for r in requests if r["error_type"] == "throttle"),
        "other_errors": sum(1 for r in requests if r["error_type"] in ("timeout", "other")),
        "fresh_boots": sum(1 for r in requests if is_fresh_boot(r)),
        "cold_p50_ms": round(percentile(colds, 50), 1) if colds else None,
        "cold_p90_ms": round(percentile(colds, 90), 1) if colds else None,
        "cold_max_ms": round(max(colds), 1) if colds else None,
        "cold_mean_ms": round(sum(colds) / len(colds), 1) if colds else None,
        "warm_p50_ms": round(percentile(warms, 50), 1) if warms else None,
    }


def print_summary_table(cells: list[dict]) -> None:
    hdr = (f"{'size':<7} {'conc':>4} {'n':>3} {'ok':>3} {'thr':>3} {'err':>3} {'boot':>4} "
           f"{'p50_ms':>9} {'p90_ms':>9} {'max_ms':>9} {'mean_ms':>9} {'warm_p50':>9}")
    print("\n" + hdr)
    print("-" * len(hdr))
    for c in cells:
        print(f"{c['size']:<7} {c['concurrency']:>4} {c['samples']:>3} {c['success']:>3} "
              f"{c['throttles']:>3} {c['other_errors']:>3} {c['fresh_boots']:>4} "
              f"{c['cold_p50_ms'] or '—':>9} {c['cold_p90_ms'] or '—':>9} "
              f"{c['cold_max_ms'] or '—':>9} {c['cold_mean_ms'] or '—':>9} "
              f"{c['warm_p50_ms'] or '—':>9}")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=(__doc__ or "").split("\n")[0])
    mode = p.add_mutually_exclusive_group(required=True)
    mode.add_argument("--smoke", action="store_true",
                      help="single cold+warm probe against the 500mb runtime")
    mode.add_argument("--full", action="store_true",
                      help="run the full sizes x concurrency matrix")
    p.add_argument("--sizes", default="500mb,1gb,2gb",
                   help="comma-separated image-size labels (default: 500mb,1gb,2gb)")
    p.add_argument("--concurrency", default="1,5,10,50",
                   help="comma-separated concurrency levels (default: 1,5,10,50)")
    p.add_argument("--rounds-c1", type=int, default=10,
                   help="rounds for concurrency=1 (default 10)")
    p.add_argument("--rounds-c5", type=int, default=4,
                   help="rounds for concurrency=5 (default 4)")
    p.add_argument("--rounds-c10", type=int, default=2,
                   help="rounds for concurrency=10 (default 2)")
    p.add_argument("--rounds-c50", type=int, default=1,
                   help="rounds for concurrency=50 (default 1)")
    p.add_argument("--out", default="results",
                   help="output directory (default: results/)")
    p.add_argument("--pause", type=float, default=5.0,
                   help="seconds between rounds (default 5)")
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


SIZE_ORDER = {"500mb": 0, "1gb": 1, "2gb": 2}


def rebuild_summary(out_dir: Path, interrupted: bool = False) -> list[dict]:
    """Aggregate the LATEST raw file per (size, concurrency) cell under
    out_dir/raw into summary.json. Scanning the disk (instead of only the
    current run's cells) lets partial runs — e.g. adding a new concurrency
    level later — accumulate into one consistent summary."""
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
        {"cells": cells, "generated_iso": utc_iso(), "interrupted": interrupted},
        indent=2))
    return cells


def main() -> int:
    args = parse_args()
    dep = load_deployments()
    for size in args.size_list:
        if size not in dep["runtimes"]:
            print(f"ERROR: size {size!r} not in deployments.json "
                  f"(have: {', '.join(dep['runtimes'])})", file=sys.stderr)
            return 2
    out_dir = HERE / args.out if not Path(args.out).is_absolute() else Path(args.out)
    client = make_client(dep["region"], max(args.conc_list))

    if args.smoke:
        rec = probe(client, dep["runtimes"]["500mb"]["arn"], "500mb", 1, 1, 0, None)
        smoke_file = out_dir / "raw" / (
            f"smoke_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.json")
        smoke_file.parent.mkdir(parents=True, exist_ok=True)
        smoke_file.write_text(json.dumps({"meta": {"mode": "smoke"}, "requests": [rec]}, indent=2))
        print(f"smoke: cold_ms={rec['cold_ms']} warm_ms={rec['warm_ms']} "
              f"success={rec['success']} stopped={rec['stopped']}")
        print(f"wrote {smoke_file}")
        return 0 if rec["success"] else 1

    ran_any = False
    interrupted = False
    try:
        for size in args.size_list:
            for conc in args.conc_list:
                run_cell(client, dep, size, conc,
                         rounds_for(args, conc), args.pause, out_dir)
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
