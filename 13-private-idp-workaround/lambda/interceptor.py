"""
Gateway REQUEST interceptor: inbound JWT authorization against a PRIVATE IdP.

This is the workaround for "AgentCore Identity cannot reach a private IdP".
Instead of giving the gateway a `customJWTAuthorizer.privateEndpoint` (which needs
VPC Lattice), the gateway is created with inbound auth NONE and this Lambda — which
IS attached to the VPC — does the JWT validation itself:

  1. read the bearer token from the request headers (requires passRequestHeaders=true)
  2. fetch the IdP's JWKS over the private network, cached per `kid`
  3. verify RS256 signature + iss / aud / exp with PyJWT
  4. reject by returning `transformedGatewayResponse`, which makes the gateway
     answer immediately and never call the target

To let a request through, echo the original JSON-RPC body back in
`transformedGatewayRequest` — see the note on allow().
"""

import json
import os
import time
import urllib.error
import urllib.request

import jwt
from jwt import PyJWKClient

IDP_JWKS_URL = os.environ["IDP_JWKS_URL"]
EXPECTED_ISS = os.environ["IDP_ISSUER"]
EXPECTED_AUD = os.environ["IDP_AUDIENCE"]
REQUIRED_SCOPE = os.environ.get("REQUIRED_SCOPE", "orders.read")

# PyJWKClient keeps its own key cache. Holding it at module scope means the cache
# survives across invocations for as long as the execution environment is reused,
# so a warm interceptor does not hit the IdP on every gateway call.
_jwk_client = PyJWKClient(IDP_JWKS_URL, cache_keys=True, lifespan=300)

# Methods allowed through without a token. The MCP handshake carries no user
# context yet, and rejecting it would break clients before they can authenticate.
OPEN_METHODS = {"initialize", "notifications/initialized", "ping"}


def deny(request_id, message, detail=None):
    """Short-circuit: the gateway returns this and never calls the target."""
    body = {"jsonrpc": "2.0", "id": request_id,
            "error": {"code": -32001, "message": message}}
    if detail:
        body["error"]["data"] = detail
    return {"interceptorOutputVersion": "1.0",
            "mcp": {"transformedGatewayResponse": {"statusCode": 403, "body": body}}}


def allow(body):
    """Let the request through.

    The original JSON-RPC body must be echoed back in `transformedGatewayRequest`.
    Returning an empty `{"mcp": {}}` instead makes the gateway answer
    `Parse error - Invalid JSON format` — the pass-through-unchanged shortcut
    documented for HTTP response interceptors does not apply here.
    """
    return {"interceptorOutputVersion": "1.0",
            "mcp": {"transformedGatewayRequest": {"body": body}}}


def bearer_from(headers):
    """Headers are case-insensitive on the wire but arrive as a plain dict."""
    for key, value in (headers or {}).items():
        if key.lower() == "authorization":
            parts = value.split(None, 1)
            if len(parts) == 2 and parts[0].lower() == "bearer":
                return parts[1].strip()
            return value.strip()
    return None


def lambda_handler(event, _context):
    mcp = (event or {}).get("mcp") or {}
    request = mcp.get("gatewayRequest") or {}
    body = request.get("body") or {}
    method = body.get("method") or ""
    request_id = body.get("id")

    # NOTE: never log the raw Authorization value — it is a live credential.
    print(json.dumps({"method": method, "id": request_id,
                      "has_headers": bool(request.get("headers"))}))

    if method in OPEN_METHODS:
        return allow(body)

    # With passRequestHeaders=false the gateway sends an EMPTY dict, not a missing
    # key — so testing `is None` would silently look like "client sent no token".
    # Treat empty as misconfiguration and say so, rather than blaming the caller.
    headers = request.get("headers")
    if not headers:
        return deny(request_id, "interceptor cannot see request headers",
                    "set inputConfiguration.passRequestHeaders=true on the interceptor")

    token = bearer_from(headers)
    if not token:
        return deny(request_id, "missing bearer token")

    try:
        signing_key = _jwk_client.get_signing_key_from_jwt(token)
        claims = jwt.decode(
            token,
            signing_key.key,
            algorithms=["RS256"],
            audience=EXPECTED_AUD,
            issuer=EXPECTED_ISS,
            options={"require": ["exp", "iss", "aud"]},
        )
    except jwt.ExpiredSignatureError:
        return deny(request_id, "token expired")
    except jwt.InvalidAudienceError:
        return deny(request_id, "wrong audience")
    except jwt.InvalidIssuerError:
        return deny(request_id, "wrong issuer")
    except jwt.InvalidSignatureError:
        return deny(request_id, "signature verification failed")
    except (jwt.InvalidTokenError, urllib.error.URLError, Exception) as exc:
        return deny(request_id, "token rejected", f"{type(exc).__name__}: {exc}")

    scopes = (claims.get("scope") or "").split()
    if REQUIRED_SCOPE and REQUIRED_SCOPE not in scopes:
        return deny(request_id, f"missing required scope {REQUIRED_SCOPE}",
                    {"scopes": scopes})

    print(json.dumps({"authorized": True, "sub": claims.get("sub"),
                      "client_id": claims.get("client_id"), "scopes": scopes,
                      "exp_in_s": claims.get("exp", 0) - int(time.time())}))
    return allow(body)
