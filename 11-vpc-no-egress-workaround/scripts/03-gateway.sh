#!/usr/bin/env bash
# Phase 3: the AgentCore Gateway + the Lambda target (Workaround 1).
# Inbound auth is AWS_IAM so the test client can SigV4-sign directly, with no
# Cognito/IdP in the loop. Outbound auth to Lambda is GATEWAY_IAM_ROLE.
source "$(dirname "$0")/lib.sh"

: "${LAMBDA_ARN:?run 02-lambda.sh first}"
GW_NAME="$PREFIX-gw"
ROLE_NAME="$PREFIX-gw-role"

# ---------- gateway service role ----------
if ! aws iam get-role --role-name "$ROLE_NAME" >/dev/null 2>&1; then
  log "Creating gateway service role"
  aws iam create-role --role-name "$ROLE_NAME" --assume-role-policy-document "{
    \"Version\":\"2012-10-17\",
    \"Statement\":[{
      \"Effect\":\"Allow\",
      \"Principal\":{\"Service\":\"bedrock-agentcore.amazonaws.com\"},
      \"Action\":\"sts:AssumeRole\",
      \"Condition\":{
        \"StringEquals\":{\"aws:SourceAccount\":\"$ACCOUNT_ID\"},
        \"ArnLike\":{\"aws:SourceArn\":\"arn:aws:bedrock-agentcore:$REGION:$ACCOUNT_ID:gateway/*\"}
      }
    }]}" >/dev/null
  sleep 12
fi
GW_ROLE_ARN=$(aws iam get-role --role-name "$ROLE_NAME" --query Role.Arn --output text)
save GW_ROLE_ARN "$GW_ROLE_ARN"

# Least privilege: invoke ONLY this Lambda (the doc's explicit recommendation).
log "Attaching least-privilege invoke policy"
aws iam put-role-policy --role-name "$ROLE_NAME" --policy-name invoke-lambda-target \
  --policy-document "{
    \"Version\":\"2012-10-17\",
    \"Statement\":[{\"Effect\":\"Allow\",\"Action\":\"lambda:InvokeFunction\",
                   \"Resource\":\"$LAMBDA_ARN\"}]}" >/dev/null
ok "role $GW_ROLE_ARN"

# ---------- gateway ----------
GW_ID=$(aws bedrock-agentcore-control list-gateways --region "$REGION" \
  --query "items[?name=='$GW_NAME'].gatewayId | [0]" --output text)
if [[ "$GW_ID" == "None" || -z "$GW_ID" ]]; then
  log "Creating gateway (authorizerType=AWS_IAM, protocol=MCP)"
  GW_ID=$(aws bedrock-agentcore-control create-gateway \
    --name "$GW_NAME" \
    --role-arn "$GW_ROLE_ARN" \
    --protocol-type MCP \
    --authorizer-type AWS_IAM \
    --exception-level DEBUG \
    --description "Verify no-VPC-egress workarounds against private RDS" \
    --region "$REGION" --query gatewayId --output text)
fi
save GW_ID "$GW_ID"

log "Waiting for gateway to become READY"
for _ in $(seq 60); do
  read -r ST URL < <(aws bedrock-agentcore-control get-gateway --gateway-identifier "$GW_ID" \
    --region "$REGION" --query '[status,gatewayUrl]' --output text)
  [[ "$ST" == "READY" ]] && break
  [[ "$ST" == "CREATE_FAILED" || "$ST" == "FAILED" ]] && {
    aws bedrock-agentcore-control get-gateway --gateway-identifier "$GW_ID" --region "$REGION" \
      --query statusReasons; exit 1; }
  sleep 5
done
save GW_URL "$URL"
save GW_ARN "arn:aws:bedrock-agentcore:$REGION:$ACCOUNT_ID:gateway/$GW_ID"
ok "gateway $GW_ID status=$ST"
ok "url $URL"

# ---------- lambda target ----------
TGT_NAME="rdsLambda"
TGT_ID=$(aws bedrock-agentcore-control list-gateway-targets --gateway-identifier "$GW_ID" \
  --region "$REGION" --query "items[?name=='$TGT_NAME'].targetId | [0]" --output text)
if [[ "$TGT_ID" == "None" || -z "$TGT_ID" ]]; then
  log "Creating Lambda target with inline tool schema"
  cat > "$ROOT_DIR/build/lambda-target.json" <<JSON
{
  "mcp": {
    "lambda": {
      "lambdaArn": "$LAMBDA_ARN",
      "toolSchema": {
        "inlinePayload": [
          {
            "name": "list_orders",
            "description": "List orders from the private RDS MySQL database. Optionally filter by status.",
            "inputSchema": {
              "type": "object",
              "properties": {
                "status": {"type": "string", "description": "Filter by status: SHIPPED, PENDING or CANCELLED"},
                "limit":  {"type": "integer", "description": "Max rows to return (1-100)"}
              },
              "required": []
            }
          },
          {
            "name": "db_info",
            "description": "Return MySQL version, hostname and the private IPs proving the query ran inside the VPC.",
            "inputSchema": {"type": "object", "properties": {}, "required": []}
          }
        ]
      }
    }
  }
}
JSON
  TGT_ID=$(aws bedrock-agentcore-control create-gateway-target \
    --gateway-identifier "$GW_ID" --name "$TGT_NAME" \
    --description "VPC-attached Lambda reaching private RDS" \
    --target-configuration "file://$ROOT_DIR/build/lambda-target.json" \
    --credential-provider-configurations '[{"credentialProviderType":"GATEWAY_IAM_ROLE"}]' \
    --region "$REGION" --query targetId --output text)
fi
save TGT_LAMBDA_ID "$TGT_ID"

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

log "Phase 3 complete."
