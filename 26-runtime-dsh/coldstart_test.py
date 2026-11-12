"""Cold-start benchmark client for a DeepSeek Harness (dsh) agent on AgentCore Runtime.

Each probe opens a fresh session (40-char runtimeSessionId, so AgentCore
assigns a new microVM) and sends two requests on it, then stops the session:

    sequence "ping":  cold {"mode": "ping"}, then warm {"mode": "ping"}
                      -> platform cold start for this image
    sequence "agent": cold {"mode": "agent", "prompt": ...}, then a warm one
                      -> what a user feels on the first message (a real
                         `dsh headless` turn on a brand-new session)

Concurrency-N rounds release N probes together. `--processes` decides how:

    1      one process: N threads released by a threading.Barrier (default;
           how every run before this option worked)
    auto   min(8, CPU count, N) spawned worker processes (opt-in). Each has its
           own boto3 client, created and with credentials resolved BEFORE the
           release, and its own thread pool; one cross-process start gate
           releases every probe of the round. c=1 runs in one spawned worker.
           Release and t0 are compared across processes with perf_counter,
           which is one system-wide clock on Linux.
    K      like auto with K workers (never more than N)

With many threads in one process, request serialization, SigV4 signing and TLS
setup of all N requests share one GIL, so sends are staggered. Three
client-side figures are recorded per request so a report can say how much of a
number is the client:

    release_to_start_ms  round release -> this probe's t0 (cold only). Stagger
                         BEFORE the clock starts: not part of any latency.
    start_to_send_ms     t0 -> botocore's before-send event (request serialized
                         and signed, about to enter the HTTP layer). Inside the
                         measured latency.
    connect_ms           TCP + TLS setup of a new connection for this request
                         (null when a pooled connection was reused). Inside the
                         measured latency.

No connection to the runtime is opened before the release: a cold call still
pays for its own connection, exactly as before. A worker process that dies is
recorded, never dropped: probes it never sent are kept as not sent
(error_type "not_sent"), probes it may have sent as failed ("worker_failed"),
and the parent stops their sessions.

botocore retries are disabled (total_max_attempts=1), so a throttle or a 5xx
is a recorded failure instead of a silently longer latency. A non-200
response, an unparseable body, or agent.ok != true is a failed request: it is
kept in the raw file and counted, and it is excluded from latency statistics.

Boot kind (cold phase, both timestamps on the microVM clock, so client/VM
clock skew cancels out):

    real_boot  the wrapper process started after the request was sent:
               (response_ts - proc_start_ts) * 1000 < client total_ms
    warm_pool  the process was already running: the session was served by an
               instance AgentCore had provisioned before the request
    unknown    no usable timestamps (the request failed before the container
               answered)

On platform V2 runtimes ("platform_version": "V2" in deployments.json) a new
session does not boot the container: it is restored from a snapshot taken when
the runtime was created, so every session reports a process start from that
time. Such a probe is labelled

    snapshot_restore   the process predates the request on a V2 runtime

instead of warm_pool. Raw files written before this label existed are
relabelled in memory when summary.json is rebuilt; the raw files themselves
are never rewritten.

Demo 10 compares (request_ts - proc_start_ts) with the client time instead.
That is the same test for a ping, but an agent turn adds seconds of handling
time to the client time and would turn warm-pool hits into "real boots", so
response_ts is used here (request_ts is the fallback when it is missing).
Warm-phase requests inherit the boot kind of their session's cold request.

Raw file, one per (runtime, sequence, concurrency) cell, written to
<out>/raw/<UTCts>_<runtime>_<sequence>_c<N>.json:

    {"meta": {runtime, runtime_name, runtime_arn, platform_version, image_uri, docker_size_bytes,
              ecr_size_bytes, model_id, dsh_version, region, sequence, prompt,
              concurrency, rounds_planned, rounds_completed, settle_seconds,
              read_timeout_seconds, started_iso, finished_iso,
              process_mode ("inline"|"spawn"), processes, threads_per_process,
              start_method, client_warmed, cpu_count,
              worker_failures: [{round, worker, phase, reason}]},
     "requests": [{                       # one entry per request, ALL of them
        runtime, platform_version, sequence, phase ("cold"|"warm"), concurrency,
        round, probe_idx, session_id, payload,
        pid, worker,                      # client process; worker is null inline
        attempted,                        # false: not sent (warm: the cold one never
                                          # reached the container; cold: its worker
                                          # process failed before the release)
        release_to_start_ms, start_to_send_ms, connect_ms,   # see above
        send_iso, send_epoch,             # client clock, just before the call
        headers_ms,                       # call -> response headers received
        total_ms,                         # call -> body fully read (or -> error)
        status_code, request_id,
        ok, error_type, error_msg,        # error_type: throttle | timeout |
                                          # http_error | bad_body | agent_failed |
                                          # other | skipped | not_sent | worker_failed
        server,                           # full JSON returned by the container
        body_text,                        # raw body (truncated) when not JSON
        invocation_seq,
        uptime_at_request_ms,             # request_ts - proc_start_ts
        server_handling_ms,               # response_ts - request_ts
        outside_handler_ms,               # total_ms - server_handling_ms
        boot_kind,
        dsh_boot_ms,                      # agent.first_output_ts - agent.spawn_ts
        first_text_ms,                    # agent.first_text_ts - agent.spawn_ts (null
                                          # when the wrapper does not report it)
        dsh_total_ms,                     # agent.exit_ts - agent.spawn_ts
        platform_overhead_ms,             # total_ms - dsh_total_ms
        anomalies                         # e.g. cold probe with invocation_seq != 1
     }, ...],
     "sessions": [{session_id, stopped, stop_error}, ...]}

<out>/summary.json aggregates the LATEST raw file of every cell:

    {"generated_iso", "interrupted", "region", "test_started_iso",
     "test_finished_iso", "settle_seconds": [...], "sources": [raw file names],
     "platform_versions": {runtime: "V1"|"V2"|null},
     "totals": {requests, attempted, ok, failures, skipped, anomalies,
                sessions, sessions_stopped, worker_failures},
     "cells": [{runtime, sequence, phase, concurrency,
                boot_kind ("all"|"real_boot"|"warm_pool"|"snapshot_restore"|"unknown"),
                n, ok, failures, skipped, anomalies, error_types,
                process_mode, processes, threads_per_process,
                metrics: {<metric>: {n, min, p50, p90, p99, max, mean}}}, ...]}

`n` is the number of requests sent, `metrics` cover the successful ones only.
"""

import argparse
import json
import multiprocessing
import multiprocessing.connection
import os
import re
import sys
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

