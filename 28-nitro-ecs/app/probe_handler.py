"""Private VPC HTTP probe. No AWS credentials or SDK requests in the handler."""
import ipaddress
import json
import os
import time
import urllib.error
import urllib.request

HTTP = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def handler(event, context):
    if event.get("op") == "noop":
        return {"ok": True, "purpose": "warm probe, not task startup"}
    address = ipaddress.ip_address(event["ip"])
    if address.version != 4 or address not in ipaddress.ip_network(os.environ["ALLOWED_CIDR"]):
        raise ValueError("target is outside the demo VPC")
    route = event.get("route", "/health")
    if route not in ("/health", "/invoke", "/fs", "/metadata", "/storage"):
        raise ValueError("unknown route")
    payload = event.get("body")
    data = None if payload is None else json.dumps(payload).encode()
    request = urllib.request.Request(f"http://{address}:8080{route}", data=data,
        headers={"content-type": "application/json"})
    start = time.perf_counter()
    # GET readiness retries are bounded; a POST is NEVER automatically replayed.
    deadline = start + min(45, max(1, context.get_remaining_time_in_millis() / 1000 - 5))
    while True:
        try:
            with HTTP.open(request, timeout=140 if data else 2) as response:
                content = response.read(65537)
            if len(content) > 65536:
                raise ValueError("task response too large")
            return {"ok": True, "probe_http_ms": (time.perf_counter() - start) * 1000,
                    "response": json.loads(content)}
        except OSError:
            if data is not None or route != "/health" or time.perf_counter() >= deadline:
                raise
            time.sleep(.2)
