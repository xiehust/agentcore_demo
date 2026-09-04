#!/usr/bin/env python3
"""Runnable RFC 8693 OBO token-exchange demo for an AgentCore MCP target.

This is intentionally dependency-free and local-only. It models:

    user JWT (aud=agentcore-gateway)
      -> Gateway/Identity token exchange
      -> delegated JWT (aud=mcp-server)
      -> protected MCP tools/call

Do not use the demo HS256 secret or token-issuing endpoint in production.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from contextlib import AbstractContextManager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

TOKEN_EXCHANGE_GRANT = "urn:ietf:params:oauth:grant-type:token-exchange"
JWT_TOKEN_TYPE = "urn:ietf:params:oauth:token-type:jwt"
ACCESS_TOKEN_TYPE = "urn:ietf:params:oauth:token-type:access_token"


class TokenError(ValueError):
    """Raised when a demo JWT is malformed or fails validation."""


def _b64url_encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _b64url_decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def issue_token(
    secret: str,
    *,
    issuer: str,
    subject: str,
    audience: str,
    scopes: list[str],
    expires_in: int = 300,
    extra_claims: dict[str, Any] | None = None,
) -> str:
    """Issue a local HS256 JWT. Production IdPs should use asymmetric keys."""
    now = int(time.time())
    header = {"alg": "HS256", "typ": "JWT"}
    payload: dict[str, Any] = {
        "iss": issuer,
        "sub": subject,
        "aud": audience,
        "scope": " ".join(scopes),
        "iat": now,
        "exp": now + expires_in,
    }
    if extra_claims:
        payload.update(extra_claims)
    signing_input = ".".join(
        [
            _b64url_encode(json.dumps(header, separators=(",", ":")).encode()),
            _b64url_encode(json.dumps(payload, separators=(",", ":")).encode()),
        ]
    )
    signature = hmac.new(secret.encode(), signing_input.encode(), hashlib.sha256).digest()
    return f"{signing_input}.{_b64url_encode(signature)}"


def verify_token(
    token: str,
    secret: str,
    *,
    expected_issuer: str,
    expected_audience: str,
) -> dict[str, Any]:
    """Verify signature, issuer, audience, and expiration of a local JWT."""
    try:
        encoded_header, encoded_payload, encoded_signature = token.split(".")
        header = json.loads(_b64url_decode(encoded_header))
        claims = json.loads(_b64url_decode(encoded_payload))
    except (ValueError, json.JSONDecodeError) as exc:
        raise TokenError("malformed JWT") from exc

    if header.get("alg") != "HS256":
        raise TokenError("unsupported JWT algorithm")
    signing_input = f"{encoded_header}.{encoded_payload}".encode()
    expected_signature = hmac.new(secret.encode(), signing_input, hashlib.sha256).digest()
    if not hmac.compare_digest(expected_signature, _b64url_decode(encoded_signature)):
        raise TokenError("invalid JWT signature")
    if claims.get("iss") != expected_issuer:
        raise TokenError("unexpected JWT issuer")
    audience = claims.get("aud")
    audiences = audience if isinstance(audience, list) else [audience]
    if expected_audience not in audiences:
        raise TokenError("unexpected JWT audience")
    if int(claims.get("exp", 0)) <= int(time.time()):
        raise TokenError("expired JWT")
    return claims


class JsonHandler(BaseHTTPRequestHandler):
    """Small JSON HTTP handler shared by the mock IdP and MCP server."""

    server_version = "AgentCoreTokenExchangeDemo/1.0"

    def log_message(self, _format: str, *_args: Any) -> None:
        return

    def send_json(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, separators=(",", ":")).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def read_form(self) -> dict[str, str]:
        length = int(self.headers.get("Content-Length", "0"))
        values = urllib.parse.parse_qs(self.rfile.read(length).decode())
        return {key: items[-1] for key, items in values.items()}

    def read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        return json.loads(self.rfile.read(length) or b"{}")


class DemoEnvironment(AbstractContextManager["DemoEnvironment"]):
    """Owns the mock authorization server and protected MCP server."""

    signing_secret = "local-demo-only-change-me"
    client_id = "agentcore-gateway-demo"
    client_secret = "local-client-secret"
    gateway_audience = "agentcore-gateway"
    mcp_audience = "mcp-server"

    def __init__(self, host: str = "127.0.0.1") -> None:
        self.host = host
        self.issuer = ""
        self.idp_server = ThreadingHTTPServer((host, 0), self._idp_handler())
        self.idp_url = f"http://{host}:{self.idp_server.server_address[1]}"
        self.issuer = self.idp_url
        self.mcp_server = ThreadingHTTPServer((host, 0), self._mcp_handler())
        self.mcp_url = f"http://{host}:{self.mcp_server.server_address[1]}/mcp"
        self._threads: list[threading.Thread] = []

    def _idp_handler(self) -> type[JsonHandler]:
        environment = self

        class IdpHandler(JsonHandler):
            def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
                parsed = urllib.parse.urlparse(self.path)
                if parsed.path == "/.well-known/openid-configuration":
                    self.send_json(
                        200,
                        {
                            "issuer": environment.issuer,
                            "token_endpoint": f"{environment.idp_url}/oauth2/token",
                            "grant_types_supported": [TOKEN_EXCHANGE_GRANT],
                            "token_endpoint_auth_methods_supported": ["client_secret_basic"],
                        },
                    )
                    return
                if parsed.path == "/demo/issue-user-token":
                    subject = urllib.parse.parse_qs(parsed.query).get("sub", ["alice"])[0]
                    token = issue_token(
                        environment.signing_secret,
                        issuer=environment.issuer,
                        subject=subject,
                        audience=environment.gateway_audience,
                        scopes=["gateway:invoke"],
                    )
                    self.send_json(200, {"access_token": token, "token_type": "Bearer"})
                    return
                self.send_json(404, {"error": "not_found"})

            def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
                if self.path != "/oauth2/token":
                    self.send_json(404, {"error": "not_found"})
                    return
                if not environment._valid_client_auth(self.headers.get("Authorization", "")):
                    self.send_json(401, {"error": "invalid_client"})
                    return
                form = self.read_form()
                if form.get("grant_type") != TOKEN_EXCHANGE_GRANT:
                    self.send_json(400, {"error": "unsupported_grant_type"})
                    return
                if form.get("subject_token_type") not in {JWT_TOKEN_TYPE, ACCESS_TOKEN_TYPE}:
                    self.send_json(400, {"error": "invalid_request", "error_description": "bad subject_token_type"})
                    return
                if form.get("audience") != environment.mcp_audience:
                    self.send_json(400, {"error": "invalid_target"})
                    return
                try:
                    source_claims = verify_token(
                        form.get("subject_token", ""),
                        environment.signing_secret,
                        expected_issuer=environment.issuer,
                        expected_audience=environment.gateway_audience,
                    )
                except TokenError as exc:
                    self.send_json(400, {"error": "invalid_grant", "error_description": str(exc)})
                    return
                requested_scopes = form.get("scope", "mcp:invoke").split()
                if "mcp:invoke" not in requested_scopes:
                    self.send_json(400, {"error": "invalid_scope"})
                    return
                delegated_token = issue_token(
                    environment.signing_secret,
                    issuer=environment.issuer,
                    subject=source_claims["sub"],
                    audience=environment.mcp_audience,
                    scopes=requested_scopes,
                    extra_claims={"act": {"sub": environment.client_id}},
                )
                self.send_json(
                    200,
                    {
                        "access_token": delegated_token,
                        "issued_token_type": ACCESS_TOKEN_TYPE,
                        "token_type": "Bearer",
                        "expires_in": 300,
                        "scope": " ".join(requested_scopes),
                    },
                )

        return IdpHandler

    def _mcp_handler(self) -> type[JsonHandler]:
        environment = self

        class McpHandler(JsonHandler):
            def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
                if self.path != "/mcp":
                    self.send_json(404, {"error": "not_found"})
                    return
                authorization = self.headers.get("Authorization", "")
                if not authorization.startswith("Bearer "):
                    self.send_json(401, {"error": "missing_bearer_token"})
                    return
                try:
                    claims = verify_token(
                        authorization.removeprefix("Bearer "),
                        environment.signing_secret,
                        expected_issuer=environment.issuer,
                        expected_audience=environment.mcp_audience,
                    )
                except TokenError as exc:
                    self.send_json(401, {"error": "invalid_token", "error_description": str(exc)})
                    return
                if "mcp:invoke" not in claims.get("scope", "").split():
                    self.send_json(403, {"error": "insufficient_scope"})
                    return
                request = self.read_json()
                request_id = request.get("id")
                method = request.get("method")
                if method == "tools/list":
                    result = {
                        "tools": [
                            {
                                "name": "whoami",
                                "description": "Return the delegated user and calling workload",
                                "inputSchema": {"type": "object", "properties": {}},
                            }
                        ]
                    }
                elif method == "tools/call" and request.get("params", {}).get("name") == "whoami":
                    identity = {
                        "delegatedUser": claims["sub"],
                        "actor": claims.get("act", {}).get("sub"),
                        "audience": claims["aud"],
                        "scope": claims["scope"],
                    }
                    result = {
                        "content": [{"type": "text", "text": json.dumps(identity)}],
                        "structuredContent": identity,
                    }
                else:
                    self.send_json(
                        200,
                        {
                            "jsonrpc": "2.0",
                            "id": request_id,
                            "error": {"code": -32601, "message": "Method not found"},
                        },
                    )
                    return
                self.send_json(200, {"jsonrpc": "2.0", "id": request_id, "result": result})

        return McpHandler

    def _valid_client_auth(self, authorization: str) -> bool:
        if not authorization.startswith("Basic "):
            return False
        try:
            raw = base64.b64decode(authorization.removeprefix("Basic ")).decode()
            client_id, client_secret = raw.split(":", 1)
        except (ValueError, UnicodeDecodeError):
            return False
        return hmac.compare_digest(client_id, self.client_id) and hmac.compare_digest(
            client_secret, self.client_secret
        )

    def start(self) -> "DemoEnvironment":
        for server, name in ((self.idp_server, "demo-idp"), (self.mcp_server, "demo-mcp")):
            thread = threading.Thread(target=server.serve_forever, name=name, daemon=True)
            thread.start()
            self._threads.append(thread)
        return self

    def close(self) -> None:
        for server in (self.idp_server, self.mcp_server):
            server.shutdown()
            server.server_close()
        for thread in self._threads:
            thread.join(timeout=2)

    def __enter__(self) -> "DemoEnvironment":
        return self.start()

    def __exit__(self, *_args: Any) -> None:
        self.close()

    def issue_user_token(self, subject: str = "alice") -> str:
        url = f"{self.idp_url}/demo/issue-user-token?{urllib.parse.urlencode({'sub': subject})}"
        with urllib.request.urlopen(url, timeout=3) as response:
            return json.load(response)["access_token"]


class GatewaySimulator:
    """Models the Gateway + AgentCore Identity OBO behavior for local testing."""

    def __init__(self, environment: DemoEnvironment) -> None:
        self.environment = environment

    def exchange(self, user_token: str, audience: str | None = None) -> str:
        form = urllib.parse.urlencode(
            {
                "grant_type": TOKEN_EXCHANGE_GRANT,
                "subject_token": user_token,
                "subject_token_type": ACCESS_TOKEN_TYPE,
                "requested_token_type": ACCESS_TOKEN_TYPE,
                "audience": audience or self.environment.mcp_audience,
                "scope": "mcp:invoke",
            }
        ).encode()
        credentials = base64.b64encode(
            f"{self.environment.client_id}:{self.environment.client_secret}".encode()
        ).decode()
        request = urllib.request.Request(
            f"{self.environment.idp_url}/oauth2/token",
            data=form,
            headers={
                "Authorization": f"Basic {credentials}",
                "Content-Type": "application/x-www-form-urlencoded",
            },
        )
        with urllib.request.urlopen(request, timeout=3) as response:
            return json.load(response)["access_token"]

    def invoke(self, user_token: str, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        delegated_token = self.exchange(user_token)
        payload = json.dumps(
            {"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}}
        ).encode()
        request = urllib.request.Request(
            self.environment.mcp_url,
            data=payload,
            headers={
                "Authorization": f"Bearer {delegated_token}",
                "Content-Type": "application/json",
            },
        )
        with urllib.request.urlopen(request, timeout=3) as response:
            return json.load(response)


def run(subject: str) -> None:
    with DemoEnvironment() as environment:
        gateway = GatewaySimulator(environment)
        user_token = environment.issue_user_token(subject)
        user_claims = verify_token(
            user_token,
            environment.signing_secret,
            expected_issuer=environment.issuer,
            expected_audience=environment.gateway_audience,
        )
        delegated_token = gateway.exchange(user_token)
        delegated_claims = verify_token(
            delegated_token,
            environment.signing_secret,
            expected_issuer=environment.issuer,
            expected_audience=environment.mcp_audience,
        )
        response = gateway.invoke(user_token, "tools/call", {"name": "whoami", "arguments": {}})

        print("1. Incoming user token claims:")
        print(json.dumps(user_claims, indent=2))
        print("\n2. Exchanged MCP token claims:")
        print(json.dumps(delegated_claims, indent=2))
        print("\n3. MCP tools/call response:")
        print(json.dumps(response, indent=2))


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the local AgentCore OBO token-exchange demo")
    parser.add_argument("--subject", default="alice", help="User subject carried across the exchange")
    args = parser.parse_args()
    run(args.subject)


if __name__ == "__main__":
    main()
