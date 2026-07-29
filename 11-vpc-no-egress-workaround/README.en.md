# AgentCore without VPC Egress — Workarounds Verified in Practice

This directory is a real deployment verification of **Workaround 1 (Lambda bridge)**
and **Workaround 2 (API Gateway + VPC Link)** from
`AgentCore 中国区无 VPC Egress Workaround 方案.html`.

AgentCore is not yet available in the AWS China regions, so verification ran in
**us-east-2 (Ohio)**. To stay faithful to the document's premise — a launch region
with no native VPC Egress and a hard-isolated network — the target VPC was built
with **no internet egress whatsoever**: no Internet Gateway, no NAT Gateway, and no
`0.0.0.0/0` route in the route table.


---

## Bottom line

**Both workarounds are verified.** Inside a VPC with zero internet egress,
AgentCore Gateway successfully read live data out of a private RDS MySQL instance.
The document's central claim holds: reaching private VPC resources does **not**
depend on the Gateway having its own VPC Egress capability.

However, testing surfaced **four things the documentation gets wrong or omits** —
one of them a security problem (see [Findings](#findings-and-documentation-corrections)).
Configured exactly as documented, the API Gateway resource policy either fails to
restrict anything, or locks the Gateway itself out.

| Workaround | Result | End-to-end p50 | Key evidence |
|---|---|---|---|
| ① Lambda bridge | ✅ Pass | 730 ms | Client IP seen by RDS = `10.30.11.161` (Lambda ENI, private subnet) |
| ② API GW + VPC Link | ✅ Pass | 737 ms | Client IP seen by RDS = `10.30.11.109` (EC2 private IP) |

> Latency includes local client process startup and a full MCP `initialize`
> handshake, so it is not pure server time; the difference between the two paths is
> within noise. Raw numbers in `results/latency.json`.

---

## Verified architecture

```
                    ┌─────────────────────────────────────────────────────┐
                    │  VPC 10.30.0.0/16  (no IGW / no NAT / no default rt) │
                    │                                                     │
  ┌──────────┐      │   ┌───────────────────┐                             │
  │  local   │ MCP  │   │ Lambda (VPC-attach)│──┐                          │
  │  client  │─────┐│   │ private subnet a/b │  │                          │
  │ (SigV4)  │     ││   └───────────────────┘  │  3306                    │
  └──────────┘     ││                          ▼                          │
                   ││   ┌──────────────────────────────┐                  │
   ┌───────────────▼┼──▶│ RDS MySQL 8.0.42             │                  │
   │  AgentCore     ││   │ PubliclyAccessible = false   │                  │
   │  Gateway       ││   │ 10.30.11.229                 │                  │
   │ (AWS_IAM in)   ││   └──────────────────────────────┘                  │
   └───────┬────────┘│                          ▲                          │
           │ SigV4   │                          │ 3306                     │
           ▼         │   ┌───────────────────┐  │                          │
   ┌──────────────┐  │   │ EC2 (private)     │──┘                          │
   │ REST API     │  │   │ 10.30.11.109:8080 │                             │
   │ (Regional)   │  │   └─────────▲─────────┘                             │
   └──────┬───────┘  │             │                                       │
          │ VPC Link │   ┌─────────┴─────────┐                             │
          └──────────┼──▶│ internal NLB      │                             │
                     │   └───────────────────┘                             │
                     └─────────────────────────────────────────────────────┘
```

What both paths share: **the Gateway never enters the VPC.**
- Workaround 1: the Gateway calls the Lambda service API over the AWS backbone;
  **Lambda's own VPC attachment** does the entering.
- Workaround 2: the Gateway calls API Gateway over the backbone; **VPC Link** does
  the entering.

---

## Resources deployed

| Type | Identifier |
|---|---|
| VPC | `vpc-018b5902896224dc5` (10.30.0.0/16, two private subnets) |
| VPC endpoints | S3 gateway endpoint + ssm / ssmmessages / ec2messages interface endpoints |
| RDS | `acdemo-noegress-mysql`, MySQL 8.0.42, db.t4g.micro, `PubliclyAccessible=false` |
| Lambda | `acdemo-noegress-db-tool`, python3.12, private subnets + bundled `pymysql` |
| EC2 | `i-0d110aadb72bc6e83`, t3.micro, no public IP, stdlib HTTP service |
| NLB | `acdemo-noegress-nlb`, internal, TCP:80 → instance :8080 |
| VPC Link | `asw9a8` |
| REST API | `ip8yrem2t4`, Regional, stage `prod`, method auth `AWS_IAM` |
| Gateway | `acdemo-noegress-gw-ongnqn4b1t`, protocol MCP, inbound `AWS_IAM` |
| Gateway targets | `rdsLambda` (lambda), `rdsApi` (apiGateway) |

Four tools exposed to agents:

```
rdsLambda___db_info        rdsLambda___list_orders
rdsApi___getDbInfo         rdsApi___listOrders
```

---

## Evidence

Full raw output in [`results/evidence.txt`](results/evidence.txt). Highlights:

**1. The VPC genuinely has no egress**

```
IGWs attached: 0     NAT gateways: 0
Routes:  10.30.0.0/16 -> local
         pl-7ba54012  -> vpce-0aec... (S3 gateway endpoint)
```
No `0.0.0.0/0`. Connecting to RDS:3306 from outside the VPC → `TimeoutError`.

**2. Workaround 1: Gateway → Lambda(VPC) → RDS**

```json
{
  "path": "AgentCore Gateway -> Lambda (VPC-attached) -> RDS",
  "rds_resolved_private_ip": "10.30.11.229",
  "version": "8.0.42",
  "db_user": "agentadmin@10.30.11.161"
}
```
The `10.30.11.161` in `db_user` is the source address MySQL itself recorded, inside
private subnet a — hard evidence the query originated within the VPC.

**3. Workaround 2: Gateway → API GW → VPC Link → NLB → EC2 → RDS**

```json
{
  "path": "AgentCore Gateway -> API Gateway -> VPC Link -> NLB -> EC2 (private subnet) -> RDS",
  "ec2_private_ip": "10.30.11.109",
  "db_user": "agentadmin@10.30.11.109"
}
```

**4. Argument passing works** — both targets filter correctly on `status` / `limit`:

```
rdsLambda___list_orders {"status":"SHIPPED"}   -> ORD-1001, ORD-1003
rdsApi___listOrders     {"status":"PENDING"}   -> ORD-1002, ORD-1005
```

**5. Only the Gateway can call the API**

```
direct call, no credentials       -> HTTP 403 Missing Authentication Token
direct SigV4 as admin identity    -> HTTP 403 explicit deny in a resource-based policy
via AgentCore Gateway             -> HTTP 200 + RDS data
```

---

## Findings and documentation corrections

### ⚠️ 1. Never key the `Deny` on `aws:SourceArn` — it locks the Gateway out

Both the HTML document and the AWS doc `gateway-vpc-egress.html` give this policy:

```json
{ "Principal": { "Service": "bedrock-agentcore.amazonaws.com" },
  "Condition": { "ArnEquals": { "aws:SourceArn": "<gateway ARN>" } } }
```

That `Allow` **does work.** We even deleted `execute-api:Invoke` from the execution
role's identity policy, and the Gateway still got HTTP 200 on the strength of this
resource policy alone — so in that authorization context `aws:SourceArn` is present
and equals the gateway ARN.

**The trap is the `Deny`.** Because an Allow-only policy restricts nothing
(finding 2), a real lockdown needs an explicit `Deny` — and writing that `Deny` as
the mirror image of the documented `Allow`:

```json
{ "Effect": "Deny", "Principal": "*",
  "Condition": { "ArnNotEqualsIfExists": { "aws:SourceArn": "<gateway ARN>" } } }
```

**immediately locks the Gateway out.** Reproduced:

```
User: arn:aws:sts::434444145045:assumed-role/acdemo-noegress-gw-role/gateway-session-e465e38c-...
is not authorized to perform: execute-api:Invoke ...
with an explicit deny in a resource-based policy
```

The error reveals the mechanism: the request is *also* evaluated against the
Gateway's **assumed-role session identity**, and in that context `aws:SourceArn` is
**absent**. IAM's negated operators (`ArnNotEquals`, `StringNotEquals`) evaluate to
**true** when the key is missing — `...IfExists` included — so the `Deny` fires, and
an explicit `Deny` always beats an `Allow`.

**The formulation that works** keys both statements on the execution role ARN via
`aws:PrincipalArn`, which is always present for SigV4 callers and resolves to the
bare role ARN for assumed-role sessions:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    { "Sid": "AllowAgentCoreGatewayRole",
      "Effect": "Allow",
      "Principal": { "AWS": "arn:aws:iam::<acct>:role/<gateway-exec-role>" },
      "Action": "execute-api:Invoke",
      "Resource": "arn:aws:execute-api:<region>:<acct>:<api-id>/<stage>/*/*" },
    { "Sid": "DenyEveryoneElse",
      "Effect": "Deny",
      "Principal": "*",
      "Action": "execute-api:Invoke",
      "Resource": "arn:aws:execute-api:<region>:<acct>:<api-id>/<stage>/*/*",
      "Condition": { "ArnNotEquals": {
        "aws:PrincipalArn": "arn:aws:iam::<acct>:role/<gateway-exec-role>" } } }
  ]
}
```

Measured result: the Gateway works, and every other principal in the account
(including admin) gets 403. Both policies live in `scripts/06-harden-apigw.py`;
`--docs` reproduces the locked-out version. The full six-row measured matrix is in
[`results/policy-matrix.md`](results/policy-matrix.md).

> Side finding: once the resource policy has an `Allow` for the execution role ARN,
> the role's **identity policy no longer needs** `execute-api:Invoke` (verified by
> deleting it). The scripts still grant it, since the AWS docs recommend it and the
> extra layer doesn't weaken the restriction.

### ⚠️ 2. An Allow-only resource policy is not a lockdown

The document says to "lock API Gateway down to the AgentCore service principal via a
resource policy". In practice, for same-account callers API Gateway takes the
**union** of identity-based and resource-based policies, so any principal in the
account holding `execute-api:Invoke` still gets in — invoking directly with admin
credentials returned 200 and full RDS data. An **explicit `Deny`** is required.

### 3. `apiGatewayToolConfiguration.toolFilters` is mandatory (undocumented)

An `apiGateway` target cannot be created with just `restApiId` + `stage`:

```
ParamValidation: Missing required parameter in
targetConfiguration.mcp.apiGateway: "apiGatewayToolConfiguration"
```

You must whitelist path + method pairs via `toolFilters`. Adding `toolOverrides` is
strongly advisable, otherwise tool names are auto-derived from the API structure and
read poorly to an agent.

### 4. Gateway inbound auth can be `AWS_IAM` — no Cognito needed for testing

The document and most examples use CUSTOM_JWT + Cognito. `--authorizer-type AWS_IAM`
works fine: sign the MCP endpoint with SigV4 for service `bedrock-agentcore`
(see `mcp_client.py`). This removes an entire IdP from the verification loop.

### 5. Two engineering gotchas

- **IAM propagation**: `CreateGatewayTarget` **eagerly validates** the execution
  role's `lambda:InvokeFunction` permission. Creating the target right after
  creating the role fails with
  `Gateway execution role lacks permission to invoke Lambda function`.
  Retrying resolves it; the scripts use an `until` retry loop.
- **API Gateway deployment throttling + policy propagation**: `CreateDeployment` is
  aggressively rate limited (`TooManyRequestsException`) and needs backoff. Resource
  policy changes require a **stage redeploy**, and then take roughly 1–2 more minutes
  to take effect (our first "should be denied" check still returned 200; polling
  turned it into 403).

### 6. What the document gets right

- Lambda target is "out-of-the-box, no additional configuration" — confirmed. Just
  attach `VpcConfig` and RDS is reachable.
- `apiGateway` targets support **Regional REST APIs only** — confirmed (the API
  accepts only `restApiId` + `stage`).
- VPC Link backends must be an **NLB** — confirmed (REST API VPC Links don't take ALBs).
- Outbound auth is **IAM or API key only** — confirmed, no OAuth option.
- Traffic never requires the Gateway to enter the VPC — confirmed; everything worked
  with zero egress routes in the VPC.

### 7. Extra notes for the China regions

- The ARN partition is `aws-cn`; every `arn:aws:...` in the policies must change.
- **Finding 1 matters more there**: China-region service principals sometimes carry a
  region suffix, but the measurements show the correct policy shouldn't use a service
  principal at all — it should use the execution role ARN. That conclusion is
  partition-independent and carries over directly.
- The S3 gateway endpoint, SSM interface endpoints, VPC Link and NLB used here are
  all mature capabilities in the China regions.

---

## Reproducing

```bash
cd 11-vpc-no-egress-workaround

