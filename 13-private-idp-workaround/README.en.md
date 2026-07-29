# Private IdP without VPC Lattice — verified workaround and guidance

Real-deployment verification of section 09 of
`AgentCore 中国区无 VPC Egress Workaround 方案.html`
(*supplement — working around AgentCore Identity not supporting a private IdP*).

Deployed and verified in **us-east-2**, reusing the **zero-internet-egress VPC**
(no IGW, no NAT) and private RDS from
[`11-vpc-no-egress-workaround`](../11-vpc-no-egress-workaround).

---

## Bottom line

**The interceptor-Lambda workaround works: 9/9 verification cases pass.**

With an IdP that AgentCore Identity cannot reach (no public IP, reachable only from
the Lambda security group, in a VPC with no egress route at all):

- **Inbound** — gateway inbound auth is `NONE`, and a VPC-attached **REQUEST
  interceptor Lambda** privately fetches the IdP's JWKS and performs RS256
  verification plus `iss` / `aud` / `exp` / `scope` checks. Invalid tokens are
  short-circuited and **never reach the target**.
- **Outbound** — the tool Lambda performs the `client_credentials` grant against the
  private IdP from inside the VPC, then queries the private RDS. **No AgentCore
  Identity OAuth credential provider involved.**

| Metric | Measured |
|---|---|
| Verification cases | **9 / 9 pass** |
| Interceptor warm overhead | **p50 2.00 ms** (min 1.43 ms, n=216) |
| Interceptor cold start | init ≈ **250 ms** + 254 ms first call (includes JWKS fetch) |
| IdP public reachability | `PublicIpAddress = null`; from outside the VPC → `TimeoutError` |

---

## Verified architecture

```
  test client (JWT issued by the private IdP)
        │  MCP over HTTPS, no SigV4 (gateway inbound = NONE)
        ▼
 ┌────────────────────────────────────────────┐
 │ AgentCore Gateway  (authorizerType=NONE)   │
 │   REQUEST interceptor ──┐                  │
 └───────────┬─────────────┼──────────────────┘
             │             │ passRequestHeaders=true
             │             ▼
             │   ┌──────────────────────────┐
             │   │ interceptor Lambda (VPC) │──┐ fetch JWKS / verify
             │   └──────────────────────────┘  │
             │      invalid → 403 short-circuit│
             ▼                                 │
      ┌──────────────────┐                     │
      │ tool Lambda (VPC)│──┐ client_credentials│
      └────────┬─────────┘  │                  │
               │            ▼                  ▼
               │   ┌──────────────────────────────────┐
               │   │ private IdP (EC2 10.30.11.13:8081)│
               │   │ no public IP, Lambda SG only      │
               │   │ /jwks  /token  /.well-known/...   │
               │   └──────────────────────────────────┘
               ▼
      ┌────────────────────────┐
      │ private RDS MySQL 8.0.42│  VPC: no IGW / no NAT
      └────────────────────────┘
```

---

## Verification matrix (9/9)

Actual output of `python scripts/04-verify.py`; full record in
[`results/verification.json`](results/verification.json):

| # | Case | Result | Reason returned by the interceptor |
|---|---|---|---|
| 1 | valid token | ✅ **reached target** | 2 PENDING orders + outbound token from `10.30.11.13` |
| 2 | no Authorization header | 🚫 blocked | `missing bearer token` |
| 3 | malformed token | 🚫 blocked | `token rejected` |
| 4 | **signature forged with attacker key** | 🚫 blocked | `signature verification failed` |
| 5 | expired token | 🚫 blocked | `token expired` |
| 6 | wrong audience | 🚫 blocked | `wrong audience` |
| 7 | wrong issuer | 🚫 blocked | `wrong issuer` |
| 8 | missing required scope | 🚫 blocked | `missing required scope orders.read` |
| 9 | unknown signing `kid` | 🚫 blocked | `token rejected` |

**Why these cases are convincing:**

