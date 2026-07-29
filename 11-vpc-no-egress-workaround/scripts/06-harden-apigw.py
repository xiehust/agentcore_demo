#!/usr/bin/env python3
"""
Phase 6: lock the REST API down so ONLY the AgentCore Gateway can invoke it.

Two findings drove the policy below, both verified empirically in us-east-2:

1. An Allow-only resource policy is not a lockdown. For same-account callers,
   API Gateway unions the identity-based and resource-based policies, so any
   principal in the account holding execute-api:Invoke still gets through.
   Restricting access therefore requires an explicit Deny.

2. That Deny must NOT be keyed on aws:SourceArn, or it locks the gateway out.
   The docs' Allow (Principal = bedrock-agentcore.amazonaws.com, condition
   ArnEquals aws:SourceArn) does work on its own. But adding the mirror-image
   Deny (ArnNotEquals / ArnNotEqualsIfExists on aws:SourceArn) denies the
   gateway, and the error names its assumed-role session:
       arn:aws:sts::<acct>:assumed-role/<gw-role>/gateway-session-<uuid>
   The request is also evaluated against that IAM identity, where aws:SourceArn
   is absent -- and negated condition operators match absent keys -- so the Deny
   fires and an explicit Deny always wins.

The policy below keys both statements on the gateway execution role via
aws:PrincipalArn, which is always present for SigV4 callers and resolves to the
bare role ARN for assumed-role sessions. Verified: gateway allowed, every other
principal denied.

Note: with this Allow in place the execution role does not additionally need
execute-api:Invoke in its identity policy (measured); the scripts still grant it
because the AWS docs recommend it and it is harmless.

Usage:
  python 06-harden-apigw.py            # apply the working policy
  python 06-harden-apigw.py --docs     # apply the docs-shaped policy, whose Deny
                                       # reproduces the gateway lockout
"""

import json
import sys
import time

import boto3


def load_state(path="state.env"):
    state = {}
    with open(path) as fh:
        for line in fh:
            line = line.strip()
            if line and "=" in line and not line.startswith("#"):
                k, v = line.split("=", 1)
                state[k] = v
    return state


S = load_state()
REGION = S.get("REGION", "us-east-2")
API_ID, STAGE, GW_ARN = S["API_ID"], S["API_STAGE"], S["GW_ARN"]
GW_ROLE_ARN = S["GW_ROLE_ARN"]
ACCOUNT = GW_ARN.split(":")[4]
RESOURCE = f"arn:aws:execute-api:{REGION}:{ACCOUNT}:{API_ID}/{STAGE}/*/*"

WORKING_POLICY = {
    "Version": "2012-10-17",
    "Statement": [
        {
            "Sid": "AllowAgentCoreGatewayRole",
            "Effect": "Allow",
            "Principal": {"AWS": GW_ROLE_ARN},
            "Action": "execute-api:Invoke",
            "Resource": RESOURCE,
        },
        {
            # aws:PrincipalArn is always present for a SigV4 caller and resolves
            # to the bare role ARN for assumed-role sessions, so no IfExists.
            "Sid": "DenyEveryoneElse",
            "Effect": "Deny",
            "Principal": "*",
            "Action": "execute-api:Invoke",
            "Resource": RESOURCE,
            "Condition": {"ArnNotEquals": {"aws:PrincipalArn": GW_ROLE_ARN}},
        },
    ],
}

DOCS_POLICY = {
    "Version": "2012-10-17",
    "Statement": [
        {
            "Sid": "AllowAgentCoreGatewayServicePrincipal",
            "Effect": "Allow",
            "Principal": {"Service": "bedrock-agentcore.amazonaws.com"},
            "Action": "execute-api:Invoke",
            "Resource": RESOURCE,
            "Condition": {"ArnEquals": {"aws:SourceArn": GW_ARN}},
        },
        {
            "Sid": "DenyEveryoneElse",
            "Effect": "Deny",
            "Principal": "*",
            "Action": "execute-api:Invoke",
            "Resource": RESOURCE,
            "Condition": {"ArnNotEqualsIfExists": {"aws:SourceArn": GW_ARN}},
        },
    ],
}

policy = DOCS_POLICY if "--docs" in sys.argv else WORKING_POLICY
label = "AWS-docs (expected to lock the gateway out)" if "--docs" in sys.argv \
    else "verified working"

api = boto3.client("apigateway", region_name=REGION)
print(f"Applying {label} resource policy to {API_ID}/{STAGE} ...")
api.update_rest_api(
    restApiId=API_ID,
    patchOperations=[{"op": "replace", "path": "/policy",
                      "value": json.dumps(policy)}],
)

# Resource-policy changes only take effect on redeploy, and CreateDeployment is
# heavily rate limited. Propagation to the stage then takes another 1-2 minutes.
for attempt in range(1, 11):
    try:
        api.create_deployment(restApiId=API_ID, stageName=STAGE)
        print(f"Redeployed stage {STAGE}")
        break
    except api.exceptions.TooManyRequestsException:
        time.sleep(attempt * 5)
else:
    print("create_deployment kept throttling", file=sys.stderr)
    sys.exit(1)

print(json.dumps(policy, indent=2))
