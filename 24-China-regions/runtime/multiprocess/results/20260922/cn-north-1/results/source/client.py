"""Spawn-isolated load client, adapted from 21-runtime-v2-beta/multiprocess_coldstart_client.py.

No AWS clients or sockets cross process boundaries. before-send is an SDK event,
not a wire timestamp. No disk writes or result IPC during measured traffic.
"""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from functools import partial
import json
import multiprocessing as mp
import os
from pathlib import Path
import queue
import resource
import threading
import time
import uuid

import boto3
from botocore.config import Config


def now():
    return datetime.now(timezone.utc).isoformat()


def save(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, default=str) + "\n")


def make_client(region, pool_size):
    session = boto3.Session(region_name=region)
    credentials = session.get_credentials()
    if credentials is None:
        raise RuntimeError("No instance role credentials")
    credentials.get_frozen_credentials()
    return session.client("bedrock-agentcore", config=Config(
        connect_timeout=15, read_timeout=120, max_pool_connections=pool_size,
        retries={"total_max_attempts": 1}))


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
        if getattr(self.local, "event", None) is not None:
            self.local.event["before_send_perf"].append(time.perf_counter())

    def response_received(self, **kwargs):
        if getattr(self.local, "event", None) is not None:
            self.local.event["response_received_perf"].append(time.perf_counter())

    def call(self, arn, sid, phase, nonce, hold=0):
        row = {"phase": phase, "runtime_arn": arn, "session_id": sid, "nonce": nonce,
               "pid": os.getpid(), "started_iso": now(), "before_send_perf": [],
               "response_received_perf": [], "valid": False}
        self.local.event = row
        start = time.perf_counter()
        row.update(started_perf=start, started_unix=time.time())
        try:
            if phase == "stop":
                response = self.raw.stop_runtime_session(agentRuntimeArn=arn,
                    runtimeSessionId=sid, qualifier="DEFAULT")
                row["completed_perf"] = time.perf_counter()
            else:
                response = self.raw.invoke_agent_runtime(agentRuntimeArn=arn,
                    runtimeSessionId=sid, qualifier="DEFAULT", contentType="application/json",
                    accept="application/json",
                    payload=json.dumps({"nonce": nonce, "hold_seconds": hold}).encode())
                row["metadata"] = response["ResponseMetadata"]
                stream = response["response"]
                try:
                    payload = stream.read()
                    row["completed_perf"] = time.perf_counter()
                finally:
                    stream.close()
                body = row["body"] = json.loads(payload)
                if body.get("ok") is not True or body.get("echo") != nonce:
                    raise ValueError("Response contract or nonce mismatch")
                if body.get("request_index") != (1 if phase == "cold" else 2):
                    raise ValueError("Unexpected request index")
                if not body.get("instance_id"):
                    raise ValueError("Missing post-restore instance marker")
            row["metadata"] = response["ResponseMetadata"]
            row["status"] = response.get("statusCode") or response["ResponseMetadata"]["HTTPStatusCode"]
            if row["status"] != 200:
                raise ValueError("Unexpected status")
            if row["metadata"].get("RetryAttempts", 0) != 0:
                raise ValueError("Unexpected SDK retry")
            row["valid"] = True
        except Exception as exc:
            row.setdefault("completed_perf", time.perf_counter())
            row.update(error_type=type(exc).__name__, error=str(exc))
            if isinstance(getattr(exc, "response", None), dict):
                row["metadata"] = exc.response.get("ResponseMetadata", {})
                row["error_code"] = exc.response.get("Error", {}).get("Code")
                row["status"] = row["metadata"].get("HTTPStatusCode")
            if phase == "stop" and row.get("error_code") == "ResourceNotFoundException":
                row["absent"] = True
        finally:
            row.update(elapsed_ms=(row["completed_perf"] - start) * 1000, finished_iso=now())
            self.local.event = None
            with self.lock:
                self.events.append(row)
        return row


def probe(recorder, arn, index, sid, hold, launch, after_cold, after_warm):
    nonce = uuid.uuid4().hex
    row = {"index": index, "session_id": sid, "pid": os.getpid()}
    launch.wait()
    cold = recorder.call(arn, sid, "cold", nonce, hold)
    row["cold_ok"] = cold["valid"]
    after_cold.wait()
    if cold["valid"]:
        warm = recorder.call(arn, sid, "warm", nonce)
        row["warm_ok"] = warm["valid"]
        row["same_instance"] = warm.get("body", {}).get("instance_id") == cold["body"]["instance_id"]
    else:
        row.update(warm_ok=False, same_instance=False)
    after_warm.wait()
    stop = recorder.call(arn, sid, "stop", nonce)
    row["stop_confirmed"] = bool(stop["valid"] or stop.get("absent"))
    return row


