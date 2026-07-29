# Strands + Nova 2 Lite on AgentCore Runtime (PUBLIC) — working end-to-end sample

An agent written with the **Strands Agents SDK**, running on
**`global.amazon.nova-2-lite-v1:0`**, deployed to an **AgentCore Runtime with
`networkMode=PUBLIC` and no VPC configuration at all**, reading a **private RDS MySQL
instance inside the zero-internet-egress VPC** from
[`11-vpc-no-egress-workaround`](../11-vpc-no-egress-workaround) through AgentCore
Gateway's MCP tools.

Deployed and verified in **us-east-2**.

---

## End-to-end path

```
   local / any caller
        │  InvokeAgentRuntime (SigV4)
        ▼
 ┌──────────────────────────────────────┐
 │ AgentCore Runtime                    │   networkMode = PUBLIC
 │ strands_nova2_orderdesk              │   ← no vpcConfig whatsoever
 │                                      │
 │  Strands Agent                       │──── Bedrock ──▶ global.amazon.nova-2-lite-v1:0
 │  + MCPClient (SigV4-signed)          │
 └───────────────┬──────────────────────┘
                 │ MCP over HTTPS (AWS_IAM inbound)
                 ▼
        ┌────────────────────┐
        │ AgentCore Gateway  │  4 tools
        └─────┬──────────┬───┘
              │          │
      ┌───────▼──┐   ┌───▼──────────────────────────┐
      │ Lambda   │   │ API GW → VPC Link → NLB → EC2│
      │ (in VPC) │   │                              │
      └───────┬──┘   └───┬──────────────────────────┘
              │          │
              ▼          ▼
      ┌────────────────────────────────┐
      │ private RDS MySQL 8.0.42       │  isolated VPC: no IGW / no NAT
      │ PubliclyAccessible = false     │
      └────────────────────────────────┘
```

This also **validates "workaround 4 — sink the egress into a tool"** from the original
design doc: the Runtime has no VPC egress capability at all, yet the agent still reads
an internal database because private access was pushed down into a Gateway tool.

---

## Verified results

Actual output of `python scripts/invoke.py` (full record in
[`results/invocations.json`](results/invocations.json)):

| # | Prompt | Answer | Tool called | Latency |
|---|---|---|---|---|
| 1 | How many orders are pending, and their refs? | There are 2 pending orders: ORD-1002 and ORD-1005. | `rdsLambda___list_orders` | 1997 ms |
| 2 | Which order was cancelled, for how much? | The cancelled order was ORD-1004 for $15.25. | `rdsLambda___list_orders` | 1845 ms |
| 3 | MySQL version and host? | MySQL **8.0.42** on host **ip-172-31-0-80**. | `rdsLambda___db_info` | 1817 ms |
| 4 | Total value of all shipped orders? | **$1,020.99** (ORD-1001 $129.99 + ORD-1003 $891.00) | `rdsLambda___list_orders` | 1786 ms |
| 5 | Force the API Gateway path | ec2_private_ip `10.30.11.109`, db_user `agentadmin@10.30.11.109` | `rdsApi___getDbInfo` | 2266 ms |

Notes:

- **The data is real.** The $1,020.99 in row 4 was computed by Nova 2 Lite from the two
  rows the tool returned; the `10.30.11.109` in row 5 is the EC2 address in the private subnet.
- **Both paths work.** The model prefers `rdsLambda___*` (more direct descriptions); named
  explicitly, `rdsApi___*` (API GW → VPC Link → NLB → EC2) works equally well.
- **The runtime has no VPC config** — `get-agent-runtime` returns just
  `{"networkMode":"PUBLIC"}`.
- `invoke.py` exits non-zero if any prompt returns an error or answers without calling a
  tool, so it doubles as a CI smoke test.

---

## Implementation notes worth knowing

### 1. Nova 2 Lite via a global inference profile

```python
model = BedrockModel(model_id="global.amazon.nova-2-lite-v1:0", region_name=REGION)
```

The `global.` prefix is a **cross-region inference profile** that may route anywhere, so
IAM must grant both the **profile** and the **underlying foundation model**, with a
wildcard region:

```json
"Action": ["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"],
"Resource": [
  "arn:aws:bedrock:*:ACCOUNT:inference-profile/global.amazon.nova-2-lite-v1:0",
  "arn:aws:bedrock:*::foundation-model/amazon.nova-2-lite-v1:0"
]
```
Granting only one of the two yields AccessDenied.

### 2. `AWS_IAM` gateway inbound means per-request SigV4 on MCP

