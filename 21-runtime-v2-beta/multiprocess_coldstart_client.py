"""Spawn-isolated AgentCore load client with per-process clients and SDK timing.

before-send is pre-transport, NOT a wire-send timestamp; response-received
includes SDK response parsing and is NOT a server-only latency measurement.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
import multiprocessing
import os
from pathlib import Path
import queue
import resource
import threading
import time
import uuid

import boto3
from botocore.config import Config

import coldstart_v2_c200 as shared

class Recorder:
    def __init__(self, raw):
        self.raw = raw
        self.local = threading.local()
        self.events = []
        self.lock = threading.Lock()
        for operation in ("InvokeAgentRuntime", "StopRuntimeSession"):
            raw.meta.events.register(f"before-send.bedrock-agentcore.{operation}", self.before_send)
            raw.meta.events.register(f"response-received.bedrock-agentcore.{operation}", self.response_received)

    def before_send(self, **kwargs):
        event = getattr(self.local, "event", None)
        if event is not None:
            event["before_send_perf"].append(time.perf_counter())

    def response_received(self, **kwargs):
        event = getattr(self.local, "event", None)
        if event is not None:
            event["response_received_perf"].append(time.perf_counter())

    def call(self, arn, session_id, operation, attempt=1):
        event = {"operation": operation, "session_id": session_id, "runtime_arn": arn,
                 "attempt": attempt, "pid": os.getpid(), "started_iso": shared.bench.utc_iso(),
                 "before_send_perf": [], "response_received_perf": []}
        self.local.event = event
        start = time.perf_counter()
        event["started_perf"] = start
        try:
            if operation == "invoke":
                response = self.raw.invoke_agent_runtime(agentRuntimeArn=arn, runtimeSessionId=session_id,
                    qualifier="DEFAULT", contentType="application/json", accept="application/json",
                    payload=json.dumps({"ping": "coldstart"}).encode())
                event["metadata"] = response["ResponseMetadata"]
                stream = response["response"]
                try:
                    payload = stream.read()
                    event["completed_perf"] = time.perf_counter()
                finally:
                    stream.close()
                event["body"] = json.loads(payload)
                event["status"] = response.get("statusCode") or response["ResponseMetadata"]["HTTPStatusCode"]
                shared.original.check_payload(event["status"], event["body"])
                if event["body"].get("echo") != {"ping": "coldstart"}:
                    raise ValueError("Unexpected echo")
            else:
                response = self.raw.stop_runtime_session(agentRuntimeArn=arn,
                    runtimeSessionId=session_id, qualifier="DEFAULT")
                event["completed_perf"] = time.perf_counter()
                event["metadata"] = response["ResponseMetadata"]
                event["status"] = response["ResponseMetadata"]["HTTPStatusCode"]
                if event["status"] != 200:
                    raise ValueError("Unexpected stop status")
            event["valid"] = True
        except Exception as exc:
            event.setdefault("completed_perf", time.perf_counter())
            event["error_type"], event["error"] = shared.bench.classify_error(exc)
            if isinstance(getattr(exc, "response", None), dict):
                event["metadata"] = exc.response.get("ResponseMetadata", {})
                event["error_code"] = exc.response.get("Error", {}).get("Code")
                event["status"] = event["metadata"].get("HTTPStatusCode")
        finally:
            event["elapsed_ms"] = (event["completed_perf"] - start) * 1000
            event["finished_iso"] = shared.bench.utc_iso()
            self.local.event = None
            with self.lock:
                self.events.append(event)
        return event


def probe(recorder, arn, concurrency, index, session_id, barrier):
    row = {"size": "500mb", "concurrency": concurrency, "round": 1, "request_idx": index,
           "session_id": session_id, "pid": os.getpid(), "cold_ms": None, "warm_ms": None,
           "success": False, "error_type": None, "error_msg": None, "status_code": None,
           "proc_start_ts": None, "request_ts": None, "stopped": False}
    barrier.wait(timeout=120)
    row["wall_start_iso"] = shared.bench.utc_iso()
    try:
        cold = recorder.call(arn, session_id, "invoke")
        if cold.get("valid"):
            row.update(cold_ms=round(cold["elapsed_ms"], 1), success=True, status_code=cold["status"],
                       proc_start_ts=cold["body"]["proc_start_ts"], request_ts=cold["body"]["request_ts"])
            warm = recorder.call(arn, session_id, "invoke", attempt=2)
            if warm.get("valid"):
                row["warm_ms"] = round(warm["elapsed_ms"], 1)
            else:
                row["error_msg"] = "warm failed: " + warm["error"]
        else:
            row.update(error_type=cold["error_type"], error_msg=cold["error"], status_code=cold.get("status"))
    finally:
        stop = recorder.call(arn, session_id, "stop")
        row["stopped"] = bool(stop.get("valid"))
        row["stop_absent"] = stop.get("error_code") == "ResourceNotFoundException"
    return row


def make_client(region, pool_size):
    # Construct in the spawned process; never inherit a Session/client/socket.
    session = boto3.Session(region_name=region)
    credentials = session.get_credentials()
    if credentials is None:
        raise RuntimeError("No AWS credentials")
    credentials.get_frozen_credentials()  # Resolve refresh before the launch barrier; never log credentials.
    return session.client("bedrock-agentcore", config=Config(
        connect_timeout=30, read_timeout=300, max_pool_connections=pool_size,
        retries={"total_max_attempts": 1}))


def worker_main(region, arn, concurrency, assignments, pool_size, barrier, done_barrier, results, factory):
    report = {"pid": os.getpid(), "indices": [i for i, _ in assignments],
              "pool_size": pool_size, "rows": [], "events": [], "errors": []}
    recorder = None
    cpu_start = time.process_time()
    started = time.perf_counter()
    try:
        raw = factory(region, pool_size)
        recorder = Recorder(raw)
        report["client_config"] = {"max_pool_connections": raw.meta.config.max_pool_connections,
                                   "retries": raw.meta.config.retries}
        report["client_ready_perf"] = time.perf_counter()
        cpu_start = time.process_time()
        started = time.perf_counter()
        with ThreadPoolExecutor(max_workers=len(assignments)) as executor:
            futures = [executor.submit(probe, recorder, arn, concurrency, index, sid, barrier)
                       for index, sid in assignments]
            results.put({"kind": "ready", "pid": os.getpid()})
            for future in futures:
                try:
                    report["rows"].append(future.result())
                except Exception as exc:
                    report["errors"].append(type(exc).__name__)
    except BaseException as exc:
        barrier.abort()
        report["errors"].append(type(exc).__name__)
    finally:
        report["cpu_seconds"] = time.process_time() - cpu_start
        report["wall_seconds"] = time.perf_counter() - started
        usage = resource.getrusage(resource.RUSAGE_SELF)
        report["max_rss_bytes"] = usage.ru_maxrss * 1024
        report["finished_perf"] = time.perf_counter()
        if recorder is not None:
            report["events"] = recorder.events
            recorder.raw.close()
        if report["errors"]:
            done_barrier.abort()
        try:
            # No result serialization/IPC until all processes finish timed traffic.
            done_barrier.wait()
        except threading.BrokenBarrierError:
            report["errors"].append("BrokenCompletionBarrier")
        results.put({"kind": "done", "report": report})


def mark_release(release):
    release.value = time.perf_counter()


def run_burst(region, arn, concurrency, out, process_count=8, factory=make_client,
              ready_timeout=120, finish_timeout=720):
    from functools import partial
    ctx = multiprocessing.get_context("spawn")
    release = ctx.Value("d", 0.0)
    barrier = ctx.Barrier(concurrency + 1, action=partial(mark_release, release), timeout=ready_timeout)
    done_barrier = ctx.Barrier(process_count, timeout=finish_timeout)
    channel = ctx.Queue()
    assignments = [[(i, shared.bench.new_session_id()) for i in range(worker, concurrency, process_count)]
                   for worker in range(process_count)]
    plan = {"region": region, "runtime_arn": arn, "concurrency": concurrency,
            "process_count": process_count, "start_method": "spawn", "started_iso": shared.bench.utc_iso(),
            "before_send_spread_target_ms": 100,
            "assignments": [{"worker": w, "indices_sessions": group, "pool_size": 2 * len(group)}
                            for w, group in enumerate(assignments)]}
    shared.original.save(out / "client_plan.json", plan)
    processes, reports, ready = [], [], set()
    failure = None
    try:
        for group in assignments:
            process = ctx.Process(target=worker_main, args=(region, arn, concurrency, group,
                                  2 * len(group), barrier, done_barrier, channel, factory))
            process.start()
            processes.append(process)
        deadline = time.monotonic() + ready_timeout
        while len(ready) < process_count:
            message = channel.get(timeout=max(.01, deadline - time.monotonic()))
            if message["kind"] != "ready":
                reports.append(message["report"])
                raise RuntimeError("Worker failed before release")
            ready.add(message["pid"])
            if time.monotonic() >= deadline:
                raise TimeoutError("Worker readiness deadline")
        barrier.wait(timeout=ready_timeout)
        deadline = time.monotonic() + finish_timeout
        while len(reports) < process_count:
            try:
                message = channel.get(timeout=min(1, max(.01, deadline - time.monotonic())))
            except queue.Empty:
                if time.monotonic() >= deadline or any(p.exitcode not in (None, 0) for p in processes):
                    raise TimeoutError("Worker failed or exceeded completion deadline")
                continue
            if message["kind"] == "done":
                reports.append(message["report"])
        for process in processes:
            process.join(timeout=5)
        if any(p.exitcode != 0 for p in processes) or any(r["errors"] for r in reports):
            raise RuntimeError("Worker failure; see client_result.json")
    except BaseException as exc:
        failure = type(exc).__name__
        raise
    finally:
        barrier.abort()
        done_barrier.abort()
        for process in processes:
            if process.is_alive():
                process.terminate()
            process.join(timeout=5)
            if process.is_alive():
                process.kill()
                process.join(timeout=5)
        result = {"plan": plan, "release_perf": release.value, "reports": reports,
                  "failure": failure, "exitcodes": [p.exitcode for p in processes],
                  "finished_iso": shared.bench.utc_iso()}
        shared.original.save(out / "client_result.json", result)
        channel.close()
    rows = sorted([row for report in reports for row in report["rows"]], key=lambda r: r["request_idx"])
    events = [event for report in reports for event in report["events"]]
    assert len(rows) == concurrency and len({r["session_id"] for r in rows}) == concurrency
    shared.original.save(out / "raw.json", {"requests": rows, "events": events})
    summary = shared.bench.summarize_cell("500mb", concurrency, rows)
    summary.update(warm_success=sum(r["warm_ms"] is not None for r in rows),
                   stop_success=sum(r["stopped"] for r in rows), stop_absent=sum(r["stop_absent"] for r in rows))
    shared.original.save(out / "summary.json", summary)
    print("MULTIPROCESS", json.dumps(summary), flush=True)
    return summary

