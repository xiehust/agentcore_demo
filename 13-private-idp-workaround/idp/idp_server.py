"""
Minimal OIDC authorization server, standing in for a self-hosted Keycloak /
PingFederate inside the VPC.

It runs on an EC2 instance in a private subnet with no public IP and no route to
the internet, so it is only reachable from inside the VPC — which is exactly the
"Private IdP" situation AgentCore Identity needs VPC Lattice to handle natively.

Endpoints (deliberately the same shape a real IdP exposes):
  GET  /health                              liveness
  GET  /.well-known/openid-configuration    OIDC discovery
  GET  /jwks                                public keys, for INBOUND JWT validation
  POST /token                               client_credentials, for OUTBOUND token exchange

Signing is RS256 using a key generated at deploy time; the private key never
leaves this instance, only `n`/`e` are published via JWKS.
"""

import base64
import json
import os
import socket
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import jwt
from cryptography.hazmat.primitives import serialization

PORT = int(os.environ.get("IDP_PORT", "8081"))
ISSUER = os.environ["IDP_ISSUER"]                     # e.g. http://10.30.11.x:8081
KID = os.environ.get("IDP_KID", "demo-key-1")
AUDIENCE = os.environ.get("IDP_AUDIENCE", "agentcore-gateway")
CLIENT_ID = os.environ.get("IDP_CLIENT_ID", "order-desk-agent")
CLIENT_SECRET = os.environ["IDP_CLIENT_SECRET"]
KEY_PATH = os.environ.get("IDP_KEY_PATH", "/opt/idp/private_key.pem")

with open(KEY_PATH, "rb") as fh:
    PRIVATE_KEY = serialization.load_pem_private_key(fh.read(), password=None)
PUBLIC_NUMBERS = PRIVATE_KEY.public_key().public_numbers()


def b64u(value: int) -> str:
    raw = value.to_bytes((value.bit_length() + 7) // 8, "big")
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


JWKS = {"keys": [{
    "kty": "RSA", "use": "sig", "alg": "RS256", "kid": KID,
    "n": b64u(PUBLIC_NUMBERS.n), "e": b64u(PUBLIC_NUMBERS.e),
}]}

DISCOVERY = {
    "issuer": ISSUER,
    "jwks_uri": f"{ISSUER}/jwks",
    "token_endpoint": f"{ISSUER}/token",
    "grant_types_supported": ["client_credentials"],
    "id_token_signing_alg_values_supported": ["RS256"],
    "token_endpoint_auth_methods_supported": ["client_secret_post"],
}


def mint_access_token(scope: str) -> tuple[str, int]:
    """Issue an RS256 access token for the client_credentials grant."""
    now = int(time.time())
    ttl = 900
    claims = {
        "iss": ISSUER,
        "aud": AUDIENCE,
        "sub": CLIENT_ID,
        "client_id": CLIENT_ID,
        "scope": scope,
        "iat": now,
        "exp": now + ttl,
        "token_use": "access",
    }
    token = jwt.encode(claims, PRIVATE_KEY, algorithm="RS256",
                       headers={"kid": KID})
    return token, ttl


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "demo-private-idp"

    def _send(self, code, payload):
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        route = urlparse(self.path).path.rstrip("/") or "/"
        if route == "/health":
            self._send(200, {"status": "ok", "issuer": ISSUER,
                             "private_ip": socket.gethostbyname(socket.gethostname())})
        elif route == "/.well-known/openid-configuration":
            self._send(200, DISCOVERY)
        elif route == "/jwks":
            self._send(200, JWKS)
        else:
            self._send(404, {"error": "not_found", "path": route})

    def do_POST(self):
        route = urlparse(self.path).path.rstrip("/") or "/"
        if route != "/token":
            self._send(404, {"error": "not_found", "path": route})
            return

        length = int(self.headers.get("Content-Length") or 0)
        form = parse_qs(self.rfile.read(length).decode())
        grant = (form.get("grant_type") or [""])[0]
        client_id = (form.get("client_id") or [""])[0]
        secret = (form.get("client_secret") or [""])[0]
        scope = (form.get("scope") or ["orders.read"])[0]

        if grant != "client_credentials":
            self._send(400, {"error": "unsupported_grant_type"})
            return
        if client_id != CLIENT_ID or secret != CLIENT_SECRET:
            self._send(401, {"error": "invalid_client"})
            return

        token, ttl = mint_access_token(scope)
        self._send(200, {"access_token": token, "token_type": "Bearer",
                         "expires_in": ttl, "scope": scope})

    def log_message(self, fmt, *args):
        print(f"{self.address_string()} {fmt % args}", flush=True)


if __name__ == "__main__":
    print(f"private IdP listening on :{PORT} issuer={ISSUER}", flush=True)
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
