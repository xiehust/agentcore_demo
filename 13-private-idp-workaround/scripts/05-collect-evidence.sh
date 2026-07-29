#!/usr/bin/env bash
# Phase 5: capture raw evidence that the IdP really is private and that both
# directions (inbound validation, outbound token exchange) work through it.
source "$(dirname "$0")/lib.sh"
cd "$ROOT_DIR"
OUT="$ROOT_DIR/results/evidence.txt"
mkdir -p "$ROOT_DIR/results"

{
echo "Private IdP workaround verification"
echo "region=$REGION account=$ACCOUNT_ID date=$(date -u +%FT%TZ)"
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

echo
echo "############ 2. Gateway config: NONE inbound + REQUEST interceptor ############"
aws bedrock-agentcore-control get-gateway --gateway-identifier "$IDP_GW_ID" --region "$REGION" \
  --query '{authorizerType:authorizerType,status:status,interceptors:interceptorConfigurations}' \
  --output json

echo
echo "############ 3. Inbound: JWT validated against the private JWKS ############"
python3 scripts/04-verify.py

echo
echo "############ 4. Outbound: tool exchanges client_credentials at the private IdP ############"
aws lambda invoke --function-name "$TOOL_FN" --region "$REGION" \
  --payload '{"status":"SHIPPED"}' --cli-binary-format raw-in-base64-out \
  --client-context "$(printf '{"custom":{"bedrockAgentCoreToolName":"secure_list_orders"}}' | base64 -w0)" \
  "$ROOT_DIR/build/outbound.json" >/dev/null
python3 -m json.tool "$ROOT_DIR/build/outbound.json"

echo
echo "############ 5. Interceptor logs (no secrets, JWKS cached across calls) ############"
LG="/aws/lambda/$INTERCEPTOR_FN"
STREAM=$(aws logs describe-log-streams --log-group-name "$LG" --region "$REGION" \
  --order-by LastEventTime --descending --max-items 1 \
  --query 'logStreams[0].logStreamName' --output text 2>/dev/null | head -1)
if [[ -n "$STREAM" && "$STREAM" != "None" ]]; then
  aws logs get-log-events --log-group-name "$LG" --log-stream-name "$STREAM" \
    --region "$REGION" --limit 25 --query 'events[].message' --output text
fi
} 2>&1 | tee "$OUT"

echo
log "Evidence written to $OUT"
