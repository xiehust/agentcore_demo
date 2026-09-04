"""AWS Lambda implementation of a demo RFC 8693 IdP and protected MCP server.

The function uses an asymmetric AWS KMS key for RS256 signing and verification.
Public traffic arrives only through API Gateway; test user tokens are minted through
an IAM-authorized direct Lambda invocation.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import time
import urllib.parse
from typing import Any

import boto3

kms = boto3.client("kms")
KMS_KEY_ARN = os.environ["KMS_KEY_ARN"]
KEY_KID = os.environ.get("KEY_KID", "agentcore-obo-demo")
ISSUER = os.environ["ISSUER"].rstrip("/")
CLIENT_ID = os.environ["CLIENT_ID"]
CLIENT_SECRET = os.environ["CLIENT_SECRET"]
GATEWAY_AUDIENCE = "agentcore-gateway-demo"
MCP_AUDIENCE = "mcp-server"
TOKEN_EXCHANGE_GRANT = "urn:ietf:params:oauth:grant-type:token-exchange"
ACCESS_TOKEN_TYPE = "urn:ietf:params:oauth:token-type:access_token"
JWT_TOKEN_TYPE = "urn:ietf:params:oauth:token-type:jwt"


def b64url_encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def b64url_decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def read_tlv(data: bytes, offset: int = 0) -> tuple[int, bytes, int]:
    tag = data[offset]
    first_length = data[offset + 1]
    cursor = offset + 2
    if first_length & 0x80:
        count = first_length & 0x7F
        length = int.from_bytes(data[cursor : cursor + count], "big")
        cursor += count
    else:
        length = first_length
    return tag, data[cursor : cursor + length], cursor + length


def public_jwk() -> dict[str, str]:
    public_key = kms.get_public_key(KeyId=KMS_KEY_ARN)["PublicKey"]
    _, spki, _ = read_tlv(public_key)
    _, _, cursor = read_tlv(spki)
    _, bit_string, _ = read_tlv(spki, cursor)
    _, rsa_sequence, _ = read_tlv(bit_string[1:])
    _, modulus_bytes, cursor = read_tlv(rsa_sequence)
    _, exponent_bytes, _ = read_tlv(rsa_sequence, cursor)
    modulus_bytes = modulus_bytes.lstrip(b"\x00")
    exponent_bytes = exponent_bytes.lstrip(b"\x00")
    return {
        "kty": "RSA",
        "kid": KEY_KID,
        "use": "sig",
        "alg": "RS256",
        "n": b64url_encode(modulus_bytes),
        "e": b64url_encode(exponent_bytes),
    }


def issue_token(
    *,
    subject: str,
    audience: str,
    scopes: list[str],
    actor: str | None = None,
    expires_in: int = 300,
) -> str:
    now = int(time.time())
    header = {"alg": "RS256", "typ": "JWT", "kid": KEY_KID}
    claims: dict[str, Any] = {
        "iss": ISSUER,
        "sub": subject,
        "aud": audience,
        "scope": " ".join(scopes),
        "client_id": CLIENT_ID,
        "iat": now,
        "exp": now + expires_in,
    }
    if actor:
        claims["act"] = {"sub": actor}
    signing_input = ".".join(
        (
            b64url_encode(json.dumps(header, separators=(",", ":")).encode()),
            b64url_encode(json.dumps(claims, separators=(",", ":")).encode()),
        )
    )
    signature = kms.sign(
        KeyId=KMS_KEY_ARN,
        Message=signing_input.encode(),
        MessageType="RAW",
        SigningAlgorithm="RSASSA_PKCS1_V1_5_SHA_256",
    )["Signature"]
    return f"{signing_input}.{b64url_encode(signature)}"


def verify_token(token: str, expected_audience: str) -> dict[str, Any]:
    try:
        encoded_header, encoded_claims, encoded_signature = token.split(".")
        header = json.loads(b64url_decode(encoded_header))
        claims = json.loads(b64url_decode(encoded_claims))
    except (ValueError, json.JSONDecodeError) as exc:
        raise ValueError("malformed JWT") from exc
    if header.get("alg") != "RS256" or header.get("kid") != KEY_KID:
        raise ValueError("unsupported JWT key or algorithm")
    verification = kms.verify(
        KeyId=KMS_KEY_ARN,
        Message=f"{encoded_header}.{encoded_claims}".encode(),
        MessageType="RAW",
        Signature=b64url_decode(encoded_signature),
        SigningAlgorithm="RSASSA_PKCS1_V1_5_SHA_256",
    )
    if not verification.get("SignatureValid"):
        raise ValueError("invalid JWT signature")
    if claims.get("iss") != ISSUER:
        raise ValueError("unexpected JWT issuer")
    audiences = claims.get("aud")
    audiences = audiences if isinstance(audiences, list) else [audiences]
    if expected_audience not in audiences:
        raise ValueError("unexpected JWT audience")
    if int(claims.get("exp", 0)) <= int(time.time()):
        raise ValueError("expired JWT")
    return claims


def response(status: int, body: Any = "", content_type: str = "application/json") -> dict[str, Any]:
    if not isinstance(body, str):
        body = json.dumps(body, separators=(",", ":"))
    return {
        "statusCode": status,
        "headers": {
            "content-type": content_type,
            "cache-control": "no-store",
        },
        "body": body,
    }


def parse_basic_auth(headers: dict[str, str]) -> tuple[str, str] | None:
    authorization = headers.get("authorization", "")
    if not authorization.startswith("Basic "):
        return None
    try:
        client_id, client_secret = base64.b64decode(
            authorization.removeprefix("Basic ")
        ).decode().split(":", 1)
    except (ValueError, UnicodeDecodeError):
        return None
    return client_id, client_secret


def token_endpoint(event: dict[str, Any], headers: dict[str, str]) -> dict[str, Any]:
    body = event.get("body", "") or ""
    if event.get("isBase64Encoded"):
        body = base64.b64decode(body).decode()
    form = {key: values[-1] for key, values in urllib.parse.parse_qs(body).items()}
    print(
        json.dumps(
            {
                "event": "token_request",
                "grant_type": form.get("grant_type"),
                "form_keys": sorted(form),
                "audience": form.get("audience"),
                "scope": form.get("scope"),
                "has_basic_auth": headers.get("authorization", "").startswith("Basic "),
            }
        )
    )
    basic = parse_basic_auth(headers)
    supplied_client_id = basic[0] if basic else form.get("client_id", "")
    supplied_secret = basic[1] if basic else form.get("client_secret", "")
    if supplied_client_id != CLIENT_ID or supplied_secret != CLIENT_SECRET:
        return response(401, {"error": "invalid_client"})

    grant_type = form.get("grant_type")
    if grant_type == "client_credentials":
        requested_scopes = form.get("scope", "mcp:invoke").split()
        return response(
            200,
            {
                "access_token": issue_token(
                    subject=CLIENT_ID,
                    audience=MCP_AUDIENCE,
                    scopes=requested_scopes,
                ),
                "token_type": "Bearer",
                "expires_in": 300,
                "scope": " ".join(requested_scopes),
            },
        )

    if grant_type != TOKEN_EXCHANGE_GRANT:
        return response(400, {"error": "unsupported_grant_type"})
    if form.get("subject_token_type") not in {ACCESS_TOKEN_TYPE, JWT_TOKEN_TYPE}:
        return response(400, {"error": "invalid_request", "error_description": "bad subject_token_type"})
    if form.get("audience") != MCP_AUDIENCE:
        return response(400, {"error": "invalid_target"})
    try:
        source_claims = verify_token(form.get("subject_token", ""), GATEWAY_AUDIENCE)
    except ValueError as exc:
        return response(400, {"error": "invalid_grant", "error_description": str(exc)})
    requested_scopes = form.get("scope", "mcp:invoke").split()
    if "mcp:invoke" not in requested_scopes:
        return response(400, {"error": "invalid_scope"})
    return response(
        200,
        {
            "access_token": issue_token(
                subject=source_claims["sub"],
                audience=MCP_AUDIENCE,
                scopes=requested_scopes,
                actor=CLIENT_ID,
            ),
            "issued_token_type": ACCESS_TOKEN_TYPE,
            "token_type": "Bearer",
            "expires_in": 300,
            "scope": " ".join(requested_scopes),
        },
    )


def mcp_endpoint(event: dict[str, Any], headers: dict[str, str]) -> dict[str, Any]:
    authorization = headers.get("authorization", "")
    if not authorization.startswith("Bearer "):
        return response(401, {"error": "missing_bearer_token"})
    try:
        claims = verify_token(authorization.removeprefix("Bearer "), MCP_AUDIENCE)
    except ValueError as exc:
        return response(401, {"error": "invalid_token", "error_description": str(exc)})
    if "mcp:invoke" not in claims.get("scope", "").split():
        return response(403, {"error": "insufficient_scope"})

    raw_body = event.get("body", "") or "{}"
    if event.get("isBase64Encoded"):
        raw_body = base64.b64decode(raw_body).decode()
    request = json.loads(raw_body)
    request_id = request.get("id")
    method = request.get("method")

    if method == "initialize":
        result = {
            "protocolVersion": "2025-06-18",
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": "agentcore-obo-demo-mcp", "version": "1.0.0"},
        }
    elif method == "notifications/initialized":
        return response(202, "", "text/plain")
    elif method == "tools/list":
        result = {
            "tools": [
                {
                    "name": "whoami",
                    "description": "Return the user and workload propagated by token exchange",
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
            "issuer": claims["iss"],
        }
        result = {
            "content": [{"type": "text", "text": json.dumps(identity)}],
            "structuredContent": identity,
        }
    else:
        return response(
            200,
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "error": {"code": -32601, "message": "Method not found"},
            },
        )
    return response(200, {"jsonrpc": "2.0", "id": request_id, "result": result})


def handler(event: dict[str, Any], _context: Any) -> dict[str, Any]:
    if event.get("action") == "issue_user_token":
        subject = event.get("subject", "alice")
        return response(
            200,
            {
                "access_token": issue_token(
                    subject=subject,
                    audience=GATEWAY_AUDIENCE,
                    scopes=["gateway:invoke"],
                ),
                "token_type": "Bearer",
                "expires_in": 300,
            },
        )

    request_context = event.get("requestContext", {}).get("http", {})
    method = request_context.get("method", "GET")
    path = event.get("rawPath", "/")
    headers = {key.lower(): value for key, value in event.get("headers", {}).items()}

    if method == "GET" and path == "/.well-known/openid-configuration":
        return response(
            200,
            {
                "issuer": ISSUER,
                "jwks_uri": f"{ISSUER}/.well-known/jwks.json",
                "authorization_endpoint": f"{ISSUER}/oauth2/authorize",
                "token_endpoint": f"{ISSUER}/oauth2/token",
                "response_types_supported": ["code"],
                "grant_types_supported": ["client_credentials", TOKEN_EXCHANGE_GRANT],
                "subject_types_supported": ["public"],
                "id_token_signing_alg_values_supported": ["RS256"],
                "token_endpoint_auth_methods_supported": ["client_secret_basic", "client_secret_post"],
            },
        )
    if method == "GET" and path == "/.well-known/jwks.json":
        return response(200, {"keys": [public_jwk()]})
    if method == "GET" and path == "/health":
        return response(200, {"status": "ok"})
    if method == "POST" and path == "/oauth2/token":
        return token_endpoint(event, headers)
    if method == "POST" and path == "/mcp":
        return mcp_endpoint(event, headers)
    return response(404, {"error": "not_found", "path": path})
