"""Measure startup / request / reply latency of the Claude Agent SDK agent on Runtime V2.

Each session uses a fresh runtimeSessionId (so AgentCore assigns a new microVM)
and sends --turns requests in sequence: turn 1 is the cold request, later turns
hit the same microVM and the same Claude CLI process. The session is stopped
afterwards. Sessions run --concurrency at a time, released by a barrier.

Writes <out>/records.jsonl (raw client timestamps + the server's done event),
<out>/summary.json, and prints a stage table per mode and turn type.

    ../.venv/bin/python bench.py --sessions 10                  # serial
    ../.venv/bin/python bench.py --sessions 20 --concurrency 10 # bursts
"""

from __future__ import annotations

import argparse
import json
import subprocess
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

import stages

HERE = Path(__file__).resolve().parent
PROMPTS = [
    "In one short sentence, what is a microVM?",
    "And how is that different from a container?",
    "Name one benefit of that difference.",
]


def parse_sse(buffer: bytes) -> tuple[list[dict], bytes]:
    """Split complete ``data: {...}\\n\\n`` events off the front of buffer."""
    events = []
    while b"\n\n" in buffer:
        block, buffer = buffer.split(b"\n\n", 1)
        for line in block.splitlines():
            if line.startswith(b"data: "):
                events.append(json.loads(line[6:]))
    return events, buffer


def invoke(data, arn: str, session_id: str, prompt: str) -> dict:
    """One InvokeAgentRuntime call, timestamping headers, first byte, first token, end."""
    c: dict = {}
    rec: dict = {"client": c, "done": None, "error": None, "events": 0}
    c["send_ns"] = time.time_ns()
    t0 = time.perf_counter_ns()
    try:
        resp = data.invoke_agent_runtime(
            agentRuntimeArn=arn, runtimeSessionId=session_id, qualifier="DEFAULT",
            contentType="application/json", accept="text/event-stream",
            payload=json.dumps({"prompt": prompt}).encode())
        c["headers_ns"] = time.time_ns()
        rec["status"] = resp.get("statusCode")
        rec["content_type"] = resp.get("contentType")
        raw = resp["response"]._raw_stream  # urllib3 response: read1 returns as data arrives
        buffer = b""
        while chunk := raw.read1(65536):
            now = time.time_ns()
            c.setdefault("first_byte_ns", now)
            events, buffer = parse_sse(buffer + chunk)
            for event in events:
                rec["events"] += 1
                if event.get("event") == "delta":
                    c.setdefault("first_token_ns", now)
                elif event.get("event") == "done":
                    rec["done"] = event
        c["done_ns"] = time.time_ns()
    except ClientError as e:
        c["done_ns"] = time.time_ns()
        err = e.response["Error"]
        rec["error"] = f"{err.get('Code')}: {err.get('Message')}"
        rec["status"] = e.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
    except Exception as e:  # noqa: BLE001 - recorded, the run continues
        c["done_ns"] = time.time_ns()
        rec["error"] = f"{type(e).__name__}: {e}"
    rec["client_perf_total_ms"] = (time.perf_counter_ns() - t0) / 1e6
    return rec


def run_session(data, mode: str, arn: str, turns: int, barrier: threading.Barrier | None) -> list[dict]:
    session_id = f"casdk-{mode}-{uuid.uuid4().hex}"  # >= 33 chars
    if barrier is not None:
        barrier.wait()
    records = []
    for turn in range(1, turns + 1):
        rec = invoke(data, arn, session_id, PROMPTS[(turn - 1) % len(PROMPTS)])
        rec.update({"mode": mode, "session_id": session_id, "turn": turn})
        records.append(rec)
        if rec["error"]:
            break
    try:
        data.stop_runtime_session(agentRuntimeArn=arn, runtimeSessionId=session_id, qualifier="DEFAULT")
        stopped = True
    except ClientError as e:
        stopped = e.response["Error"]["Code"]
    for rec in records:
        rec["stopped"] = stopped
    return records


