#!/usr/bin/env python3
"""Credential-injecting reverse proxy for GitHub ("sidecar proxy" pattern).

The agent, git and gh talk to THIS proxy instead of github.com; the proxy adds the
real GitHub credential on the way out. The token therefore never exists in the
agent process, its environment, git config or gh config.

Two deployment shapes, same code:

  A. localhost sidecar *process* inside the microVM container
     (AgentCore Runtime runs one container; start the proxy from the entrypoint
     alongside the agent).  --listen 127.0.0.1:8081
     Protects against accidental leakage (logs, env dumps, prompt injection that
     reads env); NOT a security boundary against malicious code in the same VM.

  B. out-of-sandbox proxy on EC2/ECS in the VPC (Runtime in VPC mode, SG only
     allows the runtime SG).  --listen 0.0.0.0:8081 --require-proxy-token
     The sandbox holds no GitHub credential at all, only a low-value proxy
     token (or nothing, if you trust the network path).

Routing (GHES-style, so `GH_HOST=<proxy>` and `git ... insteadOf` both work):
  /api/graphql        -> {api_upstream}/graphql                 Authorization: Bearer <token>
  /api/v3/<rest>      -> {api_upstream}/<rest>                  Authorization: Bearer <token>
  /<owner>/<repo>...  -> {git_upstream}/<owner>/<repo>...       Authorization: Basic x-access-token:<token>

Client side:
  git config --global url."http://127.0.0.1:8081/".insteadOf "https://github.com/"
  git clone https://github.com/org/repo            # transparently proxied, no credential prompt
  # shape B caller check: git -c http.extraHeader="X-Proxy-Token: $PROXY_TOKEN" clone ...

Token source: resolve_token() from git_askpass.py (broker / AgentCore Identity),
cached in memory until close to expiry. stdlib only.

Usage:
  python3 credential_proxy.py --listen 127.0.0.1:8081 [--api-upstream https://api.github.com]
          [--git-upstream https://github.com] [--require-proxy-token] [--token-ttl 3000]
"""

from __future__ import annotations

import argparse
import base64
import http.client
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable
from urllib.parse import urlsplit

HOP_BY_HOP = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te",
              "trailers", "transfer-encoding", "upgrade", "host", "content-length"}
STRIP_FROM_CLIENT = HOP_BY_HOP | {"authorization", "x-proxy-token"}


def route(path: str, api_upstream: str, git_upstream: str) -> tuple[str, str, str]:
    """Return (upstream_base, upstream_path, auth_style) for an incoming path. auth_style: bearer|basic."""
    if path == "/api/graphql" or path.startswith("/api/graphql?"):
        return api_upstream, path[len("/api"):], "bearer"
    if path.startswith("/api/v3/") or path == "/api/v3":
        rest = path[len("/api/v3"):] or "/"
        return api_upstream, rest, "bearer"
    return git_upstream, path, "basic"


def auth_header(style: str, token: str) -> str:
    if style == "bearer":
        return f"Bearer {token}"
    return "Basic " + base64.b64encode(f"x-access-token:{token}".encode()).decode()


class TokenCache:
    """Fetch a short-lived token lazily and reuse it until ttl expires (memory only)."""

    def __init__(self, fetch: Callable[[], str], ttl_seconds: int = 3000, clock: Callable[[], float] = time.monotonic):
        self._fetch, self._ttl, self._clock = fetch, ttl_seconds, clock
        self._token: str | None = None
        self._expires = 0.0
        self._lock = threading.Lock()

    def get(self) -> str:
        with self._lock:
            if self._token is None or self._clock() >= self._expires:
                self._token = self._fetch()
                self._expires = self._clock() + self._ttl
            return self._token

    def invalidate(self) -> None:
        with self._lock:
            self._token = None


