#!/usr/bin/env python3
"""
Verification matrix for the private-IdP workaround.

Tokens are minted locally with the same RSA key the private IdP holds, so we can
produce deliberately invalid variants (expired, wrong aud, wrong iss, missing
scope) and — using a second key the IdP has never seen — a forged signature.

Every case makes the same tools/call. Only a fully valid token should reach the
tool; everything else must be short-circuited by the interceptor.
"""

import json
import os
import sys
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


def rpc(method, params=None, token=None, session_id=None):
    """One JSON-RPC call. Returns (http_status, parsed_body_or_text, headers)."""
    _id[0] += 1
    body = json.dumps({"jsonrpc": "2.0", "id": _id[0],
                       "method": method, "params": params or {}}).encode()
    headers = {"Content-Type": "application/json",
               "Accept": "application/json, text/event-stream"}
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    if session_id:
        headers["Mcp-Session-Id"] = session_id

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


def attempt(token, *, send_header=True):
    """Full MCP flow with one token. Returns (reached_tool, detail)."""
    tok = token if send_header else None
    status, payload, hdrs = rpc("initialize", {
        "protocolVersion": "2025-06-18", "capabilities": {},
        "clientInfo": {"name": "idp-verifier", "version": "1.0"}}, token=tok)
    sid = hdrs.get("Mcp-Session-Id") or hdrs.get("mcp-session-id")
    if status != 200:
        return False, f"initialize HTTP {status}: {json.dumps(payload)[:200]}"

    status, payload, _ = rpc("tools/call", {
        "name": "secureOrders___secure_list_orders",
        "arguments": {"status": "PENDING"}}, token=tok, session_id=sid)

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
    if "orders" in data:
        return True, data
    return False, f"unexpected tool output — {json.dumps(data)[:200]}"


CASES = [
    ("valid token",              lambda: mint(),                                    True),
    ("no Authorization header",  lambda: None,                                      False),
    ("malformed token",          lambda: "not.a.jwt",                               False),
    ("forged signature (attacker key)", lambda: mint(ATTACKER_KEY),                 False),
    ("expired token",            lambda: mint(ttl=-60),                             False),
    ("wrong audience",           lambda: mint(aud="some-other-api"),                False),
    ("wrong issuer",             lambda: mint(iss="https://evil.example.com"),       False),
    ("missing required scope",   lambda: mint(scope="profile.read"),                False),
    ("unknown signing kid",      lambda: mint(kid="no-such-key"),                   False),
]


def main():
    print(f"gateway : {URL}")
    print(f"IdP     : {ISSUER} (private, no public IP)")
    print(f"audience: {AUDIENCE}\n")

    rows, failures = [], 0
    for name, make_token, should_pass in CASES:
        token = make_token()
        reached, detail = attempt(token, send_header=token is not None)
        good = reached == should_pass
        failures += 0 if good else 1
        verdict = "PASS" if good else "UNEXPECTED"
        arrow = "reached tool" if reached else "blocked"
        summary = detail if isinstance(detail, str) else \
            f"{detail['count']} orders, outbound token from {detail['outbound_auth']['idp_private_ip']}"
        print(f"[{verdict:10}] {name:34} -> {arrow}")
        print(f"             {summary[:150]}")
        rows.append({"case": name, "expected_pass": should_pass,
                     "reached_tool": reached, "detail": detail, "verdict": verdict})

    os.makedirs(os.path.join(ROOT, "results"), exist_ok=True)
    with open(os.path.join(ROOT, "results", "verification.json"), "w") as fh:
        json.dump({"gateway": URL, "issuer": ISSUER, "cases": rows}, fh,
                  indent=2, default=str)
    print(f"\n{len(CASES) - failures}/{len(CASES)} cases behaved as expected")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