HERE = Path(__file__).resolve().parent
SEQUENCES = ("ping", "agent")
PHASES = ("cold", "warm")
DEFAULT_PROMPT = "Reply with exactly: pong"
BOOT_REAL, BOOT_POOL, BOOT_UNKNOWN = "real_boot", "warm_pool", "unknown"
BOOT_SNAPSHOT = "snapshot_restore"
BOOT_ORDER = ("all", BOOT_REAL, BOOT_POOL, BOOT_SNAPSHOT, BOOT_UNKNOWN)
SNAPSHOT_PLATFORMS = {"V2"}  # platform versions that restore sessions from a snapshot
BASE_METRICS = ("total_ms", "headers_ms", "uptime_at_request_ms",
                "server_handling_ms", "outside_handler_ms")
AGENT_METRICS = ("dsh_boot_ms", "first_text_ms", "dsh_total_ms", "platform_overhead_ms")
CLIENT_METRICS = ("release_to_start_ms", "start_to_send_ms", "connect_ms")
BEFORE_SEND = "before-send.bedrock-agentcore.InvokeAgentRuntime"
MAX_PROCESSES = 8
READY_TIMEOUT = 120.0   # seconds a worker gets to build its client / park its threads
# Per-call fields of a request record; also the shape of a request that was not sent.
EMPTY_CALL = {"send_iso": None, "send_epoch": None, "headers_ms": None, "total_ms": None,
              "status_code": None, "request_id": None, "error_type": None, "error_msg": None,
              "server": None, "body_text": None, "release_to_start_ms": None,
              "start_to_send_ms": None, "connect_ms": None}
_MARKS = threading.local()  # client-side marks of the request the calling thread is making
THROTTLE_CODES = {
    "ThrottlingException",
    "TooManyRequestsException",
    "ThrottledException",
    "ServiceQuotaExceededException",
}
MAX_ERROR_CHARS = 500
MAX_BODY_CHARS = 2000


class DeploymentsError(Exception):
    """deployments.json is missing or does not describe the requested runtimes."""


def utc_iso(epoch: float | None = None) -> str:
    moment = (datetime.now(timezone.utc) if epoch is None
              else datetime.fromtimestamp(epoch, timezone.utc))
    return moment.isoformat()


def file_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def load_deployments(path: Path) -> dict:
    if not path.exists():
        raise DeploymentsError(
            f"{path} not found - run scripts/deploy.sh first to create the runtime.")
    dep = json.loads(path.read_text())
    if not dep.get("region") or not dep.get("runtimes"):
        raise DeploymentsError(f"{path} must contain 'region' and a non-empty 'runtimes' map.")
    for key, rt in dep["runtimes"].items():
        if not rt.get("arn"):
            raise DeploymentsError(f"{path}: runtime {key!r} has no 'arn'.")
    return dep


def make_client(region: str, max_concurrency: int, read_timeout: int = 300):
    return boto3.client(
        "bedrock-agentcore",
        region_name=region,
        config=Config(
            retries={"total_max_attempts": 1},
            read_timeout=read_timeout,
            connect_timeout=30,
            max_pool_connections=max(64, 2 * max_concurrency),
        ),
    )


def make_warm_client(region: str, pool_size: int, read_timeout: int = 300, worker=None):
    """Client for one worker process, built before the release with everything
    warmed that can be warmed without touching the runtime: the session, the
    client and the credentials (resolved and refreshed now, never logged). No
    connection is opened, so a cold call still pays for its own."""
    session = boto3.Session(region_name=region)
    credentials = session.get_credentials()
    if credentials is None:
        raise RuntimeError("no AWS credentials found")
    credentials.get_frozen_credentials()
    return session.client(
        "bedrock-agentcore",
        config=Config(
            retries={"total_max_attempts": 1},
            read_timeout=read_timeout,
            connect_timeout=30,
            max_pool_connections=max(10, pool_size),
        ),
    )


def _stamp_before_send(**kwargs) -> None:
    marks = getattr(_MARKS, "current", None)
    if marks is not None and "send" not in marks:
        marks["send"] = time.perf_counter()


def _note_connect(seconds: float) -> None:
    marks = getattr(_MARKS, "current", None)
    if marks is not None:
        marks["connect"] = marks.get("connect", 0.0) + seconds


def instrument(client) -> bool:
    """Stamp botocore's before-send event (request serialized and signed, about
    to enter the HTTP layer) for the request each thread is making. The handler
    returns None, so it never changes the request. Safe to call twice."""
    events = getattr(getattr(client, "meta", None), "events", None)
    if events is None:
        return False
    events.register(BEFORE_SEND, _stamp_before_send, unique_id="dsh-coldstart-before-send")
    return True


def install_connect_timer(connection_class=None) -> bool:
    """Time TCP + TLS setup of every new connection botocore opens, charged to
    the request of the thread that opens it. Best effort: returns False when
    this botocore has no such class, and connect_ms then stays null."""
    if connection_class is None:
        try:
            from botocore.awsrequest import AWSHTTPSConnection as connection_class
        except ImportError:
            return False
    original = getattr(connection_class, "connect", None)
    if original is None:
        return False
    if getattr(original, "times_connect", False):
        return True

    def connect(self, *args, **kwargs):
        start = time.perf_counter()
        try:
            return original(self, *args, **kwargs)
        finally:
            _note_connect(time.perf_counter() - start)

    connect.times_connect = True
    connection_class.connect = connect
    return True


def new_session_id() -> str:
    return uuid.uuid4().hex + uuid.uuid4().hex[:8]  # 40 chars (>=33 required)


def build_payload(sequence: str, prompt: str) -> dict:
    if sequence == "ping":
        return {"mode": "ping"}
    return {"mode": "agent", "prompt": prompt}


def classify_error(exc: Exception) -> tuple[str, str, int | None]:
    """Map an exception to (error_type, message, http_status)."""
    if isinstance(exc, ClientError):
        code = exc.response.get("Error", {}).get("Code", "")
        status = exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
        kind = "throttle" if code in THROTTLE_CODES or status == 429 else "http_error"
        return kind, f"{code}: {exc}"[:MAX_ERROR_CHARS], status
    name = type(exc).__name__
    kind = "timeout" if isinstance(exc, BotoCoreError) and "Timeout" in name else "other"
    return kind, f"{name}: {exc}"[:MAX_ERROR_CHARS], None


