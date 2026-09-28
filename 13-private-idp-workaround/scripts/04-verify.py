#!/usr/bin/env python3
"""
Verification matrix for the private-IdP workaround.

Tokens are minted locally with the same RSA key the private IdP holds, so we can
produce deliberately invalid variants (expired, wrong aud, wrong iss, missing
scope) and — using a second key the IdP has never seen — a forged signature.

Every case makes the same tools/call. Only a fully valid token should reach the
tool; everything else must be short-circuited by the interceptor.
"""

from datetime import datetime, timezone
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

import jwt

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def load_state():
    state = {}
    for name in ("../11-vpc-no-egress-workaround/state.env", "state.env"):
        path = os.path.join(ROOT, name)
        if not os.path.exists(path):
            continue
        with open(path) as fh:
            for line in fh:
                line = line.strip()
                if line and "=" in line and not line.startswith("#"):
                    k, v = line.split("=", 1)
                    state[k] = v
    return state


S = load_state()
URL = S["IDP_GW_URL"]
ISSUER = S["IDP_ISSUER"]
AUDIENCE = S["IDP_AUDIENCE"]
KID = S["IDP_KID"]
# Inbound auth of the gateway. NONE: the JWT rides in Authorization and requests go
# straight to the gateway. AWS_IAM (China regions reject NONE): requests must be
# SigV4-signed, so they are relayed through the signed-caller Lambda deployed by
# 03-gateway.sh (its execution role signs), and the JWT moves to TOKEN_HEADER
# because SigV4 owns Authorization.
INBOUND_AUTH = S.get("INBOUND_AUTH", "NONE")
TOKEN_HEADER = S.get("TOKEN_HEADER", "Authorization")
REGION = S.get("STATE_REGION") or os.environ.get("REGION", "us-east-2")
CALLER_FN = S.get("CALLER_FN")


def post_via_caller(body, headers, signed):
    """Relay one request through the signed-caller Lambda using the aws CLI."""
    payload = json.dumps({"url": URL, "region": REGION, "body": body.decode(),
                          "headers": headers, "signed": signed})
    with tempfile.TemporaryDirectory() as tmp:
        out = os.path.join(tmp, "out.json")
        subprocess.run(
            ["aws", "lambda", "invoke", "--function-name", CALLER_FN,
             "--region", REGION, "--cli-binary-format", "raw-in-base64-out",
             "--payload", payload, out],
            check=True, capture_output=True)
        with open(out) as fh:
            resp = json.load(fh)
    if "status" not in resp:
        raise RuntimeError(f"signed caller failed: {json.dumps(resp)[:300]}")
    return resp["body"], resp["status"], resp["headers"]

with open(os.path.join(ROOT, "build/keys/private_key.pem"), "rb") as fh:
    REAL_KEY = fh.read()
with open(os.path.join(ROOT, "build/keys/attacker_key.pem"), "rb") as fh:
    ATTACKER_KEY = fh.read()

_id = [0]


def mint(key=REAL_KEY, *, iss=None, aud=None, scope="orders.read",
         ttl=900, kid=KID):
    now = int(time.time())
    claims = {
        "iss": iss or ISSUER,
        "aud": aud or AUDIENCE,
        "sub": "order-desk-agent",
        "client_id": "order-desk-agent",
        "scope": scope,
        "iat": now - 5,
        "exp": now + ttl,
    }
    return jwt.encode(claims, key, algorithm="RS256", headers={"kid": kid})


def rpc(method, params=None, token=None, session_id=None, signed=True):
    """One JSON-RPC call. Returns (http_status, parsed_body_or_text, headers)."""
    _id[0] += 1
    body = json.dumps({"jsonrpc": "2.0", "id": _id[0],
                       "method": method, "params": params or {}}).encode()
    headers = {"Content-Type": "application/json",
               "Accept": "application/json, text/event-stream"}
    if token is not None:
        headers[TOKEN_HEADER] = f"Bearer {token}"
    if session_id:
        headers["Mcp-Session-Id"] = session_id

    if INBOUND_AUTH == "AWS_IAM":
        raw, status, hdrs = post_via_caller(body, headers, signed)
    else:
        req = urllib.request.Request(URL, data=body, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=90) as resp:
                raw, status, hdrs = resp.read().decode(), resp.status, dict(resp.headers)
        except urllib.error.HTTPError as err:
            raw, status, hdrs = err.read().decode(), err.code, dict(err.headers)

    if raw.lstrip().startswith(("event:", "data:")):
        for line in raw.splitlines():
            if line.startswith("data:"):
                raw = line[5:].strip()
                break
    try:
        return status, json.loads(raw), hdrs
    except ValueError:
        return status, raw, hdrs


def validate_tool_output(data, status):
    """Assert the demo read and outbound exchange, not just an 'orders' key."""
    if not isinstance(data, dict) or "error" in data:
        raise ValueError("tool returned an error or non-object")
    orders = data.get("orders")
    if not isinstance(orders, list) or not orders or data.get("count") != len(orders):
        raise ValueError("missing orders or inconsistent count")
    if any(not isinstance(row, dict) or row.get("status") != status for row in orders):
        raise ValueError("unexpected order status")
    auth = data.get("outbound_auth")
    if not isinstance(auth, dict):
        raise ValueError("missing outbound authentication evidence")
    if (auth.get("idp_issuer") != ISSUER or auth.get("idp_private_ip") != S["IDP_IP"]
            or auth.get("client_id") != S["IDP_CLIENT_ID"]
            or "orders.read" not in (auth.get("scope") or "").split()
            or not isinstance(auth.get("expires_in_s"), (int, float))
            or auth["expires_in_s"] <= 0
            or not isinstance(auth.get("token_from_cache"), bool)):
        raise ValueError("unexpected or expired outbound token metadata")


