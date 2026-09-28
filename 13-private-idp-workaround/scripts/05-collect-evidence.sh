#!/usr/bin/env bash
# Phase 5: capture raw evidence that the IdP really is private and that both
# directions (inbound validation, outbound token exchange) work through it.
source "$(dirname "$0")/lib.sh"
cd "$ROOT_DIR"
OUT="$RESULTS_DIR/evidence.txt"
mkdir -p "$RESULTS_DIR"
RUN_START_MS=$(date +%s%3N)

{
echo "Private IdP workaround verification"
echo "profile=${AWS_PROFILE:-default} region=$REGION account=$ACCOUNT_ID date=$(date -u +%FT%TZ)"
echo "gateway=$IDP_GW_ID url=$IDP_GW_URL"
echo "idp=$IDP_ISSUER instance=$IDP_INSTANCE_ID"
echo
echo "############ 1. The IdP is genuinely private ############"
echo "--- instance networking ---"
aws ec2 describe-instances --instance-ids "$IDP_INSTANCE_ID" --region "$REGION" \
  --query 'Reservations[0].Instances[0].{PrivateIp:PrivateIpAddress,PublicIp:PublicIpAddress,Subnet:SubnetId,State:State.Name}' \
  --output json
echo "--- who may reach the IdP port ---"
aws ec2 describe-security-groups --group-ids "$SG_IDP" --region "$REGION" \
  --query 'SecurityGroups[0].IpPermissions' --output json
echo "--- reaching the IdP from outside the VPC (expect failure) ---"
timeout 20 python3 -c "
import socket
try:
    socket.create_connection(('$IDP_IP', $IDP_PORT), timeout=12); print('REACHABLE - unexpected')
except Exception as e: print('unreachable from outside:', type(e).__name__)
"
echo "--- the VPC still has no internet egress ---"
echo "IGWs: $(aws ec2 describe-internet-gateways --region "$REGION" \
  --filters "Name=attachment.vpc-id,Values=$VPC_ID" --query 'length(InternetGateways)')"
echo "NATs: $(aws ec2 describe-nat-gateways --region "$REGION" \
  --filter "Name=vpc-id,Values=$VPC_ID" --query 'length(NatGateways)')"
echo "--- VPC routes ---"
aws ec2 describe-route-tables --region "$REGION" \
  --filters "Name=vpc-id,Values=$VPC_ID" \
  --query 'RouteTables[].{Id:RouteTableId,Routes:Routes}' --output json

echo
echo "############ 2. Gateway config: $INBOUND_AUTH inbound + REQUEST interceptor ############"
# boto3 rather than the CLI: older CLI builds silently drop interceptorConfigurations.
"$PY" - "$IDP_GW_ID" "$INBOUND_AUTH" "$INTERCEPTOR_ARN" "$IDP_TGT_ID" "$TOOL_ARN" <<'PYEOF'
import json, sys, boto3
c = boto3.client("bedrock-agentcore-control")
g = c.get_gateway(gatewayIdentifier=sys.argv[1])
t = c.get_gateway_target(gatewayIdentifier=sys.argv[1], targetId=sys.argv[4])
assert g["status"] == "READY" and g["authorizerType"] == sys.argv[2], "gateway auth/state mismatch"
assert any(x["interceptor"]["lambda"]["arn"] == sys.argv[3]
           and "REQUEST" in x["interceptionPoints"]
           and x.get("inputConfiguration", {}).get("passRequestHeaders") is True
           for x in g.get("interceptorConfigurations", [])), "interceptor configuration mismatch"
assert t["status"] == "READY", "target not ready"
assert t["targetConfiguration"]["mcp"]["lambda"]["lambdaArn"] == sys.argv[5], "target ARN mismatch"
print(json.dumps({k: g.get(k) for k in ("authorizerType", "status")}
                 | {"interceptors": g.get("interceptorConfigurations"),
                    "target": t["targetConfiguration"]}, indent=2))
PYEOF
echo "--- interceptor private networking and business JWT header ---"
aws lambda get-function-configuration --function-name "$INTERCEPTOR_FN" --region "$REGION" \
  --query '{State:State,VpcConfig:VpcConfig,TokenHeader:Environment.Variables.TOKEN_HEADER,JwksUrl:Environment.Variables.IDP_JWKS_URL}' \
  --output json
if [[ "$INBOUND_AUTH" == "AWS_IAM" ]]; then
  echo "--- test backend signs with its execution role, not the zhy user's keys ---"
  aws lambda get-function-configuration --function-name "$CALLER_FN" --region "$REGION" \
    --query '{State:State,Role:Role,VpcConfig:VpcConfig}' --output json
  aws iam get-role-policy --role-name "$PREFIX-caller-role" --policy-name invoke-gateway \
    --query PolicyDocument --output json
fi

echo
echo "############ 3. Inbound: JWT validated against the private JWKS ############"
"$PY" scripts/04-verify.py

echo
echo "############ 4. Outbound: tool exchanges client_credentials at the private IdP ############"
"$PY" - "$TOOL_FN" "$RESULTS_DIR/outbound.json" <<'PYEOF'
import base64, json, runpy, sys, boto3
verify = runpy.run_path("scripts/04-verify.py")
context = base64.b64encode(json.dumps({"custom": {
    "bedrockAgentCoreToolName": "secure_list_orders"}}).encode()).decode()
r = boto3.client("lambda").invoke(FunctionName=sys.argv[1],
    Payload=json.dumps({"status": "SHIPPED"}).encode(), ClientContext=context)
data = json.loads(r["Payload"].read())
with open(sys.argv[2], "w") as fh:
    json.dump(data, fh, indent=2)
if r.get("FunctionError"):
    raise RuntimeError(f"outbound Lambda failed: {r['FunctionError']}")
verify["validate_tool_output"](data, "SHIPPED")
print(json.dumps(data, indent=2))
print("Outbound token metadata and private RDS response validated")
PYEOF

echo
echo "############ 5. Run-window interceptor and tool logs (all streams) ############"
"$PY" - "$RUN_START_MS" "$INTERCEPTOR_FN" "$TOOL_FN" "$RESULTS_DIR/invocations.json" <<'PYEOF'
import json, sys, time, boto3
start, end = int(sys.argv[1]), int(time.time() * 1000)
logs = boto3.client("logs")
report = {"start_ms": start, "end_ms": end, "functions": {},
          "note": "CloudWatch ingestion can lag; these are observed counts, not a completeness guarantee."}
for name in sys.argv[2:4]:
    events = []
    for page in logs.get_paginator("filter_log_events").paginate(
            logGroupName=f"/aws/lambda/{name}", startTime=start, endTime=end):
        events.extend({k: e[k] for k in ("timestamp", "logStreamName", "message")}
                      for e in page.get("events", []))
    starts = sum(e["message"].startswith("START RequestId:") for e in events)
    report["functions"][name] = {"observed_invocations": starts, "events": events}
    print(f"{name}: observed {starts} invocations in this run window")
    for e in events:
        if '"authorized": false' in e["message"]:
            print(e["message"].strip())
with open(sys.argv[4], "w") as fh:
    json.dump(report, fh, indent=2)
print(report["note"])
PYEOF
} 2>&1 | tee "$OUT"

echo
log "Evidence written to $OUT"