def invoke_once(client, arn: str, session_id: str, payload: dict) -> dict:
    """One InvokeAgentRuntime call, timed to the headers and to the end of the
    body. Never raises: an exception becomes error_type/error_msg. The result
    also carries "t0_perf", the perf_counter value the timing started at."""
    out = dict(EMPTY_CALL)
    body = json.dumps(payload).encode()
    out["send_epoch"] = time.time()
    out["send_iso"] = utc_iso(out["send_epoch"])
    marks = _MARKS.current = {}
    resp = raw = None
    t0 = out["t0_perf"] = time.perf_counter()
    try:
        resp = client.invoke_agent_runtime(
            agentRuntimeArn=arn,
            runtimeSessionId=session_id,
            qualifier="DEFAULT",
            contentType="application/json",
            accept="application/json",
            payload=body,
        )
        # The response body is a stream: the call returns once the headers are in.
        out["headers_ms"] = round((time.perf_counter() - t0) * 1000.0, 1)
        raw = resp["response"].read()
        out["total_ms"] = round((time.perf_counter() - t0) * 1000.0, 1)
    except Exception as exc:  # recorded as a failed request, never dropped
        out["total_ms"] = round((time.perf_counter() - t0) * 1000.0, 1)
        out["error_type"], out["error_msg"], out["status_code"] = classify_error(exc)
    finally:
        _MARKS.current = None
    if "send" in marks:
        out["start_to_send_ms"] = round((marks["send"] - t0) * 1000.0, 1)
    if "connect" in marks:
        out["connect_ms"] = round(marks["connect"] * 1000.0, 1)
    if out["error_type"] is not None:
        return out
    meta = resp.get("ResponseMetadata") or {}
    out["status_code"] = resp.get("statusCode") or meta.get("HTTPStatusCode")
    out["request_id"] = meta.get("RequestId")
    try:
        parsed = json.loads(raw)
    except ValueError:  # includes JSONDecodeError and UnicodeDecodeError
        parsed = None
    if isinstance(parsed, dict):
        out["server"] = parsed
    else:
        out["body_text"] = bytes(raw[:MAX_BODY_CHARS]).decode("utf-8", "replace")
    return out


def _num(value) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _delta_ms(later, earlier) -> float | None:
    a, b = _num(later), _num(earlier)
    return None if a is None or b is None else round((a - b) * 1000.0, 1)


def judge(sequence: str, status, server) -> tuple[bool, str | None, str | None]:
    """Decide whether a completed call is a successful probe."""
    if status != 200:
        return False, "http_error", f"HTTP {status}"
    if not isinstance(server, dict):
        return False, "bad_body", "response body is not a JSON object"
    if sequence == "agent":
        agent = server.get("agent")
        if not isinstance(agent, dict):
            return False, "agent_failed", "response has no 'agent' block"
        if agent.get("ok") is not True:
            detail = f"exit_code={agent.get('exit_code')}: {agent.get('error')}"
            return False, "agent_failed", detail[:MAX_ERROR_CHARS]
    return True, None, None


def platform_boot_kind(kind: str, platform_version: str | None) -> str:
    """A process that predates the request is a warm-pool instance on V1 and a
    snapshot restore on V2."""
    if kind == BOOT_POOL and platform_version in SNAPSHOT_PLATFORMS:
        return BOOT_SNAPSHOT
    return kind


def classify_boot(server: dict, total_ms: float | None,
                  platform_version: str | None = None) -> str:
    proc = _num(server.get("proc_start_ts"))
    end = _num(server.get("response_ts"))
    if end is None:
        end = _num(server.get("request_ts"))
    if proc is None or end is None or total_ms is None:
        return BOOT_UNKNOWN
    kind = BOOT_REAL if (end - proc) * 1000.0 < total_ms else BOOT_POOL
    return platform_boot_kind(kind, platform_version)


def finalize(rec: dict, cold: dict | None = None) -> dict:
    """Fill ok/error and every derived field of a request record in place.
    `cold` is the session's cold record when `rec` is the warm one."""
    if rec["attempted"] and rec["error_type"] is None:
        rec["ok"], rec["error_type"], rec["error_msg"] = judge(
            rec["sequence"], rec["status_code"], rec["server"])
    else:
        rec["ok"] = False
    server = rec["server"] if isinstance(rec["server"], dict) else {}
    agent = server.get("agent") if isinstance(server.get("agent"), dict) else {}
    total = rec["total_ms"]
    handling = _delta_ms(server.get("response_ts"), server.get("request_ts"))
    dsh_total = _delta_ms(agent.get("exit_ts"), agent.get("spawn_ts"))
    answered = bool(server) and total is not None

    rec["invocation_seq"] = server.get("invocation_seq")
    rec["uptime_at_request_ms"] = _delta_ms(server.get("request_ts"), server.get("proc_start_ts"))
    rec["server_handling_ms"] = handling
    rec["outside_handler_ms"] = (round(total - handling, 1)
                                 if answered and handling is not None else None)
    rec["dsh_boot_ms"] = _delta_ms(agent.get("first_output_ts"), agent.get("spawn_ts"))
    rec["first_text_ms"] = _delta_ms(agent.get("first_text_ts"), agent.get("spawn_ts"))
    rec["dsh_total_ms"] = dsh_total
    rec["platform_overhead_ms"] = (round(total - dsh_total, 1)
                                   if answered and dsh_total is not None else None)

    anomalies = []
    seq = rec["invocation_seq"]
    if cold is None:
        rec["boot_kind"] = (classify_boot(server, total, rec.get("platform_version"))
                            if answered else BOOT_UNKNOWN)
        if isinstance(seq, int) and seq != 1:
            anomalies.append(f"cold probe has invocation_seq={seq} (expected 1)")
    else:
        rec["boot_kind"] = cold["boot_kind"]
        cold_server = cold["server"] if isinstance(cold["server"], dict) else {}
        cold_proc, proc = _num(cold_server.get("proc_start_ts")), _num(server.get("proc_start_ts"))
        if cold_proc is not None and proc is not None and cold_proc != proc:
            anomalies.append("warm probe was served by a different process than the cold probe")
        cold_seq = cold["invocation_seq"]
        if isinstance(seq, int) and isinstance(cold_seq, int) and seq != cold_seq + 1:
            anomalies.append(f"warm probe has invocation_seq={seq} (expected {cold_seq + 1})")
    rec["anomalies"] = anomalies
    return rec


class Release:
    """When a round was released (perf_counter). A multiprocessing Value has
    the same `.value`, so probe() reads either."""
    value = 0.0


def unsent(base: dict, phase: str, error_type: str, message: str) -> dict:
    return {**base, "phase": phase, "attempted": False, **EMPTY_CALL,
            "error_type": error_type, "error_msg": message}