def make_handler(api_upstream: str, git_upstream: str, tokens: TokenCache, proxy_token: str | None):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "credential-proxy/0.1"

        def log_message(self, fmt, *args):  # never log headers/bodies; the request line only
            sys.stderr.write("%s %s\n" % (self.address_string(), fmt % args))

        def _read_body(self) -> bytes:
            if self.headers.get("Transfer-Encoding", "").lower() == "chunked":
                chunks = []
                while True:
                    size = int(self.rfile.readline().split(b";")[0].strip() or b"0", 16)
                    if size == 0:
                        self.rfile.readline()  # trailing CRLF
                        return b"".join(chunks)
                    chunks.append(self.rfile.read(size))
                    self.rfile.readline()
            length = int(self.headers.get("Content-Length") or 0)
            return self.rfile.read(length) if length else b""

        def _forward(self) -> None:
            if proxy_token is not None and self.headers.get("X-Proxy-Token") != proxy_token:
                self._reply(401, b"missing or invalid X-Proxy-Token\n")
                return
            base, upstream_path, style = route(self.path, api_upstream, git_upstream)
            parts = urlsplit(base)
            conn_cls = http.client.HTTPSConnection if parts.scheme == "https" else http.client.HTTPConnection
            conn = conn_cls(parts.hostname, parts.port, timeout=60)
            headers = {k: v for k, v in self.headers.items() if k.lower() not in STRIP_FROM_CLIENT}
            headers["Host"] = parts.netloc
            headers["Authorization"] = auth_header(style, tokens.get())
            body = self._read_body()
            if body or self.command in ("POST", "PUT", "PATCH"):
                headers["Content-Length"] = str(len(body))
            try:
                conn.request(self.command, (parts.path.rstrip("/") + upstream_path) or "/", body=body or None, headers=headers)
                resp = conn.getresponse()
                payload = resp.read()
            except OSError as exc:
                self._reply(502, f"upstream error: {exc}\n".encode())
                return
            finally:
                conn.close()
            if resp.status == 401:
                tokens.invalidate()  # token expired/revoked: next request fetches a fresh one
            self.send_response(resp.status, resp.reason)
            for k, v in resp.getheaders():
                if k.lower() not in HOP_BY_HOP:
                    self.send_header(k, v)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def _reply(self, status: int, body: bytes) -> None:
            self.send_response(status)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = do_HEAD = _forward

    return Handler


def serve(listen: str, api_upstream: str, git_upstream: str, tokens: TokenCache,
          proxy_token: str | None = None) -> ThreadingHTTPServer:
    host, _, port = listen.rpartition(":")
    host = host or "127.0.0.1"
    if host not in ("127.0.0.1", "localhost", "::1") and proxy_token is None:
        # Shape B: reachable from other hosts -> insist on a caller check in addition to VPC/SG.
        raise ValueError("non-loopback listen requires --require-proxy-token (PROXY_TOKEN)")
    server = ThreadingHTTPServer((host, int(port)), make_handler(api_upstream, git_upstream, tokens, proxy_token))
    server.daemon_threads = True
    return server


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--listen", default="127.0.0.1:8081")
    p.add_argument("--api-upstream", default="https://api.github.com")
    p.add_argument("--git-upstream", default="https://github.com")
    p.add_argument("--token-ttl", type=int, default=3000, help="seconds to reuse a fetched token (GitHub App tokens live 3600s)")
    p.add_argument("--require-proxy-token", action="store_true",
                   help="require callers to send X-Proxy-Token equal to $PROXY_TOKEN (shape B, out-of-sandbox deployment)")
    a = p.parse_args()

    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from git_askpass import resolve_token  # broker / AgentCore Identity token sources

    proxy_token = os.environ["PROXY_TOKEN"] if a.require_proxy_token else None
    tokens = TokenCache(lambda: resolve_token(dict(os.environ)), ttl_seconds=a.token_ttl)
    server = serve(a.listen, a.api_upstream, a.git_upstream, tokens, proxy_token)
    sys.stderr.write(f"credential-proxy listening on {a.listen} -> api={a.api_upstream} git={a.git_upstream}\n")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
