# API Gateway resource policy — measured matrix

All rows measured against the live setup in us-east-2 (REST API `ip8yrem2t4`, stage
`prod`, gateway `acdemo-noegress-gw-ongnqn4b1t`, gateway execution role
`acdemo-noegress-gw-role`). "Other principal" = `admin_role_for_workshop`, a
same-account role holding broad `execute-api:Invoke`.

Every policy change required a stage redeploy plus ~1–2 minutes of propagation
before the new behaviour took effect.

| # | Resource policy | Gateway role identity policy grants `execute-api:Invoke` | Gateway call | Other principal | Verdict |
|---|---|---|---|---|---|
| 1 | none | yes | ✅ 200 | ✅ 200 | no restriction at all |
| 2 | `Allow` service principal + `ArnEquals aws:SourceArn` (as documented) | yes | ✅ 200 | ✅ 200 | **not a lockdown** — same-account union |
| 3 | `Allow` service principal + `ArnEquals aws:SourceArn` (as documented) | **no** | ✅ 200 | ✅ 200 | documented `Allow` genuinely matches; `aws:SourceArn` is present in that context |
| 4 | row 2 + `Deny *` with `ArnNotEqualsIfExists aws:SourceArn` | yes | ❌ 403 explicit deny | ❌ 403 | **breaks the gateway** — the mirror-image Deny locks it out |
| 5 | `Allow` role ARN + `Deny *` with `ArnNotEquals aws:PrincipalArn` | **no** | ✅ 200 | ❌ 403 | ✅ works; identity grant not required |
| 6 | `Allow` role ARN + `Deny *` with `ArnNotEquals aws:PrincipalArn` | yes | ✅ 200 | ❌ 403 | ✅ **recommended** (shipped by `06-harden-apigw.py`) |

Unauthenticated calls (no SigV4 at all) return `403 Missing Authentication Token` in
every row, because the method's `authorizationType` is `AWS_IAM`.

## Why row 4 fails

The denial message names the caller as the gateway's assumed-role session:

```
User: arn:aws:sts::434444145045:assumed-role/acdemo-noegress-gw-role/gateway-session-<uuid>
is not authorized to perform: execute-api:Invoke ...
with an explicit deny in a resource-based policy
```

Rows 3 and 4 together show the request is evaluated under two identities: a service
principal context where `aws:SourceArn` is present (so the documented `Allow`
matches), and the gateway's IAM role session where `aws:SourceArn` is **absent**.
IAM's negated operators — `ArnNotEquals`, `StringNotEquals`, and their `...IfExists`
forms — evaluate to **true** on a missing key, so the `Deny` matches in the second
context. An explicit `Deny` beats any `Allow`, so the request is refused.

`aws:PrincipalArn` avoids the problem because it is always populated for a SigV4
caller and resolves to the bare role ARN for assumed-role sessions.

## Reproducing

```bash
python3 scripts/06-harden-apigw.py          # row 6
python3 scripts/06-harden-apigw.py --docs   # row 4
```

To reproduce rows 3 and 5, additionally remove the identity grant:

```bash
aws iam delete-role-policy --role-name acdemo-noegress-gw-role \
  --policy-name invoke-apigw-target
```
