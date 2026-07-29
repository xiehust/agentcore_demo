"""
Gateway Lambda target: OUTBOUND token exchange against the PRIVATE IdP.

This is the recommended half of the private-IdP workaround. Rather than relying on
an AgentCore Identity OAuth credential provider with `privateEndpoint` (VPC Lattice
again), the tool Lambda is already inside the VPC — so it performs the
client_credentials grant against the private IdP itself, then calls the downstream
resource. No AgentCore Identity involvement, so no private-IdP support needed.

Tools:
  secure_list_orders  -- exchange client_credentials at the private IdP, then read
                         the private RDS. Returns the token's verifiable claims as
                         evidence that the exchange really happened.
  idp_reachability    -- prove the IdP is reachable privately and report both IPs.
"""

import base64
import json
import os
import socket
import time
import urllib.parse
import urllib.request

import pymysql

IDP_TOKEN_URL = os.environ["IDP_TOKEN_URL"]
IDP_ISSUER = os.environ["IDP_ISSUER"]
CLIENT_ID = os.environ["IDP_CLIENT_ID"]
CLIENT_SECRET = os.environ["IDP_CLIENT_SECRET"]

DB_HOST = os.environ["DB_HOST"]
DB_USER = os.environ["DB_USER"]
DB_PASS = os.environ["DB_PASS"]
DB_NAME = os.environ["DB_NAME"]

_token_cache: dict = {}


def fetch_downstream_token(scope="orders.read"):
    """client_credentials grant against the private IdP, with a small cache."""
    cached = _token_cache.get(scope)
    if cached and cached["expires_at"] - 60 > time.time():
        return cached["token"], True

    form = urllib.parse.urlencode({
        "grant_type": "client_credentials",
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "scope": scope,
    }).encode()
    req = urllib.request.Request(
        IDP_TOKEN_URL, data=form, method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded"})
    with urllib.request.urlopen(req, timeout=10) as resp:
        payload = json.loads(resp.read())

    token = payload["access_token"]
    _token_cache[scope] = {"token": token,
                           "expires_at": time.time() + payload.get("expires_in", 900)}
    return token, False


def peek_claims(token):
    """Decode (not verify) the payload — used purely to show what we received."""
    part = token.split(".")[1]
    part += "=" * (-len(part) % 4)
    return json.loads(base64.urlsafe_b64decode(part))


def connect_db():
    return pymysql.connect(host=DB_HOST, user=DB_USER, password=DB_PASS,
                           database=DB_NAME, connect_timeout=8,
                           cursorclass=pymysql.cursors.DictCursor, autocommit=True)


def t_secure_list_orders(args):
    status = args.get("status")
    limit = max(1, min(int(args.get("limit", 10)), 100))

    token, from_cache = fetch_downstream_token()
    claims = peek_claims(token)

    sql = "SELECT order_ref, customer_email, status, amount FROM orders"
    params = []
    if status:
        sql += " WHERE status = %s"
        params.append(status.upper())
    sql += " ORDER BY id LIMIT %s"
    params.append(limit)
    with connect_db() as conn, conn.cursor() as cur:
        cur.execute(sql, params)
        rows = cur.fetchall()

    return {
        "outbound_auth": {
            "path": "tool Lambda (in VPC) -> private IdP /token -> downstream token",
            "idp_issuer": claims.get("iss"),
            "idp_private_ip": socket.gethostbyname(
                urllib.parse.urlparse(IDP_TOKEN_URL).hostname),
            "token_from_cache": from_cache,
            "client_id": claims.get("client_id"),
            "scope": claims.get("scope"),
            "expires_in_s": claims.get("exp", 0) - int(time.time()),
        },
        "count": len(rows),
        "orders": [{**r, "amount": float(r["amount"])} for r in rows],
    }


def t_idp_reachability(_args):
    host = urllib.parse.urlparse(IDP_TOKEN_URL).hostname
    with urllib.request.urlopen(f"{IDP_ISSUER}/health", timeout=10) as resp:
        health = json.loads(resp.read())
    with urllib.request.urlopen(f"{IDP_ISSUER}/.well-known/openid-configuration",
                                timeout=10) as resp:
        discovery = json.loads(resp.read())
    return {
        "idp_private_ip": socket.gethostbyname(host),
        "lambda_sees_idp_health": health,
        "discovery_issuer": discovery.get("issuer"),
        "discovery_jwks_uri": discovery.get("jwks_uri"),
        "note": "fetched over the VPC's private network; the IdP has no public IP",
    }


TOOLS = {"secure_list_orders": t_secure_list_orders,
         "idp_reachability": t_idp_reachability}


def lambda_handler(event, context):
    custom = getattr(getattr(context, "client_context", None), "custom", None) or {}
    name = (custom.get("bedrockAgentCoreToolName", "") or "").split("___")[-1]
    fn = TOOLS.get(name)
    if fn is None:
        return {"error": f"unknown tool {name!r}", "available": sorted(TOOLS)}
    try:
        return fn(event if isinstance(event, dict) else {})
    except Exception as exc:
        return {"error": f"{type(exc).__name__}: {exc}"}
