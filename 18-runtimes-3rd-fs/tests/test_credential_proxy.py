import base64
import json
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

import credential_proxy as cp


class _Upstream(BaseHTTPRequestHandler):
    """Fake GitHub: echoes method, path, and selected headers as JSON."""

    def log_message(self, *a):
        pass

    def _echo(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        if self.path == "/expired":
            self.send_response(401); self.send_header("Content-Length", "0"); self.end_headers(); return
        payload = json.dumps({
            "method": self.command, "path": self.path, "host": self.headers.get("Host"),
            "authorization": self.headers.get("Authorization"), "x_proxy_token": self.headers.get("X-Proxy-Token"),
            "body": body.decode(),
        }).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Transfer-Encoding", "identity")  # must not be copied through
        self.end_headers()
        self.wfile.write(payload)

    do_GET = do_POST = _echo


@pytest.fixture
def upstream():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Upstream)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()


def _start_proxy(upstream, fetch, proxy_token=None, clock=None):
    tokens = cp.TokenCache(fetch, ttl_seconds=100, clock=clock or (lambda: 0.0))
    server = cp.serve("127.0.0.1:0", upstream, upstream, tokens, proxy_token)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_port}", tokens


def _get(url, headers=None, data=None):
    req = urllib.request.Request(url, headers=headers or {}, data=data, method="POST" if data else "GET")
    with urllib.request.urlopen(req, timeout=10) as r:
        return r.status, json.loads(r.read() or b"{}")


def test_route_table():
    assert cp.route("/api/v3/user", "https://api", "https://git") == ("https://api", "/user", "bearer")
    assert cp.route("/api/graphql", "https://api", "https://git") == ("https://api", "/graphql", "bearer")
    assert cp.route("/org/repo.git/info/refs?service=git-upload-pack", "https://api", "https://git") == (
        "https://git", "/org/repo.git/info/refs?service=git-upload-pack", "basic")


def test_proxy_injects_bearer_for_api_and_basic_for_git(upstream):
    calls = []
    server, base, _ = _start_proxy(upstream, lambda: calls.append(1) or "ghs_real")
    try:
        status, echo = _get(f"{base}/api/v3/user", headers={"Authorization": "Bearer attacker", "X-Proxy-Token": "x"})
        assert status == 200 and echo["path"] == "/user"
        assert echo["authorization"] == "Bearer ghs_real"          # client-supplied Authorization replaced
        assert echo["x_proxy_token"] is None                       # proxy header stripped before upstream
        assert echo["host"].startswith("127.0.0.1")

        status, echo = _get(f"{base}/org/repo.git/info/refs?service=git-upload-pack")
        expected = "Basic " + base64.b64encode(b"x-access-token:ghs_real").decode()
        assert echo["authorization"] == expected and echo["path"].startswith("/org/repo.git/info/refs")

        status, echo = _get(f"{base}/api/v3/repos/o/r/issues", data=b'{"title":"x"}')
        assert echo["method"] == "POST" and echo["body"] == '{"title":"x"}'
        assert len(calls) == 1                                     # token fetched once, then cached
    finally:
        server.shutdown()


def test_proxy_requires_proxy_token_when_configured(upstream):
    server, base, _ = _start_proxy(upstream, lambda: "ghs_real", proxy_token="session-secret")
    try:
        with pytest.raises(urllib.error.HTTPError) as exc:
            _get(f"{base}/api/v3/user")
        assert exc.value.code == 401
        status, echo = _get(f"{base}/api/v3/user", headers={"X-Proxy-Token": "session-secret"})
        assert status == 200 and echo["authorization"] == "Bearer ghs_real"
    finally:
        server.shutdown()


def test_non_loopback_listen_requires_proxy_token():
    with pytest.raises(ValueError, match="require-proxy-token"):
        cp.serve("0.0.0.0:0", "http://x", "http://x", cp.TokenCache(lambda: "t"))
    server = cp.serve("0.0.0.0:0", "http://x", "http://x", cp.TokenCache(lambda: "t"), proxy_token="s")
    server.server_close()


def test_proxy_refetches_token_after_upstream_401(upstream):
    tokens = iter(["ghs_old", "ghs_new"])
    server, base, cache = _start_proxy(upstream, lambda: next(tokens))
    try:
        with pytest.raises(urllib.error.HTTPError) as exc:
            _get(f"{base}/api/v3/expired")
        assert exc.value.code == 401
        _, echo = _get(f"{base}/api/v3/user")
        assert echo["authorization"] == "Bearer ghs_new"
    finally:
        server.shutdown()


def test_token_cache_ttl():
    now = [0.0]
    fetches = []
    cache = cp.TokenCache(lambda: fetches.append(1) or f"t{len(fetches)}", ttl_seconds=10, clock=lambda: now[0])
    assert cache.get() == "t1" and cache.get() == "t1"
    now[0] = 11
    assert cache.get() == "t2"
