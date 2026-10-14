"""Turn one invocation record (client timestamps + server probes) into stage latencies.

Pure functions only, so test_stages.py can check them offline.

Client times come from the benchmark host, server probes from the microVM. Both
sync to the Amazon Time Sync Service; ``clock_order_ok`` checks the one
ordering that must hold regardless of network delay:
client send <= server receive <= client receives the ``accepted`` event.
Stages marked ``*`` in STAGES subtract a client time from a server time.
"""

from __future__ import annotations

import statistics

# A process that started within this margin before the client sent the request
# is counted as booted by that request (proc start has 10 ms tick resolution).
BOOT_MARGIN_NS = 50_000_000

# (key, group, label) in report order. Values are milliseconds. On Runtime V2 a
# session is restored from a snapshot taken while the runtime was being created,
# so the [build] stages happened once, in that build microVM, not per session.
STAGES = [
    ("send_to_kernel_boot", "runtime", "client send → microVM kernel boot *"),
    ("kernel_to_proc", "runtime", "[build] kernel boot → Python process start"),
    ("proc_to_module", "runtime", "[build] process start → main.py first line"),
    ("sdk_import", "runtime", "[build] import claude_agent_sdk"),
    ("imports", "runtime", "[build] all imports (stdlib + SDK + FastAPI)"),
    ("proc_to_listening", "runtime", "[build] process start → HTTP listening"),
    ("listening_to_first_ping", "runtime", "[build] listening → first /ping"),
    ("send_to_listening", "runtime", "client send → HTTP listening *"),
    ("container_age_at_request", "runtime", "process age when request arrived"),
    ("send_to_received", "request", "client send → /invocations received *"),
    ("client_ttfb", "request", "client send → first SSE byte (accepted)"),
    ("agent_connect", "agent", "Claude CLI start: ClaudeSDKClient.connect()"),
    ("agent_wait", "agent", "request waited for the agent"),
    ("query_to_cli_init", "reply", "query sent → CLI init message"),
    ("query_to_message_start", "reply", "query sent → model message_start"),
    ("server_ttft", "reply", "query sent → first text token"),
    ("first_token_to_result", "reply", "first token → ResultMessage"),
    ("cli_duration", "reply", "CLI-reported duration_ms"),
    ("server_total", "reply", "request received → done event sent"),
    ("client_ttft", "client", "client send → first text token"),
    ("client_total", "client", "client send → stream end"),
]


# Non-latency evidence about how the microVM got to the request, same table format.
INDICATORS = [
    ("frozen_gap", "VM not running between boot and request (ms) †"),
    ("cache_drop_mb", "page cache at boot − at request (MB)"),
    ("agent_connect_read_mb", "page cache growth during CLI start (MB)"),
    ("agent_connect_majfault", "major page faults during CLI start"),
]


def _ms(end: int | None, start: int | None) -> float | None:
    if end is None or start is None:
        return None
    return (end - start) / 1e6


def _diff(a: int | None, b: int | None) -> int | None:
    return None if a is None or b is None else a - b


def _kb_to_mb(a: int | None, b: int | None) -> float | None:
    d = _diff(a, b)
    return None if d is None else d / 1024


