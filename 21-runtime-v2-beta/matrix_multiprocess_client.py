"""Image-aware matrix adapter for the recorded spawn client (no SDK changes).

The existing client/recorder remains frozen for evidence hashes. This adapter
passes an explicit image label through worker rows and the case summary.
"""
from functools import partial
import multiprocessing
import queue
import time

import multiprocess_coldstart_client as base

class LabelledQueue:
    def __init__(self, channel, size):
        self.channel, self.size = channel, size

    def put(self, message):
        if message["kind"] == "done":
            for row in message["report"]["rows"]:
                row["size"] = self.size
        self.channel.put(message)


def worker_main(size, region, arn, concurrency, assignments, pool_size, barrier,
                done_barrier, channel, factory):
    # The underlying workload is identical ping-pong for every image. Only the
    # explicit metadata label differs; timings/payloads/errors are untouched.
    base.worker_main(region, arn, concurrency, assignments, pool_size, barrier,
                     done_barrier, LabelledQueue(channel, size), factory)


def run_burst(size, region, arn, concurrency, out, factory=base.make_client,
              ready_timeout=120, finish_timeout=720):
    assert size in ("500mb", "1gb", "2gb") and concurrency in (1, 10, 50, 100, 200)
    count = min(8, concurrency)
    ctx = multiprocessing.get_context("spawn")
    release = ctx.Value("d", 0.0)
    barrier = ctx.Barrier(concurrency + 1, action=partial(base.mark_release, release), timeout=ready_timeout)
    done_barrier = ctx.Barrier(count, timeout=finish_timeout)
    channel = ctx.Queue()
    groups = [[(i, base.shared.bench.new_session_id()) for i in range(w, concurrency, count)] for w in range(count)]
    plan = {"size": size, "region": region, "runtime_arn": arn, "concurrency": concurrency,
            "process_count": count, "start_method": "spawn", "started_iso": base.shared.bench.utc_iso(),
            "before_send_spread_target_ms": 100,
            "assignments": [{"worker": w, "indices_sessions": group, "pool_size": 2 * len(group)}
                            for w, group in enumerate(groups)]}
    save = base.shared.original.save
    save(out / "client_plan.json", plan)
    processes, reports, ready = [], [], set()
    failure = None
    try:
        for group in groups:
            p = ctx.Process(target=worker_main, args=(size, region, arn, concurrency, group,
                            2 * len(group), barrier, done_barrier, channel, factory))
            p.start()
            processes.append(p)
        deadline = time.monotonic() + ready_timeout
        while len(ready) < count:
            message = channel.get(timeout=max(.01, deadline - time.monotonic()))
            if message["kind"] != "ready":
                reports.append(message["report"])
                raise RuntimeError("Worker failed before release")
            ready.add(message["pid"])
            if time.monotonic() >= deadline:
                raise TimeoutError("Worker readiness deadline")
        barrier.wait(timeout=ready_timeout)
        deadline = time.monotonic() + finish_timeout
        while len(reports) < count:
            try:
                message = channel.get(timeout=min(1, max(.01, deadline - time.monotonic())))
            except queue.Empty:
                if time.monotonic() >= deadline or any(p.exitcode not in (None, 0) for p in processes):
                    raise TimeoutError("Worker failure or completion deadline")
                continue
            if message["kind"] == "done":
                reports.append(message["report"])
        for p in processes:
            p.join(timeout=5)
        if any(p.exitcode != 0 for p in processes) or any(r["errors"] for r in reports):
            raise RuntimeError("Worker failure")
    except BaseException as exc:
        failure = type(exc).__name__
        raise
    finally:
        barrier.abort()
        done_barrier.abort()
        for p in processes:
            if p.is_alive():
                p.terminate()
            p.join(timeout=5)
            if p.is_alive():
                p.kill()
                p.join(timeout=5)
        save(out / "client_result.json", {"plan": plan, "release_perf": release.value,
             "reports": reports, "failure": failure, "exitcodes": [p.exitcode for p in processes],
             "finished_iso": base.shared.bench.utc_iso()})
        channel.close()
    rows = sorted([r for report in reports for r in report["rows"]], key=lambda r: r["request_idx"])
    assert len(rows) == concurrency and len({r["session_id"] for r in rows}) == concurrency
    events = [e for report in reports for e in report["events"]]
    save(out / "raw.json", {"requests": rows, "events": events})
    summary = base.shared.bench.summarize_cell(size, concurrency, rows)
    summary.update(warm_success=sum(r["warm_ms"] is not None for r in rows),
                   stop_success=sum(r["stopped"] for r in rows), stop_absent=sum(r["stop_absent"] for r in rows))
    save(out / "summary.json", summary)
    print("MATRIX CELL", size, concurrency, summary, flush=True)
    return summary