def probe(client, runtime: str, arn: str, sequence: str, prompt: str, concurrency: int,
          rnd: int, idx: int, barrier, platform_version: str | None = None, *,
          session_id: str | None = None, release=None,
          worker: int | None = None) -> tuple[list[dict], dict]:
    """Cold request + warm request on one fresh session, then stop the session.
    Returns ([cold_record, warm_record], session_record). Never raises.

    `barrier` is what holds the probe until the round is released: a
    threading.Barrier, or any object whose wait() returns False when the
    release never came. `release.value` is the perf_counter time of the release."""
    session_id = session_id or new_session_id()
    payload = build_payload(sequence, prompt)
    base = {"runtime": runtime, "platform_version": platform_version,
            "sequence": sequence, "concurrency": concurrency,
            "round": rnd, "probe_idx": idx, "session_id": session_id, "payload": payload,
            "pid": os.getpid(), "worker": worker}
    session = {"session_id": session_id, "stopped": False, "stop_error": None}
    if barrier is not None:
        try:
            released = barrier.wait() is not False
        except threading.BrokenBarrierError:
            released = False
        if not released:
            cold = finalize(unsent(base, "cold", "not_sent", "the round was never released"))
            warm = finalize(unsent(base, "warm", "skipped", "cold invoke was not sent"), cold)
            session["stop_error"] = "not sent"
            return [cold, warm], session
    call = invoke_once(client, arn, session_id, payload)
    t0 = call.pop("t0_perf")
    if release is not None:
        call["release_to_start_ms"] = round((t0 - release.value) * 1000.0, 1)
    cold = finalize({**base, "phase": "cold", "attempted": True, **call})
    if cold["status_code"] == 200 and cold["server"] is not None:
        call = invoke_once(client, arn, session_id, payload)
        call.pop("t0_perf")
        warm = {**base, "phase": "warm", "attempted": True, **call}
    else:
        # The microVM never answered, so a second invoke would be another cold
        # start, not a warm sample. Keep a placeholder so the gap is visible.
        warm = unsent(base, "warm", "skipped",
                      "cold invoke did not reach the container; warm invoke not sent")
    finalize(warm, cold)

    try:
        client.stop_runtime_session(agentRuntimeArn=arn, runtimeSessionId=session_id,
                                    qualifier="DEFAULT")
        session["stopped"] = True
    except Exception as exc:  # best effort: the idle timeout reclaims the session
        session["stop_error"] = classify_error(exc)[1]
    return [cold, warm], session


def run_round(client, runtime: str, arn: str, sequence: str, prompt: str, concurrency: int,
              rnd: int, platform_version: str | None = None) -> tuple[list[dict], list[dict]]:
    """One round in this process: `concurrency` threads released by a barrier."""
    release = Release()

    def mark_release() -> None:
        release.value = time.perf_counter()

    barrier = threading.Barrier(concurrency, action=mark_release) if concurrency > 1 else None
    results: list[tuple[list[dict], dict] | None] = [None] * concurrency

    def worker(i: int) -> None:
        results[i] = probe(client, runtime, arn, sequence, prompt, concurrency, rnd, i, barrier,
                           platform_version, release=release)

    threads = [threading.Thread(target=worker, args=(i,), daemon=True)
               for i in range(concurrency)]
    if barrier is None:
        mark_release()
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    done = [r for r in results if r is not None]
    return [rec for recs, _ in done for rec in recs], [session for _, session in done]


def plan_processes(setting: str, concurrency: int) -> tuple[str, int]:
    """--processes -> ("inline", 1) or ("spawn", number of worker processes)."""
    if setting == "auto":
        return "spawn", max(1, min(MAX_PROCESSES, os.cpu_count() or 1, concurrency))
    count = int(setting)
    return ("inline", 1) if count == 1 else ("spawn", min(count, concurrency))


class _Gate:
    """A probe thread's side of the cross-process start gate: report that this
    thread is about to block, then block until the parent hands it a token."""

    def __init__(self, tokens, arrived: threading.Semaphore, timeout: float):
        self.tokens, self.arrived, self.timeout = tokens, arrived, timeout

    def wait(self) -> bool:
        self.arrived.release()
        return self.tokens.acquire(timeout=self.timeout)


def worker_main(worker: int, factory, spec: dict, threads: int, conn, gate, release) -> None:
    """Body of one spawned worker process. It never inherits a client or a
    socket: it builds its own client first, then serves rounds until told to
    stop (or until the parent goes away and the pipe closes)."""
    try:
        client = factory(spec["region"], 2 * threads, spec["read_timeout"], worker=worker)
        instrument(client)
        install_connect_timer()
    except BaseException as exc:  # reported to the parent, which records the worker as failed
        conn.send({"kind": "failed", "worker": worker,
                   "error": f"{type(exc).__name__}: {exc}"[:MAX_ERROR_CHARS]})
        return
    conn.send({"kind": "started", "worker": worker, "pid": os.getpid()})
    try:
        with ThreadPoolExecutor(max_workers=threads) as pool:
            while True:
                command = conn.recv()
                if command["cmd"] == "stop":
                    return
                rnd, arrived = command["round"], threading.Semaphore(0)
                futures = [
                    pool.submit(probe, client, spec["runtime"], spec["arn"], spec["sequence"],
                                spec["prompt"], spec["concurrency"], rnd, idx,
                                _Gate(gate, arrived, spec["gate_timeout"]),
                                spec["platform_version"], session_id=session_id,
                                release=release, worker=worker)
                    for idx, session_id in command["probes"]]
                for _ in futures:
                    arrived.acquire()
                conn.send({"kind": "ready", "worker": worker, "round": rnd})
                outcomes = [future.result() for future in futures]
                conn.send({"kind": "done", "worker": worker, "round": rnd,
                           "requests": [rec for recs, _ in outcomes for rec in recs],
                           "sessions": [session for _, session in outcomes]})
    except (EOFError, OSError, KeyboardInterrupt):
        return  # the parent is gone, or Ctrl-C reached the whole process group