- Case 4 uses a **second RSA private key the IdP has never seen**. Catching it proves
  the interceptor really obtained the IdP's public key and verified the signature,
  rather than merely base64-decoding the token.
- Case 9's `kid` does not exist in the IdP's JWKS, so `PyJWKClient` fails to find a
  matching key — which **proves the JWKS was actually fetched from the private IdP**.
- Cases 2, 5, 6, 7 and 8 all carry **valid signatures from the real key** and differ
  only in their claims, showing validation goes beyond the signature.

---

## Three undocumented behaviours found

### ⚠️ 1. An MCP REQUEST interceptor must **echo the original body** to allow a request

The intuitive "change nothing, return an empty object" form:

```json
{ "interceptorOutputVersion": "1.0", "mcp": {} }
```

**Measured result: the client gets `HTTP 200` with JSON-RPC
`Parse error - Invalid JSON format`, and the request never reaches the target.** Note
that all nine negative cases behave correctly at the same time (the
`transformedGatewayResponse` short-circuit path is fine) — **only the allow path is
broken**, which reads very much like a bug in your rejection logic.

The correct form echoes the original body back:

```json
{ "interceptorOutputVersion": "1.0",
  "mcp": { "transformedGatewayRequest": { "body": <original JSON-RPC body> } } }
```

The documented "return an empty object to pass through unchanged"
(`{"interceptorOutputVersion":"1.0","http":{}}`) appears under **HTTP-target RESPONSE
interceptors** and **does not apply to MCP-target REQUEST interceptors**.

### ⚠️ 2. With `passRequestHeaders=false` you get an **empty dict**, not a missing field

Turn `passRequestHeaders` off and the interceptor receives `headers: {}` — the key is
**not** absent. Consequence:

```python
if headers is None:      # never true, so the misconfiguration goes undetected
```

This disguises a configuration error as "the client sent no token" and sends you
debugging the wrong thing (we hit exactly this: the symptom was
`missing bearer token`, the cause was `passRequestHeaders` being off). Test for
**emptiness** instead:

```python
if not headers:          # catches the empty dict too
    return deny(request_id, "interceptor cannot see request headers",
                "set inputConfiguration.passRequestHeaders=true")
```

### 3. `NONE` inbound + interceptor is a valid, working combination

`authorizerType=NONE` with a REQUEST interceptor **creates fine**, the gateway reaches
`READY`, and the `Authorization` header is handed to the interceptor intact. That
matters: with `AWS_IAM` inbound, `Authorization` is taken by the SigV4 signature, so
the business JWT would have to move to a custom header.

> ⚠️ **Security note**: `NONE` means the gateway endpoint has **no platform-level
> authentication** and the interceptor is the only gate. If the interceptor Lambda
> errors, times out, or is deleted, you get either an open door or a total outage. In
> production: give it Provisioned Concurrency and alarms; grant the gateway execution
> role `lambda:InvokeFunction` on **only** that function; and consider `AWS_IAM` plus a
> custom header for defence in depth.

---

## Practical guidance

### The interceptor's overhead is negligible — if you cache

`PyJWKClient` is held at **module scope** so its cache survives across invocations:

```python
_jwk_client = PyJWKClient(IDP_JWKS_URL, cache_keys=True, lifespan=300)
```

Measured (n=216): warm calls **p50 2.00 ms**; only the first call after a cold start is
**254 ms** (fetching JWKS); cold init ≈ **250 ms** (importing `cryptography` / `PyJWT`).
**Without caching, every gateway invocation hits the IdP**, amplifying both latency and
load on the IdP.

### For outbound, prefer letting the tool Lambda fetch the token

The tool Lambda is already in the VPC, so it can perform `client_credentials` directly
and cache the result (measured: `token_from_cache: true`). This path **never touches
AgentCore Identity**, so "does it support private IdPs" stops being a question — much
simpler than trying to rewrite Authorization from an interceptor.