def compute(rec: dict) -> dict:
    """Stage latencies (ms) and flags for one invocation record from bench.py."""
    c = rec["client"]
    done = rec.get("done") or {}
    boot = done.get("boot") or {}
    p = done.get("probes") or {}
    agent = done.get("agent") or {}
    server = done.get("server") or {}
    result = done.get("result") or {}
    send = c["send_ns"]

    booted_by_request = (
        boot.get("proc_start_ns") is not None and boot["proc_start_ns"] >= send - BOOT_MARGIN_NS
    )
    # Agent startup belongs to this request when it was triggered by a request
    # that arrived at or after the current one was received (i.e. this one).
    agent_in_request = (
        agent.get("connect_start_ns") is not None
        and p.get("request_received_ns") is not None
        and agent["connect_start_ns"] >= p["request_received_ns"]
    )
    s = {
        "kernel_to_proc": _ms(boot.get("proc_start_ns"), boot.get("kernel_boot_ns")),
        "proc_to_module": _ms(boot.get("module_ns"), boot.get("proc_start_ns")),
        "sdk_import": _ms(boot.get("sdk_import_ns"), boot.get("stdlib_ns")),
        "imports": _ms(boot.get("imports_ns"), boot.get("module_ns")),
        "proc_to_listening": _ms(server.get("listening_ns"), boot.get("proc_start_ns")),
        "listening_to_first_ping": _ms(server.get("first_ping_ns"), server.get("listening_ns")),
        "container_age_at_request": _ms(p.get("request_received_ns"), boot.get("proc_start_ns")),
        "send_to_received": _ms(p.get("request_received_ns"), send),
        "client_ttfb": _ms(c.get("first_byte_ns"), send),
        "agent_connect": _ms(agent.get("connect_done_ns"), agent.get("connect_start_ns")),
        "agent_wait": _ms(p.get("agent_ready_ns"), p.get("turn_lock_ns")),
        "query_to_cli_init": _ms(p.get("cli_init_msg_ns"), p.get("query_sent_ns")),
        "query_to_message_start": _ms(p.get("message_start_ns"), p.get("query_sent_ns")),
        "server_ttft": _ms(p.get("first_token_ns"), p.get("query_sent_ns")),
        "first_token_to_result": _ms(p.get("result_ns"), p.get("first_token_ns")),
        "cli_duration": result.get("duration_ms"),
        "server_total": _ms(p.get("done_sent_ns"), p.get("request_received_ns")),
        "client_ttft": _ms(c.get("first_token_ns"), send),
        "client_total": _ms(c.get("done_ns"), send),
    }
    if booted_by_request:
        s["send_to_kernel_boot"] = _ms(boot.get("kernel_boot_ns"), send)
        s["send_to_listening"] = _ms(server.get("listening_ns"), send)
    else:
        s["send_to_kernel_boot"] = s["send_to_listening"] = None
    if not agent_in_request:
        # The agent was already running (eager boot start or an earlier turn);
        # its connect time did not happen inside this request.
        s["agent_connect_outside_request"] = s.pop("agent_connect")
        s["agent_connect"] = None
    boot_vm, req_vm = boot.get("vm") or {}, p.get("vm") or {}
    before, after = agent.get("vm_before") or {}, agent.get("vm_after") or {}
    ind = {
        # wall-minus-monotonic only grows while the guest is paused (e.g. snapshotted)
        "frozen_gap": _ms(p.get("wall_minus_mono_ns"), boot.get("wall_minus_mono_ns")),
        "cache_drop_mb": _kb_to_mb(boot_vm.get("cached_kb"), req_vm.get("cached_kb")),
        "agent_connect_read_mb": _kb_to_mb(after.get("cached_kb"), before.get("cached_kb")),
        "agent_connect_majfault": _diff(after.get("pgmajfault"), before.get("pgmajfault")),
    }
    if not agent_in_request:
        ind["agent_connect_read_mb"] = ind["agent_connect_majfault"] = None
    s.update(ind)
    received = p.get("request_received_ns")
    flags = {
        "ok": rec.get("error") is None and done.get("error") is None and bool(done),
        "booted_by_request": booted_by_request,
        "agent_in_request": agent_in_request,
        "clock_order_ok": received is not None and c.get("first_byte_ns") is not None
        and send <= received <= c["first_byte_ns"],
    }
    return {"stages": s, "flags": flags}


def summarize(values: list[float]) -> dict | None:
    vals = sorted(v for v in values if v is not None)
    if not vals:
        return None
    def pct(q: float) -> float:
        if len(vals) == 1:
            return vals[0]
        k = (len(vals) - 1) * q
        lo = int(k)
        hi = min(lo + 1, len(vals) - 1)
        return vals[lo] + (vals[hi] - vals[lo]) * (k - lo)
    return {"n": len(vals), "p50": round(pct(0.5), 1), "p90": round(pct(0.9), 1),
            "min": round(vals[0], 1), "max": round(vals[-1], 1),
            "mean": round(statistics.fmean(vals), 1)}