class WorkerPool:
    """Spawned worker processes for one cell. They live for all its rounds, so
    process start, client construction and credential resolution happen once,
    before round 1, and never between rounds.

    The release is a semaphore: once every live worker has all its threads
    parked on it, the parent stamps the release time and posts one token per
    parked probe. A Barrier, Event or Condition would be the obvious choice,
    but each makes the releasing side wait for every sleeper to wake, so one
    worker killed while parked would stall or break the whole round. With
    tokens, the others still fire and only the dead worker's probes are
    recorded as not sent. Each worker talks to the parent over its own pipe,
    so a dying worker cannot leave a shared queue locked either."""

    def __init__(self, factory, spec: dict, processes: int,
                 ready_timeout: float = READY_TIMEOUT):
        self.factory, self.spec, self.ready_timeout = factory, spec, ready_timeout
        self.ctx = multiprocessing.get_context("spawn")
        self.gate = self.ctx.Semaphore(0)
        self.release = self.ctx.Value("d", 0.0, lock=False)  # one writer: the parent
        self.groups = [list(range(w, spec["concurrency"], processes)) for w in range(processes)]
        self.conns: list = [None] * processes
        self.procs: list = [None] * processes
        self.dead: dict[int, str] = {}
        self.failures: list[dict] = []
        self.cleanup_client = None
        self.start(range(processes), rnd=0)

    def start(self, workers, rnd: int) -> None:
        """Spawn the given workers and wait until each has built its client."""
        workers = list(workers)
        for w in workers:
            self.dead.pop(w, None)
            if self.conns[w] is not None:
                self.conns[w].close()
            self.conns[w], child = self.ctx.Pipe()
            self.procs[w] = self.ctx.Process(
                target=worker_main, daemon=True,
                args=(w, self.factory, self.spec, len(self.groups[w]), child, self.gate, self.release))
            self.procs[w].start()
            child.close()  # the worker holds the only other end, so its death reads as EOF here
        self.collect("started", set(workers), rnd, self.ready_timeout, "start")

    def collect(self, kind: str, waiting: set, rnd: int, timeout: float, phase: str) -> dict:
        """Wait for one `kind` message from every worker in `waiting`. A worker
        that reports failure, exits or runs out of time is killed, marked dead
        and logged; the others are not held up by it."""
        got: dict[int, dict] = {}
        deadline = time.monotonic() + timeout
        while True:
            pending = {self.conns[w]: w for w in waiting - got.keys() - self.dead.keys()}
            if not pending:
                return got
            remaining = deadline - time.monotonic()
            readable = multiprocessing.connection.wait(list(pending), max(0.0, remaining))
            if not readable:
                for w in pending.values():
                    self.fail(w, rnd, phase, f"worker process gave no '{kind}' within {timeout:g} s")
                continue
            for conn in readable:
                w = pending[conn]
                try:
                    message = conn.recv()
                except (EOFError, OSError):
                    self.procs[w].join(timeout=5)
                    self.fail(w, rnd, phase,
                              f"worker process exited with code {self.procs[w].exitcode}")
                    continue
                if message["kind"] == "failed":
                    self.fail(w, rnd, phase, message["error"])
                elif message["kind"] == kind and message.get("round", rnd) == rnd:
                    got[w] = message

    def fail(self, worker: int, rnd: int, phase: str, reason: str) -> None:
        if self.procs[worker].is_alive():
            self.procs[worker].kill()
            self.procs[worker].join(timeout=5)
        self.dead[worker] = reason
        self.failures.append({"round": rnd, "worker": worker, "phase": phase, "reason": reason})
        print(f"  [warn] round {rnd}: worker {worker} failed during {phase}: {reason}")

    def run_round(self, rnd: int) -> tuple[list[dict], list[dict]]:
        if self.dead:  # give failed workers one fresh start per round
            self.start(sorted(self.dead), rnd)
        probes = {w: [(idx, new_session_id()) for idx in group]
                  for w, group in enumerate(self.groups)}
        while self.gate.acquire(False):  # tokens a dead worker never took
            pass
        live = set(probes) - self.dead.keys()
        for w in sorted(live):
            try:
                self.conns[w].send({"cmd": "round", "round": rnd, "probes": probes[w]})
            except OSError:  # it died while idle, between rounds
                self.procs[w].join(timeout=5)
                self.fail(w, rnd, "before release",
                          f"worker process exited with code {self.procs[w].exitcode}")
        ready = self.collect("ready", live, rnd, self.ready_timeout, "before release")
        tokens = sum(len(probes[w]) for w in ready)
        self.release.value = time.perf_counter()
        for _ in range(tokens):
            self.gate.release()
        done = self.collect("done", set(ready), rnd, self.spec["finish_timeout"], "after release")
        requests, sessions = [], []
        for w, assigned in probes.items():
            if w in done:
                requests += done[w]["requests"]
                sessions += done[w]["sessions"]
            else:
                recs, sess = self.lost(w, rnd, assigned, released=w in ready)
                requests += recs
                sessions += sess
        requests.sort(key=lambda r: (r["probe_idx"], PHASES.index(r["phase"])))
        return requests, sessions

    def lost(self, worker: int, rnd: int, assigned: list, released: bool) -> tuple[list, list]:
        """Records for the probes of a worker that never reported its round."""
        spec, reason = self.spec, self.dead.get(worker, "worker process failed")
        requests, sessions = [], []
        for idx, session_id in assigned:
            base = {"runtime": spec["runtime"], "platform_version": spec["platform_version"],
                    "sequence": spec["sequence"], "concurrency": spec["concurrency"],
                    "round": rnd, "probe_idx": idx, "session_id": session_id,
                    "payload": build_payload(spec["sequence"], spec["prompt"]),
                    "pid": self.procs[worker].pid, "worker": worker}
            session = {"session_id": session_id, "stopped": False, "stop_error": "not sent"}
            if released:
                # The worker died after the release: the request may be on the wire
                # and the session may exist, so it is a failure and the session is stopped.
                cold = {**unsent(base, "cold", "worker_failed",
                                 f"{reason}; the request may have been sent"), "attempted": True}
                session["stopped"], session["stop_error"] = self.stop_session(session_id)
            else:
                cold = unsent(base, "cold", "not_sent", f"{reason}; the request was not sent")
            finalize(cold)
            warm = finalize(unsent(base, "warm", "skipped", "cold invoke did not complete"), cold)
            requests += [cold, warm]
            sessions.append(session)
        return requests, sessions

    def stop_session(self, session_id: str) -> tuple[bool, str | None]:
        try:
            if self.cleanup_client is None:
                self.cleanup_client = self.factory(self.spec["region"], 8,
                                                   self.spec["read_timeout"], worker=None)
            self.cleanup_client.stop_runtime_session(
                agentRuntimeArn=self.spec["arn"], runtimeSessionId=session_id, qualifier="DEFAULT")
            return True, None
        except Exception as exc:  # best effort: the idle timeout reclaims the session
            return False, classify_error(exc)[1]

    def close(self) -> None:
        for w, proc in enumerate(self.procs):
            if proc is not None and proc.is_alive():
                try:
                    self.conns[w].send({"cmd": "stop"})
                except OSError:
                    pass
        deadline = time.monotonic() + 5.0  # one grace period for all workers, not one each
        for w, proc in enumerate(self.procs):
            if proc is not None:
                proc.join(timeout=max(0.0, deadline - time.monotonic()))
                if proc.is_alive():
                    proc.kill()
                    proc.join(timeout=5)
                self.conns[w].close()