> On the open question the design doc left — *can an MCP target's Authorization header
> be rewritten by an interceptor?* — **this run did not verify it.** For a Lambda target
> HTTP headers are not observable (the Lambda receives tool arguments, not an HTTP
> request), so there is no clean experiment. The path above already solves the outbound
> requirement, so we recommend it rather than depending on header rewriting.

### It works without HTTPS — an incidental advantage of the workaround

The native `privateEndpoint` path requires the IdP's discovery URL to be **HTTPS** with
a **publicly trusted** certificate (otherwise you must front it with an internal ALB
holding a public ACM cert). Going through an interceptor, **neither constraint exists**,
because the HTTP client is your own code — this demo's IdP is plain HTTP at
`http://10.30.11.13:8081`. Acceptable on a short internal hop, but **still use TLS in
production**; you are simply no longer bound to a publicly trusted certificate.

---

## Layout

```
idp/idp_server.py        minimal OIDC authorization server (stand-in for Keycloak)
                         /.well-known/openid-configuration /jwks /token /health
lambda/interceptor.py    REQUEST interceptor: inbound JWT validation (PyJWT + private JWKS)
lambda/tool.py           tool Lambda: outbound client_credentials + private RDS read
scripts/01-idp.sh        generate keys, bundle deps, launch the IdP in a private subnet
scripts/02-lambdas.sh    build and deploy both Lambdas, confirm the IdP is reachable
scripts/03-gateway.sh    gateway with NONE inbound + REQUEST interceptor, plus target
scripts/04-verify.py     the 9-case matrix (mints valid/invalid tokens locally)
scripts/05-collect-evidence.sh  archive all raw evidence
scripts/cleanup.sh       remove this project's resources
results/                 verification.json / evidence.txt / interceptor-latency.json
```

## Reproducing

Prerequisite: deploy [`11-vpc-no-egress-workaround`](../11-vpc-no-egress-workaround)
first (it provides the isolated VPC, private RDS and the S3 bootstrap bucket).

```bash
cd 13-private-idp-workaround
python3 -m venv .venv && .venv/bin/pip install "pyjwt[crypto]" boto3

bash scripts/01-idp.sh          # private IdP on EC2, no public IP (~2-3 min)
bash scripts/02-lambdas.sh      # interceptor + tool Lambda, waits for the IdP
bash scripts/03-gateway.sh      # gateway (NONE inbound + interceptor) + target
.venv/bin/python scripts/04-verify.py       # the 9-case matrix
bash scripts/05-collect-evidence.sh          # archive evidence
```

`scripts/04-verify.py` exits non-zero if any case deviates from expectation, so it
doubles as a regression test.

## Two build-time traps (handled in the scripts)

- **The AL2023 AMI ships Python 3.9 and has no pip.** Wheels must be resolved for
  `cp39` and unzipped on the instance with `python3 -m zipfile -e` (a wheel is just a
  zip). Resolving for 3.11 yields a `cp311-abi3` `cryptography` that will not install
  on 3.9.
- **The build host is arm64 while Lambda and EC2 are x86_64.** Cross-platform installs
  need `pip install --platform manylinux2014_x86_64 --only-binary=:all: --target ...`;
  a plain `pip install` produces an arm64 `cryptography` that fails to import in the
  cloud.

## Cost

Adds roughly **$0.01/hour** (the t3.micro IdP). Lambda and Gateway are per-request. The
rest comes from `11-vpc-no-egress-workaround` (RDS/NLB/EC2, ~$0.10/hour).

## Cleanup

```bash
bash scripts/cleanup.sh --yes                                  # this project
bash ../11-vpc-no-egress-workaround/scripts/cleanup.sh --yes   # VPC / RDS / gateway
```

## Security statement

`idp/idp_server.py` is a **minimal demo implementation**: no refresh tokens, no client
registration, no rate limiting, no key rotation, and it serves plain HTTP. It exists
only to simulate "an OIDC server on an internal network". **Do not use it in
production** — use Keycloak, PingFederate or similar. The security-critical
**verification** side (the interceptor) uses `PyJWT` + `cryptography`; no hand-rolled
cryptography is involved.