def clock_status() -> str | None:
    try:
        return subprocess.run(["chronyc", "tracking"], capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return None


def report(records: list[dict]) -> dict:
    """Group stage latencies by mode and turn kind (cold = turn 1 of a fresh session)."""
    groups: dict[str, list[dict]] = {}
    for rec in records:
        computed = stages.compute(rec)
        rec["computed"] = computed
        if not computed["flags"]["ok"]:
            continue
        kind = "cold" if rec["turn"] == 1 else "warm"
        groups.setdefault(f"{rec['mode']}/{kind}", []).append(computed)
    summary = {}
    for key, items in sorted(groups.items()):
        names = ([name for name, _, _ in stages.STAGES] + ["agent_connect_outside_request"]
                 + [name for name, _ in stages.INDICATORS])
        summary[key] = {
            "n": len(items),
            "clock_order_violations": sum(not i["flags"]["clock_order_ok"] for i in items),
            "booted_by_request": sum(i["flags"]["booted_by_request"] for i in items),
            "stages": {name: stages.summarize([i["stages"].get(name) for i in items]) for name in names},
        }
    # Do new sessions get distinct microVMs, or copies of one booted instance?
    for mode in sorted({r["mode"] for r in records}):
        cold = [r["done"] for r in records if r["mode"] == mode and r["turn"] == 1 and r["done"]]
        summary[f"{mode}/instances"] = {
            "cold_sessions": len(cold),
            "distinct_boot_id": len({d["boot"]["boot_id"] for d in cold}),
            "distinct_listening_ns": len({d["server"]["listening_ns"] for d in cold}),
            "distinct_agent_connect_start_ns": len({d["agent"].get("connect_start_ns") for d in cold}),
        }
    return summary


def print_table(summary: dict) -> None:
    labels = {name: label for name, _, label in stages.STAGES}
    labels["agent_connect_outside_request"] = "Claude CLI start done before request (not in path)"
    labels.update(dict(stages.INDICATORS))
    for key, block in summary.items():
        if key.endswith("/instances"):
            print(f"\n== {key}: {block}")
            continue
        print(f"\n== {key}  n={block['n']}  clock-order violations={block['clock_order_violations']}"
              f"  booted-by-request={block['booted_by_request']}")
        print(f"   {'stage':<52} {'n':>3} {'p50':>9} {'p90':>9} {'min':>9} {'max':>9}")
        for name, stat in block["stages"].items():
            if stat:
                print(f"   {labels[name]:<52} {stat['n']:>3} {stat['p50']:>9.1f} {stat['p90']:>9.1f}"
                      f" {stat['min']:>9.1f} {stat['max']:>9.1f}")


def write_summary(out: Path, meta: dict, records: list[dict]) -> int:
    summary = report(records)
    errors = sum(not r["computed"]["flags"]["ok"] for r in records)
    (out / "summary.json").write_text(json.dumps({"meta": meta, "invocations": len(records),
                                                  "errors": errors, "groups": summary},
                                                 indent=2, default=str) + "\n")
    print_table(summary)
    print(f"\n{len(records)} invocations, {errors} errors -> {out}")
    return 1 if errors else 0


def rereport(out: Path) -> int:
    records = [json.loads(line) for line in (out / "records.jsonl").read_text().splitlines()]
    meta = json.loads((out / "meta.json").read_text()) if (out / "meta.json").exists() else {}
    return write_summary(out, meta, records)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--modes", default="lazy,eager")
    ap.add_argument("--sessions", type=int, default=10, help="fresh sessions per mode")
    ap.add_argument("--turns", type=int, default=3)
    ap.add_argument("--concurrency", type=int, default=1)
    ap.add_argument("--out", type=Path, default=None)
    ap.add_argument("--report", type=Path, metavar="DIR",
                    help="only rebuild DIR/summary.json from DIR/records.jsonl (no AWS calls)")
    args = ap.parse_args()
    if args.report:
        return rereport(args.report)

    deployment = json.loads((HERE / "deployment.json").read_text())
    out = args.out or HERE / "results" / time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    out.mkdir(parents=True, exist_ok=False)
    session = boto3.Session(region_name=deployment["region"])
    cfg = Config(connect_timeout=10, read_timeout=300, retries={"total_max_attempts": 1},
                 max_pool_connections=max(10, 2 * args.concurrency))
    data = session.client("bedrock-agentcore", config=cfg)
    ctl = session.client("bedrock-agentcore-control", config=cfg)

    modes = args.modes.split(",")
    meta = {"started_iso": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "args": vars(args) | {"out": str(out)},
            "deployment": deployment, "client_clock": clock_status(), "runtimes": {}}
    for mode in modes:
        rt = ctl.get_agent_runtime(agentRuntimeId=deployment["runtimes"][mode]["id"])
        meta["runtimes"][mode] = {k: str(rt.get(k)) for k in ("agentRuntimeName", "agentRuntimeVersion",
                                                               "platformVersion", "status", "lastUpdatedAt")}
        assert rt["platformVersion"] == "V2", rt.get("platformVersion")

    records: list[dict] = []
    for mode in modes:
        arn = deployment["runtimes"][mode]["arn"]
        remaining = args.sessions
        while remaining:
            batch = min(args.concurrency, remaining)
            barrier = threading.Barrier(batch) if batch > 1 else None
            with ThreadPoolExecutor(batch) as pool:
                futures = [pool.submit(run_session, data, mode, arn, args.turns, barrier) for _ in range(batch)]
                for f in futures:
                    for rec in f.result():
                        records.append(rec)
                        flag = rec["error"] or (rec["done"] or {}).get("error") or "ok"
                        print(f"{mode} turn{rec['turn']} {rec['client_perf_total_ms']:8.1f} ms  {flag}", flush=True)
            remaining -= batch

    meta["finished_iso"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    (out / "meta.json").write_text(json.dumps(meta, indent=2, default=str) + "\n")
    with (out / "records.jsonl").open("w") as f:
        for rec in records:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    return write_summary(out, meta, records)


if __name__ == "__main__":
    raise SystemExit(main())