def attempt(token, *, send_header=True, signed=True):
    """Full MCP flow with one token. Returns (reached_tool, detail)."""
    tok = token if send_header else None
    status, payload, hdrs = rpc("initialize", {
        "protocolVersion": "2025-06-18", "capabilities": {},
        "clientInfo": {"name": "idp-verifier", "version": "1.0"}}, token=tok,
        signed=signed)
    sid = hdrs.get("Mcp-Session-Id") or hdrs.get("mcp-session-id")
    if status != 200:
        return False, f"initialize HTTP {status}: {json.dumps(payload)[:200]}"

    status, payload, _ = rpc("tools/call", {
        "name": "secureOrders___secure_list_orders",
        "arguments": {"status": "PENDING"}}, token=tok, session_id=sid,
        signed=signed)

    if status != 200:
        msg = payload.get("error", {}).get("message") if isinstance(payload, dict) else payload
        return False, f"HTTP {status} — {msg}"
    if isinstance(payload, dict) and "error" in payload:
        return False, f"HTTP 200 rpc error — {payload['error'].get('message')}"

    result = payload.get("result", {}) if isinstance(payload, dict) else {}
    if result.get("isError"):
        text = (result.get("content") or [{}])[0].get("text", "")
        return False, f"tool isError — {text[:160]}"
    text = (result.get("content") or [{}])[0].get("text", "")
    try:
        data = json.loads(text)
    except ValueError:
        return False, f"unparsable tool output — {text[:160]}"
    try:
        validate_tool_output(data, "PENDING")
    except ValueError as exc:
        return False, f"unexpected tool output — {exc}"
    return True, data


# Negative cases must fail at the expected auth layer, not merely fail to return
# orders. A 5xx, broken handshake or tool error is NOT a successful rejection.
CASES = [
    ("valid token", lambda: mint(), True, None),
    ("no business JWT header", lambda: None, False,
     "HTTP 403 — missing bearer token"),
    ("malformed token", lambda: "not.a.jwt", False,
     "HTTP 403 — token rejected"),
    ("forged signature (attacker key)", lambda: mint(ATTACKER_KEY), False,
     "HTTP 403 — signature verification failed"),
    ("expired token", lambda: mint(ttl=-60), False,
     "HTTP 403 — token expired"),
    ("wrong audience", lambda: mint(aud="some-other-api"), False,
     "HTTP 403 — wrong audience"),
    ("wrong issuer", lambda: mint(iss="https://evil.example.com"), False,
     "HTTP 403 — wrong issuer"),
    ("missing required scope", lambda: mint(scope="profile.read"), False,
     "HTTP 403 — missing required scope orders.read"),
    ("unknown signing kid", lambda: mint(kid="no-such-key"), False,
     "HTTP 403 — unknown signing key"),
]
# With AWS_IAM inbound the platform is a second gate: a valid JWT without a SigV4
# signature must be refused by the gateway before the interceptor ever runs.
if INBOUND_AUTH == "AWS_IAM":
    CASES.append(("valid token but unsigned (no SigV4)", lambda: mint(), False,
                  "initialize HTTP 401:"))


def main():
    print(f"gateway : {URL}")
    print(f"IdP     : {ISSUER} (private, no public IP)")
    print(f"audience: {AUDIENCE}")
    print(f"inbound : {INBOUND_AUTH} (JWT in {TOKEN_HEADER})\n")

    rows, failures = [], 0
    for name, make_token, should_pass, expected_rejection in CASES:
        token = make_token()
        reached, detail = attempt(token, send_header=token is not None,
                                  signed="unsigned" not in name)
        good = reached == should_pass and (
            should_pass or (isinstance(detail, str) and detail.startswith(expected_rejection)))
        failures += 0 if good else 1
        verdict = "PASS" if good else "UNEXPECTED"
        arrow = "reached tool" if reached else "blocked"
        summary = detail if isinstance(detail, str) else \
            f"{detail['count']} orders, outbound token from {detail['outbound_auth']['idp_private_ip']}"
        print(f"[{verdict:10}] {name:34} -> {arrow}")
        print(f"             {summary[:150]}")
        rows.append({"case": name, "expected_pass": should_pass,
                     "expected_rejection": expected_rejection,
                     "reached_tool": reached, "detail": detail, "verdict": verdict})

    # RESULTS_DIR lets a regression run write next to, not over, the baseline.
    out_dir = os.environ.get("RESULTS_DIR") or os.path.join(ROOT, "results")
    os.makedirs(out_dir, exist_ok=True)
    with open(os.path.join(out_dir, "verification.json"), "w") as fh:
        json.dump({"gateway": URL, "issuer": ISSUER, "inbound_auth": INBOUND_AUTH,
                   "token_header": TOKEN_HEADER, "region": REGION,
                   "profile": os.environ.get("AWS_PROFILE"),
                   "verified_at": datetime.now(timezone.utc).isoformat(),
                   "signing_caller": CALLER_FN if INBOUND_AUTH == "AWS_IAM" else None,
                   "cases": rows}, fh,
                  indent=2, default=str)
    print(f"\n{len(CASES) - failures}/{len(CASES)} cases behaved as expected")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