def cell_meta(dep: dict, runtime: str, sequence: str, prompt: str, concurrency: int,
              rounds: int, settle: float, read_timeout: int, mode: str = "inline",
              processes: int = 1) -> dict:
    rt = dep["runtimes"][runtime]
    return {
        "runtime": runtime, "runtime_name": rt.get("name"), "runtime_arn": rt["arn"],
        "platform_version": rt.get("platform_version"), "image_uri": rt.get("image_uri"),
        "docker_size_bytes": rt.get("docker_size_bytes"),
        "ecr_size_bytes": rt.get("ecr_size_bytes"),
        "model_id": dep.get("model_id"), "dsh_version": dep.get("dsh_version"),
        "region": dep["region"], "sequence": sequence,
        "prompt": prompt if sequence == "agent" else None,
        "concurrency": concurrency, "rounds_planned": rounds, "rounds_completed": 0,
        "settle_seconds": settle, "read_timeout_seconds": read_timeout,
        "started_iso": utc_iso(), "finished_iso": None,
        "process_mode": mode, "processes": processes,
        "threads_per_process": [len(range(w, concurrency, processes)) for w in range(processes)],
        "start_method": "spawn" if mode == "spawn" else None,
        "client_warmed": mode == "spawn", "cpu_count": os.cpu_count(),
        "worker_failures": [],
    }


def safe_name(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]", "-", text)


def write_raw(out_dir: Path, name: str, meta: dict, requests: list[dict],
              sessions: list[dict]) -> Path:
    path = out_dir / "raw" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(
        {"meta": meta, "requests": requests, "sessions": sessions}, indent=2))
    return path


def run_cell(client, dep: dict, runtime: str, sequence: str, concurrency: int, rounds: int,
             settle: float, prompt: str, read_timeout: int, out_dir: Path, sleep,
             processes: str = "1", factory=make_warm_client) -> Path:
    """Run `rounds` rounds of one (runtime, sequence, concurrency) cell and
    write its raw file (also when interrupted). Returns the raw file path.
    `processes` is the --processes setting; `client` serves the inline mode,
    `factory` builds one client per worker process in the spawn mode."""
    mode, count = plan_processes(processes, concurrency)
    meta = cell_meta(dep, runtime, sequence, prompt, concurrency, rounds, settle, read_timeout,
                     mode, count)
    requests: list[dict] = []
    sessions: list[dict] = []
    fanout = ("1 process, in-process threads" if mode == "inline"
              else f"{count} spawned process(es) x up to {max(meta['threads_per_process'])} thread(s)")
    print(f"== {runtime} {sequence} c={concurrency} rounds={rounds} ({meta['runtime_name']}; {fanout}) ==")
    pool = None
    try:
        if mode == "spawn":
            pool = WorkerPool(factory, {
                "region": dep["region"], "arn": meta["runtime_arn"], "runtime": runtime,
                "sequence": sequence, "prompt": prompt, "concurrency": concurrency,
                "platform_version": meta["platform_version"], "read_timeout": read_timeout,
                "gate_timeout": READY_TIMEOUT + 60.0,
                "finish_timeout": 3.0 * read_timeout + 60.0}, count)
        for rnd in range(1, rounds + 1):
            if rnd > 1:
                sleep(settle)
            if pool is not None:
                recs, sess = pool.run_round(rnd)
            else:
                recs, sess = run_round(client, runtime, meta["runtime_arn"], sequence, prompt,
                                       concurrency, rnd, meta["platform_version"])
            requests.extend(recs)
            sessions.extend(sess)
            meta["rounds_completed"] = rnd
            print(f"  round {rnd}/{rounds}: {round_line(recs)}")
    finally:
        if pool is not None:
            meta["worker_failures"] = pool.failures
            pool.close()
        meta["finished_iso"] = utc_iso()
        path = write_raw(out_dir, f"{file_stamp()}_{safe_name(runtime)}_{sequence}_c{concurrency}.json",
                         meta, requests, sessions)
        print(f"  wrote {path}")
    return path


def round_line(recs: list[dict]) -> str:
    parts = []
    for phase in PHASES:
        sent = [r for r in recs if r["phase"] == phase and r["attempted"]]
        good = sorted(r["total_ms"] for r in sent if r["ok"])
        shown = (str(good) if len(good) <= 4
                 else f"p50={percentile(good, 50):.0f} max={good[-1]:.0f}")
        parts.append(f"{phase} ok={len(good)}/{len(sent)} total_ms={shown}")
    boots = [r["boot_kind"] for r in recs if r["phase"] == "cold"]
    parts.append("boot=" + ",".join(f"{k}:{boots.count(k)}" for k in BOOT_ORDER[1:] if k in boots))
    client = [f"{label} max {max(values):.1f} ms" for label, values in (
        (name, [r[key] for r in recs if r.get(key) is not None])
        for name, key in (("release->t0", "release_to_start_ms"), ("t0->send", "start_to_send_ms"),
                          ("connect", "connect_ms"))) if values]
    if client:
        parts.append("client: " + ", ".join(client))
    unsent_cold = sum(1 for r in recs if r["phase"] == "cold" and not r["attempted"])
    if unsent_cold:
        parts.append(f"NOT SENT: {unsent_cold}")
    return " | ".join(parts)


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


def stats(values: list) -> dict:
    vals = [v for v in values if v is not None]
    if not vals:
        return {"n": 0, "min": None, "p50": None, "p90": None, "p99": None,
                "max": None, "mean": None}
    return {
        "n": len(vals),
        "min": round(min(vals), 1),
        "p50": round(percentile(vals, 50), 1),
        "p90": round(percentile(vals, 90), 1),
        "p99": round(percentile(vals, 99), 1),
        "max": round(max(vals), 1),
        "mean": round(sum(vals) / len(vals), 1),
    }


def summarize(requests: list[dict]) -> list[dict]:
    """One cell per (runtime, sequence, phase, concurrency, boot kind), plus an
    "all" boot-kind row for each (runtime, sequence, phase, concurrency)."""
    groups: dict[tuple, list[dict]] = {}
    for r in requests:
        key = (r["runtime"], r["sequence"], r["phase"], r["concurrency"])
        groups.setdefault(key + ("all",), []).append(r)
        groups.setdefault(key + (r["boot_kind"],), []).append(r)

    def order(key: tuple) -> tuple:
        runtime, sequence, phase, concurrency, boot = key
        return (runtime, SEQUENCES.index(sequence), concurrency,
                PHASES.index(phase), BOOT_ORDER.index(boot))

    cells = []
    for key in sorted(groups, key=order):
        runtime, sequence, phase, concurrency, boot = key
        recs = groups[key]
        sent = [r for r in recs if r["attempted"]]
        good = [r for r in sent if r["ok"]]
        error_types: dict[str, int] = {}
        for r in sent:
            if not r["ok"]:
                error_types[r["error_type"]] = error_types.get(r["error_type"], 0) + 1
        names = BASE_METRICS + CLIENT_METRICS + (AGENT_METRICS if sequence == "agent" else ())
        cells.append({
            "runtime": runtime, "sequence": sequence, "phase": phase,
            "concurrency": concurrency, "boot_kind": boot,
            "n": len(sent), "ok": len(good), "failures": len(sent) - len(good),
            "skipped": len(recs) - len(sent),
            "anomalies": sum(1 for r in recs if r["anomalies"]),
            "error_types": dict(sorted(error_types.items())),
            "metrics": {m: stats([r.get(m) for r in good]) for m in names},
        })
    return cells