bash scripts/01-vpc-rds.sh          # zero-egress VPC + private RDS (~8-10 min)
bash scripts/02-lambda.sh           # VPC-attached Lambda + seed the schema
bash scripts/03-gateway.sh          # Gateway + lambda target
bash scripts/04-apigw-vpclink.sh    # EC2 + NLB + VPC Link + REST API (~5-8 min)
bash scripts/05-apigw-target.sh     # apiGateway target
python3 scripts/06-harden-apigw.py  # apply the working resource policy
bash scripts/07-collect-evidence.sh # run every check and archive the output

# manual invocation
python3 mcp_client.py list
python3 mcp_client.py call rdsLambda___db_info
python3 mcp_client.py call rdsApi___listOrders '{"status":"PENDING"}'
```

Resource IDs are written to `state.env` (contains the RDS password; excluded via
`.gitignore`). Scripts are re-runnable and skip resources that already exist.

## Cost

Roughly **$0.10/hour**: RDS db.t4g.micro ≈ $0.016, EC2 t3.micro ≈ $0.0104,
NLB ≈ $0.0225, three interface endpoints ≈ $0.03, plus per-request charges for
Gateway / Lambda / API Gateway. **No NAT gateway** (saves $0.045/h and an EIP).

## Cleanup

```bash
bash scripts/cleanup.sh --yes
```

Deletes in dependency order and waits for Lambda-managed ENIs to drain before
removing security groups and subnets (otherwise the VPC cannot be deleted). VPC Link
and RDS deletion each take several minutes.