def worker(region, arn, assignments, hold, launch, after_cold, after_warm, done, channel, factory):
    report = {"pid": os.getpid(), "assignments": assignments, "rows": [], "events": [], "errors": []}
    recorder = None
    begin = time.perf_counter()
    cpu = time.process_time()
    try:
        raw = factory(region, 2 * len(assignments))
        recorder = Recorder(raw)
        report["client"] = {"pool_size": raw.meta.config.max_pool_connections,
                            "retries": raw.meta.config.retries}
        report["ready_perf"] = time.perf_counter()
        cpu = time.process_time()
        begin = time.perf_counter()
        with ThreadPoolExecutor(max_workers=len(assignments)) as executor:
            futures = [executor.submit(probe, recorder, arn, i, sid, hold, launch, after_cold, after_warm)
                       for i, sid in assignments]
            channel.put({"kind": "ready", "pid": os.getpid()})
            for future in futures:
                report["rows"].append(future.result())
    except BaseException as exc:
        report["errors"].append(f"{type(exc).__name__}: {exc}")
        for gate in (launch, after_cold, after_warm, done):
            gate.abort()
    finally:
        report.update(cpu_seconds=time.process_time() - cpu, wall_seconds=time.perf_counter() - begin,
            max_rss_bytes=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024)
        if recorder:
            report["events"] = recorder.events
            recorder.raw.close()
        try:
            done.wait()
        except threading.BrokenBarrierError:
            report["errors"].append("BrokenCompletionBarrier")
        channel.put({"kind": "done", "report": report})


def release_time(shared):
    shared.value = time.perf_counter()


def sample_cpu(stop, samples):
    while not stop.is_set():
        lines = Path("/proc/stat").read_text().splitlines()
        samples.append({"perf": time.perf_counter(), "unix": time.time(),
                        "cpu": [int(v) for v in lines[0].split()[1:]],
                        "loadavg": Path("/proc/loadavg").read_text().strip()})
        stop.wait(.1)


def run_burst(region, arn, concurrency, out, hold=0, process_count=8, factory=make_client,
              ready_timeout=120, finish_timeout=420):
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    process_count = min(process_count, concurrency)
    ctx = mp.get_context("spawn")
    release = ctx.Value("d", 0)
    launch = ctx.Barrier(concurrency + 1, action=partial(release_time, release), timeout=ready_timeout)
    after_cold = ctx.Barrier(concurrency, timeout=finish_timeout)
    after_warm = ctx.Barrier(concurrency, timeout=finish_timeout)
    done = ctx.Barrier(process_count, timeout=finish_timeout)
    channel = ctx.Queue()
    assignments = [[(i, str(uuid.uuid4())) for i in range(p, concurrency, process_count)]
                   for p in range(process_count)]
    plan = {"region": region, "runtime_arn": arn, "concurrency": concurrency,
        "process_count": process_count, "hold_seconds": hold, "start_method": "spawn",
        "assignments": assignments, "before_send_spread_target_ms": 100, "created_at": now()}
    save(out / "plan.json", plan)
    processes, reports, ready, cpu_samples = [], [], set(), []
    stop_sampling = threading.Event()
    sampler = threading.Thread(target=sample_cpu, args=(stop_sampling, cpu_samples), daemon=True)
    failure = None

    def receive(deadline):
        while time.monotonic() < deadline:
            try:
                return channel.get(timeout=.25)
            except queue.Empty:
                if any(p.exitcode not in (None, 0) for p in processes):
                    raise RuntimeError("Worker exited unexpectedly")
        raise TimeoutError("Worker deadline")

    try:
        sampler.start()
        for group in assignments:
            process = ctx.Process(target=worker, args=(region, arn, group, hold,
                launch, after_cold, after_warm, done, channel, factory))
            process.start()
            processes.append(process)
        deadline = time.monotonic() + ready_timeout
        while len(ready) < process_count:
            message = receive(deadline)
            if message["kind"] != "ready":
                reports.append(message["report"])
                raise RuntimeError("Worker failed before launch")
            ready.add(message["pid"])
        launch.wait()
        deadline = time.monotonic() + finish_timeout
        while len(reports) < process_count:
            message = receive(deadline)
            if message["kind"] == "done":
                reports.append(message["report"])
        for process in processes:
            process.join(timeout=5)
        if any(p.exitcode != 0 for p in processes) or any(r["errors"] for r in reports):
            raise RuntimeError("Worker errors")
    except BaseException as exc:
        failure = f"{type(exc).__name__}: {exc}"
    finally:
        for gate in (launch, after_cold, after_warm, done):
            gate.abort()
        for process in processes:
            if process.is_alive():
                process.terminate()
            process.join(timeout=5)
            if process.is_alive():
                process.kill()
                process.join(timeout=5)
        stop_sampling.set()
        sampler.join(timeout=2)
        result = {"plan": plan, "release_perf": release.value, "reports": reports, "failure": failure,
            "exitcodes": [p.exitcode for p in processes], "cpu_samples": cpu_samples, "finished_at": now()}
        save(out / "raw.json", result)
        channel.close()
    # Recover only after workers are gone; these calls never enter latency statistics.
    confirmed = {r["session_id"] for report in reports for r in report["rows"] if r["stop_confirmed"]}
    unresolved = [sid for group in assignments for _, sid in group if sid not in confirmed]
    if release.value and unresolved:
        recorder = Recorder(factory(region, 2))
        try:
            for sid in unresolved:
                recorder.call(arn, sid, "stop", "cleanup")
            save(out / "recovery-stops.json", recorder.events)
        finally:
            recorder.raw.close()
    return result