def rebuild_summary(out_dir: Path, region: str, interrupted: bool = False,
                    platform_versions: dict | None = None) -> dict:
    """Aggregate the LATEST raw file of every (runtime, sequence, concurrency)
    cell under out_dir/raw into summary.json. Scanning the disk instead of only
    this run's cells lets later partial runs accumulate into one summary.
    `platform_versions` (runtime -> version, from deployments.json) labels the
    boot kind of raw files written before platform_version was recorded."""
    versions = dict(platform_versions or {})
    latest: dict[tuple, tuple[Path, dict]] = {}
    for f in sorted((out_dir / "raw").glob("*.json")):
        if f.name.startswith("smoke"):
            continue
        data = json.loads(f.read_text())
        meta = data.get("meta", {})
        if {"runtime", "sequence", "concurrency"} <= meta.keys():
            latest[(meta["runtime"], meta["sequence"], meta["concurrency"])] = (f, data)
    used = sorted(latest.values(), key=lambda fd: fd[0].name)
    requests = []
    for _, data in used:
        for r in data["requests"]:
            version = r.get("platform_version") or versions.get(r["runtime"])
            versions.setdefault(r["runtime"], version)
            requests.append({**r, "boot_kind": platform_boot_kind(r["boot_kind"], version)})
    sessions = [s for _, data in used for s in data.get("sessions", [])]
    metas = [data["meta"] for _, data in used]
    sent = [r for r in requests if r["attempted"]]
    summary = {
        "generated_iso": utc_iso(),
        "interrupted": interrupted,
        "region": region,
        "test_started_iso": min((m["started_iso"] for m in metas), default=None),
        "test_finished_iso": max((m["finished_iso"] for m in metas if m["finished_iso"]),
                                 default=None),
        "settle_seconds": sorted({m["settle_seconds"] for m in metas}),
        "sources": [f.name for f, _ in used],
        "platform_versions": {r: versions.get(r) for r in sorted({q["runtime"] for q in requests})},
        "totals": {
            "requests": len(requests),
            "attempted": len(sent),
            "ok": sum(1 for r in sent if r["ok"]),
            "failures": sum(1 for r in sent if not r["ok"]),
            "skipped": len(requests) - len(sent),
            "anomalies": sum(1 for r in requests if r["anomalies"]),
            "sessions": len(sessions),
            "sessions_stopped": sum(1 for s in sessions if s["stopped"]),
            "worker_failures": sum(len(m.get("worker_failures") or []) for m in metas),
        },
        "cells": summarize(requests),
    }
    fanout = {(m["runtime"], m["sequence"], m["concurrency"]): m for m in metas}
    for cell in summary["cells"]:
        meta = fanout[(cell["runtime"], cell["sequence"], cell["concurrency"])]
        cell["process_mode"] = meta.get("process_mode", "inline")  # files from before --processes
        cell["processes"] = meta.get("processes", 1)
        cell["threads_per_process"] = meta.get("threads_per_process") or [cell["concurrency"]]
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "summary.json").write_text(json.dumps(summary, indent=2))
    return summary


def _cell(value) -> str:
    return "-" if value is None else f"{value:.1f}"


def print_summary_table(cells: list[dict]) -> None:
    hdr = (f"{'runtime':<16} {'seq':<5} {'phase':<5} {'conc':>4} {'boot':<16} {'n':>4} "
           f"{'fail':>4} {'min':>9} {'p50':>9} {'p90':>9} {'p99':>9} {'max':>9} {'mean':>9}")
    print("\ntotal_ms (send -> body fully read), successful requests only")
    print(hdr)
    print("-" * len(hdr))
    for c in cells:
        t = c["metrics"]["total_ms"]
        print(f"{c['runtime']:<16} {c['sequence']:<5} {c['phase']:<5} {c['concurrency']:>4} "
              f"{c['boot_kind']:<16} {c['n']:>4} {c['failures']:>4} {_cell(t['min']):>9} "
              f"{_cell(t['p50']):>9} {_cell(t['p90']):>9} {_cell(t['p99']):>9} "
              f"{_cell(t['max']):>9} {_cell(t['mean']):>9}")


def describe(rec: dict) -> str:
    """One readable line for a single request (used by --smoke)."""
    head = f"  {rec['sequence']:<5} {rec['phase']:<4} "
    if not rec["attempted"]:
        return head + f"SKIP  {rec['error_msg']}"
    if not rec["ok"]:
        return head + (f"FAIL  {rec['error_type']} status={rec['status_code']} "
                       f"after {_cell(rec['total_ms'])} ms: {rec['error_msg']}")
    parts = [f"total={_cell(rec['total_ms'])} ms", f"headers={_cell(rec['headers_ms'])} ms",
             f"boot={rec['boot_kind']}",
             f"uptime_at_request={_cell(rec['uptime_at_request_ms'])} ms",
             f"seq={rec['invocation_seq']}"]
    if rec["sequence"] == "agent":
        parts += [f"dsh_boot={_cell(rec['dsh_boot_ms'])} ms",
                  f"first_text={_cell(rec['first_text_ms'])} ms",
                  f"dsh_total={_cell(rec['dsh_total_ms'])} ms",
                  f"platform_overhead={_cell(rec['platform_overhead_ms'])} ms",
                  f"model={rec['server']['agent'].get('model')}"]
    if rec["anomalies"]:
        parts.append("ANOMALY: " + "; ".join(rec["anomalies"]))
    return head + "ok    " + " ".join(parts)


def run_smoke(client, dep: dict, runtimes: list[str], sequences: list[str], prompt: str,
              read_timeout: int, out_dir: Path) -> int:
    requests: list[dict] = []
    sessions: list[dict] = []
    started = utc_iso()
    for runtime in runtimes:
        rt = dep["runtimes"][runtime]
        print(f"smoke: {runtime} ({rt.get('name')})")
        for sequence in sequences:
            recs, session = probe(client, runtime, rt["arn"], sequence, prompt, 1, 1, 0, None,
                                  rt.get("platform_version"))
            requests.extend(recs)
            sessions.append(session)
            for rec in recs:
                print(describe(rec))
            stop = "yes" if session["stopped"] else f"NO ({session['stop_error']})"
            print(f"  {sequence:<5} session {session['session_id'][:8]} stopped: {stop}")
    meta = {"mode": "smoke", "region": dep["region"], "runtimes": runtimes,
            "sequences": sequences, "prompt": prompt, "model_id": dep.get("model_id"),
            "dsh_version": dep.get("dsh_version"), "read_timeout_seconds": read_timeout,
            "started_iso": started, "finished_iso": utc_iso()}
    path = write_raw(out_dir, f"smoke_{file_stamp()}.json", meta, requests, sessions)
    failed = sum(1 for r in requests if not r["ok"])
    print(f"smoke: {len(requests) - failed}/{len(requests)} requests ok, wrote {path}")
    return 1 if failed else 0