An `AWS_IAM` gateway takes no bearer token, and a SigV4 signature covers the **request
body and a timestamp** — so a static `headers={...}` cannot work; each request must be
signed. MCP's `streamable_http_client` accepts an `httpx.AsyncClient`, which is the hook
for a custom `httpx.Auth` (see [`agent/sigv4_auth.py`](agent/sigv4_auth.py)):

```python
class SigV4HttpxAuth(httpx.Auth):
    requires_request_body = True          # body must exist before we sign
    def auth_flow(self, request):
        creds = self._session.get_credentials().get_frozen_credentials()
        signable = AWSRequest(method=request.method, url=str(request.url),
                              data=request.content,
                              headers={"Content-Type": "application/json"})
        _BotoSigV4(creds, "bedrock-agentcore", self._region).add_auth(signable)
        for k, v in signable.headers.items():
            request.headers[k] = v
        yield request
```

Signing a minimal header set is enough: SigV4 only requires that the headers it signed
arrive unchanged. Headers httpx adds later (`accept`, `mcp-session-id`,
`content-length`) don't affect verification.

### 3. You own the httpx client's lifecycle, or you leak a pool per invocation

`streamable_http_client(url, http_client=...)` **does not close** a client you pass in
(in the source, `client_provided=True` skips the ExitStack). Wrap it in an async context
manager so teardown happens on MCP's own event loop:

```python
@asynccontextmanager
async def transport():
    async with httpx.AsyncClient(auth=auth, timeout=httpx.Timeout(60.0, read=300.0),
                                 follow_redirects=True) as http_client:
        async with streamable_http_client(GATEWAY_URL, http_client=http_client) as streams:
            yield streams

return MCPClient(transport)
```

> The older `streamablehttp_client(url=..., auth=...)` is deprecated but **does** manage
> the client lifecycle itself. Porting to the new API by just moving the arguments across
> silently drops that.

The MCP session is opened and released **per invocation**, so concurrent runtime sessions
never share one.

### 4. The image must be arm64

AgentCore Runtime only accepts `linux/arm64` images. `deploy.sh` checks
`docker image inspect --format '{{.Architecture}}'` after the build and fails fast rather
than discovering it after a push.

### 5. CreateAgentRuntime fails on IAM propagation right after role creation

Calling `CreateAgentRuntime` immediately after creating the execution role gives:

```
ValidationException: Role validation failed for '...'.
Please verify that the role exists and its trust policy allows assumption by this service
```

That's **propagation delay, not misconfiguration**. `deploy.sh` retries when the
`ValidationException` message contains `Role validation failed` and treats every other
`ValidationException` as fatal — don't blanket-exit on all of them.

---

## Layout

```
agent/agent.py          Strands Agent + BedrockAgentCoreApp entrypoint
agent/sigv4_auth.py     httpx.Auth that SigV4-signs MCP requests
agent/requirements.txt  pinned versions
docker/Dockerfile       linux/arm64 image
scripts/deploy.sh       build → ECR → IAM → create/update PUBLIC runtime (idempotent)
scripts/invoke.py       invoke the deployed runtime, run the 4-prompt suite, archive
scripts/test_local.sh   run the container locally, hit /ping and /invocations
scripts/cleanup.sh      delete runtime / ECR / role
runtime.json            deploy output (runtime ARN, image, model, ...)
results/invocations.json raw measured output
```

---

## Running it

Prerequisite: deploy [`11-vpc-no-egress-workaround`](../11-vpc-no-egress-workaround)
first (it provides the Gateway and the private RDS). `deploy.sh` reads `GW_URL` /
`GW_ARN` from its `state.env` automatically.

```bash
cd 12-strands-nova2-runtime
python3 -m venv .venv && .venv/bin/pip install -r agent/requirements.txt

# verify locally first — much faster than a deploy cycle
bash scripts/test_local.sh "How many orders are pending?"

# deploy to a PUBLIC AgentCore Runtime
bash scripts/deploy.sh

# verify
.venv/bin/python scripts/invoke.py
.venv/bin/python scripts/invoke.py "Which orders were cancelled?"
```

To run a plain Nova 2 Lite agent with no Gateway, leave `GATEWAY_URL` empty — the agent
falls back to a no-tools mode.

## Cost

The runtime bills for CPU/memory actually consumed and nothing while idle
(`idleRuntimeSessionTimeout=900`); Nova 2 Lite is among the cheapest tiers in the Nova
family. ECR storage is ~220 MB. The bulk of the cost is the RDS/NLB/EC2 in
`11-vpc-no-egress-workaround` (~$0.10/hour).

## Cleanup

```bash
bash scripts/cleanup.sh --yes                                  # runtime / ECR / role here
bash ../11-vpc-no-egress-workaround/scripts/cleanup.sh --yes   # Gateway / VPC / RDS
```
