#!/usr/bin/env python3
"""Userland network-egress audit inside the sandbox (layer L4' in docs/03).

Wraps ``socket.socket.connect``/``connect_ex`` so every outbound TCP/UDP
connection attempt made by the agent process is written as a JSON Lines record
(destination host/IP, port, outcome, current OTEL trace/span ids). An optional
allowlist turns the hook into a soft egress control.

This complements — it does not replace — VPC Flow Logs / Route 53 query logs /
Network Firewall: the process is root with full capabilities (see
results/fuse_probe.json), so anything in-process can be bypassed by malicious
code. Its value is *context*: which tool call triggered which connection.

Usage (once, at agent start-up):

    from network_audit import install_network_audit
    install_network_audit(allow_hosts={"api.github.com", "gitlab.example.com", "*.amazonaws.com"})

Set ``NETWORK_AUDIT_DISABLE=1`` to bypass in local development.
"""

from __future__ import annotations

import fnmatch
import json
import os
import socket
import sys
import threading
from datetime import datetime, timezone
from typing import Any, Callable, Iterable

_lock = threading.Lock()
_installed = False
_reverse_cache: dict[str, str] = {}


def _otel() -> dict[str, str]:
    try:
        from opentelemetry import trace

        ctx = trace.get_current_span().get_span_context()
        if ctx and ctx.is_valid:
            return {"trace_id": format(ctx.trace_id, "032x"), "span_id": format(ctx.span_id, "016x")}
    except Exception:  # noqa: BLE001
        pass
    return {}


def remember_resolution(hostname: str, addresses: Iterable[str]) -> None:
    """Record hostname -> IP so connect() records can show the name (called from getaddrinfo wrapper)."""
    with _lock:
        for address in addresses:
            _reverse_cache[address] = hostname


def host_allowed(host: str, allow_hosts: Iterable[str] | None) -> bool:
    """Glob match (``*.amazonaws.com``). ``None`` means audit-only, allow everything."""
    if allow_hosts is None:
        return True
    return any(fnmatch.fnmatchcase(host, pattern) for pattern in allow_hosts)


def build_connect_record(address: Any, *, allow_hosts: Iterable[str] | None, outcome: str,
                         now: datetime | None = None, otel: dict[str, str] | None = None) -> dict[str, Any]:
    """Pure function: one audit record for a connect() attempt."""
    if isinstance(address, tuple) and len(address) >= 2:
        ip, port = str(address[0]), int(address[1])
    else:  # AF_UNIX etc.
        ip, port = str(address), -1
    host = _reverse_cache.get(ip, ip)
    return {
        "ts": (now or datetime.now(timezone.utc)).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
        "event": "net_connect",
        "host": host,
        "ip": ip,
        "port": port,
        "decision": "allow" if host_allowed(host, allow_hosts) else "deny",
        "outcome": outcome,
        "pid": os.getpid(),
        **(otel or {}),
    }


def _stdout_sink(record: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(record, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def install_network_audit(allow_hosts: Iterable[str] | None = None,
                          sink: Callable[[dict[str, Any]], None] = _stdout_sink) -> bool:
    """Patch socket for the current process. Returns False if disabled or already installed."""
    global _installed
    if os.environ.get("NETWORK_AUDIT_DISABLE") == "1" or _installed:
        return False
    allow = frozenset(allow_hosts) if allow_hosts is not None else None

    original_getaddrinfo = socket.getaddrinfo

    def audited_getaddrinfo(host, *args, **kwargs):  # type: ignore[no-untyped-def]
        results = original_getaddrinfo(host, *args, **kwargs)
        if isinstance(host, str):
            remember_resolution(host, (entry[4][0] for entry in results if entry and entry[4]))
        return results

    original_connect = socket.socket.connect
    original_connect_ex = socket.socket.connect_ex

    def audited_connect(self, address):  # type: ignore[no-untyped-def]
        record = build_connect_record(address, allow_hosts=allow, outcome="attempt", otel=_otel())
        if record["decision"] == "deny":
            record["outcome"] = "blocked"
            sink(record)
            raise PermissionError(f"egress to {record['host']}:{record['port']} blocked by network audit policy")
        try:
            result = original_connect(self, address)
        except OSError as exc:
            record["outcome"] = f"error:{exc.errno}"
            sink(record)
            raise
        record["outcome"] = "connected"
        sink(record)
        return result

    def audited_connect_ex(self, address):  # type: ignore[no-untyped-def]
        record = build_connect_record(address, allow_hosts=allow, outcome="attempt", otel=_otel())
        if record["decision"] == "deny":
            record["outcome"] = "blocked"
            sink(record)
            return 13  # EACCES
        code = original_connect_ex(self, address)
        record["outcome"] = "connected" if code == 0 else f"error:{code}"
        sink(record)
        return code

    socket.getaddrinfo = audited_getaddrinfo  # type: ignore[assignment]
    socket.socket.connect = audited_connect  # type: ignore[assignment]
    socket.socket.connect_ex = audited_connect_ex  # type: ignore[assignment]
    _installed = True
    return True


if __name__ == "__main__":  # quick self-demo: python3 network_audit.py https://example.com
    import urllib.request

    install_network_audit(allow_hosts={"example.com", "*.amazonaws.com"})
    for url in sys.argv[1:] or ["https://example.com"]:
        try:
            with urllib.request.urlopen(url, timeout=10) as response:  # noqa: S310 - demo only
                print(json.dumps({"url": url, "status": response.status}))
        except Exception as exc:  # noqa: BLE001
            print(json.dumps({"url": url, "error": f"{type(exc).__name__}: {exc}"}))