def default_rounds(concurrency: int) -> int:
    """Same sample budget as demo 10: 10 rounds at c=1, fewer as c grows."""
    if concurrency == 1:
        return 10
    if concurrency <= 5:
        return 4
    if concurrency <= 10:
        return 2
    return 1


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=(__doc__ or "").split("\n")[0])
    mode = p.add_mutually_exclusive_group(required=True)
    mode.add_argument("--smoke", action="store_true",
                      help="one ping sequence + one agent sequence per runtime, readable output")
    mode.add_argument("--full", action="store_true",
                      help="run every runtime x sequence x concurrency cell")
    p.add_argument("--runtimes", default=None,
                   help="comma-separated keys under 'runtimes' in deployments.json (default: all)")
    p.add_argument("--sequences", default="ping,agent",
                   help="comma-separated probe sequences (default: ping,agent)")
    p.add_argument("--concurrency", default="1",
                   help="comma-separated concurrency levels for --full (default: 1)")
    p.add_argument("--rounds", type=int, default=None,
                   help="rounds per cell (default: 10 at c=1, 4 up to c=5, 2 up to c=10, else 1)")
    p.add_argument("--processes", default="1",
                   help="client processes per cell: '1' = one process with threads (default); "
                        "'auto' = min(8, CPU count, concurrency) spawned workers, each with its "
                        "own client and thread pool; K = K spawned workers")
    p.add_argument("--prompt", default=DEFAULT_PROMPT,
                   help=f"prompt for the agent sequence (default: {DEFAULT_PROMPT!r})")
    p.add_argument("--settle-seconds", type=float, default=5.0,
                   help="pause between rounds and between cells; the warm pool refills over "
                        "minutes, so a short pause leaves later rounds as real boots (default 5)")
    p.add_argument("--read-timeout", type=int, default=300,
                   help="seconds to wait for a response (default 300)")
    p.add_argument("--out", default="results",
                   help="output directory, relative to this file unless absolute (default: results/)")
    p.add_argument("--deployments", default=str(HERE / "deployments.json"),
                   help="path to deployments.json")
    args = p.parse_args(argv)

    args.sequence_list = [s.strip() for s in args.sequences.split(",") if s.strip()]
    unknown = [s for s in args.sequence_list if s not in SEQUENCES]
    if unknown or not args.sequence_list:
        p.error(f"--sequences must be a subset of {','.join(SEQUENCES)}, got {args.sequences!r}")
    try:
        args.conc_list = [int(c) for c in args.concurrency.split(",") if c.strip()]
    except ValueError:
        p.error(f"--concurrency must be comma-separated integers, got {args.concurrency!r}")
    if not args.conc_list or any(c < 1 for c in args.conc_list):
        p.error("--concurrency values must be >= 1")
    if args.processes != "auto" and not (args.processes.isdigit() and int(args.processes) >= 1):
        p.error(f"--processes must be 'auto' or an integer >= 1, got {args.processes!r}")
    if args.rounds is not None and args.rounds < 1:
        p.error("--rounds must be >= 1")
    if args.settle_seconds < 0:
        p.error("--settle-seconds must be >= 0")
    return args


def main(argv: list[str] | None = None, client=None, sleep=None, client_factory=None) -> int:
    """`client`, `sleep` and `client_factory` are injection points for offline
    tests: `client` serves the single-process mode (and --smoke), while
    `client_factory(region, pool_size, read_timeout, worker=...)` must be
    picklable, because every spawned worker process calls it for its own client."""
    args = parse_args(argv)
    sleep = sleep or time.sleep
    try:
        dep = load_deployments(Path(args.deployments))
    except DeploymentsError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    runtimes = ([r.strip() for r in args.runtimes.split(",") if r.strip()]
                if args.runtimes else list(dep["runtimes"]))
    missing = [r for r in runtimes if r not in dep["runtimes"]]
    if missing or not runtimes:
        print(f"ERROR: runtime(s) {missing} not in deployments.json "
              f"(have: {', '.join(dep['runtimes'])})", file=sys.stderr)
        return 2
    out_dir = Path(args.out) if Path(args.out).is_absolute() else HERE / args.out
    spawning = args.full and any(plan_processes(args.processes, c)[0] == "spawn"
                                 for c in args.conc_list)
    if client is not None and spawning and client_factory is None:
        print("ERROR: a client object cannot be handed to worker processes; "
              "use --processes 1 or pass client_factory.", file=sys.stderr)
        return 2
    if client is None and not spawning:
        client = make_client(dep["region"], max(args.conc_list), args.read_timeout)
    if client is not None:
        instrument(client)
        install_connect_timer()

    if args.smoke:
        return run_smoke(client, dep, runtimes, args.sequence_list, args.prompt,
                         args.read_timeout, out_dir)

    ran_any = False
    interrupted = False
    try:
        for runtime in runtimes:
            for conc in args.conc_list:
                for sequence in args.sequence_list:
                    if ran_any:
                        sleep(args.settle_seconds)
                    ran_any = True
                    run_cell(client, dep, runtime, sequence, conc,
                             args.rounds or default_rounds(conc), args.settle_seconds,
                             args.prompt, args.read_timeout, out_dir, sleep,
                             args.processes, client_factory or make_warm_client)
    except KeyboardInterrupt:
        interrupted = True
        print("\nInterrupted - flushing partial summary.", file=sys.stderr)
    summary = rebuild_summary(out_dir, dep["region"], interrupted,
                              {key: rt.get("platform_version") for key, rt in dep["runtimes"].items()})
    print_summary_table(summary["cells"])
    totals = summary["totals"]
    print(f"\nrequests sent: {totals['attempted']}  ok: {totals['ok']}  "
          f"failures: {totals['failures']}  skipped: {totals['skipped']}  "
          f"anomalies: {totals['anomalies']}  "
          f"sessions stopped: {totals['sessions_stopped']}/{totals['sessions']}  "
          f"worker failures: {totals['worker_failures']}")
    print(f"wrote {out_dir / 'summary.json'} ({len(summary['sources'])} raw cell files)")
    if interrupted:
        return 130
    return 1 if totals["failures"] or totals["worker_failures"] else 0


if __name__ == "__main__":
    sys.exit(main())
