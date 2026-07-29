#!/usr/bin/env bash
# Phase 5: add the regional REST API as an `apiGateway` gateway target.
# Outbound auth is GATEWAY_IAM_ROLE (SigV4) — the only options for this target
# type are IAM and API key; OAuth and cross-account are not supported.
source "$(dirname "$0")/lib.sh"

: "${API_ID:?run 04-apigw-vpclink.sh first}"
: "${GW_ID:?run 03-gateway.sh first}"

TGT_NAME="rdsApi"
TGT_ID=$(aws bedrock-agentcore-control list-gateway-targets --gateway-identifier "$GW_ID" \
  --region "$REGION" --query "items[?name=='$TGT_NAME'].targetId | [0]" --output text)

if [[ "$TGT_ID" == "None" || -z "$TGT_ID" ]]; then
  log "Creating apiGateway target for REST API $API_ID stage $API_STAGE"
  # apiGatewayToolConfiguration.toolFilters is REQUIRED (not mentioned in the
  # public docs): it whitelists which path+method pairs become tools.
  # toolOverrides then gives them agent-friendly names and descriptions.
  cat > "$ROOT_DIR/build/apigw-target.json" <<JSON
{
  "mcp": {
    "apiGateway": {
      "restApiId": "$API_ID",
      "stage": "$API_STAGE",
      "apiGatewayToolConfiguration": {
        "toolFilters": [
          {"filterPath": "/orders", "methods": ["GET"]},
          {"filterPath": "/dbinfo", "methods": ["GET"]}
        ],
        "toolOverrides": [
          {
            "name": "listOrders",
            "description": "List orders from the private RDS MySQL database via API Gateway + VPC Link. Optional query params: status, limit.",
            "path": "/orders",
            "method": "GET"
          },
          {
            "name": "getDbInfo",
            "description": "Return MySQL version/hostname plus the private IPs proving the call traversed the VPC Link into the private subnet.",
            "path": "/dbinfo",
            "method": "GET"
          }
        ]
      }
    }
  }
}
JSON
  TGT_ID=$(aws bedrock-agentcore-control create-gateway-target \
    --gateway-identifier "$GW_ID" --name "$TGT_NAME" \
    --description "Regional REST API reaching private RDS via VPC Link + NLB" \
    --target-configuration "file://$ROOT_DIR/build/apigw-target.json" \
    --credential-provider-configurations '[{"credentialProviderType":"GATEWAY_IAM_ROLE"}]' \
    --region "$REGION" --query targetId --output text)
fi
save TGT_API_ID "$TGT_ID"

log "Waiting for target to become READY"
for _ in $(seq 60); do
  ST=$(aws bedrock-agentcore-control get-gateway-target --gateway-identifier "$GW_ID" \
    --target-id "$TGT_ID" --region "$REGION" --query status --output text)
  [[ "$ST" == "READY" ]] && break
  [[ "$ST" == *FAILED* || "$ST" == *UNSUCCESSFUL* ]] && {
    aws bedrock-agentcore-control get-gateway-target --gateway-identifier "$GW_ID" \
      --target-id "$TGT_ID" --region "$REGION" --query statusReasons; exit 1; }
  sleep 5
done
ok "target $TGT_ID status=$ST"

log "Phase 5 complete."
